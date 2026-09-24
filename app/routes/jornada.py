"""
🕒 La jornada del conductor (Fase 0, 2026-09-24).

Dos lecturas. Quién las ve: `Roles.VISTA_FLOTA` — gestión más control de
flota. **El conductor no**: la jornada de sus compañeros es el registro de
conducta de otros, y el permiso no puede ser más ancho que el gesto (el mismo
criterio con el que `cierres-forzados` salió de `LECTURA_FLOTA`).

La lógica vive entera en `services/jornada_conductor.py`; acá solo se valida y
se responde. Un parámetro que no se entiende es 400, nunca se ignora.

    GET /api/jornada?dia=&conductor_id=     línea de tiempo + indicadores + señales
    GET /api/jornada/resumen?desde=&hasta=  por conductor, rango ≤ 92 días
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.usuario import Usuario
from app.routes._auth_helpers import Roles

jornada_bp = Blueprint('jornada', __name__)

#: Quién ve la jornada. Con nombre y no una lista suelta: el trinquete de este
#: módulo exige que sea exactamente `Roles.VISTA_FLOTA`.
ROLES_JORNADA = Roles.VISTA_FLOTA

_NO_PUEDE = ('La jornada de los conductores la ven gestión y control de flota. '
             'Es el registro de trabajo de otras personas.')


def _quien_ve():
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = db.session.get(Usuario, uid)
    return u if u is not None and u.activo and u.rol in ROLES_JORNADA else None


@jornada_bp.route('', methods=['GET'])
@jwt_required()
def jornada_del_dia():
    """Un conductor, un día Bogotá: eventos, huecos, cobertura y señales."""
    if _quien_ve() is None:
        return jsonify({'error': _NO_PUEDE}), 403
    from app.services import jornada_conductor as svc
    try:
        dia = svc.dia_de(request.args.get('dia'), 'dia', defecto=svc.dia_operativo())
        conductor_id = svc.entero_de(request.args.get('conductor_id'), 'conductor_id')
    except svc.FiltroInvalido as e:
        return jsonify({'error': str(e)}), 400
    d = svc.jornada(conductor_id, dia)
    if d is None:
        return jsonify({'error': f'No existe el conductor {conductor_id}'}), 404
    return jsonify(d), 200


@jornada_bp.route('/resumen', methods=['GET'])
@jwt_required()
def jornada_resumen():
    """Todos los conductores en un período (por defecto, los últimos 7 días)."""
    if _quien_ve() is None:
        return jsonify({'error': _NO_PUEDE}), 403
    from app.services import jornada_conductor as svc
    try:
        desde, hasta = svc.rango_de(request.args)
    except svc.FiltroInvalido as e:
        return jsonify({'error': str(e), 'maximo_dias': svc.MAX_DIAS_RANGO}), 400
    return jsonify(svc.resumen(desde, hasta)), 200
