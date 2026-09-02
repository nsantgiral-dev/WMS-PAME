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

    def test_dispensador_respeta_orden_fisico_no_fecha_creacion(self, app, db, mobile_setup):
        """
        Regresión real (2026-07-27, PD1347): get_tarea_actual() ordenaba por
        fecha_creacion sin mirar ubicación — un pedido con líneas en pasillos
        A, B, G despachaba G primero (la línea más vieja del pedido en Siesa),
        no A (el más cercano). Se crean las tareas a propósito en orden
        cronológico INVERSO a la ruta física (G, luego B, luego A) para
        probar que el orden ganador es el físico, no el de creación.
        """
        from app.services import layout_service as svc
        from app.services.mobile_service import MobileService

        s = mobile_setup
        ub_g = svc.crear_cuerpo(s['almacen'].id, 'G', 1, 1, 1, 'PICKING')[0]
        ub_b = svc.crear_cuerpo(s['almacen'].id, 'B', 1, 1, 1, 'PICKING')[0]
        ub_a = svc.crear_cuerpo(s['almacen'].id, 'A', 1, 1, 1, 'PICKING')[0]

        tarea_g = _crear_tarea(db, s['producto'], ub_g, s['almacen'], referencia_documento='PD-ORDEN')
        _crear_tarea(db, s['producto'], ub_b, s['almacen'], referencia_documento='PD-ORDEN')
        _crear_tarea(db, s['producto'], ub_a, s['almacen'], referencia_documento='PD-ORDEN')

        resultado = MobileService.get_tarea_actual(s['usuario'].id)

        assert resultado['ubicacion'] == ub_a.codigo
        assert resultado['id'] != tarea_g.id

    def test_dispensador_continua_mismo_documento_pese_a_orden_fisico(self, app, db, mobile_setup):
        """
        El operario ya tiene una tarea EN_PROCESO/COMPLETADO de PD-X. Aunque
        haya una tarea de otro documento (PD-Y) físicamente más cerca, el
        dispensador debe seguir en PD-X hasta agotarlo — evita que el
        operario rebote entre pedidos a medio terminar.
        """
        from app.services import layout_service as svc
        from app.services.mobile_service import MobileService

        s = mobile_setup
        ub_lejos = svc.crear_cuerpo(s['almacen'].id, 'Z', 1, 1, 1, 'PICKING')[0]
        ub_cerca = svc.crear_cuerpo(s['almacen'].id, 'A', 1, 1, 1, 'PICKING')[0]

        # Tarea ya completada de PD-X — marca a PD-X como "documento en curso".
        _crear_tarea(
            db, s['producto'], ub_cerca, s['almacen'],
            referencia_documento='PD-X', estado=EstadoPicking.COMPLETADO,
            operario_id=s['usuario'].id, cantidad_recogida=10,
            fecha_completado=datetime.utcnow(),
        )
        # Pendiente de PD-X, lejos físicamente.
        tarea_x_pendiente = _crear_tarea(
            db, s['producto'], ub_lejos, s['almacen'], referencia_documento='PD-X',
        )
        # Pendiente de OTRO documento, más cerca físicamente — no debe ganarle a PD-X.
        _crear_tarea(
            db, s['producto'], ub_cerca, s['almacen'], referencia_documento='PD-Y',
        )

        resultado = MobileService.get_tarea_actual(s['usuario'].id)

        assert resultado['id'] == tarea_x_pendiente.id
        assert resultado['referencia'] == 'PD-X'

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

    def test_dispensador_ofrece_reposicion_como_nivel_2(
        self, app, db, usuario_abastecedor, inv_picking, inv_reserva, lpn_activo, almacen,
    ):
        """
        Sin Pedido/Traslado pendiente, con una TareaReposicion PENDIENTE y el
        operario con puede_abastecer=True, el dispensador la entrega como
        nivel 2 de la cola unificada (entre Pedido/Traslado y Conteo cíclico).
        """
        from app.services.reposicion_service import verificar_stock_picking
        from app.services.mobile_service import MobileService

        generadas = verificar_stock_picking(almacen_id=almacen.id)
        assert generadas == 1

        resultado = MobileService.get_tarea_actual(usuario_abastecedor.id)

        assert resultado is not None
        assert resultado['tipo'] == 'REPOSICION'
        assert resultado['ubicacion_picking'] is not None
        assert resultado['lpn_codigo'] == lpn_activo.codigo

    def test_pedido_pendiente_gana_a_reposicion(
        self, app, db, usuario_abastecedor, inv_picking, inv_reserva, lpn_activo, almacen,
        producto, ub_picking,
    ):
        """Con un Pedido PENDIENTE Y una TareaReposicion PENDIENTE, el Pedido
        sale primero — nivel 1 (Pedido/Traslado) le gana al nivel 2 (Reposición)."""
        from app.services.reposicion_service import verificar_stock_picking
        from app.services.mobile_service import MobileService

        verificar_stock_picking(almacen_id=almacen.id)
        tarea_pedido = _crear_tarea(db, producto, ub_picking, almacen)

        resultado = MobileService.get_tarea_actual(usuario_abastecedor.id)

        assert resultado['tipo'] == 'PICKING'
        assert resultado['id'] == tarea_pedido.id

    def test_reposicion_no_se_ofrece_sin_permiso_abastecer(
        self, app, db, usuario, inv_picking, inv_reserva, lpn_activo, almacen,
    ):
        """Un operario sin puede_abastecer nunca recibe una TareaReposicion —
        cae directo a None (sin conteo pendiente en este setup)."""
        from app.services.reposicion_service import verificar_stock_picking
        from app.services.mobile_service import MobileService

        verificar_stock_picking(almacen_id=almacen.id)

        resultado = MobileService.get_tarea_actual(usuario.id)

        assert resultado is None


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
