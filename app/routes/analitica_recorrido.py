"""
Analítica — 🧭 Recorrido del pedido (Fase 1, 2026-09-24).

Tres lecturas, todas de gestión (`_es_gestion`, el mismo criterio que
`/api/auditoria/flujo` y `/api/analitica/bitacora`: traen valores, clientes y
quién hizo qué). La lógica vive entera en `services/analitica_recorrido.py`;
acá solo se valida y se responde. Un parámetro que no se entiende es 400,
nunca se ignora.

    GET /api/analitica/recorrido                         embudo + métrica guía
    GET /api/analitica/recorrido/etapa/<etapa>           los pedidos de una etapa
    GET /api/analitica/recorrido/pedido/<pedido_clave>   la línea de tiempo
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_gestion

analitica_recorrido_bp = Blueprint('analitica_recorrido', __name__)

_NO_GESTION = 'Solo gestión puede consultar la analítica'


def _entero(nombre, defecto, minimo, maximo):
    v = (request.args.get(nombre) or '').strip()
    if not v:
        return defecto
    if not v.isdigit():
        from app.services.analitica_recorrido import FiltroInvalido
        raise FiltroInvalido(f'{nombre} debe ser un número (llegó {v!r})')
    return min(max(int(v), minimo), maximo)


@analitica_recorrido_bp.route('/recorrido', methods=['GET'])
@jwt_required()
def recorrido():
    """Embudo aprobado → liquidado, ciclo de caja y % del valor sin fuga."""
    if not _es_gestion():
        return jsonify({'error': _NO_GESTION}), 403
    from app.services import analitica_recorrido as svc
    try:
        filtros = svc.filtros_de(request.args)
    except svc.FiltroInvalido as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(svc.recorrido(filtros)), 200


@analitica_recorrido_bp.route('/recorrido/etapa/<etapa>', methods=['GET'])
@jwt_required()
def recorrido_etapa(etapa):
    """Los pedidos detrás de una barra: `vista` = en | llegaron | fuga."""
    if not _es_gestion():
        return jsonify({'error': _NO_GESTION}), 403
    from app.services import analitica_recorrido as svc
    try:
        filtros = svc.filtros_de(request.args)
        vista = (request.args.get('vista') or 'en').strip()
        pagina = _entero('page', 1, 1, 100000)
        por_pagina = _entero('per_page', 50, 1, 200)
        return jsonify(svc.pedidos_de_etapa(etapa, filtros, vista=vista, pagina=pagina,
                                            por_pagina=por_pagina)), 200
    except svc.FiltroInvalido as e:
        return jsonify({'error': str(e), 'etapas': list(svc.ETAPAS),
                        'vistas': list(svc.VISTAS_ETAPA)}), 400


@analitica_recorrido_bp.route('/recorrido/pedido/<pedido_clave>', methods=['GET'])
@jwt_required()
def recorrido_pedido(pedido_clave):
    """Todo lo que el WMS sabe de un pedido, en orden."""
    if not _es_gestion():
        return jsonify({'error': _NO_GESTION}), 403
    from app.services import analitica_recorrido as svc
    d = svc.linea_de_tiempo(pedido_clave)
    if d is None:
        return jsonify({'error': f'El WMS no conoce el pedido {pedido_clave}'}), 404
    return jsonify(d), 200
