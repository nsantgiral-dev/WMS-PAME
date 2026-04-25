"""Helpers de autorización compartidos entre blueprints."""
from flask_jwt_extended import get_jwt_identity
from app.models.usuario import Usuario


def _solo_admin():
    """Devuelve el usuario actual si es admin, o None."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.rol == 'admin' else None


def _es_admin_o_jefe():
    """Retorna el usuario si tiene rol admin o jefe_almacen, None en caso contrario."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.rol in ('admin', 'jefe_almacen') else None
