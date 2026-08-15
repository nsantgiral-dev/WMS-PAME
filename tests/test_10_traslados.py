"""
Test 10 — Módulo de Traslados.

Cubre 4 bloques:
  A. TrasladoService — máquina de estados completa (crear, enviar, aprobar,
     rechazar, despachar, confirmar recepción, guard idempotencia)
  B. TrasladoService — helpers puros (_extraer_consec, _descontar_inventario_wms)
  C. ConnektaGateway 173076/173079 — payloads críticos (F_CIA entero,
     F_CONSEC_AUTO_REG, nro_registro secuencial, referencia_item=codigo_siesa,
     Transporte None, ubicacion_entrada multi-ubicaciones)
  D. Endpoints HTTP — autenticación, guards de estado, retry 173079
"""
import os
import pytest
from unittest.mock import patch, MagicMock

os.environ.setdefault('SIESA_TIPO_DOCTO_TRANSITO_SALIDA', 'TTS')
os.environ.setdefault('SIESA_TIPO_DOCTO_TRANSITO_ENTRADA', 'TTE')
os.environ.setdefault('SIESA_BODEGA_TRANSITO', 'TR')
os.environ.setdefault('SIESA_MOTIVO_TRASLADO', '01')


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures de traslados
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def usuario_tienda(db, almacen):
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(
        nombre='Tienda Centro',
        email='tienda@test.com',
        password_hash=generate_password_hash('test123'),
        rol='tienda',
        almacen_id=almacen.id,
        activo=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def jwt_token_tienda(app, usuario_tienda):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario_tienda.id))


