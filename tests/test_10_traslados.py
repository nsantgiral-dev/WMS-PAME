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

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_salida(
                    bodega_origen='NB1', bodega_transito='TR',
                    items=self._items_validos(1), codigo_solicitud='ST-005',
                )

            transporte = payloads[0]['Transporte'][0]
            campos_f462 = [k for k in transporte if k.startswith('f462_')]
            assert len(campos_f462) > 0
            for campo in campos_f462:
                assert transporte[campo] is None, f'{campo} debe ser None, no string vacío'

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

    def test_173079_ubicacion_entrada_desde_item(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'
            connekta.ubicacion_entrada_default = None

            items = self._items_validos(1)
            items[0]['ubicacion_entrada'] = 'ENT-TC1'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=items, codigo_solicitud='ST-009',
                    consec_salida=1001,
                )

            ubi = payloads[0]['Movimientos'][0]['f470_id_ubicacion_aux_ent']
            assert ubi == 'ENT-TC1'

    def test_173079_ubicacion_entrada_fallback_a_gateway_default(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_transito_salida = 'TTS'
            connekta.tipo_docto_transito_entrada = 'TTE'
            connekta.ubicacion_entrada_default = 'ENT'

            payloads = []
            with patch.object(connekta, '_post',
                               side_effect=lambda c, n, p, **kw: payloads.append(p) or {}):
                connekta.transferencia_transito_entrada(
                    bodega_transito='TR', bodega_destino='TC1',
                    items=self._items_validos(1), codigo_solicitud='ST-010',
                    consec_salida=1001,
                )

            ubi = payloads[0]['Movimientos'][0]['f470_id_ubicacion_aux_ent']
            assert ubi == 'ENT'

    def test_173079_sin_ubicacion_env_manda_none(self, app):
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

            ubi = payloads[0]['Movimientos'][0]['f470_id_ubicacion_aux_ent']
            assert ubi is None

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
