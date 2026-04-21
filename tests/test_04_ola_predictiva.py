"""
Test 04 — Ola Predictiva: pre-verificación antes de liberar picking.
El picker nunca se queda esperando al montacargas.
"""
import pytest


class TestPreVerificarOla:

    def test_no_alerta_si_stock_suficiente(self, app, db, producto, almacen, ub_picking):
        """500 UNDs en PICKING, demanda 100 → OK, sin reposición."""
        from app.models.inventario import UbicacionProducto
        from app.services.ola_predictiva_service import pre_verificar_ola

        inv = UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            cantidad=500, reservado=0,
        )
        db.session.add(inv)
        db.session.commit()

        resultado = pre_verificar_ola(
            items=[{'producto_id': producto.id, 'cantidad': 100}],
            almacen_id=almacen.id,
        )

        assert resultado['hay_deficit'] is False
        assert len(resultado['items_ok']) == 1
        assert len(resultado['items_con_alerta']) == 0
        assert resultado['reposiciones_generadas'] == 0

    def test_genera_reposicion_preventiva_cuando_hay_deficit(self, app, db,
                                                               producto, almacen,
                                                               ub_picking, ub_reserva,
                                                               inv_picking, inv_reserva,
                                                               lpn_activo):
        """
        30 UNDs en PICKING, demanda 200 → déficit de 170.
        Con LPN activo en RESERVA → 1 TareaReposicion pre-disparada.
        """
        from app.services.ola_predictiva_service import pre_verificar_ola
        from app.models.tarea_reposicion import TareaReposicion

        resultado = pre_verificar_ola(
            items=[{'producto_id': producto.id, 'cantidad': 200}],
            almacen_id=almacen.id,
        )

        assert resultado['hay_deficit'] is True
        assert resultado['reposiciones_generadas'] == 1
        assert len(resultado['items_con_alerta']) == 1
        assert resultado['items_con_alerta'][0]['deficit'] == 170

        tarea = TareaReposicion.query.filter_by(producto_id=producto.id).first()
        assert tarea is not None
        assert tarea.notas and 'predictiva' in tarea.notas

    def test_alerta_sin_lpn_no_genera_tarea(self, app, db, producto, almacen,
                                              ub_picking, inv_picking):
        """Déficit pero sin LPN en RESERVA → alerta pero sin TareaReposicion."""
        from app.services.ola_predictiva_service import pre_verificar_ola
        from app.models.tarea_reposicion import TareaReposicion

        resultado = pre_verificar_ola(
            items=[{'producto_id': producto.id, 'cantidad': 200}],
            almacen_id=almacen.id,
        )

        assert resultado['hay_deficit'] is True
        assert resultado['reposiciones_generadas'] == 0
        assert TareaReposicion.query.count() == 0

    def test_multiples_productos_en_una_ola(self, app, db, producto, producto2,
                                             almacen, ub_picking, ub_reserva,
                                             inv_picking, lpn_activo):
        """Ola con 2 productos: uno OK, uno con déficit."""
        from app.models.inventario import UbicacionProducto
        from app.services.ola_predictiva_service import pre_verificar_ola

        # Producto2 con stock suficiente en PICKING
        inv2 = UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto2.id,
            cantidad=500, reservado=0,
        )
        db.session.add(inv2)
        db.session.commit()

        resultado = pre_verificar_ola(
            items=[
                {'producto_id': producto.id, 'cantidad': 200},   # déficit
                {'producto_id': producto2.id, 'cantidad': 50},   # OK
            ],
            almacen_id=almacen.id,
        )

        assert len(resultado['items_ok']) == 1
        assert resultado['items_ok'][0]['producto_id'] == producto2.id
        assert len(resultado['items_con_alerta']) == 1
        assert resultado['items_con_alerta'][0]['producto_id'] == producto.id

    def test_no_duplica_tarea_si_ya_existe(self, app, db, producto, almacen,
                                            ub_picking, ub_reserva, inv_picking,
                                            inv_reserva, lpn_activo):
        """Llamar dos veces a pre_verificar_ola no genera tareas duplicadas."""
        from app.services.ola_predictiva_service import pre_verificar_ola
        from app.models.tarea_reposicion import TareaReposicion

        pre_verificar_ola(
            items=[{'producto_id': producto.id, 'cantidad': 200}],
            almacen_id=almacen.id,
        )
        pre_verificar_ola(
            items=[{'producto_id': producto.id, 'cantidad': 200}],
            almacen_id=almacen.id,
        )

        assert TareaReposicion.query.count() == 1
