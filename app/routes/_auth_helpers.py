"""Helpers de autorización compartidos entre blueprints."""
from flask_jwt_extended import get_jwt_identity
from app.models.usuario import Usuario


def _solo_admin():
    """Devuelve el usuario actual si es admin, o None."""
    uid = get_jwt_identity()
    u = Usuario.query.get(int(uid))
    return u if u and u.rol == 'admin' else None
