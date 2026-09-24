"""
Analítica — 🩺 Salud del dato y patrones de la 📜 Bitácora (Fase 1, 2026-09-24).

La lógica vive en `app/services/analitica_salud.py`; acá solo se valida y se
responde. Solo gestión (`_es_gestion`), el mismo criterio que
`/api/auditoria/flujo` y `/api/analitica/bitacora`: estas respuestas traen
nombres de personas, motivos y números de pedido.
"""
from datetime import date, timedelta

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_gestion

analitica_salud_bp = Blueprint('analitica_salud', __name__)

#: Un rango más largo que esto es casi siempre un error de tipeo (2206 por
#: 2026) y una consulta que recorre años.
RANGO_MAXIMO_DIAS = 366
DIAS_POR_DEFECTO = 30


class FiltroInvalido(ValueError):
    """Un parámetro que no se puede interpretar. Se responde 400, nunca se ignora."""


def _fecha(nombre):
    valor = (request.args.get(nombre) or '').strip()
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise FiltroInvalido(f'{nombre}: fecha en formato YYYY-MM-DD')


def _entero(nombre):
    valor = (request.args.get(nombre) or '').strip()
    if not valor:
        return None
    if not valor.isdigit() or int(valor) <= 0:
        raise FiltroInvalido(f'{nombre}: debe ser un número entero positivo')
    return int(valor)


def filtros_comunes(exigir_almacen_existente=True):
    """`(desde, hasta, almacen_id)` del rango común de Analítica.

    Por defecto los últimos 30 días Bogotá. Basura → `FiltroInvalido`.
    """
    from app.utils.fecha import dia_operativo
    hoy = dia_operativo()
    desde, hasta = _fecha('desde'), _fecha('hasta')
    hasta = hasta or hoy
    desde = desde or (hasta - timedelta(days=DIAS_POR_DEFECTO - 1))
    if desde > hasta:
        raise FiltroInvalido('desde no puede ser posterior a hasta')
    if (hasta - desde).days + 1 > RANGO_MAXIMO_DIAS:
        raise FiltroInvalido(f'el rango no puede superar {RANGO_MAXIMO_DIAS} días')
    almacen_id = _entero('almacen_id')
    if almacen_id and exigir_almacen_existente:
        from app.extensions import db
        from app.models.almacen import Almacen
        if db.session.get(Almacen, almacen_id) is None:
            raise FiltroInvalido(f'almacen_id {almacen_id} no existe')
    return desde, hasta, almacen_id


@analitica_salud_bp.route('/salud', methods=['GET'])
@jwt_required()
def salud_del_dato():
    """¿Puedo confiar en los números de hoy? Ver `analitica_salud.salud_del_dato`."""
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede consultar la salud del dato'}), 403
    try:
        desde, hasta, almacen_id = filtros_comunes()
    except FiltroInvalido as e:
        return jsonify({'error': str(e)}), 400
    from app.services import analitica_salud as svc
    # 200 aunque el veredicto sea NO_CONFIABLE: es un reporte, no un health
    # check. Un 5xx haría que un monitor lo leyera como caída del servicio.
    return jsonify(svc.salud_del_dato(desde, hasta, almacen_id)), 200


@analitica_salud_bp.route('/bitacora/patrones', methods=['GET'])
@jwt_required()
def bitacora_patrones():
    """Por persona, acción, entidad y hora del día, con su `n`.

    Mismos filtros que `/api/analitica/bitacora` (`accion`, `entidad`,
    `usuario_id`, `almacen_id`) dentro del rango común `desde`/`hasta`.
    """
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede consultar la bitácora'}), 403
    from app.services.bitacora import ACCIONES
    try:
        # La bitácora no tiene FKs a propósito: un almacén ya borrado sigue
        # siendo un filtro válido.
        desde, hasta, almacen_id = filtros_comunes(exigir_almacen_existente=False)
        usuario_id = _entero('usuario_id')
    except FiltroInvalido as e:
        return jsonify({'error': str(e)}), 400
    accion = (request.args.get('accion') or '').strip().upper() or None
    if accion and accion not in ACCIONES:
        return jsonify({'error': f'accion desconocida: {accion}',
                        'vocabulario': list(ACCIONES)}), 400
    entidad = (request.args.get('entidad') or '').strip() or None
    if entidad and (len(entidad) > 40 or not entidad.replace('_', '').isalnum()):
        return jsonify({'error': 'entidad: nombre de entidad inválido'}), 400
    from app.services import analitica_salud as svc
    return jsonify(svc.patrones_bitacora(desde, hasta, almacen_id, accion, entidad,
                                         usuario_id)), 200
