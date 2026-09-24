"""
`GET /flota/bandeja` — la pantalla de entrada del encargado de flota.

Hoy · Pendientes · Señales en una sola respuesta, para que la pantalla haga UN
viaje y las tres pestañas hablen del mismo instante. La lectura y las políticas
viven en `flota/adaptadores/bandeja.py` y `flota/dominio/senales.py`; acá solo
se traduce HTTP y se serializa.

**Solo lectura** y `VISTA_FLOTA` (gestión + control de flota): el conductor no
ve la flota entera, y las señales hablan de turnos ajenos.

`puede_decidir` viaja en la respuesta y sale de `DECIDE_FLOTA`, la misma tupla
que gatea cerrar, descartar y aplazar un daño: la pantalla esconde los botones
que el servidor va a negar sin tener que copiar la lista de roles.
"""
from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import Roles
from flota.api._permisos import DECIDE_FLOTA, _usuario, exige
from flota.api._tiempo import iso_utc

bandeja_bp = Blueprint('flota_bandeja', __name__)


def _json(valor):
    """Serializa la bandeja. Una sola regla por tipo, en un solo sitio.

    Las horas salen con su zona (`iso_utc`), los `Decimal` como texto — un
    número de galones que pasa por `float` pierde decimales y la evidencia de
    una señal tiene que poder recalcularse a mano.
    """
    if isinstance(valor, dict):
        return {k: _json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_json(v) for v in valor]
    if isinstance(valor, datetime):
        return iso_utc(valor)
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return str(valor)
    return valor


@bandeja_bp.route('/bandeja', methods=['GET'])
@jwt_required()
@exige(Roles.VISTA_FLOTA, 'ver la bandeja de la flota')
def bandeja():
    """Hoy, pendientes y señales. `?almacen_id=` opcional."""
    from flota.adaptadores.bandeja import BandejaNoDisponible, armar_bandeja

    crudo = request.args.get('almacen_id')
    almacen_id = None
    if crudo not in (None, ''):
        try:
            almacen_id = int(crudo)
        except ValueError:
            return jsonify({'error': f'almacen_id ilegible: {crudo!r}'}), 400
        from app.extensions import db
        from app.models.almacen import Almacen
        if db.session.get(Almacen, almacen_id) is None:
            return jsonify({'error': f'no existe el almacén {almacen_id}'}), 400

    try:
        datos = armar_bandeja(almacen_id=almacen_id)
    except BandejaNoDisponible as e:
        # 503 y no una bandeja vacía: sin la tabla de daños, «sin pendientes»
        # sería mentira.
        return jsonify({'error': 'la bandeja no se puede armar',
                        'detalle': str(e)}), 503

    u = _usuario()
    datos['puede_decidir'] = bool(u and u.rol in DECIDE_FLOTA)
    datos['tu_rol'] = u.rol if u else None
    return jsonify(_json(datos)), 200


__all__ = ['bandeja_bp']