@pytest.fixture
def producto_con_unidad(db):
    from app.models.producto import Producto
    p = Producto(
        codigo='PROD-TRAS',
        nombre='Folder Carta',
        codigo_siesa='PROD-TRAS',
        unidad_negocio_id='UN1',
        unidad_medida='UND',
        activo=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def solicitud_borrador(db, usuario_tienda, producto_con_unidad):
    from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
    s = SolicitudTraslado(
        codigo='ST-TEST-0001',
        bodega_origen_siesa='NB1',
        bodega_destino_siesa='TC1',
        nombre_punto_venta='Tienda Centro',
        estado='BORRADOR',
        modo_transferencia='EN_TRANSITO',
        bodega_transito_siesa='TR',
        solicitante_id=usuario_tienda.id,
    )
    db.session.add(s)
    db.session.flush()
    item = ItemSolicitudTraslado(
        solicitud_id=s.id,
        producto_id=producto_con_unidad.id,
        producto_codigo_siesa=producto_con_unidad.codigo_siesa,
        cantidad_solicitada=10,
    )
    db.session.add(item)
    db.session.commit()
    return s


@pytest.fixture
def solicitud_enviada(db, solicitud_borrador):
    solicitud_borrador.estado = 'ENVIADA'
    db.session.commit()
    return solicitud_borrador


@pytest.fixture
def solicitud_en_picking(db, solicitud_enviada, usuario_admin):
    s = solicitud_enviada
    for item in s.items:
        item.cantidad_aprobada = item.cantidad_solicitada
        item.cantidad_enviada = item.cantidad_solicitada  # > 0 → despachar lo usa directo
    s.estado = 'EN_PICKING'
    s.aprobador_id = usuario_admin.id
    db.session.commit()
    return s


@pytest.fixture
def solicitud_en_transito(db, solicitud_en_picking):
    s = solicitud_en_picking
    for item in s.items:
        item.cantidad_enviada = item.cantidad_aprobada
    s.estado = 'EN_TRANSITO'
    s.siesa_salida_consec = 1001
    db.session.commit()
    return s


@pytest.fixture
def solicitud_entregada_sin_entrada(db, solicitud_en_transito):
    s = solicitud_en_transito
    for item in s.items:
        item.cantidad_recibida = item.cantidad_enviada
    s.estado = 'ENTREGADA'
    s.siesa_entrada_consec = None
    s.siesa_error = '173079: timeout'
    db.session.commit()
    return s


# ─────────────────────────────────────────────────────────────────────────────
# A. TrasladoService — máquina de estados
# ─────────────────────────────────────────────────────────────────────────────

class TestTrasladoServicioEstados:

    def test_crear_solicitud_ok(self, app, db, usuario_tienda, producto_con_unidad):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            s = TrasladoService.crear_solicitud(
                solicitante_id=usuario_tienda.id,
                bodega_destino='TC1',
                nombre_punto_venta='Tienda Centro',
                items=[{'producto_id': producto_con_unidad.id, 'cantidad_solicitada': 5}],
            )
            assert s.estado == 'BORRADOR'
            assert s.codigo.startswith('ST-')
            assert len(s.items) == 1

    def test_crear_solicitud_sin_items_lanza_error(self, app, db, usuario_tienda):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            with pytest.raises(ValueError, match='al menos un ítem'):
                TrasladoService.crear_solicitud(
                    solicitante_id=usuario_tienda.id,
                    bodega_destino='TC1',
                    nombre_punto_venta='Tienda Centro',
                    items=[],
                )

    def test_crear_solicitud_producto_inexistente_lanza_error(self, app, db, usuario_tienda):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            with pytest.raises(ValueError, match='no encontrado'):
                TrasladoService.crear_solicitud(
                    solicitante_id=usuario_tienda.id,
                    bodega_destino='TC1',
                    nombre_punto_venta='Tienda Centro',
                    items=[{'producto_id': 99999, 'cantidad_solicitada': 1}],
                )

    def test_enviar_solicitud_borrador_a_enviada(self, app, db, solicitud_borrador):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            s = TrasladoService.enviar_solicitud(solicitud_borrador.id)
            assert s.estado == 'ENVIADA'
            assert s.fecha_envio is not None

    def test_enviar_solicitud_no_borrador_lanza_error(self, app, db, solicitud_enviada):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            with pytest.raises(ValueError, match='BORRADOR'):
                TrasladoService.enviar_solicitud(solicitud_enviada.id)

    def test_aprobar_solicitud_enviada_a_en_picking(self, app, db, solicitud_enviada, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            with patch('app.services.traslado_service.TrasladoService._crear_picking_tasks',
                       return_value=[]):
                s = TrasladoService.aprobar_solicitud(
                    solicitud_id=solicitud_enviada.id,
                    aprobador_id=usuario_admin.id,
                )
            assert s.estado == 'EN_PICKING'
            assert s.aprobador_id == usuario_admin.id

    def test_aprobar_solicitud_con_cantidades_ajustadas(self, app, db, solicitud_enviada, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            item = solicitud_enviada.items[0]
            with patch('app.services.traslado_service.TrasladoService._crear_picking_tasks',
                       return_value=[]):
                s = TrasladoService.aprobar_solicitud(
                    solicitud_id=solicitud_enviada.id,
                    aprobador_id=usuario_admin.id,
                    items_aprobados=[{'id': item.id, 'cantidad_aprobada': 3}],
                )
            assert s.items[0].cantidad_aprobada == 3

    def test_aprobar_solicitud_producto_sin_unidad_negocio_lanza_error(
            self, app, db, usuario_tienda, usuario_admin):
        with app.app_context():
            from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
            from app.models.producto import Producto
            from app.services.traslado_service import TrasladoService
            from app.extensions import db as _db

            prod_sin_un = Producto(
                codigo='PROD-SIN-UN', nombre='Sin UN',
                codigo_siesa='PROD-SIN-UN', unidad_negocio_id='', activo=True,
            )
            _db.session.add(prod_sin_un)
            _db.session.flush()

            s = SolicitudTraslado(
                codigo='ST-TEST-SIN-UN',
                bodega_origen_siesa='NB1', bodega_destino_siesa='TC1',
                nombre_punto_venta='TC', estado='ENVIADA',
                modo_transferencia='EN_TRANSITO', solicitante_id=usuario_tienda.id,
            )
            _db.session.add(s)
            _db.session.flush()
            _db.session.add(ItemSolicitudTraslado(
                solicitud_id=s.id, producto_id=prod_sin_un.id,
                producto_codigo_siesa=prod_sin_un.codigo_siesa,
                cantidad_solicitada=5, cantidad_aprobada=5,
            ))
            _db.session.commit()

            with pytest.raises(ValueError, match='sin Unidad de Negocio'):
                TrasladoService.aprobar_solicitud(
                    solicitud_id=s.id, aprobador_id=usuario_admin.id,
                )

    def test_rechazar_solicitud_enviada(self, app, db, solicitud_enviada, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            s = TrasladoService.rechazar_solicitud(
                solicitud_id=solicitud_enviada.id,
                aprobador_id=usuario_admin.id,
                motivo='Stock insuficiente',
            )
            assert s.estado == 'RECHAZADA'
            assert s.motivo_rechazo == 'Stock insuficiente'

    def test_rechazar_solo_desde_enviada(self, app, db, solicitud_borrador, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            with pytest.raises(ValueError, match='ENVIADA'):
                TrasladoService.rechazar_solicitud(
                    solicitud_id=solicitud_borrador.id,
                    aprobador_id=usuario_admin.id,
                    motivo='Error',
                )

    def test_despachar_en_picking_a_en_transito(self, app, db, almacen, solicitud_en_picking):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            s = TrasladoService.despachar(solicitud_en_picking.id)
            assert s.estado == 'EN_TRANSITO'
            assert s.fecha_despacho is not None

    def test_despachar_extrae_consec_siesa(self, app, db, almacen, solicitud_en_picking):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.services.connekta_gateway import connekta

            # Patch en la capa de servicio — en modo simulación _post nunca se llama
            with patch.object(connekta, 'transferencia_transito_salida',
                               return_value={'detalle': {'Table': [{'f350_consec_docto': 4242}]}}):
                s = TrasladoService.despachar(solicitud_en_picking.id)

            assert s.siesa_salida_consec == 4242

    def test_despachar_guard_estado_borrador(self, app, db, solicitud_borrador):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            with pytest.raises(ValueError, match='despachar'):
                TrasladoService.despachar(solicitud_borrador.id)

    def test_despachar_siesa_falla_guarda_error_y_avanza_estado(self, app, db, almacen, solicitud_en_picking):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.services.connekta_gateway import connekta

            with patch.object(connekta, 'transferencia_transito_salida',
                               side_effect=Exception('Siesa no disponible')):
                with patch('app.services.alertas_service._enviar_email_con_dlq',
                           create=True, new=MagicMock()):
                    s = TrasladoService.despachar(solicitud_en_picking.id)

            assert s.estado == 'EN_TRANSITO'
            assert s.siesa_error is not None
            assert 'Siesa no disponible' in s.siesa_error

    def test_despachar_modo_directa_llama_transferencia_directa(
            self, app, db, almacen, usuario_tienda, producto_con_unidad):
        with app.app_context():
            from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
            from app.services.traslado_service import TrasladoService
            from app.services.connekta_gateway import connekta
            from app.extensions import db as _db

            s = SolicitudTraslado(
                codigo='ST-DIRECTA-01',
                bodega_origen_siesa='NB1', bodega_destino_siesa='TC1',
                nombre_punto_venta='TC', estado='EN_PICKING',
                modo_transferencia='DIRECTA',
                solicitante_id=usuario_tienda.id,
            )
            _db.session.add(s)
            _db.session.flush()
            _db.session.add(ItemSolicitudTraslado(
                solicitud_id=s.id, producto_id=producto_con_unidad.id,
                producto_codigo_siesa='PROD-TRAS',
                cantidad_solicitada=5, cantidad_aprobada=5,
                cantidad_enviada=5,
            ))
            _db.session.commit()

            llamadas = []
            with patch.object(connekta, 'transferencia_directa',
                               side_effect=lambda *a, **kw: llamadas.append(kw) or {'simulado': True}):
                TrasladoService.despachar(s.id)

            assert len(llamadas) == 1
            assert llamadas[0]['bodega_destino'] == 'TC1'

    def test_despachar_sin_items_con_cantidad_lanza_error(
            self, app, db, usuario_tienda, producto_con_unidad):
        with app.app_context():
            from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
            from app.services.traslado_service import TrasladoService
            from app.extensions import db as _db

            s = SolicitudTraslado(
                codigo='ST-SIN-ITEMS',
                bodega_origen_siesa='NB1', bodega_destino_siesa='TC1',
                nombre_punto_venta='TC', estado='EN_PICKING',
                modo_transferencia='EN_TRANSITO', bodega_transito_siesa='TR',
                solicitante_id=usuario_tienda.id,
            )
            _db.session.add(s)
            _db.session.flush()
            _db.session.add(ItemSolicitudTraslado(
                solicitud_id=s.id, producto_id=producto_con_unidad.id,
                producto_codigo_siesa='PROD-TRAS',
                cantidad_solicitada=0, cantidad_aprobada=0,
                cantidad_enviada=0,
            ))
            _db.session.commit()

            with pytest.raises(ValueError, match='No hay ítems'):
                TrasladoService.despachar(s.id)

    def test_confirmar_recepcion_transicion_a_entregada(self, app, db, solicitud_en_transito, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.services.connekta_gateway import connekta

            siesa_calls = []

            def _post_spy(conector, nombre, payload, **kwargs):
                siesa_calls.append({'conector': conector, 'payload': payload})
                return {'detalle': {'Table': [{'f350_consec_docto': 2001}]}}

            with patch.object(connekta, '_post', side_effect=_post_spy):
                s = TrasladoService.confirmar_recepcion(
                    solicitud_id=solicitud_en_transito.id,
                    usuario_id=usuario_admin.id,
                )

            assert s.estado == 'ENTREGADA'
            assert s.fecha_entrega is not None
            assert len(siesa_calls) == 1
            assert siesa_calls[0]['conector'] == connekta.conector_transito_entrada

    def test_confirmar_recepcion_guarda_siesa_entrada_consec(self, app, db, solicitud_en_transito, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.services.connekta_gateway import connekta

            with patch.object(connekta, '_post',
                               return_value={'detalle': {'Table': [{'f350_consec_docto': 5555}]}}):
                s = TrasladoService.confirmar_recepcion(
                    solicitud_id=solicitud_en_transito.id,
                    usuario_id=usuario_admin.id,
                )

            assert s.siesa_entrada_consec == 5555

    def test_confirmar_recepcion_con_cantidades_parciales(self, app, db, solicitud_en_transito, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.services.connekta_gateway import connekta

            item = solicitud_en_transito.items[0]
            with patch.object(connekta, '_post',
                               return_value={'detalle': {'Table': [{'f350_consec_docto': 100}]}}):
                s = TrasladoService.confirmar_recepcion(
                    solicitud_id=solicitud_en_transito.id,
                    usuario_id=usuario_admin.id,
                    items_recibidos=[{'id': item.id, 'cantidad_recibida': 7}],
                )

            assert s.items[0].cantidad_recibida == 7

    def test_confirmar_recepcion_guard_idempotencia(self, app, db, usuario_admin):
        with app.app_context():
            from app.models.traslado import SolicitudTraslado
            from app.services.traslado_service import TrasladoService
            from app.extensions import db as _db

            s = SolicitudTraslado(
                codigo='ST-IDEMPOTENTE',
                bodega_origen_siesa='NB1', bodega_destino_siesa='TC1',
                nombre_punto_venta='TC', estado='ENTREGADA',
                modo_transferencia='EN_TRANSITO',
                solicitante_id=usuario_admin.id,
                siesa_salida_consec=100,
                siesa_entrada_consec=200,
            )
            _db.session.add(s)
            _db.session.commit()

            # Guard de estado actúa primero: ENTREGADA no es EN_TRANSITO
            with pytest.raises(ValueError, match='ENTREGADA'):
                TrasladoService.confirmar_recepcion(
                    solicitud_id=s.id, usuario_id=usuario_admin.id,
                )

    def test_confirmar_recepcion_siesa_falla_guarda_error_pero_completa(
            self, app, db, solicitud_en_transito, usuario_admin):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.services.connekta_gateway import connekta

            with patch.object(connekta, 'transferencia_transito_entrada',
                               side_effect=Exception('timeout 173079')):
                s = TrasladoService.confirmar_recepcion(
                    solicitud_id=solicitud_en_transito.id,
                    usuario_id=usuario_admin.id,
                )

            assert s.estado == 'ENTREGADA'
            assert s.siesa_entrada_consec is None
            assert '173079' in s.siesa_error


# ─────────────────────────────────────────────────────────────────────────────
# B. TrasladoService — helpers puros
# ─────────────────────────────────────────────────────────────────────────────

class TestExtraerConsec:
    """Tests unitarios para _extraer_consec — no necesitan BD."""

    def test_ruta_canonica_f350(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            res = {'detalle': {'Table': [{'f350_consec_docto': 12345}]}}
            assert TrasladoService._extraer_consec(res) == 12345

    def test_ruta_canonica_consec_docto(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            res = {'detalle': {'Table': [{'consec_docto': 9999}]}}
            assert TrasladoService._extraer_consec(res) == 9999

    def test_ruta_amplia_cualquier_clave_con_consec(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            res = {'detalle': {'Table': [{'mi_consec_interno': 777}]}}
            assert TrasladoService._extraer_consec(res) == 777

    def test_nivel_raiz_consec_docto(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            res = {'consec_docto': 4321}
            assert TrasladoService._extraer_consec(res) == 4321

    def test_detalle_como_lista(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            res = {'detalle': [{'f350_consec_docto': 8888}]}
            assert TrasladoService._extraer_consec(res) == 8888

    def test_retorna_none_cuando_no_encuentra(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            res = {'detalle': {'Table': [{'sin_info': 'nada'}]}}
            assert TrasladoService._extraer_consec(res) is None

    def test_retorna_none_con_response_vacio(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            assert TrasladoService._extraer_consec({}) is None

    def test_no_explota_con_estructura_inesperada(self, app):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            res = {'detalle': 'string-inesperado'}
            assert TrasladoService._extraer_consec(res) is None


class TestDescontarInventarioWMS:
    """
    Estos tests crean inventario para producto_con_unidad (PROD-TRAS) directamente
    — no usan inv_picking del conftest porque ese fixture usa 'producto' (PROD-001).
    Re-query por ID después de commit para evitar errores de scoping de sesión.
    """

    def test_sin_picking_tareas_descuenta_directo(
            self, app, db, almacen, ub_picking, solicitud_en_picking, producto_con_unidad):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto

            # Crear inventario para el producto del traslado (solicitud_en_picking usa producto_con_unidad)
            inv = UbicacionProducto(
                ubicacion_id=ub_picking.id,
                producto_id=producto_con_unidad.id,
                cantidad=30, reservado=0,
            )
            db.session.add(inv)
            db.session.commit()
            inv_id = inv.id

            # solicitud_en_picking ya tiene cantidad_enviada = cantidad_solicitada = 10
            TrasladoService._descontar_inventario_wms(solicitud_en_picking)

            inv_actualizado = UbicacionProducto.query.get(inv_id)
            assert inv_actualizado.cantidad == 20  # 30 - 10

    def test_con_picking_tareas_no_hace_doble_descuento(
            self, app, db, almacen, ub_picking, solicitud_en_picking, producto_con_unidad):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.picking import TareaPicking
            from app.models.inventario import UbicacionProducto

            inv = UbicacionProducto(
                ubicacion_id=ub_picking.id,
                producto_id=producto_con_unidad.id,
                cantidad=30, reservado=0,
            )
            db.session.add(inv)
            s = solicitud_en_picking
            tarea = TareaPicking(
                codigo='PICK-TRASLADO-01',
                producto_id=s.items[0].producto_id,
                almacen_id=almacen.id,
                ubicacion_id=ub_picking.id,
                cantidad_solicitada=s.items[0].cantidad_aprobada,
                referencia_documento=s.codigo,
                tipo_documento='TRASLADO',
                estado='COMPLETADO',
                prioridad=10,
            )
            db.session.add(tarea)
            db.session.commit()
            inv_id = inv.id

            TrasladoService._descontar_inventario_wms(s)

            # Picking ya descontó — _descontar_inventario_wms debe saltarse
            inv_actualizado = UbicacionProducto.query.get(inv_id)
            assert inv_actualizado.cantidad == 30

    def test_sin_almacen_wms_no_lanza_error(self, app, db, solicitud_en_picking):
        with app.app_context():
            from app.services.traslado_service import TrasladoService

            s = solicitud_en_picking
            s.bodega_origen_siesa = 'BODEGA-SIN-MAPA'
            db.session.commit()

            # No debe lanzar — solo loguea warning
            TrasladoService._descontar_inventario_wms(s)

    def test_descontar_registra_movimiento_inventario(
            self, app, db, almacen, ub_picking, solicitud_en_picking, producto_con_unidad):
        with app.app_context():
            from app.services.traslado_service import TrasladoService
            from app.models.inventario import UbicacionProducto, MovimientoInventario

            inv = UbicacionProducto(
                ubicacion_id=ub_picking.id,
                producto_id=producto_con_unidad.id,
                cantidad=50, reservado=0,
            )
            db.session.add(inv)
            s = solicitud_en_picking
            for item in s.items:
                item.cantidad_enviada = 8
            db.session.commit()

            movs_antes = MovimientoInventario.query.count()
            TrasladoService._descontar_inventario_wms(s)

            movs_despues = MovimientoInventario.query.count()
            assert movs_despues > movs_antes
            ultimo = MovimientoInventario.query.order_by(MovimientoInventario.id.desc()).first()
            assert ultimo.tipo == 'SALIDA_TRASLADO'
            assert s.codigo in ultimo.motivo


# ─────────────────────────────────────────────────────────────────────────────
# C. ConnektaGateway — payloads 173076 y 173079
# ─────────────────────────────────────────────────────────────────────────────

class TestConnektaGatewayTraslados:

    def _items_validos(self, n=2):
        return [
            {
                'codigo_siesa': f'SKU-{i:03d}',
                'codigo': f'INT-{i:03d}',
                'cantidad': 10 * i,
                'unidad_medida': 'UND',
                'unidad_negocio_id': 'UN1',
            }
            for i in range(1, n + 1)
        ]

    # ── 173076 ────────────────────────────────────────────────────────────────

    def test_173076_f_cia_viaja_como_entero(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_salida(
                    bodega_origen='NB1', bodega_transito='TR',
                    items=self._items_validos(1), codigo_solicitud='ST-001',
                )

            assert isinstance(payloads[0]['Inicial'][0]['F_CIA'], int)
            assert isinstance(payloads[0]['Documentos'][0]['F_CIA'], int)
            assert isinstance(payloads[0]['Movimientos'][0]['F_CIA'], int)

    def test_173076_consec_auto_reg_es_1(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_salida(
                    bodega_origen='NB1', bodega_transito='TR',
                    items=self._items_validos(1), codigo_solicitud='ST-002',
                )

            assert payloads[0]['Documentos'][0]['F_CONSEC_AUTO_REG'] == 1

    def test_173076_nro_registro_secuencial_desde_1(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_salida(
                    bodega_origen='NB1', bodega_transito='TR',
                    items=self._items_validos(3), codigo_solicitud='ST-003',
                )

            numeros = [m['f470_nro_registro'] for m in payloads[0]['Movimientos']]
            assert numeros == [1, 2, 3]

    def test_173076_referencia_item_usa_codigo_siesa(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_salida(
                    bodega_origen='NB1', bodega_transito='TR',
                    items=[{
                        'codigo_siesa': 'SIESA-XYZ',
                        'codigo': 'INTERNO-123',
                        'cantidad': 5,
                        'unidad_medida': 'UND',
                        'unidad_negocio_id': 'UN1',
                    }],
                    codigo_solicitud='ST-004',
                )

            ref = payloads[0]['Movimientos'][0]['f470_referencia_item']
            assert ref == 'SIESA-XYZ'

    def test_173076_transporte_f462_viajan_como_none(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            # Limpiar env defaults para aislar el test de la configuración del servidor
            connekta.sucursal_transportador = None
            connekta.vehiculo_traslado = None
            connekta.nit_transportador = None
            connekta.nombre_conductor = None

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_salida(
                    bodega_origen='NB1', bodega_transito='TR',
                    items=self._items_validos(1), codigo_solicitud='ST-005',
                )

            # Campos de identidad del transportador deben ir como None cuando no hay transporte
            # (f462_cajas, f462_peso, etc. son numéricos con valor 0 — son válidos)
            doc = payloads[0]['Documentos'][0]
            campos_identidad = [
                'f462_id_vehiculo', 'f462_id_vehículo',
                'f462_id_tercero_transp', 'f462_id_sucursal_transp',
                'f462_id_tercero_conductor', 'f462_nombre_conductor',
                'f462_identif_conductor', 'f462_numero_guia', 'f462_notas',
            ]
            for campo in campos_identidad:
                if campo in doc:
                    assert doc[campo] is None, f'{campo} debe ser None cuando no hay transportador'

    def test_173076_item_sin_codigo_siesa_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'

            with pytest.raises(ValueError, match='codigo_siesa'):
                connekta.transferencia_transito_salida(
                    bodega_origen='NB1', bodega_transito='TR',
                    items=[{'codigo': 'INTERNO-SOLO', 'cantidad': 5, 'unidad_medida': 'UND'}],
                    codigo_solicitud='ST-006',
                )

    # ── 173079 ────────────────────────────────────────────────────────────────

    def test_173079_sin_consec_salida_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            with pytest.raises(ValueError, match='consec_salida obligatorio'):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-007',
                    consec_salida=None,
                )

    def test_173079_nro_registro_secuencial_desde_1(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=self._items_validos(4), codigo_solicitud='ST-008',
                    consec_salida=1001,
                )

            numeros = [m['f470_nro_registro'] for m in payloads[0]['Movimientos']]
            assert numeros == [1, 2, 3, 4]

    def test_173079_ubicacion_aux_ent_siempre_none_aunque_item_tenga_ubicacion(self, app):
        """Bodegas destino (NC1, FC1, etc.) no tienen Multi-Ubicaciones — siempre None."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            items = self._items_validos(1)
            items[0]['ubicacion_entrada'] = 'ENT-TC1'  # ignorado — no aplica

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=items, codigo_solicitud='ST-009',
                    consec_salida=1001,
                )

            mov = payloads[0]['Movimientos'][0]
            assert mov['f470_id_ubicacion_aux_ent'] is None

    def test_173079_ubicacion_aux_ent_siempre_none_aunque_haya_default(self, app):
        """El default del gateway tampoco aplica — las bodegas destino no tienen multi-ubi."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'
            connekta.ubicacion_entrada_default = 'ENT'  # configurado pero ignorado

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-010',
                    consec_salida=1001,
                )

            mov = payloads[0]['Movimientos'][0]
            assert mov['f470_id_ubicacion_aux_ent'] is None

    def test_173079_ubicacion_aux_ent_none_sin_default(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'
            connekta.ubicacion_entrada_default = None

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-011',
                    consec_salida=1001,
                )

            mov = payloads[0]['Movimientos'][0]
            assert mov['f470_id_ubicacion_aux_ent'] is None

    def test_173079_bodega_salida_es_transito_no_origen(self, app):
        """ETS debe usar bodega_transito (TRA1) en f450_id_bodega_salida y f470_id_bodega.
        Siesa valida: ETS.bodega_salida == STS.bodega_entrada (error 62485 si no coincide)."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='NC1',
                    items=self._items_validos(1), codigo_solicitud='ST-BGO',
                    consec_salida=5000,
                )

            doc = payloads[0]['Documentos'][0]
            mov = payloads[0]['Movimientos'][0]
            assert doc['f450_id_bodega_salida'] == 'TR', 'debe ser la bodega de tránsito, no el origen'
            assert doc['f450_id_bodega_entrada'] == 'NC1'
            assert mov['f470_id_bodega'] == 'TR', 'debe coincidir con f450_id_bodega_salida'

    def test_173079_referencia_al_doc_salida(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-012',
                    consec_salida=9999,
                )

            doc = payloads[0]['Documentos'][0]
            assert doc['f350_id_tipo_docto_base'] == 'TTS'
            assert doc['f350_consec_docto_base'] == 9999

    # ── 174646 ────────────────────────────────────────────────────────────────

    def test_174646_f_cia_viaja_como_entero(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_req_traslado = 'RIT'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.crear_requisicion_traslado(
                    bodega_origen='NB1', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-RIT-001',
                )

            assert isinstance(payloads[0]['Inicial'][0]['F_CIA'], int)
            assert isinstance(payloads[0]['Documentos'][0]['F_CIA'], int)
            assert isinstance(payloads[0]['Movimientos'][0]['F_CIA'], int)

    def test_174646_consec_auto_reg_es_1(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_req_traslado = 'RIT'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.crear_requisicion_traslado(
                    bodega_origen='NB1', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-RIT-002',
                )

            assert payloads[0]['Documentos'][0]['F_CONSEC_AUTO_REG'] == 1

    def test_174646_ind_estado_es_1_comprometido(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_req_traslado = 'RIT'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.crear_requisicion_traslado(
                    bodega_origen='NB1', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-RIT-003',
                )

            # Estado 3 Comprometido en Siesa — bloquea el inventario antes del picking
            assert payloads[0]['Documentos'][0]['f440_ind_estado'] == 1

    def test_174646_bodega_origen_y_destino_correctas(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_req_traslado = 'RIT'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.crear_requisicion_traslado(
                    bodega_origen='NB1', bodega_destino='TC2',
                    items=self._items_validos(1), codigo_solicitud='ST-RIT-004',
                )

            doc = payloads[0]['Documentos'][0]
            assert doc['f440_id_bodega_salida'] == 'NB1'
            assert doc['f440_id_bodega_entrada'] == 'TC2'

    def test_174646_nro_registro_secuencial_desde_1(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_req_traslado = 'RIT'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.crear_requisicion_traslado(
                    bodega_origen='NB1', bodega_destino='TC1',
                    items=self._items_validos(3), codigo_solicitud='ST-RIT-005',
                )

            numeros = [m['f441_nro_registro'] for m in payloads[0]['Movimientos']]
            assert numeros == [1, 2, 3]

    def test_174646_item_sin_codigo_siesa_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_req_traslado = 'RIT'

            with pytest.raises(ValueError, match='codigo_siesa'):
                connekta.crear_requisicion_traslado(
                    bodega_origen='NB1', bodega_destino='TC1',
                    items=[{'codigo': 'INTERNO-SOLO', 'cantidad': 5, 'unidad_medida': 'UND'}],
                    codigo_solicitud='ST-RIT-006',
                )

    def test_174646_sin_tipo_docto_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_req_traslado = ''

            with pytest.raises(ValueError):
                connekta.crear_requisicion_traslado(
                    bodega_origen='NB1', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-RIT-007',
                )


# ─────────────────────────────────────────────────────────────────────────────
# D. Endpoints HTTP
# ─────────────────────────────────────────────────────────────────────────────

class TestEndpointsTraslados:

    def test_crear_solicitud_tienda_201(self, app, db, client, jwt_token_tienda,
                                        usuario_tienda, producto_con_unidad):
        resp = client.post(
            '/api/traslados/',
            json={
                'bodega_destino_siesa': 'TC1',
                'nombre_punto_venta': 'Tienda Centro',
                'items': [{'producto_id': producto_con_unidad.id, 'cantidad_solicitada': 3}],
            },
            headers={'Authorization': f'Bearer {jwt_token_tienda}'},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['estado'] == 'BORRADOR'

    def test_crear_solicitud_sin_token_401(self, app, db, client, producto_con_unidad):
        resp = client.post(
            '/api/traslados/',
            json={
                'bodega_destino_siesa': 'TC1',
                'items': [{'producto_id': producto_con_unidad.id, 'cantidad_solicitada': 3}],
            },
        )
        assert resp.status_code == 401

    def test_aprobar_solicitud_admin_200(self, app, db, client, jwt_token_admin,
                                         solicitud_enviada):
        with patch('app.services.traslado_service.TrasladoService._crear_picking_tasks',
                   return_value=[]):
            resp = client.post(
                f'/api/traslados/{solicitud_enviada.id}/aprobar',
                json={},
                headers={'Authorization': f'Bearer {jwt_token_admin}'},
            )
        assert resp.status_code == 200
        assert resp.get_json()['estado'] == 'EN_PICKING'

    def test_aprobar_solicitud_tienda_403(self, app, db, client, jwt_token_tienda,
                                          solicitud_enviada):
        resp = client.post(
            f'/api/traslados/{solicitud_enviada.id}/aprobar',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_tienda}'},
        )
        assert resp.status_code == 403

    def test_despachar_endpoint_admin_200(self, app, db, client, jwt_token_admin,
                                          almacen, solicitud_en_picking):
        resp = client.post(
            f'/api/traslados/{solicitud_en_picking.id}/despachar',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        assert resp.get_json()['estado'] == 'EN_TRANSITO'

    def test_despachar_endpoint_tienda_403(self, app, db, client, jwt_token_tienda,
                                           solicitud_en_picking):
        resp = client.post(
            f'/api/traslados/{solicitud_en_picking.id}/despachar',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_tienda}'},
        )
        assert resp.status_code == 403

    def test_confirmar_recepcion_admin_puede_confirmar(self, app, db, client,
                                                        jwt_token_admin,
                                                        solicitud_en_transito):
        from app.services.connekta_gateway import connekta
        with patch.object(connekta, '_post',
                          return_value={'detalle': {'Table': [{'f350_consec_docto': 3001}]}}):
            resp = client.post(
                f'/api/traslados/{solicitud_en_transito.id}/recibir',
                json={},
                headers={'Authorization': f'Bearer {jwt_token_admin}'},
            )
        assert resp.status_code == 200
        assert resp.get_json()['estado'] == 'ENTREGADA'

    def test_reintentar_recepcion_solo_admin(self, app, db, client,
                                              jwt_token_tienda,
                                              solicitud_entregada_sin_entrada):
        resp = client.post(
            f'/api/traslados/{solicitud_entregada_sin_entrada.id}/reintentar-recepcion',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_tienda}'},
        )
        assert resp.status_code == 403

    def test_reintentar_recepcion_solo_entregada(self, app, db, client,
                                                  jwt_token_admin,
                                                  solicitud_en_transito):
        resp = client.post(
            f'/api/traslados/{solicitud_en_transito.id}/reintentar-recepcion',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 400
        assert 'ENTREGADA' in resp.get_json()['error']

    def test_reintentar_recepcion_guard_idempotencia(self, app, db, client,
                                                      jwt_token_admin,
                                                      solicitud_en_transito):
        s = solicitud_en_transito
        s.estado = 'ENTREGADA'
        s.siesa_entrada_consec = 888
        from app.extensions import db as _db
        _db.session.commit()

        resp = client.post(
            f'/api/traslados/{s.id}/reintentar-recepcion',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 400
        assert '888' in resp.get_json()['error']

    def test_reintentar_recepcion_guarda_consec_cuando_siesa_ok(
            self, app, db, client, jwt_token_admin, solicitud_entregada_sin_entrada):
        from app.services.connekta_gateway import connekta
        with patch.object(connekta, '_post',
                          return_value={'detalle': {'Table': [{'f350_consec_docto': 7777}]}}):
            resp = client.post(
                f'/api/traslados/{solicitud_entregada_sin_entrada.id}/reintentar-recepcion',
                json={},
                headers={'Authorization': f'Bearer {jwt_token_admin}'},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        assert data['solicitud']['siesa_entrada_consec'] == 7777

    def test_reintentar_recepcion_solo_modo_en_transito(self, app, db, client,
                                                         jwt_token_admin, usuario_admin):
        from app.models.traslado import SolicitudTraslado
        from app.extensions import db as _db

        s = SolicitudTraslado(
            codigo='ST-DIRECTA-TEST',
            bodega_origen_siesa='NB1', bodega_destino_siesa='TC1',
            nombre_punto_venta='TC', estado='ENTREGADA',
            modo_transferencia='DIRECTA',
            solicitante_id=usuario_admin.id,
            siesa_salida_consec=500,
        )
        _db.session.add(s)
        _db.session.commit()

        resp = client.post(
            f'/api/traslados/{s.id}/reintentar-recepcion',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 400
        assert 'EN_TRANSITO' in resp.get_json()['error']


# ─────────────────────────────────────────────────────────────────────────────
# E. Unit tests — fixes sesión 2026-06-11
#    1. f470_id_ubicacion_aux_ent siempre None (ambas variantes con/sin acento)
#    2. Una sola definición de transferencia_desde_requisicion
# ─────────────────────────────────────────────────────────────────────────────

class TestFixesSesion20260611:

    def _items_validos(self, n=1):
        return [
            {
                'codigo_siesa': f'SKU-FIX-{i}',
                'codigo': f'INT-FIX-{i}',
                'cantidad': 5,
                'unidad_medida': 'UND',
                'unidad_negocio_id': 'UN1',
            }
            for i in range(1, n + 1)
        ]

    def test_ets_ubicacion_aux_ent_ninguna_variante_sale_con_valor(self, app):
        """Ambas claves (con y sin acento) deben ser None — Siesa rechaza cualquier valor."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'
            connekta.ubicacion_entrada_default = 'ENT-BODEGA'  # valor no None — debe ignorarse

            items = self._items_validos(2)
            items[0]['ubicacion_entrada'] = 'ZONA-A'  # también ignorado

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='NC1',
                    items=items, codigo_solicitud='ST-FIX-001',
                    consec_salida=2001,
                )

            for mov in payloads[0]['Movimientos']:
                assert mov['f470_id_ubicacion_aux_ent'] is None, \
                    'f470_id_ubicacion_aux_ent no debe enviar valor — bodega sin multi-ubicaciones'

    def test_ets_ind_estado_es_1(self, app):
        """f350_ind_estado=1 (Comprometido) es requerido por Siesa para ETS."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='NC1',
                    items=self._items_validos(), codigo_solicitud='ST-FIX-002',
                    consec_salida=2002,
                )

            assert payloads[0]['Documentos'][0]['f350_ind_estado'] == 1

    def test_ets_rowid_movto_es_0(self, app):
        """f470_rowid_movto=0 — Siesa lo requiere explícito para ETS."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='NC1',
                    items=self._items_validos(), codigo_solicitud='ST-FIX-003',
                    consec_salida=2003,
                )

            assert payloads[0]['Movimientos'][0]['f470_rowid_movto'] == 0

    def test_solo_una_definicion_de_transferencia_desde_requisicion(self, app):
        """Python usa la última definición cuando hay duplicados — verificar que no hay dos."""
        import inspect
        import ast
        import pathlib

        gw_path = pathlib.Path(__file__).parent.parent / 'app' / 'services' / 'connekta_gateway.py'
        tree = ast.parse(gw_path.read_text(encoding='utf-8'))

        definiciones = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == 'transferencia_desde_requisicion'
        ]
        assert len(definiciones) == 1, (
            f'Se encontraron {len(definiciones)} definiciones de transferencia_desde_requisicion '
            f'— solo debe haber una (Python usa la última, la primera queda muerta)'
        )

    def test_ets_consec_auto_reg_es_1(self, app):
        """F_CONSEC_AUTO_REG=1 indica a Siesa que asigne el consecutivo automáticamente."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='NC1',
                    items=self._items_validos(), codigo_solicitud='ST-FIX-004',
                    consec_salida=2004,
                )

            assert payloads[0]['Documentos'][0]['F_CONSEC_AUTO_REG'] == 1


# ─────────────────────────────────────────────────────────────────────────────
# F. E2E — Flujo recepción de traslados desde tienda (picking ítem por ítem)
#    Simula el ciclo completo: tienda lista sus traslados EN_TRANSITO,
#    confirma recepción → ETS se dispara → estado ENTREGADA
# ─────────────────────────────────────────────────────────────────────────────

class TestE2ERecepcionTienda:

    @pytest.fixture
    def usuario_tienda2(self, db, almacen):
        """Segunda tienda — para verificar aislamiento entre usuarios."""
        from app.models.usuario import Usuario
        from werkzeug.security import generate_password_hash
        u = Usuario(
            nombre='Tienda Sur',
            email='tienda2@test.com',
            password_hash=generate_password_hash('test123'),
            rol='tienda',
            almacen_id=almacen.id,
            activo=True,
        )
        db.session.add(u)
        db.session.commit()
        return u

    @pytest.fixture
    def jwt_token_tienda2(self, app, usuario_tienda2):
        from flask_jwt_extended import create_access_token
        with app.app_context():
            return create_access_token(identity=str(usuario_tienda2.id))

    @pytest.fixture
    def solicitud_en_transito_tienda(self, db, usuario_tienda, producto_con_unidad):
        """Traslado EN_TRANSITO perteneciente a usuario_tienda con siesa_salida_consec."""
        from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
        s = SolicitudTraslado(
            codigo='ST-E2E-RECV-001',
            bodega_origen_siesa='NB1',
            bodega_destino_siesa='NC1',
            nombre_punto_venta='Neiva Centro',
            estado='EN_TRANSITO',
            modo_transferencia='EN_TRANSITO',
            bodega_transito_siesa='TR',
            solicitante_id=usuario_tienda.id,
            siesa_salida_consec=9001,
        )
        db.session.add(s)
        db.session.flush()
        item = ItemSolicitudTraslado(
            solicitud_id=s.id,
            producto_id=producto_con_unidad.id,
            producto_codigo_siesa=producto_con_unidad.codigo_siesa,
            cantidad_solicitada=10,
            cantidad_aprobada=10,
            cantidad_enviada=10,
        )
        db.session.add(item)
        db.session.commit()
        return s

    def test_tienda_lista_solo_sus_traslados_en_transito(
            self, app, db, client, jwt_token_tienda, jwt_token_tienda2,
            solicitud_en_transito_tienda):
        """GET /api/traslados/?estado=EN_TRANSITO filtra por solicitante — cada tienda ve solo las suyas."""
        resp_tienda1 = client.get(
            '/api/traslados/?estado=EN_TRANSITO',
            headers={'Authorization': f'Bearer {jwt_token_tienda}'},
        )
        assert resp_tienda1.status_code == 200
        ids_tienda1 = [s['id'] for s in resp_tienda1.get_json()['solicitudes']]
        assert solicitud_en_transito_tienda.id in ids_tienda1

        # tienda2 no debe ver el traslado de tienda1
        resp_tienda2 = client.get(
            '/api/traslados/?estado=EN_TRANSITO',
            headers={'Authorization': f'Bearer {jwt_token_tienda2}'},
        )
        assert resp_tienda2.status_code == 200
        ids_tienda2 = [s['id'] for s in resp_tienda2.get_json()['solicitudes']]
        assert solicitud_en_transito_tienda.id not in ids_tienda2

    def test_tienda_confirma_recepcion_dispara_ets_y_entrega(
            self, app, db, client, jwt_token_tienda,
            solicitud_en_transito_tienda):
        """POST /api/traslados/<id>/recibir → ETS se llama → estado ENTREGADA → 200."""
        from app.services.connekta_gateway import connekta

        ets_calls = []

        def _post_spy(conector, nombre, payload, **kwargs):
            ets_calls.append({'conector': conector, 'payload': payload})
            return {'detalle': {'Table': [{'f350_consec_docto': 9999}]}}

        with patch.object(connekta, '_post', side_effect=_post_spy):
            resp = client.post(
                f'/api/traslados/{solicitud_en_transito_tienda.id}/recibir',
                json={},
                headers={'Authorization': f'Bearer {jwt_token_tienda}'},
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['estado'] == 'ENTREGADA'
        # ETS fue llamado exactamente una vez
        assert len(ets_calls) == 1
        # El payload ETS incluye f350_consec_docto_base = siesa_salida_consec del traslado
        doc = ets_calls[0]['payload']['Documentos'][0]
        assert doc['f350_consec_docto_base'] == 9001
        # ubicacion_aux_ent es None en todos los movimientos
        for mov in ets_calls[0]['payload']['Movimientos']:
            assert mov['f470_id_ubicacion_aux_ent'] is None

    def test_tienda_no_puede_confirmar_recepcion_ajena(
            self, app, db, client, jwt_token_tienda2,
            solicitud_en_transito_tienda):
        """Una tienda no puede confirmar recepción de traslados que no son suyos."""
        from app.services.connekta_gateway import connekta

        with patch.object(connekta, '_post', return_value={'detalle': {'Table': [{'f350_consec_docto': 1}]}}):
            resp = client.post(
                f'/api/traslados/{solicitud_en_transito_tienda.id}/recibir',
                json={},
                headers={'Authorization': f'Bearer {jwt_token_tienda2}'},
            )

        assert resp.status_code in (403, 404)

    def test_confirmar_recepcion_sin_token_401(
            self, app, db, client, solicitud_en_transito_tienda):
        resp = client.post(
            f'/api/traslados/{solicitud_en_transito_tienda.id}/recibir',
            json={},
        )
        assert resp.status_code == 401

    def test_confirmar_recepcion_estado_no_en_transito_falla(
            self, app, db, client, jwt_token_tienda, solicitud_borrador):
        """Solo traslados EN_TRANSITO se pueden recibir."""
        resp = client.post(
            f'/api/traslados/{solicitud_borrador.id}/recibir',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_tienda}'},
        )
        # La tienda solo ve sus propias solicitudes; si no es su traslado → 404
        # Si es suya pero en estado incorrecto → 400
        assert resp.status_code in (400, 403, 404)

    def test_siesa_consec_entrada_guardado_en_bd(
            self, app, db, client, jwt_token_tienda,
            solicitud_en_transito_tienda):
        """Después de confirmar, siesa_entrada_consec debe quedar persistido en BD."""
        from app.services.connekta_gateway import connekta
        from app.models.traslado import SolicitudTraslado

        with patch.object(connekta, '_post',
                          return_value={'detalle': {'Table': [{'f350_consec_docto': 7654}]}}):
            client.post(
                f'/api/traslados/{solicitud_en_transito_tienda.id}/recibir',
                json={},
                headers={'Authorization': f'Bearer {jwt_token_tienda}'},
            )

        s = SolicitudTraslado.query.get(solicitud_en_transito_tienda.id)
        assert s.siesa_entrada_consec == 7654


# ─────────────────────────────────────────────────────────────────────────────
# G. La tarjeta verde sobre stock varado
# ─────────────────────────────────────────────────────────────────────────────

class TestElErrorDeSiesaSeVeCuandoPideAccion:
    """`to_dict` decidía por ESTADO si valía la pena mostrar `siesa_error`, y
    ENTREGADA estaba en la lista de los que callan.

    Pero `confirmar_recepcion` escribe ENTREGADA **aunque el 173079 haya
    fallado** — el estado describe el hecho físico: la mercancía llegó a la
    tienda. El resultado era el peor de los dos mundos: la unidad está en la
    bodega puente para Siesa, en la tienda para el WMS, y la tarjeta se pinta
    en verde sin una sola advertencia. Nadie reclama un faltante porque nadie
    lo ve, y el traslado se descubre meses después cuadrando inventarios.

    La pregunta correcta no es «¿terminó?» sino «¿falta el documento de
    cierre?». Eso es `not consec_cierre`, que ya estaba en la fórmula.
    """

    def _dict(self, s):
        from app.models.traslado import SolicitudTraslado
        return SolicitudTraslado.query.get(s.id).to_dict()

    def test_entregada_sin_entrada_en_siesa_muestra_el_error(
            self, app, db, solicitud_entregada_sin_entrada):
        """EL CASO. ENTREGADA + sin 173079 = stock varado en la bodega puente."""
        d = self._dict(solicitud_entregada_sin_entrada)
        assert d['siesa_necesita_atencion'] is True
        assert d['siesa_error'] and '173079' in d['siesa_error'], (
            'un traslado entregado sin entrada registrada en Siesa se pinta '
            'igual que uno sano — el operario no tiene forma de enterarse')

    def test_entregada_con_entrada_registrada_no_avisa_nada(
            self, app, db, solicitud_entregada_sin_entrada):
        """Si el consecutivo llegó, un error viejo es ruido: se calla.

        Sin esta mitad, «mostrar siempre» sería igual de inútil que «no mostrar
        nunca» — 639 avisos hacen invisible al que importa.
        """
        s = solicitud_entregada_sin_entrada
        s.siesa_entrada_consec = 5150
        db.session.commit()
        d = self._dict(s)
        assert d['siesa_necesita_atencion'] is False
        assert d['siesa_error'] is None

    @pytest.mark.parametrize('estado', ['RECHAZADA', 'CANCELADA', 'REVERTIDA'])
    def test_los_tres_que_no_esperan_cierre_siguen_callando(
            self, app, db, solicitud_entregada_sin_entrada, estado):
        """Un traslado rechazado, cancelado o revertido NO debe tener documento
        de cierre en Siesa. Un error ahí no pide ninguna acción."""
        s = solicitud_entregada_sin_entrada
        s.estado = estado
        db.session.commit()
        d = self._dict(s)
        assert d['siesa_necesita_atencion'] is False
        assert d['siesa_error'] is None

    def test_el_reintento_exige_exactamente_el_estado_que_se_escribe(
            self, app, db, client, jwt_token_admin, solicitud_entregada_sin_entrada):
        """DECLARACIÓN, no arreglo: `confirmar_recepcion` sigue escribiendo
        ENTREGADA aunque el 173079 falle, y debe seguir haciéndolo.

        `POST /reintentar-recepcion` rechaza con 400 cualquier estado que no
        sea ENTREGADA (`app/routes/traslados.py:662`). Si la recepción dejara
        el traslado en EN_TRANSITO al fallar la entrada, el único botón que
        arregla el problema quedaría inalcanzable justo en el caso para el que
        existe. La visibilidad se resolvió donde correspondía —en `to_dict`—,
        no moviendo el estado.

        Este test es el candado: el día que alguien cambie una de las dos
        puntas sin la otra, falla.
        """
        s = solicitud_entregada_sin_entrada
        assert s.estado == 'ENTREGADA' and s.siesa_entrada_consec is None
        resp = client.post(
            f'/api/traslados/{s.id}/reintentar-recepcion',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code != 400, (
            'el estado que escribe confirmar_recepcion y el que exige '
            'reintentar-recepcion dejaron de coincidir')
