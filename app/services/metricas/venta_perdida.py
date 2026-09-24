"""
Métrica 5 — Venta perdida por agotados $.

Lee `EventoStockAgotado` directamente — no pasa por
`eventos_agotado_service` (DIP: el lector no se acopla al escritor, solo al
modelo/esquema compartido).

Los traslados se excluyen siempre: un traslado bloqueado por falta de stock
no es una venta perdida, es un movimiento interno frustrado. **Se excluyen por
complemento** (`TipoDocumento.es_venta`), no se incluye la venta por igualdad:
hasta el 2026-09-24 este archivo filtraba `== 'PEDIDO'`, y el flujo real escribe
`'PEDIDO_SIESA'` — la métrica daba ≈ 0 todos los días. Ver
`app/models/tipo_documento.py`.

**«Sin precio» no es $0** (Regla 0). `precio_venta_capturado = NULL` es un
evento cuyo producto no tenía precio al capturarse — hoy casi todos, porque
`Producto.precio_venta` no lo puebla ninguna sincronización. Esos eventos NO
suman al total (no se inventa el precio) y se declaran en `sin_precio`: con
`sin_precio.eventos > 0` el total es una **cota inferior**, no la venta perdida.

Evento hacia adelante (ya acordado con el usuario): un rango de fechas
anterior al despliegue de esta funcionalidad simplemente no tendrá eventos
— no es un bug, es la ausencia de histórico documentada en la migración.
"""
from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models.evento_stock_agotado import EventoStockAgotado
from app.models.tipo_documento import TipoDocumento
from app.utils.fecha import rango_dia_operativo_utc, dia_operativo_de


def filtros_venta_perdida(almacen_id, fecha_desde: date, fecha_hasta: date) -> tuple:
    """**La** definición de qué evento es venta perdida en un rango. La usan el
    total, el detalle del tablero y las fugas de la analítica
    (`analitica_fugas`): si cada uno escribiera su filtro, el día que alguien
    cambiara la exclusión de traslados en uno solo, dos pantallas darían dos
    ventas perdidas distintas del mismo día.

    `almacen_id=None` = todos los almacenes (la analítica lo pide así; el
    tablero siempre manda uno).
    """
    inicio_utc, fin_utc = rango_dia_operativo_utc(fecha_desde, fecha_hasta)
    filtros = [
        TipoDocumento.es_venta(EventoStockAgotado.tipo_documento),
        EventoStockAgotado.creado_en >= inicio_utc,
        EventoStockAgotado.creado_en < fin_utc,
    ]
    if almacen_id is not None:
        filtros.insert(0, EventoStockAgotado.almacen_id == almacen_id)
    return tuple(filtros)


def calcular_venta_perdida(almacen_id: int, fecha_desde: date, fecha_hasta: date) -> dict:
    filtros = filtros_venta_perdida(almacen_id, fecha_desde, fecha_hasta)

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
    sin_precio = {'eventos': 0, 'unidades': 0}
    eventos = 0
    filas = db.session.query(
        EventoStockAgotado.creado_en, EventoStockAgotado.cantidad_faltante,
        EventoStockAgotado.precio_venta_capturado,
    ).filter(*filtros).all()
    for creado_en, cantidad, precio in filas:
        eventos += 1
        if precio is None:
            sin_precio['eventos'] += 1
            sin_precio['unidades'] += int(cantidad or 0)
            continue
        clave = dia_operativo_de(creado_en).isoformat()
        por_dia[clave] = por_dia.get(clave, 0) + float(cantidad or 0) * float(precio)

    return {
        'venta_perdida_total': float(total or 0),
        # El total solo suma eventos CON precio. Si hay eventos sin precio, el
        # total es una cota inferior: se declara, no se rellena con cero.
        'eventos': eventos,
        'sin_precio': sin_precio,
        'total_es_cota_inferior': sin_precio['eventos'] > 0,
        'por_categoria': por_categoria,
        'por_dia': por_dia,
        'fecha_desde': fecha_desde.isoformat(),
        'fecha_hasta': fecha_hasta.isoformat(),
    }


def listar_venta_perdida_detalle(almacen_id: int, fecha_desde: date, fecha_hasta: date,
                                  page: int = 1, per_page: int = 50):
    q = EventoStockAgotado.query.filter(
        *filtros_venta_perdida(almacen_id, fecha_desde, fecha_hasta)
    ).order_by(EventoStockAgotado.creado_en.desc())
    return q.paginate(page=page, per_page=per_page, error_out=False)
