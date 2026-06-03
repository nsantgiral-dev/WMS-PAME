"""Scoping centralizado de tareas por usuario — S + I de SOLID.

Un único lugar donde se decide qué tareas ve cada usuario.
Las rutas nunca filtran directamente; siempre pasan por aquí.
"""
from app.routes._auth_helpers import Roles


def scope_picking(usuario, query):
    """
    Aplica el filtro de bodega/tipo al query de TareaPicking según el rol del usuario.
      - Admin / gestión: ve todo
      - Operario NB1:    filtra por almacen_id
      - Tienda:          filtra por bodega_origen_siesa + solo TRASLADO (nunca PD ni Conteo)
    """
    if not usuario:
        return query.filter_by(id=None)  # sin usuario → cero resultados
    if usuario.rol in Roles.GESTION:
        return query
    if usuario.rol == Roles.TIENDA:
        from app.models.picking import TareaPicking
        return query.filter(
            TareaPicking.bodega_origen_siesa == usuario.bodega_siesa_id,
            TareaPicking.tipo_documento == 'TRASLADO',
        )
    # Operario / empacador / demás roles de almacén NB1
    if usuario.almacen_id:
        return query.filter_by(almacen_id=usuario.almacen_id)
    return query


def scope_packing(usuario, query):
    """
    Aplica el filtro de bodega/tipo al query de TareaPacking según el rol del usuario.
      - Admin / gestión: ve todo
      - Empacador NB1:   filtra por almacen_id  (ve PD + ST)
      - Tienda:          filtra por bodega_origen_siesa + solo TRASLADO
    """
    if not usuario:
        return query.filter_by(id=None)
    if usuario.rol in Roles.GESTION:
        return query
    if usuario.rol == Roles.TIENDA:
        from app.models.packing import TareaPacking
        return query.filter(
            TareaPacking.bodega_origen_siesa == usuario.bodega_siesa_id,
            TareaPacking.tipo_documento == 'TRASLADO',
        )
    # Empacador / operario NB1
    if usuario.almacen_id:
        return query.filter_by(almacen_id=usuario.almacen_id)
    return query
