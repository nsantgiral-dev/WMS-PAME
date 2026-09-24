"""
Analítica — 💸 Fugas (Fase 1, 2026-09-24). Solo parsea y valida: la política
vive en `app/services/analitica_fugas.py`.

    GET /api/analitica/fugas                 las ocho fugas + «Fugas del período»
    GET /api/analitica/fugas/<clave_fuga>    una fuga con sus casos, paginados

Parámetros comunes: `almacen_id` (opcional), `desde`, `hasta` (YYYY-MM-DD,
día de Bogotá; por defecto los últimos 30 días). Basura → 400, nunca se ignora.
"""
from datetime import date, timedelta

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_gestion

analitica_fugas_bp = Blueprint('analitica_fugas', __name__)

#: Un rango más largo que esto no es una consulta de pantalla: carga todo el
#: histórico dos veces (período y anterior).
MAX_DIAS = 366
DIAS_POR_DEFECTO = 30


class FiltroInvalido(ValueError):
    pass


def parsear_filtros(args) -> tuple:
    """`(desde, hasta, almacen_id)` validados, o `FiltroInvalido`."""
    from app.models.almacen import Almacen
    from app.utils.fecha import dia_operativo

    def _fecha(nombre):
        crudo = (args.get(nombre) or '').strip()
        if not crudo:
            return None
        try:
            return date.fromisoformat(crudo)
        except ValueError:
            raise FiltroInvalido(f'{nombre} debe ser YYYY-MM-DD, llegó {crudo!r}')

    hasta = _fecha('hasta') or dia_operativo()
    desde = _fecha('desde') or (hasta - timedelta(days=DIAS_POR_DEFECTO - 1))
    if desde > hasta:
        raise FiltroInvalido('desde es posterior a hasta')
    if (hasta - desde).days + 1 > MAX_DIAS:
        raise FiltroInvalido(f'el rango no puede pasar de {MAX_DIAS} días')

    crudo = (args.get('almacen_id') or '').strip()
    almacen_id = None
    if crudo:
        try:
            almacen_id = int(crudo)
        except ValueError:
            raise FiltroInvalido(f'almacen_id debe ser un número, llegó {crudo!r}')
        if Almacen.query.get(almacen_id) is None:
            raise FiltroInvalido(f'almacen_id {almacen_id} no existe')
    return desde, hasta, almacen_id


def _entero(args, nombre, defecto):
    crudo = (args.get(nombre) or '').strip()
    if not crudo:
        return defecto
    try:
        v = int(crudo)
    except ValueError:
        raise FiltroInvalido(f'{nombre} debe ser un número, llegó {crudo!r}')
    if v < 1:
        raise FiltroInvalido(f'{nombre} debe ser ≥ 1')
    return v


@analitica_fugas_bp.route('/fugas', methods=['GET'])
@jwt_required()
def listar_fugas():
    """Las ocho fugas del rango con su total, tendencia y dónde pesan más."""
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede ver las fugas'}), 403
    try:
        desde, hasta, almacen_id = parsear_filtros(request.args)
    except FiltroInvalido as e:
        return jsonify({'error': str(e)}), 400
    from app.services.analitica_fugas import calcular_fugas
    return jsonify(calcular_fugas(desde, hasta, almacen_id)), 200


@analitica_fugas_bp.route('/fugas/<clave_fuga>', methods=['GET'])
@jwt_required()
def detalle_de_fuga(clave_fuga):
    """Una fuga con sus casos, paginados (`page`, `per_page` ≤ 200)."""
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede ver las fugas'}), 403
    from app.services.analitica_fugas import CLAVES, detalle_fuga
    if clave_fuga not in CLAVES:
        return jsonify({'error': f'fuga desconocida: {clave_fuga}',
                        'claves': list(CLAVES)}), 404
    try:
        desde, hasta, almacen_id = parsear_filtros(request.args)
        page = _entero(request.args, 'page', 1)
        per_page = _entero(request.args, 'per_page', 50)
    except FiltroInvalido as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(detalle_fuga(clave_fuga, desde, hasta, almacen_id,
                                page=page, per_page=per_page)), 200
