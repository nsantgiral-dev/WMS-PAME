"""
Test 03 — Motor de Reabastecimiento RESERVA → PICKING.
Flujo completo: detección de mínimo → asignación → confirmación → LPN CONSUMIDO.
"""
import pytest
import unittest.mock as mock


class TestVerificarStockPicking:

    def test_genera_tarea_cuando_bajo_minimo(self, app, db,
                                              inv_picking, inv_reserva, lpn_activo,
                                              ub_picking, producto, almacen):
        """30 UNDs en PICKING, mínimo=50 → debe generar 1 TareaReposicion."""
        from app.services.reposicion_service import verificar_stock_picking
        from app.models.tarea_reposicion import TareaReposicion

        generadas = verificar_stock_picking(almacen_id=almacen.id)

        assert generadas == 1
        tarea = TareaReposicion.query.filter_by(
            producto_id=producto.id,
            estado='PENDIENTE'
        ).first()
        assert tarea is not None
        assert tarea.lpn_id == lpn_activo.id
        assert tarea.ubicacion_picking_id == ub_picking.id

    def test_no_genera_duplicado_si_ya_existe_tarea(self, app, db,
                                                      inv_picking, inv_reserva,
                                                      lpn_activo, almacen):
        """Si ya hay una tarea PENDIENTE, no se crea otra."""
        from app.services.reposicion_service import verificar_stock_picking
        from app.models.tarea_reposicion import TareaReposicion

        verificar_stock_picking(almacen_id=almacen.id)
        generadas2 = verificar_stock_picking(almacen_id=almacen.id)

        assert generadas2 == 0
        assert TareaReposicion.query.count() == 1

    def test_no_genera_tarea_si_sobre_minimo(self, app, db,
                                              ub_picking, producto, almacen):
        """100 UNDs, mínimo=50 → no hay alerta."""
        from app.models.inventario import UbicacionProducto
        from app.services.reposicion_service import verificar_stock_picking

        inv = UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            cantidad=100, reservado=0,
        )
        db.session.add(inv)
        db.session.commit()

        generadas = verificar_stock_picking(almacen_id=almacen.id)
        assert generadas == 0

    def test_no_genera_si_sin_lpn_en_reserva(self, app, db,
                                               inv_picking, almacen):
        """Stock bajo pero sin LPN disponible → 0 tareas (y warning en log)."""
        from app.services.reposicion_service import verificar_stock_picking
        from app.models.tarea_reposicion import TareaReposicion

        generadas = verificar_stock_picking(almacen_id=almacen.id)
        assert generadas == 0
        assert TareaReposicion.query.count() == 0

    def test_cantidad_a_reponer_llena_hasta_maximo(self, app, db,
                                                    inv_picking, inv_reserva,
                                                    lpn_activo, ub_picking, almacen):
        """30 en PICKING, max=200 → cantidad_unidades = min(1240, 200-30) = 170."""
        from app.services.reposicion_service import verificar_stock_picking
        from app.models.tarea_reposicion import TareaReposicion

        verificar_stock_picking(almacen_id=almacen.id)

        tarea = TareaReposicion.query.first()
        assert tarea.cantidad_unidades == 170  # 200 - 30


class TestAsignacionAbastecedor:

    def test_asigna_tarea_pendiente(self, app, db, inv_picking, inv_reserva,
                                    lpn_activo, almacen, usuario):
        from app.services.reposicion_service import verificar_stock_picking, get_tarea_abastecedor

        verificar_stock_picking(almacen_id=almacen.id)
        tarea_dict = get_tarea_abastecedor(usuario.id)

        assert tarea_dict is not None
        assert tarea_dict['estado'] == 'EN_PROCESO'
        assert tarea_dict['tipo'] == 'REPOSICION'

    def test_retorna_none_si_no_hay_tareas(self, app, db, usuario):
        from app.services.reposicion_service import get_tarea_abastecedor
        resultado = get_tarea_abastecedor(usuario.id)
        assert resultado is None

    def test_retorna_misma_tarea_si_ya_en_proceso(self, app, db,
                                                    inv_picking, inv_reserva,
                                                    lpn_activo, almacen, usuario):
        from app.services.reposicion_service import verificar_stock_picking, get_tarea_abastecedor

        verificar_stock_picking(almacen_id=almacen.id)
        t1 = get_tarea_abastecedor(usuario.id)
        t2 = get_tarea_abastecedor(usuario.id)

        assert t1['id'] == t2['id']


class TestConfirmarReposicion:

    def _setup_tarea(self, app, db, inv_picking, inv_reserva, lpn_activo, almacen, usuario):
        from app.services.reposicion_service import verificar_stock_picking, get_tarea_abastecedor
        verificar_stock_picking(almacen_id=almacen.id)
        return get_tarea_abastecedor(usuario.id)

    def test_flujo_completo_rompe_paca(self, app, db, inv_picking, inv_reserva,
                                        lpn_activo, ub_picking, almacen, usuario):
        """
        El flujo dorado: confirmar reposición actualiza todo correctamente.
        LPN → CONSUMIDO, stock PICKING += 1240, TareaReposicion → COMPLETADA.
        """
        from app.services.reposicion_service import confirmar_reposicion
        from app.models.lpn import LPN
        from app.models.inventario import UbicacionProducto
        from app.models.tarea_reposicion import TareaReposicion

        tarea_dict = self._setup_tarea(app, db, inv_picking, inv_reserva,
                                        lpn_activo, almacen, usuario)

        # Mockear la DLQ para no llamar a Siesa
        with mock.patch('app.services.reposicion_service._encolar_siesa_job'):
            resultado = confirmar_reposicion(
                tarea_id=tarea_dict['id'],
                abastecedor_id=usuario.id,
                lpn_codigo_escaneado='LPN-0000001',
            )

        assert resultado['ok'] is True

        # LPN consumido
        lpn = LPN.query.get(lpn_activo.id)
        assert lpn.estado == 'CONSUMIDO'
        assert lpn.fecha_consumo is not None

        # Stock PICKING incrementado (30 + 1240 = 1270)
        inv = UbicacionProducto.query.filter_by(ubicacion_id=ub_picking.id).first()
        assert inv.cantidad == 1270

        # Tarea completada
        tarea = TareaReposicion.query.get(tarea_dict['id'])
        assert tarea.estado == 'COMPLETADA'
        assert tarea.unidades_movidas == 1240

    def test_falla_si_lpn_incorrecto(self, app, db, inv_picking, inv_reserva,
                                      lpn_activo, almacen, usuario):
        from app.services.reposicion_service import confirmar_reposicion

        tarea_dict = self._setup_tarea(app, db, inv_picking, inv_reserva,
                                        lpn_activo, almacen, usuario)

        with pytest.raises(ValueError, match='no coincide'):
            confirmar_reposicion(
                tarea_id=tarea_dict['id'],
                abastecedor_id=usuario.id,
                lpn_codigo_escaneado='LPN-OTRO',
            )

    def test_falla_si_no_es_el_abastecedor(self, app, db, inv_picking, inv_reserva,
                                             lpn_activo, almacen, usuario):
        from app.services.reposicion_service import confirmar_reposicion

        tarea_dict = self._setup_tarea(app, db, inv_picking, inv_reserva,
                                        lpn_activo, almacen, usuario)

        with pytest.raises(ValueError, match='no te pertenece'):
            confirmar_reposicion(
                tarea_id=tarea_dict['id'],
                abastecedor_id=9999,  # otro usuario
            )
