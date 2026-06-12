"""Scoping centralizado de tareas por usuario — S + I de SOLID.

Un único lugar donde se decide qué tareas ve cada usuario.
Las rutas nunca filtran directamente; siempre pasan por aquí.

Roles de tienda (TIENDA, PICKER_TRASLADO, PACKER_TRASLADO):
  - Solo ven tareas tipo TRASLADO
  - Filtradas a su propia bodega_siesa_id
  - Nunca ven PD ni conteos cíclicos
"""
from app.routes._auth_helpers import Roles

# Roles que son de tienda: scoping exclusivo a TRASLADO por bodega_siesa_id
_ROLES_TIENDA = (Roles.TIENDA, Roles.PICKER_TRASLADO, Roles.PACKER_TRASLADO)


def scope_picking(usuario, query):
    """
    Aplica el filtro de bodega/tipo al query de TareaPicking según el rol del usuario.
      - Admin / gestión:            ve todo
      - Operario / empacador NB1:   filtra por almacen_id (ve PD + ST)
      - Tienda / picker_traslado
        / packer_traslado:          filtra por bodega_siesa_id + solo TRASLADO
    """
    if not usuario:
        return query.filter_by(id=None)
    if usuario.rol in Roles.GESTION:
        return query
    if usuario.rol in _ROLES_TIENDA:
        from app.models.picking import TareaPicking
        return query.filter(
            TareaPicking.bodega_origen_siesa == usuario.bodega_siesa_id,
            TareaPicking.tipo_documento == 'TRASLADO',
        )
    # Operario / empacador NB1
    if usuario.almacen_id:
        return query.filter_by(almacen_id=usuario.almacen_id)
    return query


def scope_packing(usuario, query):
    """
    Aplica el filtro de bodega/tipo al query de TareaPacking según el rol del usuario.
      - Admin / gestión:            ve todo
      - Empacador NB1:              filtra por almacen_id (PD) + bodega_origen_siesa (ST)
      - Tienda / picker_traslado
        / packer_traslado:          filtra por bodega_siesa_id + solo TRASLADO
    """
    if not usuario:
        return query.filter_by(id=None)
    if usuario.rol in Roles.GESTION:
        return query
    if usuario.rol in _ROLES_TIENDA:
        from app.models.packing import TareaPacking
        return query.filter(
            TareaPacking.bodega_origen_siesa == usuario.bodega_siesa_id,
            TareaPacking.tipo_documento == 'TRASLADO',
        )
    # Empacador / operario NB1:
    #   - PEDIDO: filtrar por almacen_id (igual que siempre)
    #   - TRASLADO: mostrar TODOS — el packing de traslados siempre es en la bodega origen (NB1)
    #     y la TareaPacking puede tener un almacen_id distinto al del empacador si el almacén
    #     virtual de picking (creado por _crear_picking_tienda) tiene un id diferente al almacén
    #     principal (id=1) que usan los empacadores. Incluir todos los TRASLADO es seguro porque
    #     las tiendas quedan excluidas por la rama _ROLES_TIENDA anterior.
    if usuario.almacen_id:
        from app.models.packing import TareaPacking
        from sqlalchemy import or_
        return query.filter(
            or_(
                TareaPacking.almacen_id == usuario.almacen_id,
                TareaPacking.tipo_documento == 'TRASLADO',
            )
        )
    return query
