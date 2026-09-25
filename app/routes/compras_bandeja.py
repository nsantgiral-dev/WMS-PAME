"""
Compras — la pantalla del comprador (2026-09-25). Solo LECTURA.

GET /api/compras/bandeja               — 🛒 qué pedir, a quién, a qué precio
GET /api/compras/bandeja/confianza     — franja: ¿puedo decidir con esto?
GET /api/compras/bandeja/sku           — ¿por qué esta referencia está (o no)?
GET /api/compras/bandeja/contenedor    — 🚢 propuesta o por qué no se puede
GET /api/compras/bandeja/temporada     — 🎒 tener − hay − viene = pedir
GET /api/compras/bandeja/lo-pedido     — 📦 OCs abiertas, llegadas, deriva

Todo `_es_compras()` (admin, jefe de almacén, gerente, compras): es la
pantalla de quien firma la compra. El servicio (`compras_bandeja`) solo
compone lo que ya calcula el motor.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_compras

logger = logging.getLogger(__name__)
compras_bandeja_bp = Blueprint('compras_bandeja', __name__)

_SIN_PERMISO = 'La pantalla de compras es de admin, jefe de almacén, gerente o compras'


def _responder(nombre, fn):
    from app.routes._auth_helpers import Roles
    from app.services import compras_bandeja as cb
    u = _es_compras()
    try:
        return jsonify(cb.para_quien_mira(fn(), bool(u) and u.rol == Roles.ADMIN)), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:                                    # noqa: BLE001
        logger.exception('[BANDEJA] %s', nombre)
        return jsonify({'error': f'No se pudo armar {nombre}: {e}'}), 500


@compras_bandeja_bp.route('', methods=['GET'])
@jwt_required()
def bandeja():
    if not _es_compras():
        return jsonify({'error': _SIN_PERMISO}), 403
    from app.services import compras_bandeja as cb
    return _responder('la bandeja', cb.bandeja)


@compras_bandeja_bp.route('/confianza', methods=['GET'])
@jwt_required()
def confianza():
    if not _es_compras():
        return jsonify({'error': _SIN_PERMISO}), 403
    from app.services import compras_bandeja as cb
    return _responder('la franja de confianza', cb.confianza)


@compras_bandeja_bp.route('/sku', methods=['GET'])
@jwt_required()
def explicar_sku():
    if not _es_compras():
        return jsonify({'error': _SIN_PERMISO}), 403
    ref = (request.args.get('referencia') or '').strip()
    if not ref:
        return jsonify({'error': 'referencia es requerida'}), 400
    from app.services import compras_bandeja as cb
    return _responder('la explicación', lambda: cb.explicar_sku(ref))


@compras_bandeja_bp.route('/contenedor', methods=['GET'])
@jwt_required()
def contenedor():
    if not _es_compras():
        return jsonify({'error': _SIN_PERMISO}), 403
    tipo = request.args.get('tipo', '40STD')
    from app.services import compras_bandeja as cb
    return _responder('el contenedor', lambda: cb.contenedor(tipo))


@compras_bandeja_bp.route('/temporada', methods=['GET'])
@jwt_required()
def temporada():
    if not _es_compras():
        return jsonify({'error': _SIN_PERMISO}), 403
    from app.services import compras_bandeja as cb
    return _responder('la temporada', cb.temporada)


@compras_bandeja_bp.route('/lo-pedido', methods=['GET'])
@jwt_required()
def lo_pedido():
    if not _es_compras():
        return jsonify({'error': _SIN_PERMISO}), 403
    from app.services import compras_bandeja as cb
    return _responder('lo pedido', cb.lo_pedido)
