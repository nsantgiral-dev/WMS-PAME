"""
Los invariantes de frontera, corridos contra los datos que ya hay.

Mismo código que el arnés de `tests/flujo`. Allá se evalúa sobre un pedido
sintético recorrido con los servicios reales; acá sobre la operación. Escribir
la regla dos veces sería la divergencia que la Regla 0 prohíbe: el test pasaría
y la auditoría diría otra cosa, y nadie sabría cuál creer.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_gestion
from app.services import auditoria as _aud

logger = logging.getLogger(__name__)

auditoria_bp = Blueprint('auditoria', __name__)


@auditoria_bp.route('/flujo', methods=['GET'])
@jwt_required()
def auditar_flujo():
    """`?flujo=venta` — sin parámetro corre todos.

    Solo gestión: los hallazgos traen números de pedido, clientes y montos.
    """
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede auditar el flujo'}), 403

    flujo = (request.args.get('flujo') or '').strip() or None
    if flujo and flujo not in _aud.flujos():
        return jsonify({'error': f'flujo desconocido: {flujo}',
                        'flujos': _aud.flujos()}), 400

    reporte = _aud.auditar(flujo)
    reporte['flujos_disponibles'] = _aud.flujos()
    reporte['nota'] = (
        'Un invariante vigila una frontera entre etapas: lo que entró, '
        '¿es lo que salió de la anterior? `bloqueantes > 0` significa que hay '
        'datos sobre los que alguien va a operar y están mal. '
        'Los mismos invariantes corren en CI sobre un pedido sintético '
        '(tests/flujo) — si acá aparece algo que allá no, es un problema de '
        'datos, no de código.')
    # 200 aunque haya hallazgos: es un reporte, no un health check. Un 5xx acá
    # haría que un monitor lo tratara como caída del servicio.
    #
    # No hay un endpoint aparte para listar el catálogo: este reporte ya trae
    # TODOS los invariantes con su código, frontera, severidad y consecuencia,
    # rotos o no. Un segundo endpoint con la misma información sería una copia
    # que algún día diverge — y encima nació sin consumidor, que es como se
    # descubrió.
    return jsonify(reporte), 200
