"""Presencia y asignación de trabajo (m051asignacion, 2026-09-27).

Las rutas solo parsean: la política vive en `app/services/presencia.py` (quién
está) y `app/services/asignacion.py` (a quién se le puede dar qué).
"""
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.routes._auth_helpers import Roles

logger = logging.getLogger(__name__)
asignacion_bp = Blueprint('asignacion', __name__)


def _lider():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None, (jsonify({'error': 'Token inválido'}), 401)
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return None, (jsonify({'error': 'Solo supervisión gestiona la presencia del equipo'}), 403)
    return u, None


def _almacen_id():
    crudo = (request.args.get('almacen_id') or '').strip()
    if not crudo:
        return None
    return int(crudo)


@asignacion_bp.route('/equipo', methods=['GET'])
@jwt_required()
def equipo():
    """Quién está, qué tiene cada uno y qué quedó por decidir. Query: `almacen_id`."""
    lider, error = _lider()
    if error:
        return error
    try:
        almacen_id = _almacen_id()
    except ValueError:
        return jsonify({'error': 'almacen_id inválido'}), 400
    from app.services import asignacion
    return jsonify(asignacion.equipo(almacen_id)), 200


@asignacion_bp.route('/candidatos', methods=['GET'])
@jwt_required()
def candidatos():
    """Destinatarios posibles de un trabajo: `disponibles` (pueden y están) y
    `no_disponibles` (podrían, con el motivo). Query: `tipo`, `almacen_id`."""
    lider, error = _lider()
    if error:
        return error
    from app.services import asignacion
    tipo = (request.args.get('tipo') or '').strip().upper()
    if tipo not in asignacion.TIPOS:
        return jsonify({'error': f'tipo inválido: {tipo!r} (válidos: {", ".join(asignacion.TIPOS)})'}), 400
    try:
        almacen_id = _almacen_id()
    except ValueError:
        return jsonify({'error': 'almacen_id inválido'}), 400
    bodega = (request.args.get('bodega') or '').strip().upper() or None
    return jsonify(asignacion.candidatos(tipo, almacen_id=almacen_id, bodega=bodega)), 200


@asignacion_bp.route('/ausencias', methods=['POST'])
@jwt_required()
def declarar_ausencia():
    """Body: `{usuario_id, motivo, desde?, regreso?, nota?}` (fechas AAAA-MM-DD).
    Si cubre hoy, lo que la persona tenía asignado vuelve a la cola."""
    lider, error = _lider()
    if error:
        return error
    data = request.get_json() or {}
    from app.services import presencia
    try:
        r = presencia.declarar_ausencia(
            int(data.get('usuario_id') or 0), data.get('motivo'), por_id=lider.id,
            desde=data.get('desde'), regreso=data.get('regreso'), nota=data.get('nota'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except (TypeError, ValueError) as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(r), 201


@asignacion_bp.route('/ausencias/<int:ausencia_id>/anular', methods=['POST'])
@jwt_required()
def anular_ausencia(ausencia_id):
    """La persona volvió antes, o la ausencia se registró por error."""
    lider, error = _lider()
    if error:
        return error
    from app.services import presencia
    try:
        r = presencia.anular_ausencia(ausencia_id, por_id=lider.id,
                                      motivo=(request.get_json() or {}).get('motivo'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    return jsonify(r), 200
