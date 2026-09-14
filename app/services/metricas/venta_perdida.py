"""
Métrica 5 — Venta perdida por agotados $.

Lee `EventoStockAgotado` directamente — no pasa por
`eventos_agotado_service` (DIP: el lector no se acopla al escritor, solo al
modelo/esquema compartido).

`tipo_documento == 'TRASLADO'` se excluye siempre: un traslado bloqueado por
falta de stock no es una venta perdida, es un movimiento interno frustrado.

Evento hacia adelante (ya acordado con el usuario): un rango de fechas
anterior al despliegue de esta funcionalidad simplemente no tendrá eventos
— no es un bug, es la ausencia de histórico documentada en la migración.
"""
from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models.evento_stock_agotado import EventoStockAgotado
from app.utils.fecha import rango_dia_operativo_utc, dia_operativo_de


def calcular_venta_perdida(almacen_id: int, fecha_desde: date, fecha_hasta: date) -> dict:
    inicio_utc, fin_utc = rango_dia_operativo_utc(fecha_desde, fecha_hasta)
    filtros = (
        EventoStockAgotado.almacen_id == almacen_id,
        EventoStockAgotado.tipo_documento == 'PEDIDO',
        EventoStockAgotado.creado_en >= inicio_utc,
        EventoStockAgotado.creado_en < fin_utc,
    )

    monto = func.coalesce(func.sum(
        EventoStockAgotado.cantidad_faltante * EventoStockAgotado.precio_venta_capturado
    ), 0)

    total = db.session.query(monto).filter(*filtros).scalar()

    por_categoria_rows = db.session.query(
        EventoStockAgotado.categoria_producto, monto
    ).filter(*filtros).group_by(EventoStockAgotado.categoria_producto).all()
    por_categoria = {(cat or 'Sin categoría'): float(v or 0) for cat, v in por_categoria_rows}

    # Agrupado en Python con dia_operativo_de(), no `func.date()` sobre la
    # columna UTC cruda — un evento de las 7-11:59 p.m. Colombia cae en la
    # fecha UTC del día siguiente (Regla 5 del proyecto).
    por_dia: dict = {}
    filas = db.session.query(
        EventoStockAgotado.creado_en, EventoStockAgotado.cantidad_faltante,
        EventoStockAgotado.precio_venta_capturado,
    ).filter(*filtros).all()
    for creado_en, cantidad, precio in filas:
        clave = dia_operativo_de(creado_en).isoformat()
        por_dia[clave] = por_dia.get(clave, 0) + float(cantidad or 0) * float(precio or 0)

    return {
        'venta_perdida_total': float(total or 0),
        'por_categoria': por_categoria,
        'por_dia': por_dia,
        'fecha_desde': fecha_desde.isoformat(),
        'fecha_hasta': fecha_hasta.isoformat(),
    }


def listar_venta_perdida_detalle(almacen_id: int, fecha_desde: date, fecha_hasta: date,
                                  page: int = 1, per_page: int = 50):
    inicio_utc, fin_utc = rango_dia_operativo_utc(fecha_desde, fecha_hasta)
    q = EventoStockAgotado.query.filter(
        EventoStockAgotado.almacen_id == almacen_id,
        EventoStockAgotado.tipo_documento == 'PEDIDO',
        EventoStockAgotado.creado_en >= inicio_utc,
        EventoStockAgotado.creado_en < fin_utc,
    ).order_by(EventoStockAgotado.creado_en.desc())
    return q.paginate(page=page, per_page=per_page, error_out=False)
