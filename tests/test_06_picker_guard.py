"""
Test 06 — Regla Estricta del Picker: nunca va a zonas RESERVA.
El picker solo puede picar en PICKING y GENERAL.
"""
import pytest


class TestPickerSoloVeZonasPermitidas:

    def _crear_tarea_picking(self, db, producto, almacen, ubicacion, prioridad=1):
        """Helper: crea una TareaPicking en una ubicación dada."""
        from app.models.picking import TareaPicking
        from app.models.inventario import UbicacionProducto
        import uuid

        # Asegurar stock reservado
        inv = UbicacionProducto.query.filter_by(
            ubicacion_id=ubicacion.id, producto_id=producto.id
        ).first()
        if not inv:
            inv = UbicacionProducto(
                ubicacion_id=ubicacion.id, producto_id=producto.id,
                cantidad=100, reservado=0,
            )
            db.session.add(inv)

        tarea = TareaPicking(
            codigo=f'PICK-TEST-{str(uuid.uuid4())[:6]}',
            producto_id=producto.id,
            cantidad_solicitada=10,
            ubicacion_id=ubicacion.id,
            almacen_id=almacen.id,
            estado='PENDIENTE',
            prioridad=prioridad,
        )
        db.session.add(tarea)
        db.session.commit()
        return tarea

    def test_picker_toma_tarea_en_zona_picking(self, app, db, producto, almacen,
                                                ub_picking, usuario):
        """Tarea en zona PICKING → asignada normalmente al picker."""
        from app.services.mobile_service import MobileService

        self._crear_tarea_picking(db, producto, almacen, ub_picking)
        resultado = MobileService.get_tarea_actual(usuario.id)

        assert resultado is not None
        assert resultado['tipo'] == 'PICKING'
        assert resultado['ubicacion'] == 'PIK-01-A'

    def test_picker_toma_tarea_en_zona_general(self, app, db, producto, almacen,
                                                ub_general, usuario):
        """Tarea en zona GENERAL → permitida para el picker."""
        from app.services.mobile_service import MobileService

        self._crear_tarea_picking(db, producto, almacen, ub_general)
        resultado = MobileService.get_tarea_actual(usuario.id)

        assert resultado is not None
        assert resultado['ubicacion'] == 'GEN-01'

    def test_picker_NO_ve_tarea_en_zona_reserva(self, app, db, producto, almacen,
                                                  ub_reserva, usuario):
        """
        REGLA CRÍTICA: tarea en zona RESERVA → NUNCA asignada al picker.
        El picker no puede ir a las estanterías altas de pacas selladas.
        """
        from app.services.mobile_service import MobileService

        self._crear_tarea_picking(db, producto, almacen, ub_reserva)
        resultado = MobileService.get_tarea_actual(usuario.id)

        # El picker no debe ver esta tarea
        assert resultado is None or resultado.get('ubicacion') != 'RES-01-A'

    def test_picker_toma_picking_no_reserva_cuando_ambas_existen(self, app, db,
                                                                    producto, almacen,
                                                                    ub_picking, ub_reserva,
                                                                    usuario):
        """
        Con tareas en RESERVA y PICKING simultáneamente:
        el picker siempre toma la de PICKING, nunca la de RESERVA.
        """
        from app.services.mobile_service import MobileService

        # Tarea en RESERVA (prioridad alta) y en PICKING (prioridad baja)
        self._crear_tarea_picking(db, producto, almacen, ub_reserva, prioridad=10)
        self._crear_tarea_picking(db, producto, almacen, ub_picking, prioridad=1)

        resultado = MobileService.get_tarea_actual(usuario.id)

        assert resultado is not None
        assert resultado['ubicacion'] == 'PIK-01-A'

    def test_picker_sin_tareas_retorna_none(self, app, db, usuario):
        """Sin tareas disponibles → None."""
        from app.services.mobile_service import MobileService
        resultado = MobileService.get_tarea_actual(usuario.id)
        assert resultado is None


class TestConfiguracionLimites:
    """El admin puede configurar stock_minimo/stock_maximo en el WMS."""

    def test_patch_limites_via_api(self, app, db, client, jwt_token, ub_picking):
        response = client.patch(
            f'/api/reposicion/ubicacion/{ub_picking.id}/limites',
            json={'stock_minimo': 75, 'stock_maximo': 300, 'secuencia_ruteo': 5},
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert data['ubicacion']['stock_minimo'] == 75
        assert data['ubicacion']['stock_maximo'] == 300
        assert data['ubicacion']['secuencia_ruteo'] == 5

    def test_listar_ubicaciones_picking(self, app, db, client, jwt_token,
                                         ub_picking, producto):
        from app.models.inventario import UbicacionProducto
        inv = UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            cantidad=100, reservado=0,
        )
        db.session.add(inv)
        db.session.commit()

        response = client.get(
            '/api/reposicion/ubicaciones',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['total'] >= 1
        ubs = {u['codigo']: u for u in data['ubicaciones']}
        assert 'PIK-01-A' in ubs
        assert ubs['PIK-01-A']['limites_configurados'] is True  # tiene min y max
