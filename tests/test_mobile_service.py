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

    @staticmethod
    def _crear_otro_operario(db, almacen, email):
        from app.models.usuario import Usuario
        from werkzeug.security import generate_password_hash
        u = Usuario(nombre='Otro Operario', email=email,
                    password_hash=generate_password_hash('test123'),
                    rol='operario', almacen_id=almacen.id, activo=True)
        db.session.add(u)
        db.session.commit()
        return u

    def test_pedido_chico_queda_pegado_al_primer_operario(self, app, db, mobile_setup, producto2):
        """
        Pedido con 2 líneas (< PEDIDO_LINEAS_PARALELIZABLE, default 4): el
        operario que toma la primera línea se queda con el pedido — otro
        operario no puede tomar la segunda línea mientras la primera siga
        sin terminar. Partir un pedido chico entre varios pickers no gana
        velocidad real y sí le suma coordinación al empaque.
        """
        from app.services.mobile_service import MobileService

        s = mobile_setup
        otro = self._crear_otro_operario(db, s['almacen'], 'otro_chico@test.com')

        linea1 = _crear_tarea(db, s['producto'], s['ubicacion'], s['almacen'],
                               referencia_documento='PD-CHICO')
        linea2 = _crear_tarea(db, producto2, s['ubicacion'], s['almacen'],
                               referencia_documento='PD-CHICO')

        resultado_a = MobileService.get_tarea_actual(s['usuario'].id)
        assert resultado_a['id'] == linea1.id

        resultado_b = MobileService.get_tarea_actual(otro.id)
        assert resultado_b is None, (
            'la línea 2 de un pedido chico no debe ofrecerse a otro operario '
            'mientras la línea 1 la tenga alguien más')

        db.session.refresh(linea2)
        assert linea2.operario_id is None

    def test_pedido_chico_libera_la_segunda_linea_al_mismo_operario(self, app, db, mobile_setup, producto2):
        """El "pegado" es al operario, no un bloqueo total — el mismo
        operario que tiene la línea 1 sigue recibiendo las demás líneas de
        su propio pedido chico al terminarlas (mismo criterio que ya prueba
        test_dispensador_continua_mismo_documento_pese_a_orden_fisico)."""
        from datetime import datetime as _dt
        from app.services.mobile_service import MobileService

        s = mobile_setup
        _crear_tarea(
            db, s['producto'], s['ubicacion'], s['almacen'],
            referencia_documento='PD-CHICO-2', estado=EstadoPicking.COMPLETADO,
            operario_id=s['usuario'].id, cantidad_recogida=10,
            fecha_completado=_dt.utcnow(),
        )
        linea2 = _crear_tarea(db, producto2, s['ubicacion'], s['almacen'],
                               referencia_documento='PD-CHICO-2')

        resultado = MobileService.get_tarea_actual(s['usuario'].id)

        assert resultado['id'] == linea2.id

    def test_pedido_grande_se_reparte_entre_varios_operarios(self, app, db, mobile_setup, producto2):
        """Pedido con >= PEDIDO_LINEAS_PARALELIZABLE líneas (4 por defecto):
        dos operarios distintos SÍ pueden tomar líneas distintas del mismo
        pedido al mismo tiempo — acá sí conviene paralelizar."""
        from app.models.producto import Producto
        from app.services.mobile_service import MobileService

        s = mobile_setup
        otro = self._crear_otro_operario(db, s['almacen'], 'otro_grande@test.com')
        prod3 = Producto(codigo='PROD-003', nombre='Cuaderno', codigo_siesa='PROD-003', activo=True)
        prod4 = Producto(codigo='PROD-004', nombre='Borrador', codigo_siesa='PROD-004', activo=True)
        db.session.add_all([prod3, prod4])
        db.session.commit()

        _crear_tarea(db, s['producto'], s['ubicacion'], s['almacen'], referencia_documento='PD-GRANDE')
        _crear_tarea(db, producto2, s['ubicacion'], s['almacen'], referencia_documento='PD-GRANDE')
        _crear_tarea(db, prod3, s['ubicacion'], s['almacen'], referencia_documento='PD-GRANDE')
        _crear_tarea(db, prod4, s['ubicacion'], s['almacen'], referencia_documento='PD-GRANDE')

        resultado_a = MobileService.get_tarea_actual(s['usuario'].id)
        resultado_b = MobileService.get_tarea_actual(otro.id)

        assert resultado_a is not None and resultado_b is not None
        assert resultado_a['referencia'] == 'PD-GRANDE'
        assert resultado_b['referencia'] == 'PD-GRANDE'
        assert resultado_a['id'] != resultado_b['id']

    def test_umbral_de_paralelizacion_es_configurable(self, app, db, mobile_setup, producto2, monkeypatch):
        """PEDIDO_LINEAS_PARALELIZABLE se puede bajar (ej. a 2) y un pedido de
        2 líneas pasa a tratarse como grande — se reparte igual que uno de 4+."""
        import app.services.mobile_service as mobile_service_mod
        from app.services.mobile_service import MobileService

        monkeypatch.setattr(mobile_service_mod, 'PEDIDO_LINEAS_PARALELIZABLE', 2)

        s = mobile_setup
        otro = self._crear_otro_operario(db, s['almacen'], 'otro_umbral@test.com')

        _crear_tarea(db, s['producto'], s['ubicacion'], s['almacen'], referencia_documento='PD-DOS')
        _crear_tarea(db, producto2, s['ubicacion'], s['almacen'], referencia_documento='PD-DOS')

        resultado_a = MobileService.get_tarea_actual(s['usuario'].id)
        resultado_b = MobileService.get_tarea_actual(otro.id)

        assert resultado_a is not None and resultado_b is not None
        assert resultado_a['id'] != resultado_b['id']

    def test_traslado_no_aplica_regla_de_pedido_chico(self, app, db, mobile_setup, producto2):
        """La regla de "pedido chico pegajoso" es solo para PEDIDO — un
        TRASLADO con pocas líneas se sigue repartiendo libre entre cualquier
        picker, sin importar el umbral."""
        from app.services.mobile_service import MobileService

        s = mobile_setup
        otro = self._crear_otro_operario(db, s['almacen'], 'otro_traslado@test.com')

        _crear_tarea(db, s['producto'], s['ubicacion'], s['almacen'],
                     tipo_documento='TRASLADO', referencia_documento='ST-CHICO',
                     bodega_origen_siesa=s['almacen'].bodega_siesa_id)
        _crear_tarea(db, producto2, s['ubicacion'], s['almacen'],
                     tipo_documento='TRASLADO', referencia_documento='ST-CHICO',
                     bodega_origen_siesa=s['almacen'].bodega_siesa_id)

        resultado_a = MobileService.get_tarea_actual(s['usuario'].id)
        resultado_b = MobileService.get_tarea_actual(otro.id)

        assert resultado_a is not None and resultado_b is not None
        assert resultado_a['id'] != resultado_b['id']


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
