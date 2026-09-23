"""Stock de productos separado por almacén.

`Producto.stock_total` suma las ubicaciones de TODOS los almacenes. En la
pantalla de Stock eso mezclaba el CD con las tiendas: el 2026-09-23, recién
cargado NB1 desde Siesa producción, PAPELSP9218 mostraba 446 cuando NB1 tenía
110 — los otros 336 eran de NC1 y NS1, con datos del ensayo. Un jefe de CD que
busca un SKU lee «hay mercancía» donde no la hay.

Se agrega en SQL, una sola consulta para toda la página del catálogo, y la
separación vendible/averiado sale de la política canónica de `picking_service`
— la misma que usan las propiedades del modelo, así que la suma por almacén
cuadra con el total del producto por construcción.
"""
from sqlalchemy import case, func

from app.extensions import db
from app.models.almacen import Almacen
from app.models.inventario import UbicacionProducto
from app.models.ubicacion import Ubicacion

#: Los campos de stock que expone `Producto.to_dict()` y que este módulo
#: calcula por almacén. Mismos nombres, así el frontend no distingue.
CAMPOS_STOCK = ('stock_total', 'stock_vendible', 'stock_averiado', 'stock_disponible')


def stock_por_almacen(producto_ids) -> dict:
    """`{producto_id: {almacen_id: {'almacen_id', 'almacen', <CAMPOS_STOCK>}}}`.

    Solo filas con `cantidad > 0`, igual que `Producto.stock_total`. Un
    producto sin stock en ningún almacén no aparece: quien lo lea decide qué
    mostrar, en vez de recibir ceros que no distinguen «no hay» de «no se
    consultó».
    """
    ids = list(producto_ids or [])
    if not ids:
        return {}

    from app.services.picking_service import (
        filtro_ubicacion_averias, filtro_ubicacion_vendible)

    cant = UbicacionProducto.cantidad
    libre = (cant - func.coalesce(UbicacionProducto.reservado, 0)
             - func.coalesce(UbicacionProducto.bloqueado, 0))

    filas = (db.session.query(
                UbicacionProducto.producto_id,
                Almacen.id,
                Almacen.codigo,
                func.sum(cant),
                func.sum(case((filtro_ubicacion_vendible(), cant), else_=0)),
                func.sum(case((filtro_ubicacion_averias(), cant), else_=0)),
                # `cantidad_disponible()` es max(0, …) por fila, no sobre la suma.
                func.sum(case((libre > 0, libre), else_=0)))
             .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
             .join(Almacen, Almacen.id == Ubicacion.almacen_id)
             .filter(UbicacionProducto.producto_id.in_(ids), cant > 0)
             .group_by(UbicacionProducto.producto_id, Almacen.id, Almacen.codigo)
             .all())

    salida = {}
    for prod_id, alm_id, alm_codigo, total, vendible, averiado, disponible in filas:
        salida.setdefault(prod_id, {})[alm_id] = {
            'almacen_id': alm_id,
            'almacen': alm_codigo,
            'stock_total': int(total or 0),
            'stock_vendible': int(vendible or 0),
            'stock_averiado': int(averiado or 0),
            'stock_disponible': int(disponible or 0),
        }
    return salida
