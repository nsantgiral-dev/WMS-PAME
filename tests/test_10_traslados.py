"""
Test 10 — Módulo de Traslados.

Cubre 3 bloques:
  A. TrasladoService — máquina de estados (crear, enviar, aprobar, rechazar,
     confirmar recepción, guard idempotencia)
  B. ConnektaGateway 173076/173079 — payloads críticos (F_CIA entero,
     F_CONSEC_AUTO_REG, nro_registro secuencial, referencia_item=codigo_siesa,
     Transporte None, ubicacion_entrada multi-ubicaciones)
  C. Endpoints HTTP — autenticación, guards de estado, retry 173079
"""
import os
import pytest
from unittest.mock import patch, MagicMock

# Variables mínimas para que el gateway no aborte antes de llegar a los guards
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
def solicitud_en_transito(db, solicitud_enviada, usuario_admin):
    s = solicitud_enviada
    for item in s.items:
        item.cantidad_aprobada = item.cantidad_solicitada
        item.cantidad_enviada = item.cantidad_solicitada
    s.estado = 'EN_TRANSITO'
    s.aprobador_id = usuario_admin.id
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
            item = ItemSolicitudTraslado(
                solicitud_id=s.id, producto_id=prod_sin_un.id,
                producto_codigo_siesa=prod_sin_un.codigo_siesa,
                cantidad_solicitada=5, cantidad_aprobada=5,
            )
            _db.session.add(item)
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

    def test_confirmar_recepcion_guard_idempotencia(self, app, db, usuario_admin):
        with app.app_context():
            from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
            from app.services.traslado_service import TrasladoService
            from app.extensions import db as _db

            s = SolicitudTraslado(
                codigo='ST-IDEMPOTENTE',
                bodega_origen_siesa='NB1', bodega_destino_siesa='TC1',
                nombre_punto_venta='TC', estado='ENTREGADA',
                modo_transferencia='EN_TRANSITO',
                solicitante_id=usuario_admin.id,
                siesa_salida_consec=100,
                siesa_entrada_consec=200,  # ya fue procesado
            )
            _db.session.add(s)
            _db.session.commit()

            # El guard de estado actúa antes del de consecutivo:
            # ENTREGADA no está en los estados permitidos (EN_TRANSITO, DESPACHADA)
            with pytest.raises(ValueError, match='ENTREGADA'):
                TrasladoService.confirmar_recepcion(
                    solicitud_id=s.id, usuario_id=usuario_admin.id,
                )


# ─────────────────────────────────────────────────────────────────────────────
# B. ConnektaGateway — payloads 173076 y 173079
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
# C. Endpoints HTTP
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
        from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
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
