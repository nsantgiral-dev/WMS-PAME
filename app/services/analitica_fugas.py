"""
Analítica — 💸 Fugas (Fase 1, 2026-09-24).

Una **fuga** es plata que la operación deja ir en el rango: una venta que no
se hizo, mercancía que falta, una entrega que volvió, una factura con el
cliente y sin pago, cobros que no entraron a caja, mercancía parada donde
nadie la mira, trabajo que se tiró y documentos que no llegaron a Siesa.

## Qué NO hace este módulo

No define ninguna política nueva de negocio. Cada fuga **lee la función que
ya decide** (Regla 0, «una política, una función»):

| Fuga | La política que lee |
|---|---|
| Venta perdida | `metricas.venta_perdida.filtros_venta_perdida` |
| Faltantes ajustados | `metricas.conteo._cargar_cadenas` + `_ajustes` (qué ajuste cuenta, qué se excluye, el costo de la foto) |
| Rechazos, entregado sin pago | `EstadoEntrega`, `motivos_rechazo.etiqueta`, el desenlace `siesa_nc_*` |
| Plata en la calle | `rezago_liquidacion` (qué ruta está sin liquidar, sus días, su urgencia) |
| Mercancía en limbo | `fotos_siesa_service.filas_vigentes` + `_BODEGAS_SERVICIO` + TRA-30/TRA-31 |
| Trabajo perdido | `bitacora_acciones` (lo que ya registraron cancelar/reabrir) |
| Documentos trabados | `siesa_jobs` en `FALLIDO` |

Lo único que se define acá es **cómo se valoriza cada caso** cuando la
política original no lo hace, y cada valorización está escrita en el
docstring de su fuga.

## Las cuatro reglas que el trinquete defiende

1. **Sin valor ≠ 0.** Un caso sin precio o sin costo tiene `pesos = None` y
   se cuenta aparte (`sin_valor`). El total de la fuga suma solo lo
   valorizado y dice que es cota inferior.
2. **Período anterior de igual duración**: `[desde − n, desde − 1]` con
   `n = hasta − desde + 1`. Comparar un rango de 30 días contra un mes
   calendario de 31 mide la diferencia de días, no la operación.
3. **AV1 y TRA1 nunca son vendibles.** El limbo solo mira las bodegas de
   servicio y nunca una operada; y su valor no entra a ningún total de
   «vendible».
4. **«Fugas del período» suma solo fugas con valor conocido** y dice cuántas
   quedan fuera por no tenerlo. Sumar un `None` como cero es exactamente lo
   que la Regla 0 prohíbe.

Cero Siesa: todo sale de la base del WMS y de las fotos ya guardadas.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import and_, func, or_

from app.extensions import db
from app.utils.fecha import ahora_bogota, dia_operativo_de, rango_dia_operativo_utc

# ─────────────────────────────────────────────────────────────────────────────
# El caso y la fuga
# ─────────────────────────────────────────────────────────────────────────────

#: Una clave que no está acá es un 404, no una fuga vacía.
CLAVES = ('venta_perdida', 'faltantes_inventario', 'rechazos_ruta',
          'entregado_sin_pago', 'credito_no_autorizado', 'plata_en_la_calle',
          'mercancia_en_limbo',
          'trabajo_perdido', 'documentos_trabados')

POR_PAGINA_MAX = 200
#: Bodega cuyo costo valoriza el limbo: el CDI. `AV1` es «Averías CDI» y la
#: foto de las bodegas de servicio no trae costo (`fotografiar_stock` solo
#: costea las operadas). Un SKU sin costo en NB1 ese día queda sin valor.
BODEGA_COSTO_REFERENCIA = 'NB1'

SIN_ALMACEN = 'Sin almacén'


@dataclass
class Caso:
    """Una fila del detalle. `pesos=None` es «no sabemos cuánto vale»."""
    referencia: str
    pesos: Optional[float]
    unidades: Optional[float]
    almacen_id: Optional[int]
    motivo: str
    dia: Optional[date] = None
    pedido_clave: Optional[str] = None
    detalle: dict = field(default_factory=dict)

    def to_dict(self, almacenes: dict) -> dict:
        return {
            'referencia': self.referencia,
            'pesos': None if self.pesos is None else round(self.pesos, 2),
            'sin_valor': self.pesos is None,
            'unidades': self.unidades,
            'almacen_id': self.almacen_id,
            'almacen': _nombre_almacen(almacenes, self.almacen_id),
            'motivo': self.motivo,
            'dia': self.dia.isoformat() if self.dia else None,
            'pedido_clave': self.pedido_clave,
            'detalle': self.detalle,
        }


@dataclass
class Resultado:
    """Lo que devuelve la función de cada fuga para un rango."""
    casos: list
    #: `None` = hay dato. Un texto = **no hay con qué medir** (no una fuga en
    #: cero): la fuga sale con `pesos`, `casos` y `unidades` en `None`.
    sin_dato: Optional[str] = None
    #: Parte del universo que no se pudo medir (la fuga igual tiene total,
    #: pero es cota inferior).
    faltan: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class Fuga:
    clave: str
    titulo: str
    definicion: str
    #: Qué significa la segunda dimensión («por motivo») en esta fuga.
    dimension_motivo: str
    unidad: str
    #: `False` en las fugas que no pertenecen a un almacén (el limbo): con un
    #: almacén filtrado se muestran, pero no suman al total del almacén.
    por_almacen_aplica: bool
    fn: Callable


class _Ctx:
    """Caché de lo que dos fugas o dos períodos leen igual en una petición."""

    def __init__(self, almacen_id):
        self.almacen_id = almacen_id
        self._cache = {}

    def una_vez(self, clave, fabrica):
        if clave not in self._cache:
            self._cache[clave] = fabrica()
        return self._cache[clave]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _f(v):
    return None if v is None else float(v)


def _nombre_almacen(almacenes, almacen_id):
    if almacen_id is None:
        return SIN_ALMACEN
    a = almacenes.get(almacen_id)
    return a['nombre'] if a else f'Almacén {almacen_id}'


def _almacenes() -> dict:
    from app.models.almacen import Almacen
    return {a.id: {'nombre': a.nombre, 'ciudad': a.ciudad, 'bodega': a.bodega_siesa_id}
            for a in Almacen.query.all()}


def periodo_anterior(desde: date, hasta: date) -> tuple:
    """El período inmediatamente anterior, **de la misma cantidad de días**."""
    n = (hasta - desde).days + 1
    return desde - timedelta(days=n), desde - timedelta(days=1)


def _rango_utc(desde, hasta):
    return rango_dia_operativo_utc(desde, hasta)


def _unidades_empacadas(tarea) -> Optional[int]:
    items = list(getattr(tarea, 'items', None) or [])
    if not items:
        return None
    return int(sum((i.cantidad_real or 0) for i in items))


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Venta perdida por agotado
# ─────────────────────────────────────────────────────────────────────────────

def _venta_perdida(desde, hasta, ctx) -> Resultado:
    """Unidades que un operario fue a buscar y no estaban (`FALTANTE` al
    bloquear el picking), valorizadas al precio capturado en el instante del
    agotado. Los traslados no cuentan: la exclusión es la de
    `filtros_venta_perdida`, no una copia.

    Un evento sin `precio_venta_capturado` es **sin valor**, nunca $0: hoy
    `Producto.precio_venta` no lo puebla ninguna sincronización, así que el
    total es cota inferior mientras haya eventos sin precio.
    """
    from app.models.evento_stock_agotado import EventoStockAgotado
    from app.models.picking import TareaPicking
    from app.services.metricas.venta_perdida import filtros_venta_perdida

    eventos = (EventoStockAgotado.query
               .filter(*filtros_venta_perdida(ctx.almacen_id, desde, hasta))
               .order_by(EventoStockAgotado.creado_en.desc()).all())
    ids_tarea = {e.tarea_picking_id for e in eventos if e.tarea_picking_id}
    claves = {}
    if ids_tarea:
        claves = dict(db.session.query(TareaPicking.id, TareaPicking.pedido_clave)
                      .filter(TareaPicking.id.in_(sorted(ids_tarea))).all())
    casos = []
    for e in eventos:
        precio = _f(e.precio_venta_capturado)
        prod = e.producto
        casos.append(Caso(
            referencia=e.pedido_siesa_ref or e.tarea_codigo or f'evento#{e.id}',
            pesos=None if precio is None else float(e.cantidad_faltante or 0) * precio,
            unidades=int(e.cantidad_faltante or 0),
            almacen_id=e.almacen_id,
            motivo=e.categoria_producto or 'Sin categoría',
            dia=dia_operativo_de(e.creado_en),
            pedido_clave=claves.get(e.tarea_picking_id),
            detalle={'producto': prod.codigo if prod else None,
                     'nombre': prod.nombre if prod else None,
                     'clase_abc': e.clasificacion_abc,
                     'precio_unitario': precio,
                     'pedidas': e.cantidad_solicitada},
        ))
    return Resultado(casos)


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Faltantes de inventario ajustados
# ─────────────────────────────────────────────────────────────────────────────

def _faltantes_inventario(desde, hasta, ctx) -> Resultado:
    """Ajustes de conteo que llegaron a Siesa (o van en vuelo) en el rango,
    valorizados al **costo de la foto del conteo** (`costo_prom_uni_siesa`).

    El universo, las exclusiones (ensayo, sin fecha de confirmación, sin
    diferencia) y el día de cada ajuste son los de `metricas.conteo._ajustes`:
    esta fuga no decide qué es un ajuste. Cada caso es una cadena ajustada:

    · faltante (`AJ-SAL`): pesos positivos — plata que se fue;
    · sobrante (`AJ-ENT`): pesos negativos — aparece mercancía.

    El total de la fuga es el **neto** (faltantes − sobrantes) y se muestran
    los dos por separado (`por_motivo` y `extra`). Si el neto sale negativo no
    le resta a «Fugas del período»: un sobrante de inventario no devuelve la
    plata de una entrega rechazada (`aporte_al_total` lo recorta en cero).

    Costo ausente o ≤ 0 → caso sin valor, igual que en las estadísticas.
    """
    from app.services.metricas import conteo as mc

    cadenas = ctx.una_vez('cadenas_conteo', lambda: mc._cargar_cadenas(ctx.almacen_id))
    bloque, validos = mc._ajustes(cadenas, desde, hasta)
    casos = []
    for c in validos:
        r = c.raiz
        dif = r.diferencia
        costo = _f(r.costo_prom_uni_siesa)
        valor = None if costo is None or costo <= 0 else -dif * costo
        casos.append(Caso(
            referencia=r.codigo,
            pesos=valor,
            unidades=-dif,
            almacen_id=r.almacen_id,
            motivo='Faltante' if dif < 0 else 'Sobrante',
            dia=c.dia,
            detalle={'producto': r.producto_codigo_siesa,
                     'nombre': r.producto.nombre if r.producto else None,
                     'diferencia': dif, 'costo_unitario': costo,
                     'como': ('Dentro de tolerancia' if r.ajuste_por_tolerancia
                              else 'Aprobado por supervisión' if r.aprobador_id
                              else 'Automático: dos conteos iguales'),
                     'en_vuelo': r.estado == 'AJUSTANDO'},
        ))
    valor = bloque['valor']
    extra = {
        'faltantes': {'unidades': bloque['unidades_sal'], 'pesos': valor['sal']},
        'sobrantes': {'unidades': bloque['unidades_ent'], 'pesos': valor['ent']},
        # Las estadísticas miden el neto desde el inventario (entradas −
        # salidas); la fuga lo mide desde la plata perdida (salidas − entradas).
        'neto_perdido_pesos': round(valor['sal'] - valor['ent'], 2),
        'etiqueta_valor': valor['etiqueta'],
        'excluidos': bloque['excluidos'],
    }
    return Resultado(casos, extra=extra)


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Rechazos en ruta   ·   4 · Entregado sin pago
# ─────────────────────────────────────────────────────────────────────────────

def _recaudos(desde, hasta, ctx, estados):
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    ini, fin = _rango_utc(desde, hasta)
    q = (RecaudoEntrega.query.join(TareaPacking, TareaPacking.id == RecaudoEntrega.tarea_id)
         .filter(RecaudoEntrega.estado_entrega.in_(estados),
                 RecaudoEntrega.fecha_confirmacion >= ini,
                 RecaudoEntrega.fecha_confirmacion < fin))
    if ctx.almacen_id is not None:
        q = q.filter(TareaPacking.almacen_id == ctx.almacen_id)
    return q.order_by(RecaudoEntrega.fecha_confirmacion.desc()).all()


def _precios_de_la_factura(pedido_clave) -> dict:
    """`{referencia: precio unitario neto | None}` de las líneas facturadas del
    pedido, según la foto de ventas. `None` = la referencia tiene más de un
    precio unitario (varias líneas o varias facturas a precios distintos): no
    se adivina cuál corresponde a lo devuelto. Anuladas (estado 9) no cuentan.
    """
    from app.models.fotos_siesa import FotoVentaLinea
    if not pedido_clave:
        return {}
    filas = (FotoVentaLinea.query
             .filter(FotoVentaLinea.pedido_clave == pedido_clave,
                     or_(FotoVentaLinea.estado_docto.is_(None),
                            FotoVentaLinea.estado_docto != 9)).all())
    por_ref = defaultdict(set)
    for f in filas:
        cant, neto = _f(f.cantidad), _f(f.vlr_neto)
        if not f.referencia or not cant or neto is None:
            continue
        por_ref[f.referencia.strip()].add(round(neto / cant, 4))
    return {ref: (next(iter(p)) if len(p) == 1 else None) for ref, p in por_ref.items()}


def _valor_devuelto_parcial(recaudo, tarea):
    """Valor de lo que volvió en una entrega PARCIAL: `devuelta × precio
    unitario neto de la factura` por referencia (`_precios_de_la_factura`).

    Si **una sola** referencia devuelta no tiene precio, la parada entera queda
    sin valor: un total parcial es más engañoso que ninguno (la misma regla con
    que `listar_paradas` manda la parada al campo libre).
    Devuelve `(pesos | None, unidades | None, sin_precio: list)`.
    """
    from app.models.producto import Producto
    items = recaudo.items_entregados or []
    devueltos = [it for it in items if int(it.get('cantidad_devuelta') or 0) > 0]
    if not devueltos:
        return None, None, ['sin detalle de lo devuelto']
    precios = _precios_de_la_factura(tarea.pedido_clave)
    codigos = {str(it.get('codigo') or '').strip() for it in devueltos}
    siesa = dict(db.session.query(Producto.codigo, Producto.codigo_siesa)
                 .filter(Producto.codigo.in_(sorted(codigos))).all()) if codigos else {}
    total, unidades, sin_precio = 0.0, 0, []
    for it in devueltos:
        cod = str(it.get('codigo') or '').strip()
        cant = int(it.get('cantidad_devuelta') or 0)
        unidades += cant
        precio = None
        for ref in (siesa.get(cod), cod):
            if ref and precios.get(ref.strip()) is not None:
                precio = precios[ref.strip()]
                break
        if precio is None:
            sin_precio.append(cod)
            continue
        total += cant * precio
    return (None if sin_precio else total), unidades, sin_precio


def _desenlace_nc(r) -> str:
    if r.siesa_nc_resultado:
        return r.siesa_nc_resultado
    return 'EN_COLA' if r.siesa_nc_triggered else 'SIN_NC'


_TEXTO_NC = {'ENVIADO': 'Nota crédito enviada', 'YA_SALDADA': 'Factura ya saldada',
             'SIN_VERIFICAR': 'Nota crédito sin verificar', 'FALLIDO': 'Nota crédito fallida',
             'SIN_LINEAS': 'Nota crédito sin líneas', 'EN_COLA': 'Nota crédito en cola',
             'SIN_NC': 'Sin nota crédito todavía'}


def _rechazos_ruta(desde, hasta, ctx) -> Resultado:
    """Paradas donde la mercancía **volvió** —`RECHAZADO` (todo) o `PARCIAL`
    (una parte)— confirmadas en el rango (día Bogotá de `fecha_confirmacion`).

    Valor: rechazo total = `valor_factura` de la parada; parcial = lo
    devuelto a precio de la factura (`_valor_devuelto_parcial`). Sin
    `valor_factura` o sin precio de lo devuelto → sin valor.

    Cada caso lleva su desenlace de nota crédito (`siesa_nc_*`): la NC no
    recupera la venta, documenta que no ocurrió.
    """
    from app.models.recaudo_entrega import EstadoEntrega
    from app.services import motivos_rechazo
    from app.services.senales_ruta import faltantes_de_retorno_de_recaudos
    casos, desenlaces = [], Counter()
    recaudos = _recaudos(desde, hasta, ctx, (EstadoEntrega.RECHAZADO, EstadoEntrega.PARCIAL))
    # Declarado por el conductor vs contado por recepción: lo que el conductor
    # dijo que volvía y no llegó a bodega. Solo devoluciones ya confirmadas.
    faltantes = faltantes_de_retorno_de_recaudos([r.id for r in recaudos])
    faltante_por_conductor = defaultdict(lambda: {'devoluciones': 0, 'faltante_unidades': 0.0,
                                                  'sobrante_unidades': 0.0})
    for r in recaudos:
        t = r.tarea
        if r.estado_entrega == EstadoEntrega.RECHAZADO:
            pesos, unidades, sin_precio = _f(t.valor_factura), _unidades_empacadas(t), []
            motivo = (motivos_rechazo.etiqueta(r.motivo_rechazo) if r.motivo_rechazo
                      else 'Rechazo sin motivo registrado')
        else:
            pesos, unidades, sin_precio = _valor_devuelto_parcial(r, t)
            motivo = (motivos_rechazo.etiqueta(r.motivo_rechazo) if r.motivo_rechazo
                      else 'Entrega parcial')
        nc = _desenlace_nc(r)
        desenlaces[nc] += 1
        falt = faltantes.get(r.id)
        if falt:
            g = faltante_por_conductor[r.ruta.conductor_id if r.ruta else None]
            g['devoluciones'] += 1
            g['faltante_unidades'] += falt['faltante_unidades']
            g['sobrante_unidades'] += falt['sobrante_unidades']
        casos.append(Caso(
            referencia=t.numero_pedido_siesa or t.codigo,
            pesos=pesos, unidades=unidades, almacen_id=t.almacen_id, motivo=motivo,
            dia=dia_operativo_de(r.fecha_confirmacion), pedido_clave=t.pedido_clave,
            detalle={'estado': 'Rechazada' if r.estado_entrega == EstadoEntrega.RECHAZADO
                     else 'Parcial', 'cliente': t.cliente, 'ruta_id': r.ruta_id,
                     'nota_credito': _TEXTO_NC.get(nc, nc),
                     'referencias_sin_precio': sin_precio,
                     'faltante_retorno': falt['faltante_unidades'] if falt else None},
        ))
    from app.models.conductor import Conductor
    nombres = dict(db.session.query(Conductor.id, Conductor.nombre)
                   .filter(Conductor.id.in_([c for c in faltante_por_conductor if c] or [-1])).all())
    return Resultado(casos, extra={
        'notas_credito': {_TEXTO_NC.get(k, k): v for k, v in sorted(desenlaces.items())},
        'faltante_de_retorno': sorted(
            [{'conductor_id': cid, 'conductor': nombres.get(cid), **{k: round(v, 4) if isinstance(v, float) else v
                                                                     for k, v in g.items()}}
             for cid, g in faltante_por_conductor.items()],
            key=lambda f: -f['faltante_unidades']),
    })


#: Tramos de días con la factura abierta. Los mismos del rezago del conteo:
#: no es un umbral de este negocio, es una forma de agrupar una antigüedad.
TRAMOS_DIAS = (('0–2 días', 0, 2), ('3–7 días', 3, 7), ('8–30 días', 8, 30),
               ('más de 30 días', 31, None))


def _tramo(dias):
    if dias is None:
        return 'Sin fecha'
    for texto, lo, hi in TRAMOS_DIAS:
        if dias >= lo and (hi is None or dias <= hi):
            return texto
    return TRAMOS_DIAS[0][0]


def _entregado_sin_pago(desde, hasta, ctx) -> Resultado:
    """Paradas `ENTREGADO_SIN_PAGO`: la mercancía se quedó con el cliente y la
    factura quedó abierta. El conductor no lo marca: lo deriva
    `confirmar_parada` del motivo «no pagó y se quedó con la mercancía».

    Valor: `valor_factura − monto_cobrado` (lo que quedó sin cobrar; un
    abono parcial se descuenta). Sin `valor_factura` → sin valor.
    Dimensión: días desde la entrega hasta hoy (la factura sigue abierta a
    menos que alguien la cobre por fuera del WMS, que el WMS no ve).
    """
    from app.models.recaudo_entrega import EstadoEntrega
    hoy = ahora_bogota().date()
    casos = []
    for r in _recaudos(desde, hasta, ctx, (EstadoEntrega.ENTREGADO_SIN_PAGO,)):
        t = r.tarea
        vf = _f(t.valor_factura)
        pesos = None if vf is None else max(0.0, vf - float(r.monto_cobrado or 0))
        dia = dia_operativo_de(r.fecha_confirmacion)
        dias = (hoy - dia).days
        casos.append(Caso(
            referencia=t.numero_pedido_siesa or t.codigo, pesos=pesos,
            unidades=_unidades_empacadas(t), almacen_id=t.almacen_id,
            motivo=_tramo(dias), dia=dia, pedido_clave=t.pedido_clave,
            detalle={'cliente': t.cliente, 'ruta_id': r.ruta_id, 'dias': dias,
                     'valor_factura': vf, 'abonado': float(r.monto_cobrado or 0),
                     'con_evidencia': bool(r.foto_entrega)},
        ))
    return Resultado(casos, extra={
        'por_conductor': ctx.una_vez(('sin_pago_por_conductor', desde, hasta),
                                     lambda: tasa_sin_pago_por_conductor(desde, hasta, ctx.almacen_id)),
    })


def _credito_no_autorizado(desde, hasta, ctx) -> Resultado:
    """Paradas de **contado contraentrega** (≤ 15 días de crédito, regla del
    dueño 2026-09-24) que el conductor entregó sin cobrar —CRÉDITO, EXENTO o
    $0— y que nadie autorizó como crédito. No define la política: la lee
    (`cond_pago.credito_no_autorizado`).

    Valor: `valor_factura − monto_cobrado` (lo que quedó sin cobrar; en un
    PARCIAL es cota superior: incluye lo devuelto, que no se valoriza acá).
    Sin `valor_factura` → sin valor. Dimensión: qué se registró.
    """
    from app.models.recaudo_entrega import EstadoEntrega
    from app.services import cond_pago as _cp
    casos = []
    for r in _recaudos(desde, hasta, ctx, (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL)):
        t = r.tarea
        if not _cp.credito_no_autorizado(r, t):
            continue
        vf = _f(t.valor_factura)
        cobrado = float(r.monto_cobrado or 0)
        pesos = None if vf is None else max(0.0, vf - cobrado)
        fp = (r.forma_pago or '').upper()
        motivo = (f'Registrado {fp}' if _cp.forma_no_cobra(fp)
                  else 'Entregado con $0 cobrado')
        cobro = _cp.cobro_de_recaudo(r, t)
        casos.append(Caso(
            referencia=t.numero_pedido_siesa or t.codigo, pesos=pesos,
            unidades=_unidades_empacadas(t), almacen_id=t.almacen_id,
            motivo=motivo, dia=dia_operativo_de(r.fecha_confirmacion),
            pedido_clave=t.pedido_clave,
            detalle={'cliente': t.cliente, 'ruta_id': r.ruta_id,
                     'estado_entrega': r.estado_entrega, 'forma_pago': r.forma_pago,
                     'valor_factura': vf, 'cobrado': cobrado,
                     'cond_pago': cobro.get('codigo'), 'dias_credito': cobro.get('dias'),
                     'clasif_origen': cobro.get('origen')},
        ))
    return Resultado(casos)


def tasa_sin_pago_por_conductor(desde, hasta, almacen_id=None) -> list:
    """«No pagó y se quedó con la mercancía», **por conductor**, como tasa.

    Es una SEÑAL para el encargado, no una sanción (regla 2 de flota): un
    conductor con una tasa alta puede tener la ruta de los clientes difíciles.
    Por eso va con su denominador —las paradas que confirmó en el rango— y con
    cuántas de las sin pago no traen foto de evidencia. Sin paradas → no
    aparece (no hay tasa de cero sobre nada).

    Vive acá, junto a la fuga que valoriza ENTREGADO_SIN_PAGO, y la liquidación
    la lee de acá: una política, una función.
    """
    from app.models.conductor import Conductor
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    ini, fin = _rango_utc(desde, hasta)
    q = (db.session.query(RutaDespacho.conductor_id, RecaudoEntrega.estado_entrega,
                          RecaudoEntrega.foto_entrega.isnot(None), TareaPacking.valor_factura,
                          RecaudoEntrega.monto_cobrado)
         .join(RutaDespacho, RutaDespacho.id == RecaudoEntrega.ruta_id)
         .join(TareaPacking, TareaPacking.id == RecaudoEntrega.tarea_id)
         .filter(RecaudoEntrega.fecha_confirmacion >= ini,
                 RecaudoEntrega.fecha_confirmacion < fin))
    if almacen_id is not None:
        q = q.filter(TareaPacking.almacen_id == almacen_id)
    por = defaultdict(lambda: {'paradas': 0, 'sin_pago': 0, 'sin_evidencia': 0,
                               'pesos': 0.0, 'sin_valor': 0})
    for cid, estado, con_foto, vf, cobrado in q.all():
        g = por[cid]
        g['paradas'] += 1
        if estado == EstadoEntrega.ENTREGADO_SIN_PAGO:
            g['sin_pago'] += 1
            if not con_foto:
                g['sin_evidencia'] += 1
            if vf is None:
                g['sin_valor'] += 1
            else:
                g['pesos'] += max(0.0, float(vf) - float(cobrado or 0))
    nombres = dict(db.session.query(Conductor.id, Conductor.nombre)
                   .filter(Conductor.id.in_(list(por) or [-1])).all())
    filas = [{'conductor_id': cid, 'conductor': nombres.get(cid),
              'paradas': g['paradas'], 'sin_pago': g['sin_pago'],
              'tasa': round(g['sin_pago'] / g['paradas'], 4),
              'sin_evidencia': g['sin_evidencia'],
              'pesos': round(g['pesos'], 2), 'sin_valor': g['sin_valor']}
             for cid, g in por.items()]
    return sorted(filas, key=lambda f: (-f['tasa'], -f['sin_pago'], -f['paradas']))


# ─────────────────────────────────────────────────────────────────────────────
# 5 · Plata en la calle
# ─────────────────────────────────────────────────────────────────────────────

_TEXTO_URGENCIA = {'ok': 'Al día', 'atrasada': 'Atrasada', 'cruza_mes': 'Cruza de mes'}


def _plata_en_la_calle(desde, hasta, ctx) -> Resultado:
    """Rutas **entregadas y sin liquidar hoy** (`rezago_liquidacion`), cuya
    entrega cae en el rango. Un caso por ruta y almacén.

    Valor: lo que el conductor **declaró cobrar** en esas paradas
    (`monto_cobrado`) — plata que salió del cliente y no entró a caja en
    Siesa. Una ruta sin ninguna parada confirmada no tiene valor conocido
    (sin valor, no $0). Aparte, en `extra`, la cartera que sigue abierta por
    no liquidar (`valor_factura` de lo entregado).

    Una ruta **sin fecha** no se puede ubicar en un rango: se cuenta en el
    período actual y se declara (`sin_fecha`) — Regla 0, el lado conservador
    es que aparezca.
    """
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
    from app.services import rezago_liquidacion as rl

    hoy = ahora_bogota().date()
    es_actual = ctx.una_vez('periodo_actual', lambda: (desde, hasta)) == (desde, hasta)
    rutas = ctx.una_vez('rutas_sin_liquidar', rl.rutas_entregadas_sin_liquidar)
    casos, sin_fecha, cartera, cartera_sin_valor = [], 0, 0.0, 0
    for ruta in rutas:
        ref = rl.fecha_de_referencia(ruta)
        if ref is None:
            if not es_actual:
                continue
            sin_fecha += 1
        elif not (desde <= ref <= hasta):
            continue
        recaudos = RecaudoEntrega.query.filter_by(ruta_id=ruta.id).all()
        tareas = {b.tarea_id for b in Bulto.query.filter_by(ruta_despacho_id=ruta.id).all()
                  if b.tarea_id}
        tareas |= {r.tarea_id for r in recaudos}
        almacen_de = dict(db.session.query(TareaPacking.id, TareaPacking.almacen_id)
                          .filter(TareaPacking.id.in_(sorted(tareas))).all()) if tareas else {}
        por_almacen = defaultdict(list)
        for tid in tareas:
            por_almacen[almacen_de.get(tid)].append(tid)
        if not por_almacen:
            por_almacen[None] = []
        dias = rl.dias_de_rezago(ruta, hoy)
        urgencia = _TEXTO_URGENCIA.get(rl.urgencia(ruta, hoy), rl.urgencia(ruta, hoy))
        for alm, tids in por_almacen.items():
            if ctx.almacen_id is not None and alm != ctx.almacen_id:
                continue
            rs = [r for r in recaudos if r.tarea_id in tids]
            pesos = sum(float(r.monto_cobrado or 0) for r in rs) if rs else None
            for r in rs:
                if r.estado_entrega in EstadoEntrega.SIN_RETORNO:
                    vf = _f(r.tarea.valor_factura)
                    if vf is None:
                        cartera_sin_valor += 1
                    else:
                        cartera += vf
            casos.append(Caso(
                referencia=f'Ruta {ruta.id}', pesos=pesos,
                unidades=len(tids), almacen_id=alm,
                motivo=urgencia if ref is not None else 'Sin fecha de entrega',
                dia=ref,
                detalle={'ruta_id': ruta.id, 'dias': dias,
                         'paradas': len(tids), 'paradas_confirmadas': len(rs),
                         'estado_financiero': ruta.estado_financiero},
            ))
    return Resultado(casos, extra={
        'sin_fecha': sin_fecha,
        'cartera_abierta_por_no_liquidar': {'pesos': round(cartera, 2),
                                            'paradas_sin_valor': cartera_sin_valor},
    })


# ─────────────────────────────────────────────────────────────────────────────
# 6 · Mercancía en limbo
# ─────────────────────────────────────────────────────────────────────────────

_TEXTO_BODEGA = {'AV1': 'Averías (AV1)', 'TRA1': 'En tránsito (TRA1)'}


def bodegas_de_limbo() -> tuple:
    """Las bodegas de servicio: **nunca** una operada. Lista blanca, la misma
    que decide qué entra a `stock_siesa` sin ser vendible."""
    from app.services.inventario_siesa_service import _BODEGAS_PV, _BODEGAS_SERVICIO
    return tuple(b for b in _BODEGAS_SERVICIO if b not in _BODEGAS_PV)


def _ultima_foto_completa(bodega, desde, hasta):
    from app.models.fotos_siesa import FotoCorrida, TipoFoto
    c = (FotoCorrida.query
         .filter(FotoCorrida.tipo == TipoFoto.STOCK, FotoCorrida.alcance == bodega,
                 FotoCorrida.completa.is_(True),
                 FotoCorrida.dia_operativo >= desde, FotoCorrida.dia_operativo <= hasta)
         .order_by(FotoCorrida.dia_operativo.desc(), FotoCorrida.terminada_at.desc())
         .first())
    return c.dia_operativo if c else None


def _mercancia_en_limbo(desde, hasta, ctx) -> Resultado:
    """Existencias en **AV1** (averías) y **TRA1** (tránsito) según la última
    foto completa del rango (`filas_vigentes`): stock que no falta ni sobra,
    pero no está en ninguna bodega que alguien venda.

    Valor: `existencia × costo promedio de ese SKU en NB1` (el CDI) en la
    foto del mismo día — la foto de las bodegas de servicio no trae costo.
    Sin costo en NB1 → sin valor. Nunca se suma como vendible: el universo es
    `bodegas_de_limbo()`, que excluye toda bodega operada, y la fila además
    tiene que venir marcada `vendible=False`.

    Una bodega sin foto completa en el rango es **no sabemos**, no cero.
    `extra` agrega los traslados atascados (TRA-30) y las averías sin
    dictaminar (TRA-31), que son foto de hoy.
    """
    from app.models.fotos_siesa import TipoFoto
    from app.services.fotos_siesa_service import filas_vigentes

    casos, faltan, dias = [], [], {}
    for bodega in bodegas_de_limbo():
        dia = _ultima_foto_completa(bodega, desde, hasta)
        if dia is None:
            faltan.append(f'{bodega}: sin foto completa entre {desde} y {hasta}')
            continue
        dias[bodega] = dia.isoformat()
        filas = filas_vigentes(TipoFoto.STOCK, bodega, dia) or []
        ref = filas_vigentes(TipoFoto.STOCK, BODEGA_COSTO_REFERENCIA, dia) or []
        costos = {f.codigo_siesa: _f(f.costo_prom_uni) for f in ref}
        for f in filas:
            if f.vendible or not (f.existencia or 0) > 0:
                continue
            costo = _f(f.costo_prom_uni) or costos.get(f.codigo_siesa)
            casos.append(Caso(
                referencia=f.codigo_siesa,
                pesos=None if not costo or costo <= 0 else float(f.existencia) * costo,
                unidades=float(f.existencia), almacen_id=None,
                motivo=_TEXTO_BODEGA.get(bodega, bodega), dia=dia,
                detalle={'bodega': bodega, 'costo_unitario': costo,
                         'rezagada': bool(f.rezagada),
                         'stock_actualizado_at': (f.stock_actualizado_at.isoformat()
                                                  if f.stock_actualizado_at else None)},
            ))
    extra = {'dia_de_la_foto': dias, 'costo_de': BODEGA_COSTO_REFERENCIA}
    if ctx.una_vez('periodo_actual', lambda: (desde, hasta)) == (desde, hasta):
        extra.update(_limbo_de_traslados())
    sin_dato = None
    if len(faltan) == len(bodegas_de_limbo()):
        sin_dato = ('No hay foto completa de las bodegas de servicio en el rango '
                    '(FOTOS_SIESA apagado o la corrida no terminó).')
    return Resultado(casos, sin_dato=sin_dato, faltan=faltan, extra=extra)


def _limbo_de_traslados() -> dict:
    from app.services.auditoria.traslados import (
        DIAS_EN_TRANSITO_ANORMAL, se_pueden_contar_los_traslados_en_vuelo,
        toda_averia_recibida_termina_dictaminada)
    try:
        vuelo = se_pueden_contar_los_traslados_en_vuelo()
        averias = toda_averia_recibida_termina_dictaminada()
    except Exception as e:  # la auditoría no puede tumbar la fuga
        return {'traslados': {'error': str(e)[:200]}}
    # Lo anterior al corte no suma a la fuga (`corte.separar`, la misma regla
    # que la auditoría aplica a estos hallazgos).
    from app.services import corte as _corte
    vuelo, _antes_vuelo = _corte.separar(vuelo, lambda h: h.fecha)
    averias, _antes_averias = _corte.separar(averias, lambda h: h.fecha)
    atascados = [h for h in vuelo if h.referencia != 'sin-fecha-de-despacho']
    sin_fecha = next((h.datos.get('total', 0) for h in vuelo
                      if h.referencia == 'sin-fecha-de-despacho'), 0)
    return {'traslados': {
        'atascados': len(atascados),
        'umbral_dias': DIAS_EN_TRANSITO_ANORMAL,
        'sin_fecha_de_despacho': sin_fecha,
        'averias_sin_dictaminar': sum(h.datos.get('total', 0) for h in averias),
        'codigos': [h.referencia for h in atascados][:20],
        'antes_del_corte': len(_antes_vuelo) + len(_antes_averias),
    }}


# ─────────────────────────────────────────────────────────────────────────────
# 7 · Trabajo perdido
# ─────────────────────────────────────────────────────────────────────────────

def _trabajo_perdido(desde, hasta, ctx) -> Resultado:
    """Trabajo hecho y tirado, según la bitácora del rango:

    · picking **cancelado** o **reabierto** con unidades ya recogidas
      (`antes.cantidad_recogida > 0`): alguien caminó, las sacó y hay que
      devolverlas. La cancelación que cierra una **auditoría** no cuenta: lo
      recogido pasa al empaque (`despues.auditoria_resultado`).
    · empaque **cancelado** que ya había empezado (`antes.estado` distinto
      de PENDIENTE); unidades = lo verificado en sus líneas.

    **Sin valor, siempre**: es trabajo, no mercancía — la mercancía vuelve al
    estante, y el WMS no tiene un costo de mano de obra por unidad. Se cuenta
    en casos y unidades, con quién hizo el trabajo y quién lo tiró.
    La bitácora existe desde `m035bitacora`: antes de eso no hay registro.
    """
    from app.models.bitacora import BitacoraAccion
    from app.models.packing import TareaPacking
    from app.models.picking import TareaPicking
    from app.models.usuario import Usuario

    q = (BitacoraAccion.query
         .filter(BitacoraAccion.dia_operativo >= desde, BitacoraAccion.dia_operativo <= hasta,
                 or_(and_(BitacoraAccion.entidad == 'TareaPicking',
                                BitacoraAccion.accion.in_(('CANCELAR', 'REABRIR'))),
                        and_(BitacoraAccion.entidad == 'TareaPacking',
                                BitacoraAccion.accion == 'CANCELAR'))))
    if ctx.almacen_id is not None:
        q = q.filter(BitacoraAccion.almacen_id == ctx.almacen_id)
    filas = q.order_by(BitacoraAccion.ocurrido_en.desc()).all()

    ids_pick = {f.entidad_id for f in filas if f.entidad == 'TareaPicking' and f.entidad_id}
    ids_pack = {f.entidad_id for f in filas if f.entidad == 'TareaPacking' and f.entidad_id}
    picks = {t.id: t for t in TareaPicking.query.filter(TareaPicking.id.in_(sorted(ids_pick))).all()} if ids_pick else {}
    packs = {t.id: t for t in TareaPacking.query.filter(TareaPacking.id.in_(sorted(ids_pack))).all()} if ids_pack else {}
    ids_usr = set()
    for f in filas:
        ids_usr.add(f.usuario_id)
        ids_usr.add((f.antes or {}).get('operario_id') or (f.antes or {}).get('empacador_id'))
    ids_usr.discard(None)
    nombres = dict(db.session.query(Usuario.id, Usuario.nombre)
                   .filter(Usuario.id.in_(sorted(ids_usr))).all()) if ids_usr else {}

    casos = []
    por_persona = Counter()
    for f in filas:
        antes, despues = f.antes or {}, f.despues or {}
        if f.entidad == 'TareaPicking':
            if 'auditoria_resultado' in despues:
                continue
            try:
                unidades = int(antes.get('cantidad_recogida') or 0)
            except (TypeError, ValueError):
                unidades = 0
            if unidades <= 0:
                continue
            t = picks.get(f.entidad_id)
            hizo = antes.get('operario_id')
            que = 'Picking cancelado' if f.accion == 'CANCELAR' else 'Picking reabierto'
        else:
            if (antes.get('estado') or 'PENDIENTE') == 'PENDIENTE':
                continue
            t = packs.get(f.entidad_id)
            unidades = _unidades_empacadas(t) if t is not None else None
            hizo = antes.get('empacador_id')
            que = 'Empaque cancelado'
        quien = nombres.get(f.usuario_id) or ('Sistema' if f.usuario_id is None else f'Usuario {f.usuario_id}')
        por_persona[quien] += 1
        casos.append(Caso(
            referencia=f.entidad_codigo or f'{f.entidad}#{f.entidad_id}',
            pesos=None, unidades=unidades, almacen_id=f.almacen_id,
            motivo=(f.motivo or 'Sin motivo registrado')[:80],
            dia=f.dia_operativo,
            pedido_clave=getattr(t, 'pedido_clave', None) if t is not None else None,
            detalle={'que': que, 'lo_tiro': quien,
                     'lo_hizo': nombres.get(hizo) if hizo else None,
                     'motivo_completo': f.motivo},
        ))
    return Resultado(casos, extra={
        'motivo_sin_valor': 'Es trabajo, no mercancía: el WMS no tiene costo de mano de obra por unidad.',
        'por_quien_lo_tiro': dict(por_persona.most_common(20)),
    })


# ─────────────────────────────────────────────────────────────────────────────
# 8 · Documentos trabados en Siesa
# ─────────────────────────────────────────────────────────────────────────────

TEXTO_DOCUMENTO = {
    'DESPACHO_F470': 'Remisión y factura del despacho',
    'RECIBO_CAJA': 'Recibo de caja',
    'DOCUMENTO_CONTABLE_RET': 'Retención',
    'NOTA_CREDITO_FACTURA': 'Nota crédito de ruta',
    'NOTA_CREDITO_DEVOLUCION_CLIENTE': 'Nota crédito de devolución',
    'MOTIVO_DIAN_NC': 'Motivo DIAN de nota crédito',
    'AJUSTE_CONTEO': 'Ajuste de conteo',
    'TRASLADO_AVERIAS': 'Traslado a averías',
    'ENTRADA_OC': 'Entrada de compra',
    'DESPACHO_TRASLADO': 'Salida de traslado',
    'TRANSFERENCIA_UBICACIONES': 'Transferencia entre ubicaciones',
}
#: No son documentos: un correo que no salió no es plata trabada.
TIPOS_NO_DOCUMENTO = ('ALERTA_EMAIL',)


def valor_de_job(job) -> tuple:
    """`(pesos | None, almacen_id | None, pedido_clave | None)` de un job.

    Solo tres documentos llevan un valor que el WMS conoce sin inventarlo:
    · recibo de caja y retención: `payload.monto` (lo que se iba a mandar);
    · despacho: `valor_factura` de la tarea de empaque;
    · ajuste de conteo: `cantidad × costo de la foto` de la sesión.
    Todo lo demás es sin valor declarado.
    """
    import json
    from app.models.conteo import SesionConteo
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    try:
        p = json.loads(job.payload or '{}')
        if not isinstance(p, dict):
            p = {}
    except (TypeError, ValueError):
        p = {}
    tarea = None
    if job.referencia_tipo == 'TareaPacking' and job.referencia_id:
        tarea = db.session.get(TareaPacking, job.referencia_id)
    elif job.referencia_tipo == 'RecaudoEntrega' and job.referencia_id:
        r = db.session.get(RecaudoEntrega, job.referencia_id)
        tarea = r.tarea if r else None
    alm = tarea.almacen_id if tarea else None
    clave = tarea.pedido_clave if tarea else None
    if job.tipo in ('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET'):
        return _f(p.get('monto')), alm, clave
    if job.tipo == 'DESPACHO_F470':
        return (_f(tarea.valor_factura) if tarea else None), alm, clave
    if job.tipo == 'AJUSTE_CONTEO' and job.referencia_id:
        s = db.session.get(SesionConteo, job.referencia_id)
        if s is None:
            return None, alm, clave
        costo = _f(s.costo_prom_uni_siesa)
        cant = _f(p.get('cantidad'))
        valor = None if not costo or costo <= 0 or cant is None else cant * costo
        return valor, s.almacen_id, clave
    return None, alm, clave


def _documentos_trabados(desde, hasta, ctx) -> Resultado:
    """Jobs de Siesa que **siguen** trabados —`siesa_job_service.fallidos_vigentes`,
    la única que los cuenta: sin los superados por un COMPLETADO posterior ni
    los que la reconciliación cerró— **creados** en el rango. No es «no existe
    en Siesa»: es «nadie lo va a reintentar solo». Valor por `valor_de_job`.
    `extra.fallidos_hoy` cuenta todos los que hoy siguen trabados desde el
    corte, sin importar cuándo se crearon, para que uno viejo no desaparezca
    por salirse del rango; `extra.recientes`/`viejos` los separa por edad.
    """
    from app.services import corte as _corte
    from app.services.siesa_job_service import fallidos_vigentes
    ini, fin = _rango_utc(desde, hasta)
    trabados = fallidos_vigentes(desde=ini, hasta=fin, excluir_tipos=TIPOS_NO_DOCUMENTO)
    casos = []
    for j in trabados['jobs']:
        pesos, alm, clave = valor_de_job(j)
        if ctx.almacen_id is not None and alm != ctx.almacen_id:
            continue
        casos.append(Caso(
            referencia=f'Job {j.id}', pesos=pesos, unidades=1, almacen_id=alm,
            motivo=TEXTO_DOCUMENTO.get(j.tipo, j.tipo), dia=dia_operativo_de(j.fecha_creacion),
            pedido_clave=clave,
            detalle={'tipo': j.tipo, 'intentos': j.intentos,
                     'error': (j.error_ultimo or '')[:300] or None},
        ))
    extra = {'superados': trabados['superados']}
    if ctx.una_vez('periodo_actual', lambda: (desde, hasta)) == (desde, hasta):
        hoy = fallidos_vigentes(desde=_corte.inicio_auditoria(),
                                excluir_tipos=TIPOS_NO_DOCUMENTO)
        extra.update(fallidos_hoy=len(hoy['jobs']), recientes=len(hoy['recientes']),
                     viejos=len(hoy['viejos']),
                     ventana_reciente_dias=hoy['ventana_reciente_dias'])
    return Resultado(casos, extra=extra)


# ─────────────────────────────────────────────────────────────────────────────
# El catálogo
# ─────────────────────────────────────────────────────────────────────────────

FUGAS = {f.clave: f for f in (
    Fuga('venta_perdida', 'Venta perdida por agotado',
         'Unidades que un pedido pidió y no estaban en el estante, al precio del momento.',
         'Categoría', 'unidades no vendidas', True, _venta_perdida),
    Fuga('faltantes_inventario', 'Faltantes de inventario ajustados',
         'Ajustes de conteo enviados a Siesa, a costo de la foto del conteo: faltantes menos sobrantes.',
         'Faltante o sobrante', 'unidades netas perdidas', True, _faltantes_inventario),
    Fuga('rechazos_ruta', 'Rechazos en ruta',
         'Mercancía que salió en ruta y volvió, total o en parte, con su nota crédito.',
         'Motivo', 'unidades devueltas', True, _rechazos_ruta),
    Fuga('entregado_sin_pago', 'Entregado sin pago',
         'Mercancía que se quedó con el cliente sin pagar: la factura sigue abierta.',
         'Días con la factura abierta', 'unidades entregadas', True, _entregado_sin_pago),
    Fuga('credito_no_autorizado', 'Crédito no autorizado',
         'Facturas de contado contraentrega entregadas sin cobrar y que nadie autorizó como crédito.',
         'Qué se registró', 'unidades entregadas', True, _credito_no_autorizado),
    Fuga('plata_en_la_calle', 'Plata en la calle',
         'Lo que el conductor cobró en rutas entregadas que nadie ha liquidado.',
         'Urgencia', 'paradas', True, _plata_en_la_calle),
    Fuga('mercancia_en_limbo', 'Mercancía en limbo',
         'Existencias en averías (AV1) y en tránsito (TRA1): no se venden y nadie las mira.',
         'Bodega', 'unidades', False, _mercancia_en_limbo),
    Fuga('trabajo_perdido', 'Trabajo perdido',
         'Pickings y empaques cancelados o reabiertos después de hacer el trabajo.',
         'Motivo', 'unidades manipuladas', True, _trabajo_perdido),
    Fuga('documentos_trabados', 'Documentos trabados en Siesa',
         'Documentos que el WMS dejó de intentar mandar a Siesa.',
         'Documento', 'documentos', True, _documentos_trabados),
)}
assert tuple(FUGAS) == CLAVES


# ─────────────────────────────────────────────────────────────────────────────
# Agregación — la misma para todas
# ─────────────────────────────────────────────────────────────────────────────

def _agrupar(casos, clave_de, almacenes=None):
    grupos = defaultdict(lambda: {'pesos': 0.0, 'casos': 0, 'unidades': 0.0,
                                  'sin_valor': 0})
    for c in casos:
        g = grupos[clave_de(c)]
        g['casos'] += 1
        if c.unidades is not None:
            g['unidades'] += c.unidades
        if c.pesos is None:
            g['sin_valor'] += 1
        else:
            g['pesos'] += c.pesos
    filas = []
    for k, g in grupos.items():
        fila = {'pesos': round(g['pesos'], 2), 'casos': g['casos'],
                'unidades': g['unidades'], 'sin_valor': g['sin_valor']}
        if almacenes is not None:
            fila.update({'almacen_id': k, 'almacen': _nombre_almacen(almacenes, k),
                         'ciudad': (almacenes.get(k) or {}).get('ciudad')})
        else:
            fila['motivo'] = k
        filas.append(fila)
    return sorted(filas, key=lambda f: (-f['sin_valor'] if f['casos'] == f['sin_valor'] else 0,
                                        -abs(f['pesos']), -f['casos']))


def totales(res: Resultado) -> dict:
    """Pesos, unidades y casos de un resultado. **Sin valor ≠ 0**: los casos
    sin valor no suman a `pesos` y se cuentan en `sin_valor`; si hay alguno,
    `pesos` es cota inferior. Sin dato → todo `None`."""
    if res.sin_dato:
        return {'pesos': None, 'casos': None, 'unidades': None,
                'sin_valor': {'casos': None, 'unidades': None},
                'es_cota_inferior': False, 'sin_dato': res.sin_dato}
    valorizados = [c for c in res.casos if c.pesos is not None]
    sin = [c for c in res.casos if c.pesos is None]
    unidades = [c.unidades for c in res.casos if c.unidades is not None]
    return {
        'pesos': round(sum(c.pesos for c in valorizados), 2) if valorizados else (None if sin else 0.0),
        'casos': len(res.casos),
        'unidades': sum(unidades) if unidades else (None if res.casos else 0),
        'sin_valor': {'casos': len(sin),
                      'unidades': sum(c.unidades for c in sin if c.unidades is not None)},
        'es_cota_inferior': bool(sin) and bool(valorizados) or bool(res.faltan),
        'sin_dato': None,
    }


def tendencia(actual: dict, anterior: dict) -> dict:
    """Contra el período anterior de igual duración. Por pesos si los dos
    tienen valor; si no, por casos; si falta alguno, `sin_base`."""
    def _delta(a, b):
        return None if a is None or b is None else round(a - b, 2)
    dp = _delta(actual['pesos'], anterior['pesos'])
    dc = _delta(actual['casos'], anterior['casos'])
    base = dp if (dp is not None and (actual['pesos'] or anterior['pesos'])) else dc
    direccion = ('sin_base' if base is None else
                 'sube' if base > 0 else 'baja' if base < 0 else 'igual')
    return {'anterior': {k: anterior[k] for k in ('pesos', 'casos', 'unidades')},
            'delta_pesos': dp, 'delta_casos': dc, 'direccion': direccion}


def estado_de(t: dict, tend: dict) -> str:
    """Píldora: `ok` sin casos; `critico` si hay casos y sube (o no se sabe);
    `advertencia` si hay casos y no sube."""
    if t['sin_dato']:
        return 'critico'
    if not t['casos']:
        return 'ok'
    return 'critico' if tend['direccion'] in ('sube', 'sin_base') else 'advertencia'


def aporte_al_total(fuga: Fuga, t: dict, almacen_id) -> tuple:
    """`(pesos | None, motivo si no aporta)`. Lo único que suma a «Fugas del
    período»: una fuga con valor conocido. Nunca negativa."""
    if t['sin_dato']:
        return None, 'sin dato: ' + t['sin_dato']
    if almacen_id is not None and not fuga.por_almacen_aplica:
        return None, 'no pertenece a un almacén (bodegas de servicio)'
    if t['pesos'] is None:
        return None, 'ningún caso tiene precio o costo'
    return max(0.0, t['pesos']), None


def _resumen_fuga(fuga: Fuga, res: Resultado, res_ant: Resultado, almacenes, almacen_id):
    t = totales(res)
    ta = totales(res_ant)
    tend = tendencia(t, ta)
    aporte, fuera = aporte_al_total(fuga, t, almacen_id)
    return {
        'clave': fuga.clave, 'titulo': fuga.titulo, 'definicion': fuga.definicion,
        'unidad': fuga.unidad, 'dimension_motivo': fuga.dimension_motivo,
        **t,
        'aporte_al_total': None if aporte is None else round(aporte, 2),
        'fuera_del_total_por': fuera,
        'faltan': res.faltan,
        'tendencia': tend,
        'estado': estado_de(t, tend),
        'por_almacen': [] if t['sin_dato'] else _agrupar(res.casos, lambda c: c.almacen_id, almacenes),
        'por_motivo': [] if t['sin_dato'] else _agrupar(res.casos, lambda c: c.motivo),
        'con_recorrido': sum(1 for c in res.casos if c.pedido_clave),
        'extra': res.extra,
    }


def _mediana(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def ordenar(fugas: list) -> list:
    """Orden de pantalla:

    1. sin dato (no se sabe cuánto hay) y **sin valor con volumen relevante**
       —sus casos sin valor ≥ la mediana de casos de las fugas con valor—:
       no saber cuánto vale no la vuelve chica (Regla 0);
    2. las de valor conocido, por pesos de mayor a menor;
    3. sin valor con poco volumen;
    4. las que no tuvieron casos.
    """
    con_valor = [f for f in fugas if f['pesos'] is not None and f['casos']]
    ref = _mediana([f['casos'] for f in con_valor]) or 1

    def nivel(f):
        if f['sin_dato']:
            return 0
        if not f['casos']:
            return 3
        if f['pesos'] is None:
            return 0 if f['sin_valor']['casos'] >= ref else 2
        return 1

    return sorted(fugas, key=lambda f: (nivel(f), -(f['pesos'] or 0),
                                        -(f['sin_valor']['casos'] or 0)))


def _fuentes(desde, hasta) -> dict:
    from app.models.bitacora import BitacoraAccion
    from app.models.evento_stock_agotado import EventoStockAgotado
    primera_bit = db.session.query(func.min(BitacoraAccion.dia_operativo)).scalar()
    primer_ev = db.session.query(func.min(EventoStockAgotado.creado_en)).scalar()
    fotos = {b: _ultima_foto_completa(b, desde, hasta) for b in bodegas_de_limbo()}
    return {
        'base_wms': {'descripcion': 'Base del WMS, en vivo', 'completa': True},
        'bitacora': {'desde': primera_bit.isoformat() if primera_bit else None,
                     'completa': bool(primera_bit and primera_bit <= desde),
                     'nota': 'Trabajo perdido se lee de la bitácora: antes de su '
                             'primer registro no hay con qué medirlo.'},
        'eventos_agotado': {'desde': dia_operativo_de(primer_ev).isoformat() if primer_ev else None,
                            'completa': bool(primer_ev and dia_operativo_de(primer_ev) <= desde)},
        'foto_stock_servicio': {'dia': {b: (d.isoformat() if d else None) for b, d in fotos.items()},
                                'completa': all(fotos.values())},
        'foto_ventas': {'nota': 'Precio de lo devuelto en entregas parciales.'},
    }


class _Rangos:
    """El rango pedido llevado al corte (`FECHA_INICIO_AUDITORIA`).

    · El período actual empieza en el corte si el rango lo cruza.
    · El anterior, de igual duración que el pedido, también se recorta; si cae
      entero antes del corte **no hay base** (`sin_dato`): comparar contra el
      ensayo diría «bajó» o «subió» sobre datos que no son operación.
    · El tramo recortado se evalúa aparte y se publica en `antes_del_corte`
      por fuga: se cuenta, no suma ni cambia el estado.
    """

    def __init__(self, desde, hasta):
        from app.services import corte
        self.pedido = (desde, hasta)
        self.desde, self.hasta, self.info = corte.recortar_rango(desde, hasta)
        ant = periodo_anterior(desde, hasta)
        a_desde, a_hasta, _ = corte.recortar_rango(*ant)
        self.anterior = (a_desde, a_hasta)
        self.anterior_vacio = a_desde > a_hasta
        self.corte = corte.estado()

    def evaluar(self, fuga, ctx):
        vacio = self.desde > self.hasta
        res = Resultado([]) if vacio else fuga.fn(self.desde, self.hasta, ctx)
        res_ant = (Resultado([], sin_dato='el período anterior es anterior al corte')
                   if self.anterior_vacio else fuga.fn(*self.anterior, ctx))
        antes = None
        if self.info['aplicado']:
            pre = fuga.fn(*self.info['antes'], ctx)
            t = totales(pre)
            antes = {'casos': t['casos'], 'pesos': t['pesos']}
        return res, res_ant, antes

    def meta(self):
        return {'desde_efectivo': self.desde.isoformat() if self.desde else None,
                'anterior_efectivo': ({'desde': self.anterior[0].isoformat(),
                                       'hasta': self.anterior[1].isoformat()}
                                      if not self.anterior_vacio else None),
                **self.corte,
                'dias_antes_del_corte': self.info.get('dias_antes_del_corte', 0)}


def calcular_fugas(desde: date, hasta: date, almacen_id: int = None) -> dict:
    """Las ocho fugas del rango, ordenadas, con el total del período. Desde el
    corte (`FECHA_INICIO_AUDITORIA`): lo anterior se cuenta en
    `antes_del_corte` de cada fuga y no suma."""
    almacenes = _almacenes()
    ant_desde, ant_hasta = periodo_anterior(desde, hasta)
    rangos = _Rangos(desde, hasta)
    ctx = _Ctx(almacen_id)
    ctx.una_vez('periodo_actual', lambda: (rangos.desde, rangos.hasta))
    fugas = []
    for fuga in FUGAS.values():
        res, res_ant, antes = rangos.evaluar(fuga, ctx)
        r = _resumen_fuga(fuga, res, res_ant, almacenes, almacen_id)
        r['antes_del_corte'] = antes
        fugas.append(r)
    fugas = ordenar(fugas)

    suman = [f for f in fugas if f['aporte_al_total'] is not None]
    fuera = [{'clave': f['clave'], 'titulo': f['titulo'], 'por': f['fuera_del_total_por'],
              'casos': f['casos']}
             for f in fugas if f['aporte_al_total'] is None and (f['casos'] or f['sin_dato'])]
    total = round(sum(f['aporte_al_total'] for f in suman), 2)
    total_ant = 0.0
    for f in suman:
        pa = f['tendencia']['anterior']['pesos']
        total_ant += max(0.0, pa) if pa is not None else 0.0
    ahora = datetime.utcnow()
    fuentes = _fuentes(desde, hasta)
    return {
        'meta': {
            'desde': desde.isoformat(), 'hasta': hasta.isoformat(),
            'almacen_id': almacen_id,
            'almacen': _nombre_almacen(almacenes, almacen_id) if almacen_id else 'Todos',
            'periodo_anterior': {'desde': ant_desde.isoformat(), 'hasta': ant_hasta.isoformat()},
            'calculado_en': ahora.isoformat() + 'Z',
            'completa': all(v.get('completa', True) for v in fuentes.values()),
            'fuentes': fuentes,
            'corte': rangos.meta(),
        },
        'resumen': {
            'total_pesos': total,
            'total_anterior_pesos': round(total_ant, 2),
            'fugas_que_suman': len(suman),
            'fugas_fuera_del_total': len(fuera),
            'fuera_del_total': fuera,
            'es_cota_inferior': bool(fuera) or any(f['es_cota_inferior'] for f in suman),
            'casos': sum(f['casos'] or 0 for f in fugas),
            'nota': ('Solo suma las fugas con valor conocido. Las que no tienen '
                     'precio o costo quedan fuera y se cuentan aparte: no saber '
                     'cuánto valen no las vuelve cero.'),
        },
        'fugas': fugas,
    }


def detalle_fuga(clave: str, desde: date, hasta: date, almacen_id: int = None,
                 page: int = 1, per_page: int = 50) -> dict:
    """Una fuga con sus casos paginados: los sin valor primero (Regla 0),
    después por pesos de mayor a menor. `pedido_clave` va en cada fila para
    el recorrido del pedido."""
    if clave not in FUGAS:
        raise KeyError(clave)
    fuga = FUGAS[clave]
    almacenes = _almacenes()
    rangos = _Rangos(desde, hasta)
    ctx = _Ctx(almacen_id)
    ctx.una_vez('periodo_actual', lambda: (rangos.desde, rangos.hasta))
    res, res_ant, antes = rangos.evaluar(fuga, ctx)
    resumen = _resumen_fuga(fuga, res, res_ant, almacenes, almacen_id)
    resumen['antes_del_corte'] = antes
    casos = sorted(res.casos, key=lambda c: (c.pesos is not None, -(c.pesos or 0),
                                             -(c.unidades or 0)))
    per_page = max(1, min(int(per_page), POR_PAGINA_MAX))
    page = max(1, int(page))
    ini = (page - 1) * per_page
    return {
        'meta': {'desde': desde.isoformat(), 'hasta': hasta.isoformat(),
                 'almacen_id': almacen_id,
                 'calculado_en': datetime.utcnow().isoformat() + 'Z',
                 'completa': not res.faltan and not res.sin_dato,
                 'fuentes': _fuentes(desde, hasta),
                 'corte': rangos.meta()},
        'fuga': resumen,
        'casos': [c.to_dict(almacenes) for c in casos[ini:ini + per_page]],
        'total': len(casos),
        'pagina': page,
        'por_pagina': per_page,
    }


__all__ = ['CLAVES', 'FUGAS', 'calcular_fugas', 'detalle_fuga', 'periodo_anterior',
           'totales', 'tendencia', 'ordenar', 'aporte_al_total', 'valor_de_job',
           'bodegas_de_limbo']
