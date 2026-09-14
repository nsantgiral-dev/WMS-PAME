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
from app.utils.fecha import rango_dia_operativo_utc, dia_operativo_de


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

    # Agrupado en Python, no `func.date()` sobre la columna UTC cruda — un
    # despacho de las 7-11:59 p.m. Colombia cae en la fecha UTC del día
    # siguiente (Regla 5 del proyecto). Solo trae fecha_despachado/valor_factura,
    # no las tareas completas.
    por_dia: dict = {}
    filas = db.session.query(TareaPacking.fecha_despachado, TareaPacking.valor_factura).filter(*filtros).all()
    for fecha_desp, valor in filas:
        clave = dia_operativo_de(fecha_desp).isoformat()
        por_dia[clave] = por_dia.get(clave, 0) + float(valor or 0)

    return {
        'pedidos': int(pedidos or 0),
        'lineas': int(lineas or 0),
        'unidades': int(unidades or 0),
        'valor_total': float(valor_total or 0),
        'por_dia': por_dia,
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
