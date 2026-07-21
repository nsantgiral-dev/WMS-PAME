"""
Tests de MobileService — dispensador de tareas y procesamiento de scans.
Verifica el flujo completo: dispensar tarea, escanear producto, confirmar.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from app.models.picking import TareaPicking, EstadoPicking


@pytest.fixture
def mobile_setup(db, almacen, producto, ub_picking, inv_picking, usuario):
    """Setup completo para tests de mobile service."""
    return {
        'almacen': almacen,
        'producto': producto,
        'ubicacion': ub_picking,
        'inventario': inv_picking,
        'usuario': usuario,
    }


def _crear_tarea(db, producto, ubicacion, almacen, **overrides):
    """Helper para crear TareaPicking con defaults sensatos."""
    import uuid
    defaults = dict(
        codigo=f'PICK-{uuid.uuid4().hex[:8].upper()}',
        producto_id=producto.id,
        cantidad_solicitada=10,
        cantidad_recogida=0,
        ubicacion_id=ubicacion.id,
        almacen_id=almacen.id,
        estado=EstadoPicking.PENDIENTE,
        operario_id=None,
        tipo_documento='PEDIDO',
        referencia_documento='PED-TEST-001',
        prioridad=5,
    )
    defaults.update(overrides)
    tarea = TareaPicking(**defaults)
    db.session.add(tarea)
    db.session.commit()
    return tarea


class TestDispensador:

    def test_dispensador_sin_tareas(self, app, db, mobile_setup):
        """Sin tareas pendientes ni en proceso, get_tarea_actual devuelve None."""
        s = mobile_setup
        from app.services.mobile_service import MobileService

        resultado = MobileService.get_tarea_actual(s['usuario'].id)

        assert resultado is None

    def test_dispensador_asigna_picking(self, app, db, mobile_setup):
        """Con una tarea PENDIENTE sin operario, el dispensador la asigna y la devuelve."""
        s = mobile_setup
        from app.services.mobile_service import MobileService

        tarea = _crear_tarea(db, s['producto'], s['ubicacion'], s['almacen'])

        resultado = MobileService.get_tarea_actual(s['usuario'].id)

        assert resultado is not None
        assert resultado['id'] == tarea.id
        assert resultado['tipo'] == 'PICKING'
        assert resultado['estado'] == 'EN_PROCESO'
        assert resultado['producto_codigo'] == s['producto'].codigo
        assert resultado['cantidad_requerida'] == 10

        # Verificar que la tarea fue mutada en DB
        db.session.refresh(tarea)
        assert tarea.operario_id == s['usuario'].id
        assert tarea.estado == EstadoPicking.EN_PROCESO
        assert tarea.fecha_inicio is not None

    def test_dispensador_devuelve_activa(self, app, db, mobile_setup):
        """Si el operario ya tiene una tarea EN_PROCESO, la devuelve sin asignar otra."""
        s = mobile_setup
        from app.services.mobile_service import MobileService

        tarea_activa = _crear_tarea(
            db, s['producto'], s['ubicacion'], s['almacen'],
            estado=EstadoPicking.EN_PROCESO,
            operario_id=s['usuario'].id,
            fecha_inicio=datetime.utcnow(),
        )
        # Crear otra tarea pendiente que NO debe asignarse
        _crear_tarea(db, s['producto'], s['ubicacion'], s['almacen'],
                     codigo='PICK-EXTRA')

        resultado = MobileService.get_tarea_actual(s['usuario'].id)

        assert resultado is not None
        assert resultado['id'] == tarea_activa.id
        assert resultado['estado'] == 'EN_PROCESO'


class TestProcesarEscaneo:

    def test_scan_producto_correcto(self, app, db, mobile_setup):
        """Escanear el codigo correcto incrementa cantidad_recogida."""
        s = mobile_setup
        from app.services.mobile_service import MobileService, _SCAN_DEBOUNCE

        tarea = _crear_tarea(
            db, s['producto'], s['ubicacion'], s['almacen'],
            estado=EstadoPicking.EN_PROCESO,
            operario_id=s['usuario'].id,
        )

        _SCAN_DEBOUNCE.clear()  # limpiar cache entre tests

        resultado = MobileService.procesar_escaneo(
            operario_id=s['usuario'].id,
            tarea_id=tarea.id,
            tipo='PICKING',
            codigo=s['producto'].codigo,
            cantidad=1,
            total_acumulado=3,
        )

        assert resultado['exito'] is True
        assert resultado['tipo'] == 'PICKING'
        assert resultado['cantidad_actual'] == 3
        assert resultado['cantidad_requerida'] == 10

    def test_scan_producto_incorrecto(self, app, db, mobile_setup):
        """Escanear un codigo que no pertenece al producto lanza ValueError."""
        s = mobile_setup
        from app.services.mobile_service import MobileService, _SCAN_DEBOUNCE

        tarea = _crear_tarea(
            db, s['producto'], s['ubicacion'], s['almacen'],
            estado=EstadoPicking.EN_PROCESO,
            operario_id=s['usuario'].id,
        )

        _SCAN_DEBOUNCE.clear()

        with pytest.raises(ValueError) as exc_info:
            MobileService.procesar_escaneo(
                operario_id=s['usuario'].id,
                tarea_id=tarea.id,
                tipo='PICKING',
                codigo='CODIGO-INEXISTENTE-XYZ',
                cantidad=1,
            )

        error = exc_info.value.args[0]
        assert error['tipo'] == 'PRODUCTO_INCORRECTO'

    def test_scan_exceso_rechaza(self, app, db, mobile_setup):
        """Escanear mas alla de cantidad_solicitada lanza ValueError con tipo EXCESO."""
        s = mobile_setup
        from app.services.mobile_service import MobileService, _SCAN_DEBOUNCE

        tarea = _crear_tarea(
            db, s['producto'], s['ubicacion'], s['almacen'],
            cantidad_solicitada=5,
            cantidad_recogida=4,
            estado=EstadoPicking.EN_PROCESO,
            operario_id=s['usuario'].id,
        )

        _SCAN_DEBOUNCE.clear()

        with pytest.raises(ValueError) as exc_info:
            MobileService.procesar_escaneo(
                operario_id=s['usuario'].id,
                tarea_id=tarea.id,
                tipo='PICKING',
                codigo=s['producto'].codigo,
                cantidad=1,
                total_acumulado=6,  # 6 > 5 solicitada
            )

        error = exc_info.value.args[0]
        assert error['tipo'] == 'EXCESO'

    def test_scan_debounce(self, app, db, mobile_setup):
        """Mismo scan con igual total_acumulado dentro del TTL devuelve resultado cacheado."""
        s = mobile_setup
        from app.services.mobile_service import MobileService, _SCAN_DEBOUNCE

        tarea = _crear_tarea(
            db, s['producto'], s['ubicacion'], s['almacen'],
            cantidad_solicitada=10,
            cantidad_recogida=0,
            estado=EstadoPicking.EN_PROCESO,
            operario_id=s['usuario'].id,
        )

        _SCAN_DEBOUNCE.clear()

        # Primer scan -- procesa normalmente
        res1 = MobileService.procesar_escaneo(
            operario_id=s['usuario'].id,
            tarea_id=tarea.id,
            tipo='PICKING',
            codigo=s['producto'].codigo,
            cantidad=1,
            total_acumulado=3,
        )

        # Segundo scan identico -- debounce devuelve el cache
        res2 = MobileService.procesar_escaneo(
            operario_id=s['usuario'].id,
            tarea_id=tarea.id,
            tipo='PICKING',
            codigo=s['producto'].codigo,
            cantidad=1,
            total_acumulado=3,
        )

        # Ambos deben ser iguales (mismo objeto cacheado)
        assert res1 == res2
        assert res2['cantidad_actual'] == 3

        # La DB no fue incrementada de nuevo
        db.session.refresh(tarea)
        assert tarea.cantidad_recogida == 3


class TestConfirmarTarea:

    @patch('app.services.mobile_service.PickingService')
    def test_confirmar_picking(self, mock_picking_svc, app, db, mobile_setup):
        """confirmar_tarea(tipo=PICKING) llama a PickingService.confirmar_picking."""
        s = mobile_setup
        from app.services.mobile_service import MobileService

        tarea = _crear_tarea(
            db, s['producto'], s['ubicacion'], s['almacen'],
            cantidad_solicitada=5,
            cantidad_recogida=5,
            estado=EstadoPicking.EN_PROCESO,
            operario_id=s['usuario'].id,
        )

        # Mock del resultado de confirmar_picking
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            'id': tarea.id,
            'estado': 'COMPLETADO',
            'cantidad_recogida': 5,
            'referencia': 'PED-TEST-001',
        }
        mock_picking_svc.confirmar_picking.return_value = mock_result

        resultado = MobileService.confirmar_tarea(
            operario_id=s['usuario'].id,
            tarea_id=tarea.id,
            tipo='PICKING',
        )

        mock_picking_svc.confirmar_picking.assert_called_once_with(
            tarea_id=tarea.id,
            cantidad_recogida=5,
            usuario_id=s['usuario'].id,
        )
        assert resultado['estado'] == 'COMPLETADO'
