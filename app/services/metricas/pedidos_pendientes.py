"""
Métrica 3 — Pedidos dejados de despachar (fill rate + motivo).

`PedidoSiesa.fecha_entrega` es `String`, no `Date` — verificado contra
producción (2026-09-08): formato ISO `YYYY-MM-DDTHH:MM:SS`, sin timezone,
215/215 filas activas con esa misma longitud (19 chars). Se parsea
defensivamente; una fila que no calce ese formato se excluye del rango en
vez de reventar el tablero completo (Regla 0 del proyecto: ante dato
inesperado, declararlo — acá, dejarlo fuera del cálculo, no forzarlo).

`estado_siesa` (Integer): los códigos de anulación (-1, 5, 9) ya están
documentados y en uso real en `packing_service.py` (pre-check de cierre).
Verificado contra producción: hoy el 100% de los pedidos activos tiene
`estado_siesa=3` — no hay evidencia todavía de qué código exacto usa Siesa
para "3=comprometido" vs otros estados intermedios, así que solo se excluyen
los códigos YA confirmados como anulación; un `estado_siesa` nulo o
desconocido se conserva (más conservador: no descartar demanda real por
falta de dato, mismo criterio que Regla 0).

El motivo (métrica 3) reclasifica `TareaPicking.motivo_bloqueo` — no existe
FK real entre PedidoSiesa y TareaPicking, la correlación es por igualdad de
string (`TareaPicking.referencia_documento == PedidoSiesa.numero_pedido`),
igual que en el resto del WMS.
"""
from datetime import date, datetime

from app.models.almacen import Almacen
from app.models.pedido_siesa import PedidoSiesa
from app.models.picking import TareaPicking

ESTADOS_SIESA_ANULADO = (-1, 5, 9)

MOTIVO_MAP = {
    'FALTANTE': 'Falta de inventario',
    'BACKORDER_SIESA': 'Falta de inventario',
    'UBICACION_VACIA': 'Falta de inventario',
    'MERCANCIA_AVERIADA': 'Proceso/calidad',
    'PRODUCTO_INCORRECTO': 'Proceso/calidad',
}


def mapear_motivo(motivo_bloqueo: str | None) -> str:
    """Pura, sin DB — testeable sin fixtures de base de datos."""
    return MOTIVO_MAP.get(motivo_bloqueo, 'Sin clasificar')


def _parse_fecha_entrega(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except (ValueError, TypeError):
        return None


def _lineas_en_rango(almacen_id: int, fecha_desde: date, fecha_hasta: date) -> list[PedidoSiesa]:
    almacen = Almacen.query.get(almacen_id)
    if not almacen:
        raise ValueError(f'Almacén {almacen_id} no encontrado')

    candidatos = PedidoSiesa.query.filter(PedidoSiesa.bodega == almacen.bodega_siesa_id).all()
    resultado = []
    for p in candidatos:
        if p.estado_siesa in ESTADOS_SIESA_ANULADO:
            continue
        fecha_ent = _parse_fecha_entrega(p.fecha_entrega)
        if fecha_ent is None or not (fecha_desde <= fecha_ent <= fecha_hasta):
            continue
        resultado.append(p)
    return resultado


def _motivos_por_pedido_producto(numeros_pedido: set) -> dict:
    if not numeros_pedido:
        return {}
    tareas = TareaPicking.query.filter(
        TareaPicking.referencia_documento.in_(numeros_pedido),
        TareaPicking.estado == 'BLOQUEADO',
    ).all()
    mapa = {}
    for t in tareas:
        mapa.setdefault((t.referencia_documento, t.producto_id), t.motivo_bloqueo)
    return mapa


def calcular_pedidos_pendientes(almacen_id: int, fecha_desde: date, fecha_hasta: date) -> dict:
    lineas = _lineas_en_rango(almacen_id, fecha_desde, fecha_hasta)

    total_pedida = sum(p.cantidad_pedida or 0 for p in lineas)
    total_remisionada = sum((p.cantidad_pedida or 0) - (p.cantidad_pendiente or 0) for p in lineas)
    fill_rate = (total_remisionada / total_pedida) if total_pedida else None

    pendientes = [p for p in lineas if (p.cantidad_pendiente or 0) > 0]
    motivos_map = _motivos_por_pedido_producto({p.numero_pedido for p in pendientes})

    por_motivo = {}
    por_dia = {}
    for p in pendientes:
        motivo = mapear_motivo(motivos_map.get((p.numero_pedido, p.producto_id)))
        por_motivo[motivo] = por_motivo.get(motivo, 0) + 1
        fecha_ent = _parse_fecha_entrega(p.fecha_entrega)
        if fecha_ent:
            clave = fecha_ent.isoformat()
            por_dia[clave] = por_dia.get(clave, 0) + 1

    return {
        'lineas_pendientes': len(pendientes),
        'fill_rate': fill_rate,
        'por_motivo': por_motivo,
        'por_dia': por_dia,
        'fecha_desde': fecha_desde.isoformat(),
        'fecha_hasta': fecha_hasta.isoformat(),
    }


def listar_pedidos_pendientes_detalle(almacen_id: int, fecha_desde: date, fecha_hasta: date,
                                       page: int = 1, per_page: int = 50) -> dict:
    lineas = _lineas_en_rango(almacen_id, fecha_desde, fecha_hasta)
    pendientes = [p for p in lineas if (p.cantidad_pendiente or 0) > 0]
    motivos_map = _motivos_por_pedido_producto({p.numero_pedido for p in pendientes})

    items = []
    for p in pendientes:
        fecha_ent = _parse_fecha_entrega(p.fecha_entrega)
        items.append({
            'numero_pedido': p.numero_pedido,
            'item_codigo': p.item_codigo,
            'item_descripcion': p.item_descripcion,
            'cliente': p.cliente,
            'fecha_entrega': fecha_ent.isoformat() if fecha_ent else None,
            'dias_atraso': (fecha_hasta - fecha_ent).days if fecha_ent else None,
            'cantidad_pedida': p.cantidad_pedida,
            'cantidad_pendiente': p.cantidad_pendiente,
            'motivo': mapear_motivo(motivos_map.get((p.numero_pedido, p.producto_id))),
        })
    items.sort(key=lambda i: i['dias_atraso'] or 0, reverse=True)

    total = len(items)
    inicio = (page - 1) * per_page
    return {
        'items': items[inicio:inicio + per_page],
        'total': total,
        'page': page,
        'per_page': per_page,
    }
