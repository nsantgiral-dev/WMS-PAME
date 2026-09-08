"""
Métrica 1 — Pedidos despachados desde una bodega.

Pura en el sentido que importa para el tablero: recibe fechas ya resueltas
(no lee `request`, no sabe de Flask), y es el mismo criterio de cierre que
usa el resto del WMS para "el pedido salió" — `TareaPacking.estado ==
'DESPACHADO'` (no `CARGADO`, que es un estado de `Bulto`/muelle, entidad
distinta; ver `closing/pedido_closer.py` y `closing/traslado_closer.py`,
los dos únicos sitios que asignan ese estado).

`valor_factura` es el valor neto de la FE de esa tarea de packing — ya
prorrateado por tarea cuando el pedido se despachó en varias tandas, no el
valor del pedido completo (ver `TareaPacking.valor_factura`, comentario del
modelo).
"""
from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.utils.fecha import rango_dia_operativo_utc


def calcular_pedidos_despachados(almacen_id: int, fecha_desde: date, fecha_hasta: date) -> dict:
    inicio_utc, fin_utc = rango_dia_operativo_utc(fecha_desde, fecha_hasta)
    filtros = (
        TareaPacking.almacen_id == almacen_id,
        TareaPacking.estado == 'DESPACHADO',
        TareaPacking.fecha_despachado >= inicio_utc,
        TareaPacking.fecha_despachado < fin_utc,
    )

    pedidos, valor_total = db.session.query(
        func.count(TareaPacking.id),
        func.coalesce(func.sum(TareaPacking.valor_factura), 0),
    ).filter(*filtros).one()

    lineas, unidades = db.session.query(
        func.count(ItemPacking.id),
        func.coalesce(func.sum(ItemPacking.cantidad_real), 0),
    ).join(TareaPacking, ItemPacking.tarea_id == TareaPacking.id).filter(*filtros).one()

    return {
        'pedidos': int(pedidos or 0),
        'lineas': int(lineas or 0),
        'unidades': int(unidades or 0),
        'valor_total': float(valor_total or 0),
        'fecha_desde': fecha_desde.isoformat(),
        'fecha_hasta': fecha_hasta.isoformat(),
    }


def listar_pedidos_despachados_detalle(almacen_id: int, fecha_desde: date, fecha_hasta: date,
                                        page: int = 1, per_page: int = 50):
    inicio_utc, fin_utc = rango_dia_operativo_utc(fecha_desde, fecha_hasta)
    q = TareaPacking.query.filter(
        TareaPacking.almacen_id == almacen_id,
        TareaPacking.estado == 'DESPACHADO',
        TareaPacking.fecha_despachado >= inicio_utc,
        TareaPacking.fecha_despachado < fin_utc,
    ).order_by(TareaPacking.fecha_despachado.desc())
    return q.paginate(page=page, per_page=per_page, error_out=False)
