"""Helpers de autorización compartidos entre blueprints."""
from flask_jwt_extended import get_jwt_identity
from app.models.usuario import Usuario


class Roles:
    ADMIN          = 'admin'
    SUPERVISOR     = 'supervisor'
    JEFE_ALMACEN   = 'jefe_almacen'
    GERENTE        = 'gerente'
    OPERARIO       = 'operario'
    EMPACADOR      = 'empacador'
    RECEPCIONISTA  = 'recepcionista'
    TIENDA         = 'tienda'
    CONDUCTOR      = 'conductor'

    # Grupos reutilizables
    GESTION        = (ADMIN, SUPERVISOR, JEFE_ALMACEN, GERENTE)
    ALMACEN        = (ADMIN, JEFE_ALMACEN)
    DESPACHO       = (ADMIN, SUPERVISOR, GERENTE, JEFE_ALMACEN)
    SUPERVISION    = (ADMIN, SUPERVISOR, JEFE_ALMACEN)
    PACKING_ROLES  = (ADMIN, SUPERVISOR, EMPACADOR)
    RECEPCION_ROLES = (ADMIN, JEFE_ALMACEN, RECEPCIONISTA)
    LEAD           = (ADMIN, SUPERVISOR)


def _solo_admin():
    """Devuelve el usuario actual si es admin, o None."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.rol == Roles.ADMIN else None


def _es_admin_o_jefe():
    """Retorna el usuario si tiene rol admin o jefe_almacen, None en caso contrario."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.rol in Roles.ALMACEN else None


def _es_gestion():
    """Retorna el usuario si tiene rol de gestión (admin/supervisor/jefe_almacen/gerente)."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.rol in Roles.GESTION else None


def _get_uid():
    """Convierte get_jwt_identity() a int de forma segura. Retorna None si falla."""
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
