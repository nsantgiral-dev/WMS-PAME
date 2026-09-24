"""
Retención de cartera — `/api/cartera/*`. Ver `app/services/cartera_service.py`.

Dos puertas a la misma política:

· **Servicio a servicio (el Gestor de Cartera)** — `Authorization: Bearer
  <CARTERA_GESTOR_TOKEN>`, `Idempotency-Key` en todo POST. Es la vía principal:
  el usuario de cartera decide sin entrar al WMS.

    GET  /api/cartera/retenciones?estado=&nit=&cambiados_desde=&limite=
    GET  /api/cartera/retenciones/<id>
    POST /api/cartera/retenciones/<id>/autorizar          {usuario, motivo, codigo_excepcion, tope_valor, vence_en}
    POST /api/cartera/retenciones/<id>/convertir-contado  {usuario, motivo}
    POST /api/cartera/retenciones/<id>/reevaluar          {usuario?}
    POST /api/cartera/habilitaciones                      {nit, sucursal?, canal, acuerdo_vigente, acuerdo_vence, excepciones[], as_of, usuario} | {clientes: [...], usuario}
    GET  /api/cartera/salud

· **Respaldo en el WMS (JWT)** — `/api/cartera/panel/*`, con el permiso
  `puede_autorizar_cartera` para decidir y gestión para mirar.
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.extensions import db
from app.routes._auth_helpers import (_puede_autorizar_cartera, _solo_admin, _ve_cartera,
                                      exige_token_servicio)

logger = logging.getLogger(__name__)
cartera_bp = Blueprint('cartera', __name__)

_TOKEN = 'CARTERA_GESTOR_TOKEN'
_QUE = 'la API de cartera para el Gestor'


# ── utilidades ────────────────────────────────────────────────────────────

def _fecha_iso(texto):
    if not texto:
        return None
    d = datetime.fromisoformat(str(texto).strip().replace('Z', '+00:00'))
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _usuario_del_gestor(data):
    """`usuario` es un objeto `{username, nombre, email, rol, sistema}`. Sin
    username ni email no se sabe quién decide: 400."""
    u = (data or {}).get('usuario')
    if not isinstance(u, dict):
        raise ValueError('usuario debe ser un objeto {username, nombre, email, rol, sistema}')
    if not (str(u.get('username') or '').strip() or str(u.get('email') or '').strip()):
        raise ValueError('usuario.username o usuario.email es obligatorio')
    return {k: (str(u.get(k)).strip()[:120] if u.get(k) is not None else None)
            for k in ('username', 'nombre', 'email', 'rol', 'sistema')}


def _responder(fn):
    """Traduce las excepciones de la política a HTTP."""
    from app.services.cartera_service import AccionRechazada
    try:
        return fn()
    except LookupError as e:
        db.session.rollback()
        return {'error': str(e)}, 404
    except AccionRechazada as e:
        db.session.rollback()
        return {'error': str(e), 'estado': e.estado}, 409
    except ValueError as e:
        db.session.rollback()
        return {'error': str(e)}, 400


def _idempotente(endpoint: str, retencion_id, fn):
    """`Idempotency-Key` obligatoria. La misma clave devuelve la misma
    respuesta sin volver a ejecutar; usada en otra operación → 409. Solo se
    guarda lo exitoso: un error se puede reintentar con la misma clave."""
    from app.models.cartera import CarteraIdempotencia
    clave = (request.headers.get('Idempotency-Key') or '').strip()
    if not clave:
        return jsonify({'error': 'Falta la cabecera Idempotency-Key'}), 400
    if len(clave) > 120:
        return jsonify({'error': 'Idempotency-Key de más de 120 caracteres'}), 400
    previa = CarteraIdempotencia.query.filter_by(clave=clave).first()
    if previa is not None:
        if previa.endpoint != endpoint or previa.retencion_id != retencion_id:
            return jsonify({'error': 'Esa Idempotency-Key ya se usó en otra operación'}), 409
        return jsonify({**(previa.respuesta or {}), 'idempotente': True}), 200
    cuerpo, status = _responder(fn)
    if 200 <= status < 300:
        db.session.add(CarteraIdempotencia(clave=clave, endpoint=endpoint,
                                           retencion_id=retencion_id, status=status,
                                           respuesta=cuerpo))
        db.session.commit()
    return jsonify(cuerpo), status


# ═════════════════════════════════════════════════════════════════════════════
# Gestor de Cartera (servicio a servicio)
# ═════════════════════════════════════════════════════════════════════════════

@cartera_bp.route('/retenciones', methods=['GET'])
@exige_token_servicio(_TOKEN, _QUE)
def gestor_listar():
    from app.models.cartera import EstadoRetencion
    from app.services import cartera_service as cs
    estado = (request.args.get('estado') or '').strip().upper() or None
    if estado and estado not in EstadoRetencion.TODOS:
        return jsonify({'error': f'estado debe ser uno de {", ".join(EstadoRetencion.TODOS)}'}), 400
    try:
        desde = _fecha_iso(request.args.get('cambiados_desde'))
    except ValueError:
        return jsonify({'error': 'cambiados_desde no es una fecha ISO 8601'}), 400
    try:
        limite = int(request.args.get('limite') or 200)
    except ValueError:
        return jsonify({'error': 'limite no es un entero'}), 400
    try:
        return jsonify(cs.listar(estado, request.args.get('nit'), desde, limite)), 200
    except Exception as e:  # noqa: BLE001 — nunca una lista vacía ante un error
        logger.exception('[CARTERA] listar retenciones falló')
        return jsonify({'error': f'No se pudieron leer las retenciones: {e}'}), 503


@cartera_bp.route('/retenciones/<int:rid>', methods=['GET'])
@exige_token_servicio(_TOKEN, _QUE)
def gestor_detalle(rid):
    from app.models.cartera import RetencionCartera
    from app.services import cartera_service as cs
    r = db.session.get(RetencionCartera, rid)
    if r is None:
        return jsonify({'error': f'Retención {rid} no encontrada'}), 404
    return jsonify({'retencion': cs.retencion_publica(r)}), 200


@cartera_bp.route('/retenciones/<int:rid>/autorizar', methods=['POST'])
@exige_token_servicio(_TOKEN, _QUE)
def gestor_autorizar(rid):
    from app.services import cartera_service as cs
    data = request.get_json(silent=True) or {}

    def _hacer():
        usuario = _usuario_del_gestor(data)
        if data.get('tope_valor') in (None, ''):
            raise ValueError('tope_valor es obligatorio')
        r = cs.autorizar(rid, usuario, data.get('motivo'), data.get('tope_valor'),
                         data.get('vence_en'), data.get('codigo_excepcion'), origen='GESTOR')
        return {'retencion': cs.retencion_publica(r)}, 200
    return _idempotente('autorizar', rid, _hacer)


@cartera_bp.route('/retenciones/<int:rid>/convertir-contado', methods=['POST'])
@exige_token_servicio(_TOKEN, _QUE)
def gestor_convertir(rid):
    from app.services import cartera_service as cs
    data = request.get_json(silent=True) or {}

    def _hacer():
        r = cs.convertir_a_contado(rid, _usuario_del_gestor(data), data.get('motivo'),
                                   origen='GESTOR')
        return {'retencion': cs.retencion_publica(r)}, 200
    return _idempotente('convertir-contado', rid, _hacer)


@cartera_bp.route('/retenciones/<int:rid>/reevaluar', methods=['POST'])
@exige_token_servicio(_TOKEN, _QUE)
def gestor_reevaluar(rid):
    from app.services import cartera_service as cs
    data = request.get_json(silent=True) or {}

    def _hacer():
        usuario = _usuario_del_gestor(data) if data.get('usuario') else None
        r = cs.reevaluar(rid, usuario, origen='GESTOR')
        return {'retencion': cs.retencion_publica(r)}, 200
    return _idempotente('reevaluar', rid, _hacer)


@cartera_bp.route('/habilitaciones', methods=['POST'])
@exige_token_servicio(_TOKEN, _QUE)
def gestor_habilitaciones():
    from app.services import cartera_service as cs
    data = request.get_json(silent=True) or {}

    def _hacer():
        usuario = _usuario_del_gestor(data)
        quien = cs._texto_usuario(usuario)
        clientes = data.get('clientes') if isinstance(data.get('clientes'), list) else [data]
        if not clientes:
            raise ValueError('clientes vacío')
        guardadas = []
        for c in clientes:
            if not isinstance(c, dict):
                raise ValueError('cada cliente es un objeto')
            f = cs.registrar_habilitacion(c, enviado_por=quien)
            guardadas.append({'nit': f.nit, 'sucursal': f.sucursal or None,
                              'as_of': f.as_of.isoformat() if f.as_of else None})
        db.session.commit()
        return {'guardadas': guardadas, 'n': len(guardadas)}, 200
    return _idempotente('habilitaciones', None, _hacer)


@cartera_bp.route('/salud', methods=['GET'])
@exige_token_servicio(_TOKEN, _QUE)
def gestor_salud():
    from app.services import cartera_service as cs
    try:
        return jsonify(cs.salud()), 200
    except Exception as e:  # noqa: BLE001
        logger.exception('[CARTERA] salud falló')
        return jsonify({'error': f'No se pudo calcular la salud de cartera: {e}'}), 503


# ═════════════════════════════════════════════════════════════════════════════
# Respaldo en el WMS (JWT)
# ═════════════════════════════════════════════════════════════════════════════

def _usuario_wms(u) -> dict:
    return {'username': u.email, 'nombre': u.nombre, 'email': u.email, 'rol': u.rol,
            'sistema': 'wms'}


@cartera_bp.route('/panel/retenciones', methods=['GET'])
@jwt_required()
def panel_listar():
    u = _ve_cartera()
    if not u:
        return jsonify({'error': 'Sin permiso para ver las retenciones de cartera'}), 403
    from app.models.cartera import EstadoRetencion
    from app.services import cartera_service as cs
    estado = (request.args.get('estado') or 'RETENIDO').strip().upper()
    if estado not in EstadoRetencion.TODOS:
        return jsonify({'error': 'estado inválido'}), 400
    try:
        datos = cs.listar(estado, request.args.get('nit'), None, 200)
    except Exception as e:  # noqa: BLE001
        logger.exception('[CARTERA] panel: listar falló')
        return jsonify({'error': f'No se pudieron leer las retenciones: {e}'}), 503
    datos['puede_autorizar'] = bool(_puede_autorizar_cartera())
    # El lote de paradas viejas es `_solo_admin`: el botón solo con él.
    datos['puede_autorizar_lote'] = bool(_solo_admin())
    datos['usuario_id'] = u.id
    return jsonify(datos), 200


@cartera_bp.route('/panel/retenciones/<int:rid>/autorizar', methods=['POST'])
@jwt_required()
def panel_autorizar(rid):
    u = _puede_autorizar_cartera()
    if not u:
        return jsonify({'error': 'Sin permiso para autorizar excepciones de cartera'}), 403
    from app.services import cartera_service as cs
    data = request.get_json(silent=True) or {}
    cuerpo, status = _responder(lambda: (
        {'retencion': cs.retencion_publica(cs.autorizar(
            rid, _usuario_wms(u), data.get('motivo'), data.get('tope_valor'),
            data.get('vence_en'), data.get('codigo_excepcion'),
            origen='WMS', usuario_id=u.id))}, 200))
    return jsonify(cuerpo), status


@cartera_bp.route('/panel/retenciones/<int:rid>/convertir-contado', methods=['POST'])
@jwt_required()
def panel_convertir(rid):
    u = _puede_autorizar_cartera()
    if not u:
        return jsonify({'error': 'Sin permiso para convertir un pedido a contado'}), 403
    from app.services import cartera_service as cs
    data = request.get_json(silent=True) or {}
    cuerpo, status = _responder(lambda: (
        {'retencion': cs.retencion_publica(cs.convertir_a_contado(
            rid, _usuario_wms(u), data.get('motivo'), origen='WMS', usuario_id=u.id))}, 200))
    return jsonify(cuerpo), status


@cartera_bp.route('/panel/retenciones/<int:rid>/reevaluar', methods=['POST'])
@jwt_required()
def panel_reevaluar(rid):
    u = _ve_cartera()
    if not u:
        return jsonify({'error': 'Sin permiso para re-evaluar retenciones de cartera'}), 403
    from app.services import cartera_service as cs
    cuerpo, status = _responder(lambda: (
        {'retencion': cs.retencion_publica(cs.reevaluar(
            rid, _usuario_wms(u), origen='WMS', usuario_id=u.id))}, 200))
    return jsonify(cuerpo), status


@cartera_bp.route('/panel/credito-lote', methods=['GET'])
@jwt_required()
def panel_credito_lote_ver():
    """Paradas «crédito no autorizado» confirmadas antes de la regla de
    contado (`CONTADO_DESPLIEGUE_FECHA`). Vista previa de lo que autoriza el
    POST: el mismo cálculo."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    from app.services import cartera_service as cs
    corte = cs.fecha_despliegue_contado()
    if corte is None:
        return jsonify({'error': 'CONTADO_DESPLIEGUE_FECHA no está configurada',
                        'paradas': []}), 409
    filas = cs.recaudos_anteriores_a_contado()
    return jsonify({'corte': corte.isoformat(), 'n': len(filas),
                    'paradas': [{'recaudo_id': r.id, 'ruta_id': r.ruta_id,
                                 'pedido': getattr(r.tarea, 'numero_pedido_siesa', None),
                                 'cliente': getattr(r.tarea, 'cliente', None),
                                 'forma_pago': r.forma_pago,
                                 'confirmada': r.fecha_confirmacion.isoformat()
                                 if r.fecha_confirmacion else None}
                                for r in filas]}), 200


@cartera_bp.route('/panel/credito-lote', methods=['POST'])
@jwt_required()
def panel_credito_lote():
    """Autoriza en lote, con UN motivo común («anterior a la regla de
    contado»), las paradas de la vista previa. Bitácora por parada."""
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo admin'}), 403
    from app.services import cartera_service as cs
    data = request.get_json(silent=True) or {}
    ids = data.get('recaudo_ids')
    if ids is not None and not isinstance(ids, list):
        return jsonify({'error': 'recaudo_ids debe ser una lista'}), 400
    cuerpo, status = _responder(lambda: (
        cs.autorizar_lote_anteriores(data.get('motivo'), admin.id, ids), 200))
    return jsonify(cuerpo), status
