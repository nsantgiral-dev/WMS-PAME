"""
Tests de PickingService — el flujo más crítico del CDI.
Si picking falla, nada sale del almacén.
"""
import pytest


@pytest.fixture
def picking_setup(db, almacen, producto, ub_picking, inv_picking, usuario):
    """Setup completo para tests de picking."""
    return {
        'almacen': almacen,
        'producto': producto,
        'ubicacion': ub_picking,
        'inventario': inv_picking,
        'usuario': usuario,
    }


class TestCrearTareas:

    def test_crear_tareas_genera_picking(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id,
            cantidad=5,
            almacen_id=s['almacen'].id,
            referencia_documento='PED-001',
            tipo_documento='PEDIDO',
        )
        assert len(tareas) >= 1
        assert tareas[0].cantidad_solicitada == 5
        assert tareas[0].estado == 'PENDIENTE'
        assert tareas[0].referencia_documento == 'PED-001'

    def test_crear_tareas_reserva_stock(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        from app.models.inventario import UbicacionProducto
        antes = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado

        PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )

        despues = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado
        assert despues == antes + 5

    def test_crear_tareas_stock_insuficiente(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        with pytest.raises(ValueError, match='[Ss]tock insuficiente'):
            PickingService.crear_tareas(
                producto_id=s['producto'].id,
                cantidad=99999,  # más de lo disponible
                almacen_id=s['almacen'].id,
            )


class TestFefoPrefiereUbicacionReal:
    """
    Caso real PD1343 (2026-07-24): SIESA-GENERAL (bucket sin ubicación física
    del sync de Siesa) con stock viejo siempre ganaba contra un hueco de Layout
    recién asignado, porque FEFO desempataba solo por fecha_ingreso — y
    GENERAL, al llevar meses cargado, es casi siempre más "antiguo" que
    cualquier hueco recién organizado. calcular_fefo() ahora prioriza
    ubicaciones reales (PICKING/RESERVA/AVERIAS) sobre GENERAL sin importar
    la fecha.
    """

    def test_prefiere_hueco_real_aunque_general_sea_mas_antiguo_y_tenga_mas_stock(
        self, app, db, almacen, producto, ub_picking, ub_general,
    ):
        from datetime import datetime, timedelta
        from app.models.inventario import UbicacionProducto
        from app.services.picking_service import PickingService

        db.session.add(UbicacionProducto(
            ubicacion_id=ub_general.id, producto_id=producto.id,
            cantidad=4000, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow() - timedelta(days=120),  # viejo — caso PD1343
        ))
        db.session.add(UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            cantidad=10, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow(),  # recién asignado hoy
        ))
        db.session.commit()

        fefo = PickingService.calcular_fefo(producto.id, 3, almacen.id)

        assert fefo['completo'] is True
        assert len(fefo['asignaciones']) == 1
        assert fefo['asignaciones'][0]['ubicacion_id'] == ub_picking.id

    def test_general_sigue_como_respaldo_si_ubicacion_real_no_alcanza(
        self, app, db, almacen, producto, ub_picking, ub_general,
    ):
        from datetime import datetime, timedelta
        from app.models.inventario import UbicacionProducto
        from app.services.picking_service import PickingService

        db.session.add(UbicacionProducto(
            ubicacion_id=ub_general.id, producto_id=producto.id,
            cantidad=4000, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow() - timedelta(days=120),
        ))
        db.session.add(UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            cantidad=10, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow(),
        ))
        db.session.commit()

        # Pide más de lo que tiene el hueco real (10) — debe completar con GENERAL
        fefo = PickingService.calcular_fefo(producto.id, 15, almacen.id)

        assert fefo['completo'] is True
        assert len(fefo['asignaciones']) == 2
        assert fefo['asignaciones'][0]['ubicacion_id'] == ub_picking.id
        assert fefo['asignaciones'][0]['cantidad'] == 10
        assert fefo['asignaciones'][1]['ubicacion_id'] == ub_general.id
        assert fefo['asignaciones'][1]['cantidad'] == 5


class TestConfirmarPicking:

    def test_confirmar_decrementa_stock(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        from app.models.inventario import UbicacionProducto

        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        stock_antes = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad

        PickingService.confirmar_picking(
            tarea_id=tarea.id,
            cantidad_recogida=5,
            usuario_id=s['usuario'].id,
        )

        stock_despues = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad
        assert stock_despues == stock_antes - 5

    def test_confirmar_tarea_completada(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.confirmar_picking(tarea.id, 5, s['usuario'].id)
        db.session.refresh(tarea)
        assert tarea.estado == 'COMPLETADO'
        assert tarea.cantidad_recogida == 5

    def test_confirmar_doble_no_decrementa_doble(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.confirmar_picking(tarea.id, 5, s['usuario'].id)
        with pytest.raises(ValueError, match='[Yy]a completada'):
            PickingService.confirmar_picking(tarea.id, 5, s['usuario'].id)

    def test_confirmar_cantidad_parcial(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=10,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.confirmar_picking(tarea.id, 7, s['usuario'].id)
        db.session.refresh(tarea)
        assert tarea.cantidad_recogida == 7
        assert tarea.estado == 'COMPLETADO'

    def test_confirmar_exceso_rechaza(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        with pytest.raises(ValueError, match='[Ss]upera'):
            PickingService.confirmar_picking(tarea.id, 999, s['usuario'].id)


class TestIniciarCancelarReabrir:

    def test_iniciar_picking(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.iniciar_picking(tarea.id, s['usuario'].id)
        db.session.refresh(tarea)
        assert tarea.estado == 'EN_PROCESO'
        assert tarea.operario_id == s['usuario'].id

    def test_cancelar_picking_libera_reserva(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        from app.models.inventario import UbicacionProducto

        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        reservado_antes = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado

        PickingService.cancelar_picking(tarea.id, motivo='Test cancel')

        reservado_despues = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado

        db.session.refresh(tarea)
        assert tarea.estado == 'CANCELADO'
        assert reservado_despues == reservado_antes - 5
