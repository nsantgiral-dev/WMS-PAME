"""
Analítica — 🧭 Recorrido del pedido (Fase 1, 2026-09-24).

La pregunta que contesta: **de lo que Siesa aprobó, cuánto se volvió caja
liquidada, en cuánto tiempo, y dónde se cayó lo que no llegó.** Es el embudo
pedido → caja de la tesis de la Ley 1116.

**Una política, una función.** Cada cifra se define UNA vez acá; las tres
rutas (`routes/analitica_recorrido.py`) y la pantalla leen de lo mismo.
`_evaluar()` decide, pedido por pedido, hasta qué etapa llegó, con qué marca
de tiempo y si se cayó. Todo lo demás son sumas sobre eso.

## Las siete etapas y su evidencia

| Etapa | Evidencia (lo único que la prueba) | Marca de tiempo |
|---|---|---|
| aprobado   | La línea se vio en Siesa (`pedidos_historia`, o `pedidos_siesa`) | `primera_vez_vista_at` |
| recogido   | Todas las tareas de picking vivas del pedido COMPLETADAS | la última `fecha_completado` |
| empacado   | Un empaque vivo VERIFICADO o DESPACHADO | `fecha_verificado` |
| despachado | Empaque DESPACHADO / con remisión | `fecha_despachado` |
| entregado  | Parada confirmada ENTREGADO, PARCIAL o ENTREGADO_SIN_PAGO | `fecha_confirmacion` del recaudo |
| cobrado    | Dinero en la puerta (`monto_cobrado` > 0) o RC enviado/saldado; a crédito pasa sin marca | `fecha_confirmacion` / `siesa_rc_at` |
| liquidado  | La ruta del pedido LIQUIDADA | `rutas_despacho.liquidada_en` |

Las etapas son **acumulativas**: un pedido que tiene evidencia de «entregado»
pasó por las anteriores aunque una no dejó marca (el empaque manual no tiene
picking; una ruta liquidada antes de `m035bitacora` no tiene fecha). Esa etapa
cuenta como alcanzada **sin marca**: entra al conteo y queda fuera de los
tiempos, declarada en `sin_marca`. Nunca se le inventa una hora.

## La unión es por `pedido_clave`, y lo que no la tiene se ve

`cadena_pedido.clave_pedido` es la única clave. Un empaque, un picking o una
línea de historia sin clave **no se une por texto** (eso era lo que la clave
vino a reemplazar): se cuenta en `sin_enlazar`, con su propio mini embudo para
los empaques —que sí se pueden seguir hacia adelante—, y nunca se descarta.

## Cohorte

El rango de fechas es por el día (Bogotá) en que el pedido **entró**: la
primera vez que el WMS lo vio, en Siesa (`pedidos_historia`) o, si la historia
todavía no existía, en su primer registro del WMS. Cuál de las dos fue queda
en cada pedido (`entrada_fuente`). Un pedido pendiente en Siesa sin historia
ni registro WMS no tiene día de entrada: se cuenta en `meta` y no se asigna a
ningún rango.

## Estado final del pedido

`COMPLETO_SIN_FUGA` · `COMPLETO_CON_FUGA` (liquidado, pero algo se perdió en el
camino: recogido incompleto, entrega parcial, nota crédito, devolución) ·
`FUGA` (se cayó y no sigue) · `DETENIDO` (esperando a un humano: un bloqueo de
picking) · `EN_CURSO` · `FUERA_DEL_WMS` (salió de pendientes en Siesa sin un
solo registro en el WMS — no es una fuga de caja que el WMS pueda medir).

## La métrica guía

- **Ciclo de caja**: días entre aprobado y liquidado, para los pedidos que
  llegaron a liquidado con las dos marcas. Mediana, p90 y `n`.
- **% del valor que completa el ciclo sin fuga**: valor aprobado de los
  `COMPLETO_SIN_FUGA` / valor aprobado de la cohorte con valor (sin
  `FUERA_DEL_WMS`). Viene con su gemelo sobre los pedidos ya **cerrados**
  (completos + caídos), porque los que siguen en camino bajan la primera cifra
  sin ser fugas.

El valor es el que Siesa declara en el pedido (`pedidos_historia.vlr_neto`,
suma de las líneas). Si una línea no lo trae, el pedido queda **sin valor**
—no con valor parcial— y se cuenta aparte.

Cero llamadas a Siesa: todo sale de la base del WMS.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.extensions import db
from app.utils.fecha import dia_operativo, dia_operativo_de, rango_dia_operativo_utc

ETAPAS = ('aprobado', 'recogido', 'empacado', 'despachado', 'entregado', 'cobrado',
          'liquidado')

TITULOS = {
    'aprobado':   'Aprobado en Siesa',
    'recogido':   'Recogido en bodega',
    'empacado':   'Empacado',
    'despachado': 'Despachado',
    'entregado':  'Entregado al cliente',
    'cobrado':    'Cobrado',
    'liquidado':  'Liquidado',
}


class EstadoFinal:
    COMPLETO_SIN_FUGA = 'COMPLETO_SIN_FUGA'
    COMPLETO_CON_FUGA = 'COMPLETO_CON_FUGA'
    FUGA = 'FUGA'
    DETENIDO = 'DETENIDO'
    EN_CURSO = 'EN_CURSO'
    FUERA_DEL_WMS = 'FUERA_DEL_WMS'
    TODOS = (COMPLETO_SIN_FUGA, COMPLETO_CON_FUGA, FUGA, DETENIDO, EN_CURSO, FUERA_DEL_WMS)
    #: Los que ya no se mueven: sobre estos la tasa no la baja lo que va en camino.
    CERRADOS = (COMPLETO_SIN_FUGA, COMPLETO_CON_FUGA, FUGA)


TITULOS_ESTADO = {
    EstadoFinal.COMPLETO_SIN_FUGA: 'Completó el ciclo sin pérdidas',
    EstadoFinal.COMPLETO_CON_FUGA: 'Completó con pérdida parcial',
    EstadoFinal.FUGA: 'Se cayó en el camino',
    EstadoFinal.DETENIDO: 'Detenido esperando decisión',
    EstadoFinal.EN_CURSO: 'En camino',
    EstadoFinal.FUERA_DEL_WMS: 'Salió por fuera del WMS',
}

#: Motivo de bloqueo del picking → palabras de bodega.
_BLOQUEOS = {
    'UBICACION_VACIA': 'Ubicación vacía',
    'FALTANTE': 'Faltante en la ubicación',
    'MERCANCIA_AVERIADA': 'Mercancía averiada',
    'PRODUCTO_INCORRECTO': 'Producto incorrecto en la ubicación',
    'BACKORDER_SIESA': 'Sin existencia comprometible en Siesa',
}

SIN_MOTIVO = 'sin motivo registrado'
PREFIJO_SIN_CLAVE = 'SIN-CLAVE-PK'
DIAS_POR_DEFECTO = 30
MAX_DIAS_RANGO = 366
_CHUNK = 500


# ─────────────────────────────────────────────────────────────────────────────
# Estadística — una definición de mediana y de p90 para todo el módulo
# ─────────────────────────────────────────────────────────────────────────────

def percentil(valores, p):
    """Percentil por rango más cercano (`p` en 0–100). `None` si no hay datos.

    Rango más cercano y no interpolado a propósito: con n chico (lo normal hoy)
    el p90 es un pedido real que se puede abrir, no un punto entre dos.
    """
    xs = sorted(v for v in valores if v is not None)
    if not xs:
        return None
    k = max(1, -(-len(xs) * p // 100))   # techo sin float
    return xs[int(k) - 1]


def mediana(valores):
    xs = sorted(v for v in valores if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def resumen_tiempos(horas, sin_marca=0, negativos=0):
    """`{n, mediana_horas, p90_horas, sin_marca, negativos}` — la forma única
    de un tiempo entre dos marcas. `n = 0` → mediana y p90 `None`, no 0."""
    xs = [h for h in horas if h is not None]
    return {
        'n': len(xs),
        'mediana_horas': _r(mediana(xs)),
        'p90_horas': _r(percentil(xs, 90)),
        'sin_marca': sin_marca,
        'negativos': negativos,
    }


def tasa(num, den):
    """`{tasa, num, n}`. Con denominador 0 la tasa es `None` («—»), no 0."""
    return {'tasa': (round(num / den, 4) if den else None), 'num': num, 'n': den}


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def _horas(a, b):
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


# ─────────────────────────────────────────────────────────────────────────────
# Filtros comunes
# ─────────────────────────────────────────────────────────────────────────────

class FiltroInvalido(ValueError):
    """Un parámetro que no se puede interpretar. Las rutas lo vuelven 400."""


def filtros_de(args) -> dict:
    """`{desde, hasta, almacen_id}` desde los query args. **400, nunca ignorado.**

    Por defecto los últimos 30 días Bogotá (hoy incluido). `hasta < desde`,
    una fecha que no es `YYYY-MM-DD`, un almacén que no es entero o un rango
    de más de un año son `FiltroInvalido`.
    """
    from datetime import date

    def _fecha(nombre):
        v = (args.get(nombre) or '').strip()
        if not v:
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            raise FiltroInvalido(f'{nombre} debe ser YYYY-MM-DD (llegó {v!r})')

    hasta = _fecha('hasta') or dia_operativo()
    desde = _fecha('desde') or (hasta - timedelta(days=DIAS_POR_DEFECTO - 1))
    if hasta < desde:
        raise FiltroInvalido('hasta no puede ser anterior a desde')
    if (hasta - desde).days + 1 > MAX_DIAS_RANGO:
        raise FiltroInvalido(f'el rango no puede pasar de {MAX_DIAS_RANGO} días')
    alm = (args.get('almacen_id') or '').strip()
    if alm:
        if not alm.isdigit():
            raise FiltroInvalido(f'almacen_id debe ser un número (llegó {alm!r})')
        alm = int(alm)
    else:
        alm = None
    return {'desde': desde, 'hasta': hasta, 'almacen_id': alm}


# ─────────────────────────────────────────────────────────────────────────────
# El pedido, reconstruido
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Pedido:
    clave: str
    enlazado: bool = True
    historia: list = field(default_factory=list)
    en_pendientes_siesa: bool = False
    pickings: list = field(default_factory=list)
    packings: list = field(default_factory=list)
    recaudos: list = field(default_factory=list)
    rutas: dict = field(default_factory=dict)
    devoluciones: list = field(default_factory=list)
    motivos_bitacora: dict = field(default_factory=dict)

    # Lo que calcula `_evaluar`
    entrada_at: datetime = None
    entrada_fuente: str = None
    almacen_id: int = None
    cliente: str = None
    valor: Decimal = None
    valor_motivo: str = None
    marcas: dict = field(default_factory=dict)
    ultimo: int = 0
    implicitas: set = field(default_factory=set)
    fuga: dict = None
    detenido: dict = None
    parciales: list = field(default_factory=list)
    fuera_del_wms: str = None
    a_credito: bool = False
    #: Contado contraentrega registrado sin cobro y sin autorización.
    credito_no_autorizado: bool = False
    valor_facturado: Decimal = None
    monto_cobrado: Decimal = None

    @property
    def estado_final(self):
        if self.fuera_del_wms:
            return EstadoFinal.FUERA_DEL_WMS
        if self.fuga:
            return EstadoFinal.FUGA
        if self.ultimo == len(ETAPAS) - 1:
            return (EstadoFinal.COMPLETO_CON_FUGA if self.parciales
                    else EstadoFinal.COMPLETO_SIN_FUGA)
        if self.detenido:
            return EstadoFinal.DETENIDO
        return EstadoFinal.EN_CURSO

    @property
    def numero(self):
        """`'PD1502'` para mostrar. El pedido sin clave muestra su empaque."""
        if not self.enlazado:
            pk = self.packings[0] if self.packings else None
            return (pk.numero_pedido_siesa or pk.codigo) if pk else self.clave
        partes = self.clave.split('-')
        return f'{partes[1]}{partes[2]}' if len(partes) == 3 else self.clave

    @property
    def co(self):
        partes = (self.clave or '').split('-')
        return partes[0] if self.enlazado and len(partes) == 3 else None


def _en_trozos(ids):
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def _motivo_bitacora(p, entidad, ids, accion):
    for i in ids:
        for (acc, motivo) in p.motivos_bitacora.get((entidad, i), []):
            if acc == accion and motivo:
                return motivo
    return None


def _evaluar(p: Pedido, almacen_de_bodega: dict):
    """Hasta dónde llegó el pedido, con qué marcas, y si se cayó. **La única
    que lo decide.** Ver la tabla del encabezado del módulo."""
    from app.models.packing import EstadoPacking
    from app.models.picking import EstadoPicking
    from app.models.recaudo_entrega import EstadoEntrega
    from app.models.ruta_despacho import EstadoFinancieroRuta
    from app.services import motivos_rechazo
    from app.services import cond_pago as _cp_trato

    # ── Aprobado ──
    t_siesa = min((h.primera_vez_vista_at for h in p.historia
                   if h.primera_vez_vista_at), default=None)
    t_wms = min([t for t in [pk.fecha_creacion for pk in p.pickings] +
                 [pk.fecha_creacion for pk in p.packings] if t], default=None)
    candidatos = [t for t in (t_siesa, t_wms) if t]
    p.entrada_at = min(candidatos) if candidatos else None
    p.entrada_fuente = ('SIESA' if t_siesa and (not t_wms or t_siesa <= t_wms)
                        else ('WMS' if t_wms else None))
    # Si la historia empezó a escribirse DESPUÉS de que el WMS ya trabajaba el
    # pedido, su «primera vez» no es la aprobación: no se usa como marca.
    marcas = {'aprobado': t_siesa if p.entrada_fuente == 'SIESA' else None}
    if marcas['aprobado'] is None:
        p.implicitas.add('aprobado')

    # ── Cliente, almacén, valor ──
    p.cliente = next((h.cliente for h in p.historia if h.cliente), None) or \
        next((pk.cliente for pk in p.packings if pk.cliente), None)
    p.almacen_id = next((x.almacen_id for x in p.packings + p.pickings if x.almacen_id), None)
    if p.almacen_id is None:
        p.almacen_id = next((almacen_de_bodega.get((h.bodega or '').strip().upper())
                             for h in p.historia if h.bodega), None)
    if not p.historia:
        p.valor, p.valor_motivo = None, 'sin historia del pedido en Siesa'
    elif any(h.vlr_neto is None for h in p.historia):
        p.valor, p.valor_motivo = None, 'alguna línea del pedido no trae valor'
    else:
        p.valor = sum((Decimal(h.vlr_neto) for h in p.historia), Decimal('0'))

    # ── Recogido ──
    vivas = [t for t in p.pickings if t.estado != EstadoPicking.CANCELADO]
    canceladas = [t for t in p.pickings if t.estado == EstadoPicking.CANCELADO]
    rec_ok = bool(vivas) and all(t.estado == EstadoPicking.COMPLETADO for t in vivas)
    marcas['recogido'] = (max((t.fecha_completado for t in vivas if t.fecha_completado),
                              default=None) if rec_ok else None)

    # ── Empacado / despachado ──
    pk_vivos = [x for x in p.packings if x.estado != EstadoPacking.CANCELADO]
    empacados = [x for x in pk_vivos if x.estado in (EstadoPacking.VERIFICADO,
                                                     EstadoPacking.DESPACHADO)
                 or x.fecha_verificado or x.fecha_despachado]
    despachados = [x for x in pk_vivos if x.estado == EstadoPacking.DESPACHADO
                   or x.fecha_despachado or x.rm_consec]
    emp_ok, des_ok = bool(empacados), bool(despachados)
    marcas['empacado'] = min((x.fecha_verificado for x in empacados if x.fecha_verificado),
                             default=None)
    marcas['despachado'] = min((x.fecha_despachado for x in despachados if x.fecha_despachado),
                               default=None)
    facturas = [x.valor_factura for x in despachados]
    p.valor_facturado = (sum((Decimal(v) for v in facturas), Decimal('0'))
                         if facturas and all(v is not None for v in facturas) else None)

    # ── Entregado / cobrado / liquidado — el último desenlace de la parada ──
    ids_vivos = {x.id for x in pk_vivos}
    recaudos = sorted((r for r in p.recaudos if r.tarea_id in ids_vivos),
                      key=lambda r: (r.fecha_confirmacion or datetime.min, r.id))
    rec = recaudos[-1] if recaudos else None
    ent_ok = bool(rec) and rec.estado_entrega in EstadoEntrega.SIN_RETORNO
    marcas['entregado'] = rec.fecha_confirmacion if ent_ok else None

    cob_ok = False
    marcas['cobrado'] = None
    if rec is not None and rec.estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL):
        monto = Decimal(rec.monto_cobrado or 0)
        p.monto_cobrado = monto
        # Por la política (`cond_pago.trato_de_cobro`), no por `forma_pago`:
        # un CREDITO sobre una factura de contado contraentrega NO es «cobro
        # ok» — es plata que nadie cobró. Solo el crédito real o autorizado
        # pasa a cartera.
        _trato = _cp_trato.trato_de_cobro(rec, next(
            (x for x in pk_vivos if x.id == rec.tarea_id), None))
        if _trato == _cp_trato.TRATO_CREDITO:
            cob_ok, p.a_credito = True, True          # pasa a cartera: sin marca
        elif _trato == _cp_trato.TRATO_NO_AUTORIZADO:
            p.credito_no_autorizado = True
        elif monto > 0:
            cob_ok, marcas['cobrado'] = True, rec.fecha_confirmacion
        elif rec.siesa_rc_resultado in ('ENVIADO', 'YA_SALDADA'):
            cob_ok, marcas['cobrado'] = True, rec.siesa_rc_at

    ruta = p.rutas.get(rec.ruta_id) if rec is not None else None
    if ruta is None:
        # Sin parada confirmada, la ruta sale de los bultos (para la línea de tiempo).
        ruta = next(iter(p.rutas.values()), None) if p.rutas else None
    liq_ok = bool(rec) and ruta is not None and (
        ruta.liquidada_en is not None
        or ruta.estado_financiero == EstadoFinancieroRuta.LIQUIDADA)
    marcas['liquidado'] = ruta.liquidada_en if liq_ok else None

    evidencia = {'aprobado': True, 'recogido': rec_ok, 'empacado': emp_ok,
                 'despachado': des_ok, 'entregado': ent_ok, 'cobrado': cob_ok,
                 'liquidado': liq_ok}
    ultimo = max(i for i, e in enumerate(ETAPAS) if evidencia[e])

    # ── Fugas que cortan el recorrido ──
    cortes = []
    if p.pickings and not vivas and not pk_vivos:
        motivo = _motivo_bitacora(p, 'TareaPicking', [t.id for t in canceladas], 'CANCELAR')
        cortes.append((1, 'PICKING_CANCELADO', f'Picking cancelado: {motivo or SIN_MOTIVO}'))
    if p.packings and not pk_vivos:
        motivo = _motivo_bitacora(p, 'TareaPacking', [x.id for x in p.packings], 'CANCELAR')
        cortes.append((2, 'EMPAQUE_CANCELADO', f'Empaque cancelado: {motivo or SIN_MOTIVO}'))
    if rec is not None and rec.estado_entrega == EstadoEntrega.RECHAZADO:
        cortes.append((4, 'RECHAZO_EN_RUTA',
                       f'Rechazado en ruta: {motivos_rechazo.etiqueta(rec.motivo_rechazo)}'))
    if rec is not None and rec.estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO:
        cortes.append((5, 'ENTREGADO_SIN_PAGO', 'Entregado sin pago: la mercancía se quedó '
                                                 'con el cliente y la factura sigue abierta'))
    if p.credito_no_autorizado:
        cortes.append((5, 'CREDITO_NO_AUTORIZADO',
                       'Factura de contado contraentrega registrada sin cobro '
                       '(crédito que nadie autorizó)'))
    elif (ent_ok and not cob_ok and liq_ok
            and rec.estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL)):
        cortes.append((5, 'LIQUIDADO_SIN_COBRO', 'Ruta liquidada sin cobro registrado '
                                                  'ni venta a crédito'))
    if cortes:
        idx, codigo, motivo = min(cortes)
        ultimo = min(ultimo, idx - 1)
        p.fuga = {'etapa': ETAPAS[idx], 'codigo': codigo, 'motivo': motivo}

    anulado = any(h.motivo_salida == 'ANULADO' for h in p.historia) or \
        any(x.pedido_anulado_siesa for x in p.packings)
    if not p.fuga and anulado and ultimo < len(ETAPAS) - 1:
        p.fuga = {'etapa': ETAPAS[ultimo + 1], 'codigo': 'ANULADO_EN_SIESA',
                  'motivo': 'Pedido anulado en Siesa'}

    # Salió de pendientes en Siesa sin un solo registro en el WMS.
    if (not p.fuga and not p.pickings and not p.packings and p.historia
            and all(h.motivo_salida for h in p.historia)):
        p.fuera_del_wms = ('Cumplido en Siesa sin pasar por el WMS'
                           if all(h.motivo_salida == 'CUMPLIDO' for h in p.historia)
                           else 'Salió de pendientes en Siesa sin clasificar')

    # ── Detenido: esperando a un humano ──
    if not p.fuga and not p.fuera_del_wms:
        bloq = [t for t in vivas if t.estado == EstadoPicking.BLOQUEADO]
        if bloq and ultimo < 1:
            t = bloq[0]
            m = ('Sin stock para recoger' if t.bloqueo_sin_stock
                 else _BLOQUEOS.get(t.motivo_bloqueo or '', t.motivo_bloqueo or SIN_MOTIVO))
            p.detenido = {'etapa': 'recogido', 'codigo': 'BLOQUEO_PICKING',
                          'motivo': f'Bloqueo de picking: {m}'}
        elif any(x.estado == EstadoPacking.BLOQUEADO for x in pk_vivos) and ultimo < 2:
            p.detenido = {'etapa': 'empacado', 'codigo': 'BLOQUEO_EMPAQUE',
                          'motivo': 'Empaque bloqueado'}

    # ── Fugas parciales: el pedido sigue, con menos ──
    if ultimo >= 1 and p.pickings:
        pedido_por_prod, recogido_por_prod = {}, {}
        for t in p.pickings:
            base = (float(t.cantidad_pedida) if t.cantidad_pedida is not None else None)
            if base is not None:
                pedido_por_prod[t.producto_id] = max(pedido_por_prod.get(t.producto_id, 0), base)
            else:
                pedido_por_prod[t.producto_id] = (pedido_por_prod.get(t.producto_id, 0)
                                                  + (t.cantidad_solicitada or 0))
            if t.estado == EstadoPicking.COMPLETADO:
                recogido_por_prod[t.producto_id] = (recogido_por_prod.get(t.producto_id, 0)
                                                    + (t.cantidad_recogida or 0))
        if any(recogido_por_prod.get(k, 0) < v for k, v in pedido_por_prod.items()):
            p.parciales.append({'etapa': 'recogido', 'codigo': 'RECOGIDO_INCOMPLETO',
                                'motivo': 'Recogido incompleto'})
    if rec is not None and ent_ok and rec.estado_entrega == EstadoEntrega.PARCIAL:
        p.parciales.append({'etapa': 'entregado', 'codigo': 'ENTREGA_PARCIAL',
                            'motivo': 'Entrega parcial (nota crédito)'})
    elif rec is not None and ent_ok and rec.siesa_nc_resultado:
        p.parciales.append({'etapa': 'entregado', 'codigo': 'NOTA_CREDITO',
                            'motivo': 'Nota crédito sobre la factura'})
    for d in p.devoluciones:
        if d.estado == 'CONFIRMADA' and ent_ok:
            p.parciales.append({'etapa': 'entregado', 'codigo': 'DEVOLUCION_CLIENTE',
                                'motivo': ('Devolución total del cliente' if d.es_total
                                           else 'Devolución parcial del cliente')})
            break

    for i, e in enumerate(ETAPAS):
        if i <= ultimo and marcas.get(e) is None:
            p.implicitas.add(e)
    p.marcas = {e: (marcas.get(e) if i <= ultimo else None) for i, e in enumerate(ETAPAS)}
    p.ultimo = ultimo
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Carga
# ─────────────────────────────────────────────────────────────────────────────

def _almacen_de_bodega():
    from app.models.almacen import Almacen
    return {(a.bodega_siesa_id or '').strip().upper(): a.id
            for a in Almacen.query.all() if a.bodega_siesa_id}


def _entradas():
    """`{clave: (t_siesa, t_wms)}` de todo pedido con clave que el WMS conoce."""
    from sqlalchemy import func
    from app.models.packing import TareaPacking
    from app.models.pedido_historia import PedidoHistoria
    from app.models.picking import TareaPicking
    from app.services.cadena_pedido import TIPOS_DE_PEDIDO

    e = {}
    for clave, t in (db.session.query(PedidoHistoria.pedido_clave,
                                      func.min(PedidoHistoria.primera_vez_vista_at))
                     .filter(PedidoHistoria.pedido_clave.isnot(None))
                     .group_by(PedidoHistoria.pedido_clave)):
        e[clave] = [t, None]
    for modelo, filtro in ((TareaPicking, TareaPicking.tipo_documento.in_(TIPOS_DE_PEDIDO)),
                           (TareaPacking, TareaPacking.tipo_documento.in_(TIPOS_DE_PEDIDO))):
        for clave, t in (db.session.query(modelo.pedido_clave, func.min(modelo.fecha_creacion))
                         .filter(modelo.pedido_clave.isnot(None), filtro)
                         .group_by(modelo.pedido_clave)):
            par = e.setdefault(clave, [None, None])
            par[1] = min([x for x in (par[1], t) if x], default=None)
    return e


def _cargar(pedidos: dict):
    """Llena cada `Pedido` con sus filas. Consultas por lotes, sin N+1."""
    from app.models.bitacora import BitacoraAccion
    from app.models.bulto import Bulto
    from app.models.devolucion_cliente import DevolucionCliente
    from app.models.packing import TareaPacking
    from app.models.pedido_historia import PedidoHistoria
    from app.models.picking import TareaPicking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho

    claves = [c for c, p in pedidos.items() if p.enlazado]
    for trozo in _en_trozos(claves):
        for h in PedidoHistoria.query.filter(PedidoHistoria.pedido_clave.in_(trozo)):
            pedidos[h.pedido_clave].historia.append(h)
        for t in TareaPicking.query.filter(TareaPicking.pedido_clave.in_(trozo)):
            pedidos[t.pedido_clave].pickings.append(t)
        for x in TareaPacking.query.filter(TareaPacking.pedido_clave.in_(trozo)):
            pedidos[x.pedido_clave].packings.append(x)

    pk_a_pedido = {x.id: p for p in pedidos.values() for x in p.packings}
    ruta_ids = set()
    for trozo in _en_trozos(pk_a_pedido):
        for r in RecaudoEntrega.query.filter(RecaudoEntrega.tarea_id.in_(trozo)):
            pk_a_pedido[r.tarea_id].recaudos.append(r)
            ruta_ids.add(r.ruta_id)
        for b in Bulto.query.filter(Bulto.tarea_id.in_(trozo)):
            if b.ruta_despacho_id:
                pk_a_pedido[b.tarea_id].rutas.setdefault(b.ruta_despacho_id, None)
                ruta_ids.add(b.ruta_despacho_id)
        for d in DevolucionCliente.query.filter(DevolucionCliente.tarea_packing_id.in_(trozo)):
            pk_a_pedido[d.tarea_packing_id].devoluciones.append(d)
    rutas = {}
    for trozo in _en_trozos(ruta_ids):
        for r in RutaDespacho.query.filter(RutaDespacho.id.in_(trozo)):
            rutas[r.id] = r
    for p in pedidos.values():
        ids = set(p.rutas) | {r.ruta_id for r in p.recaudos}
        p.rutas = {i: rutas[i] for i in ids if i in rutas}

    # Motivos de cancelación de la bitácora (picking y empaque).
    por_entidad = {'TareaPicking': {}, 'TareaPacking': {}}
    for p in pedidos.values():
        for t in p.pickings:
            por_entidad['TareaPicking'][t.id] = p
        for x in p.packings:
            por_entidad['TareaPacking'][x.id] = p
    for entidad, mapa in por_entidad.items():
        for trozo in _en_trozos(mapa):
            for a in (BitacoraAccion.query
                      .filter(BitacoraAccion.entidad == entidad,
                              BitacoraAccion.entidad_id.in_(trozo),
                              BitacoraAccion.accion == 'CANCELAR')
                      .order_by(BitacoraAccion.ocurrido_en)):
                mapa[a.entidad_id].motivos_bitacora.setdefault(
                    (entidad, a.entidad_id), []).append((a.accion, a.motivo))


def cohorte(desde, hasta, almacen_id=None):
    """Los pedidos que **entraron** en el rango, evaluados, y lo que no se unió.

    Devuelve `(pedidos, extra)`: `pedidos` incluye los enlazados y los
    empaques sin clave (`enlazado=False`); `extra` trae los conteos que la
    pantalla declara (`sin_fecha_de_entrada`, `fuera_de_alcance`, pickings y
    líneas de historia sin clave).
    """
    from app.models.packing import TareaPacking
    from app.models.pedido_historia import PedidoHistoria
    from app.models.pedido_siesa import PedidoSiesa
    from app.models.picking import TareaPicking
    from app.services.cadena_pedido import TIPOS_DE_PEDIDO, clave_pedido

    from app.services import corte as _corte
    ini, fin = rango_dia_operativo_utc(desde, hasta)
    # Lo que entró antes de FECHA_INICIO_AUDITORIA es del ensayo: se cuenta en
    # `antes_del_corte` y no entra al embudo ni a las fugas.
    corte_utc = _corte.inicio_auditoria()
    desde_utc = max(ini, corte_utc) if corte_utc is not None else ini
    entradas = _entradas()
    pedidos, antes_del_corte = {}, 0
    for clave, (t_siesa, t_wms) in entradas.items():
        t = min([x for x in (t_siesa, t_wms) if x], default=None)
        if t is not None and ini <= t < fin:
            if _corte.es_anterior(t, corte_utc):
                antes_del_corte += 1
                continue
            pedidos[clave] = Pedido(clave=clave)

    # Empaques de pedido sin clave: se siguen hacia adelante, aparte.
    for x in (TareaPacking.query
              .filter(TareaPacking.pedido_clave.is_(None),
                      TareaPacking.tipo_documento.in_(TIPOS_DE_PEDIDO),
                      TareaPacking.fecha_creacion >= desde_utc, TareaPacking.fecha_creacion < fin)):
        pedidos[f'{PREFIJO_SIN_CLAVE}{x.id}'] = Pedido(clave=f'{PREFIJO_SIN_CLAVE}{x.id}',
                                                       enlazado=False, packings=[x])

    pendientes = {}
    for co, tipo, consec in (db.session.query(PedidoSiesa.centro_op, PedidoSiesa.tipo_docto,
                                              PedidoSiesa.consec_docto).distinct()):
        c = clave_pedido(co, tipo, consec)
        if c:
            pendientes[c] = True
    for c, p in pedidos.items():
        p.en_pendientes_siesa = c in pendientes

    _cargar(pedidos)
    almacen_de_bodega = _almacen_de_bodega()
    fuera_de_alcance = 0
    elegidos = []
    for p in pedidos.values():
        _evaluar(p, almacen_de_bodega)
        if p.almacen_id is None and p.enlazado:
            fuera_de_alcance += 1      # su bodega no tiene almacén en el WMS
            continue
        if almacen_id is not None and p.almacen_id != almacen_id:
            continue
        elegidos.append(p)

    def _en_almacen(q, modelo):
        return q.filter(modelo.almacen_id == almacen_id) if almacen_id is not None else q

    picking_sin_clave = _en_almacen(TareaPicking.query.filter(
        TareaPicking.pedido_clave.is_(None),
        TareaPicking.tipo_documento.in_(TIPOS_DE_PEDIDO),
        TareaPicking.fecha_creacion >= desde_utc, TareaPicking.fecha_creacion < fin),
        TareaPicking).count()
    historia_sin_clave = PedidoHistoria.query.filter(
        PedidoHistoria.pedido_clave.is_(None),
        PedidoHistoria.primera_vez_vista_at >= desde_utc,
        PedidoHistoria.primera_vez_vista_at < fin).count()
    extra = {
        'sin_fecha_de_entrada': sum(1 for c in pendientes if c not in entradas),
        'fuera_de_alcance': fuera_de_alcance,
        'picking_sin_clave': picking_sin_clave,
        'historia_sin_clave': historia_sin_clave,
        'antes_del_corte': antes_del_corte,
    }
    return elegidos, extra


# ─────────────────────────────────────────────────────────────────────────────
# Frescura
# ─────────────────────────────────────────────────────────────────────────────

def _estado_corte():
    from app.services import corte
    return corte.estado()


def _meta(filtros, pedidos, extra, ahora):
    from sqlalchemy import func
    from app.models.bitacora import BitacoraAccion
    from app.models.pedido_historia import PedidoHistoria

    hist_desde, hist_ultima = db.session.query(
        func.min(PedidoHistoria.primer_dia_visto),
        func.max(PedidoHistoria.ultima_vez_vista_at)).one()
    bit_desde = db.session.query(func.min(BitacoraAccion.dia_operativo)).scalar()
    enl = [p for p in pedidos if p.enlazado]
    sin_hist = sum(1 for p in enl if p.entrada_fuente != 'SIESA')
    if hist_desde is None:
        h_ok, h_motivo = False, 'La historia de pedidos todavía no tiene filas: la entrada de cada pedido sale del WMS'
    elif hist_desde > filtros['desde']:
        h_ok, h_motivo = False, (f'La historia de pedidos empieza el {hist_desde.isoformat()}: '
                                 'antes de esa fecha la aprobación no tiene marca')
    elif sin_hist:
        h_ok, h_motivo = False, f'{sin_hist} pedido(s) sin marca de aprobación en Siesa'
    else:
        h_ok, h_motivo = True, None
    if bit_desde is None or bit_desde > filtros['desde']:
        b_ok = False
        b_motivo = ('La bitácora de acciones empieza el '
                    f'{bit_desde.isoformat()}' if bit_desde else
                    'La bitácora de acciones no tiene filas') + \
            ': antes, cancelaciones y bloqueos no tienen motivo'
    else:
        b_ok, b_motivo = True, None
    return {
        'desde': filtros['desde'].isoformat(),
        'hasta': filtros['hasta'].isoformat(),
        'almacen_id': filtros['almacen_id'],
        'calculado_en': ahora.isoformat(),
        'corte': ('Día (Bogotá) en que el pedido entró: la primera vez que se vio '
                  'en Siesa, o su primer registro en el WMS si la historia no lo tiene'),
        'fuentes': {
            'historia_pedidos': {
                'nombre': 'Historia de pedidos (Siesa)',
                'completa': h_ok, 'motivo': h_motivo,
                'desde': hist_desde.isoformat() if hist_desde else None,
                'actualizado_en': hist_ultima.isoformat() if hist_ultima else None,
                'pedidos_sin_marca_de_aprobacion': sin_hist,
            },
            'wms': {'nombre': 'Operación del WMS', 'completa': True, 'motivo': None,
                    'actualizado_en': ahora.isoformat()},
            'bitacora': {'nombre': 'Bitácora de acciones', 'completa': b_ok,
                         'motivo': b_motivo,
                         'desde': bit_desde.isoformat() if bit_desde else None},
        },
        'sin_fecha_de_entrada': extra['sin_fecha_de_entrada'],
        'fuera_de_alcance': extra['fuera_de_alcance'],
        # FECHA_INICIO_AUDITORIA: pedidos del rango que entraron antes del
        # corte, fuera del embudo (se cuentan, no se esconden).
        'antes_del_corte': extra.get('antes_del_corte', 0),
        'corte_auditoria': _estado_corte(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Lo que leen las rutas
# ─────────────────────────────────────────────────────────────────────────────

def _suma(ps):
    con = [p for p in ps if p.valor is not None]
    return (float(sum((p.valor for p in con), Decimal('0'))) if con else None), len(ps) - len(con)


def _agrupar_motivos(items):
    """`[(pedido, dict_fuga)]` → `[{codigo, motivo, pedidos, valor, sin_valor}]`."""
    grupos = {}
    for p, f in items:
        g = grupos.setdefault((f['codigo'], f['motivo']), [])
        g.append(p)
    out = []
    for (codigo, motivo), ps in grupos.items():
        valor, sin_valor = _suma(ps)
        out.append({'codigo': codigo, 'motivo': motivo, 'pedidos': len(ps),
                    'valor': valor, 'sin_valor': sin_valor})
    return sorted(out, key=lambda g: (-g['pedidos'], g['motivo']))


def _embudo(enl):
    n0 = len(enl)
    etapas = []
    previos = None
    for i, e in enumerate(ETAPAS):
        llegaron = [p for p in enl if p.ultimo >= i]
        valor, sin_valor = _suma(llegaron)
        en_etapa = [p for p in llegaron if p.ultimo == i
                    and p.estado_final in (EstadoFinal.EN_CURSO, EstadoFinal.DETENIDO)]
        horas, neg = [], 0
        if i > 0:
            for p in llegaron:
                h = _horas(p.marcas.get(ETAPAS[i - 1]), p.marcas.get(e))
                if h is None:
                    continue
                if h < 0:
                    neg += 1
                else:
                    horas.append(h)
        sin_marca = sum(1 for p in llegaron if e in p.implicitas)
        etapas.append({
            'etapa': e,
            'titulo': TITULOS[e],
            'pedidos': len(llegaron),
            'valor': valor,
            'sin_valor': sin_valor,
            'sin_marca': sin_marca,
            'en_etapa': len(en_etapa),
            'conversion_anterior': (tasa(len(llegaron), len(previos)) if previos is not None
                                    else None),
            'conversion_inicio': tasa(len(llegaron), n0),
            'tiempo_desde_anterior': (resumen_tiempos(
                horas, sin_marca=len(llegaron) - len(horas) - neg, negativos=neg)
                if i > 0 else None),
            'fugas': _agrupar_motivos([(p, p.fuga) for p in enl
                                       if p.fuga and p.fuga['etapa'] == e]),
            'detenidos': _agrupar_motivos([(p, p.detenido) for p in enl
                                           if p.detenido and p.detenido['etapa'] == e
                                           and p.estado_final == EstadoFinal.DETENIDO]),
            'perdidas_parciales': _agrupar_motivos([(p, f) for p in enl for f in p.parciales
                                                    if f['etapa'] == e]),
        })
        previos = llegaron
    # Lo que se realizó en plata, donde el WMS lo tiene.
    desp = [p for p in enl if p.ultimo >= ETAPAS.index('despachado')]
    fac = [p.valor_facturado for p in desp if p.valor_facturado is not None]
    etapas[ETAPAS.index('despachado')]['valor_facturado'] = {
        'valor': float(sum(fac)) if fac else None, 'n': len(fac), 'de': len(desp)}
    cob = [p for p in enl if p.ultimo >= ETAPAS.index('cobrado')]
    dinero = [p.monto_cobrado for p in cob if not p.a_credito and p.monto_cobrado is not None]
    etapas[ETAPAS.index('cobrado')]['cobrado_en_puerta'] = {
        'valor': float(sum(dinero)) if dinero else None, 'n': len(dinero), 'de': len(cob),
        'a_credito': sum(1 for p in cob if p.a_credito)}
    return etapas


def _guia(enl):
    liq = [p for p in enl if p.ultimo == len(ETAPAS) - 1 and not p.fuga]
    dias, neg = [], 0
    for p in liq:
        h = _horas(p.marcas.get('aprobado'), p.marcas.get('liquidado'))
        if h is None:
            continue
        if h < 0:
            neg += 1
        else:
            dias.append(h / 24.0)
    ciclo = {
        'n': len(dias),
        'mediana_dias': _r(mediana(dias)),
        'p90_dias': _r(percentil(dias, 90)),
        'liquidados': len(liq),
        'sin_marca': len(liq) - len(dias) - neg,
        'negativos': neg,
    }
    base = [p for p in enl if p.estado_final != EstadoFinal.FUERA_DEL_WMS]
    con_valor = [p for p in base if p.valor is not None]
    den = float(sum((p.valor for p in con_valor), Decimal('0')))
    num = float(sum((p.valor for p in con_valor
                     if p.estado_final == EstadoFinal.COMPLETO_SIN_FUGA), Decimal('0')))
    cerr = [p for p in con_valor if p.estado_final in EstadoFinal.CERRADOS]
    den_c = float(sum((p.valor for p in cerr), Decimal('0')))
    composicion = []
    for est in EstadoFinal.TODOS:
        ps = [p for p in enl if p.estado_final == est]
        valor, sin_valor = _suma(ps)
        composicion.append({'estado': est, 'titulo': TITULOS_ESTADO[est],
                            'pedidos': len(ps), 'valor': valor, 'sin_valor': sin_valor})
    return {
        'ciclo_caja': ciclo,
        'valor_sin_fuga': {
            'tasa': round(num / den, 4) if den > 0 else None,
            'valor_sin_fuga': num,
            'valor_base': den if con_valor else None,
            'pedidos_base': len(con_valor),
            'pedidos_sin_valor': len(base) - len(con_valor),
            'pedidos_fuera_del_wms': len(enl) - len(base),
        },
        'valor_sin_fuga_cerrados': {
            'tasa': round(num / den_c, 4) if den_c > 0 else None,
            'valor_base': den_c if cerr else None,
            'pedidos_base': len(cerr),
        },
        'composicion': composicion,
    }


def _sin_enlazar(sueltos, extra):
    embudo = []
    for i, e in enumerate(ETAPAS):
        if i < ETAPAS.index('empacado'):
            continue
        embudo.append({'etapa': e, 'titulo': TITULOS[e],
                       'pedidos': sum(1 for p in sueltos if p.ultimo >= i)})
    fac = [p.valor_facturado for p in sueltos if p.valor_facturado is not None]
    return {
        'empaques': len(sueltos),
        'embudo': embudo,
        'valor_facturado': float(sum(fac)) if fac else None,
        'valor_facturado_n': len(fac),
        'picking_sin_clave': extra['picking_sin_clave'],
        'historia_sin_clave': extra['historia_sin_clave'],
        'muestra': [_fila(p, p.ultimo, None) for p in sueltos[:50]],
        'que_significa': ('Registros de pedido sin la clave CO-tipo-consecutivo: no se '
                          'pueden unir al resto de su cadena sin adivinar por texto. Se '
                          'cuentan aquí y no entran al embudo principal.'),
    }


def recorrido(filtros: dict, ahora: datetime = None) -> dict:
    """El embudo, la métrica guía y lo que no se pudo unir."""
    ahora = ahora or datetime.utcnow()
    pedidos, extra = cohorte(filtros['desde'], filtros['hasta'], filtros['almacen_id'])
    enl = [p for p in pedidos if p.enlazado]
    sueltos = sorted((p for p in pedidos if not p.enlazado),
                     key=lambda p: p.entrada_at or datetime.min, reverse=True)
    return {
        'pedidos': len(enl),
        'embudo': _embudo(enl),
        'guia': _guia(enl),
        'sin_enlazar': _sin_enlazar(sueltos, extra),
        'definiciones': DEFINICIONES,
        'meta': _meta(filtros, pedidos, extra, ahora),
    }


DEFINICIONES = {
    'embudo': ('Pedidos que entraron en el rango y llegaron a cada etapa. Una etapa '
               'cuenta si hay evidencia de ella o de una posterior; sin marca de hora '
               'entra al conteo y no a los tiempos.'),
    'valor': ('Valor que Siesa declara en el pedido (suma de sus líneas). Un pedido con '
              'una línea sin valor queda «sin valor» y no se suma.'),
    'ciclo_caja': ('Días entre la aprobación (primera vez visto en Siesa) y la '
                   'liquidación de su ruta, en los pedidos que llegaron a liquidado '
                   'sin caerse y tienen las dos marcas.'),
    'valor_sin_fuga': ('Valor de los pedidos liquidados sin ninguna pérdida (todo '
                       'recogido, entregado completo, sin nota crédito ni devolución) sobre '
                       'el valor de todos los pedidos de la cohorte con valor, sin contar '
                       'los que salieron por fuera del WMS.'),
    'valor_sin_fuga_cerrados': ('La misma tasa, pero solo sobre los pedidos que ya no se '
                                'mueven (completos o caídos): lo que va en camino no la baja.'),
    'tiempos': ('Mediana y percentil 90 (rango más cercano) de las horas entre la marca de '
                'la etapa anterior y la de esta, con n. Negativos: marcas en orden '
                'imposible, fuera del cálculo.'),
}


def _dias_en_etapa(p, i, ahora):
    ini = p.marcas.get(ETAPAS[i]) or (p.entrada_at if i == 0 else None)
    if ini is None:
        return None
    fin = p.marcas.get(ETAPAS[i + 1]) if p.ultimo > i and i + 1 < len(ETAPAS) else None
    if p.ultimo > i and fin is None:
        return None               # pasó a la siguiente sin marca: no se sabe cuánto
    return _r(((fin or ahora) - ini).total_seconds() / 86400.0, 1)


def _fila(p, i, ahora):
    motivo = (p.fuga or p.detenido or {}).get('motivo') or p.fuera_del_wms
    return {
        'clave': p.clave,
        'enlazado': p.enlazado,
        'numero': p.numero,
        'co': p.co,
        'cliente': p.cliente,
        'almacen_id': p.almacen_id,
        'valor': float(p.valor) if p.valor is not None else None,
        'valor_motivo': p.valor_motivo,
        'etapa_actual': ETAPAS[p.ultimo],
        'etapa_actual_titulo': TITULOS[ETAPAS[p.ultimo]],
        'estado': p.estado_final,
        'estado_titulo': TITULOS_ESTADO[p.estado_final],
        'motivo': motivo,
        'perdidas_parciales': [f['motivo'] for f in p.parciales],
        'a_credito': p.a_credito,
        'entrada': p.entrada_at.isoformat() if p.entrada_at else None,
        'entrada_fuente': p.entrada_fuente,
        'dias_en_etapa': _dias_en_etapa(p, i, ahora) if ahora else None,
    }


VISTAS_ETAPA = ('en', 'llegaron', 'fuga')


def pedidos_de_etapa(etapa: str, filtros: dict, vista: str = 'en', pagina: int = 1,
                     por_pagina: int = 50, ahora: datetime = None) -> dict:
    """La lista detrás de una barra del embudo.

    `vista`: `en` = llegaron a esta etapa y todavía no pasan a la siguiente
    (en camino o detenidos) · `llegaron` = todos los que la alcanzaron ·
    `fuga` = se cayeron al intentar llegar a esta etapa (o se detuvieron acá).
    Orden: más días en la etapa primero.
    """
    if etapa not in ETAPAS:
        raise FiltroInvalido(f'etapa desconocida: {etapa!r}')
    if vista not in VISTAS_ETAPA:
        raise FiltroInvalido(f'vista desconocida: {vista!r}')
    ahora = ahora or datetime.utcnow()
    i = ETAPAS.index(etapa)
    pedidos, extra = cohorte(filtros['desde'], filtros['hasta'], filtros['almacen_id'])
    enl = [p for p in pedidos if p.enlazado]
    if vista == 'en':
        sel = [p for p in enl if p.ultimo == i
               and p.estado_final in (EstadoFinal.EN_CURSO, EstadoFinal.DETENIDO)]
    elif vista == 'llegaron':
        sel = [p for p in enl if p.ultimo >= i]
    else:
        sel = [p for p in enl if (p.fuga and p.fuga['etapa'] == etapa)
               or (p.estado_final == EstadoFinal.DETENIDO and p.detenido['etapa'] == etapa)]
    filas = [_fila(p, min(i, p.ultimo), ahora) for p in sel]
    filas.sort(key=lambda f: (f['dias_en_etapa'] is None, -(f['dias_en_etapa'] or 0),
                              f['clave']))
    total = len(filas)
    ini = (pagina - 1) * por_pagina
    return {
        'etapa': etapa,
        'titulo': TITULOS[etapa],
        'vista': vista,
        'total': total,
        'pagina': pagina,
        'por_pagina': por_pagina,
        'pedidos': filas[ini:ini + por_pagina],
        'meta': _meta(filtros, pedidos, extra, ahora),
    }


# ─────────────────────────────────────────────────────────────────────────────
# La línea de tiempo de un pedido
# ─────────────────────────────────────────────────────────────────────────────

def _pedido_por_clave(clave: str):
    """Un pedido suelto, sin importar el rango. `None` si el WMS no lo conoce."""
    from app.models.packing import TareaPacking
    from app.models.pedido_siesa import PedidoSiesa
    from app.services.cadena_pedido import clave_pedido

    if clave.startswith(PREFIJO_SIN_CLAVE):
        num = clave[len(PREFIJO_SIN_CLAVE):]
        x = db.session.get(TareaPacking, int(num)) if num.isdigit() else None
        if x is None or x.pedido_clave is not None:
            return None
        p = Pedido(clave=clave, enlazado=False, packings=[x])
    else:
        p = Pedido(clave=clave)
    pedidos = {clave: p}
    _cargar(pedidos)
    if p.enlazado and not (p.historia or p.pickings or p.packings):
        partes = clave.split('-')
        if len(partes) == 3 and partes[2].isdigit():
            filas = PedidoSiesa.query.filter_by(tipo_docto=partes[1],
                                                consec_docto=int(partes[2])).all()
            if any(clave_pedido(f.centro_op, f.tipo_docto, f.consec_docto) == clave
                   for f in filas):
                p.en_pendientes_siesa = True
                p.cliente = next((f.cliente for f in filas if f.cliente), None)
        if not p.en_pendientes_siesa:
            return None
    _evaluar(p, _almacen_de_bodega())
    return p


def linea_de_tiempo(clave: str, ahora: datetime = None) -> dict:
    """Todo lo que el WMS sabe de un pedido, en orden. `None` si no existe.

    Marcas de cada etapa, quién hizo qué (autor en la fila, no adivinado),
    documentos Siesa con consecutivo y desenlace, trabajos de la cola y las
    acciones de la bitácora sobre cada una de sus entidades.
    """
    from app.models.bitacora import BitacoraAccion
    from app.models.bulto import Bulto
    from app.models.siesa_job import SiesaJob
    from app.models.usuario import Usuario

    ahora = ahora or datetime.utcnow()
    p = _pedido_por_clave((clave or '').strip())
    if p is None:
        return None

    ev = []

    def add(en, etapa, titulo, detalle=None, quien_id=None, documento=None, tipo='marca'):
        ev.append({'en': en, 'etapa': etapa, 'titulo': titulo, 'detalle': detalle,
                   'quien_id': quien_id, 'documento': documento, 'tipo': tipo})

    for h in sorted(p.historia, key=lambda h: h.primera_vez_vista_at or datetime.min):
        add(h.primera_vez_vista_at, 'aprobado',
            f'Línea vista en Siesa: {h.item_codigo or "—"}',
            f'Pedida {_r(h.cantidad_pedida)} · remisionada {_r(h.cantidad_remisionada)}'
            + (f' · valor {_r(h.vlr_neto)}' if h.vlr_neto is not None else ' · sin valor'))
        if h.salida_at:
            add(h.salida_at, 'aprobado', f'Línea salió de pendientes: {h.motivo_salida}',
                h.item_codigo)
    for t in p.pickings:
        add(t.fecha_creacion, 'recogido', f'Picking creado {t.codigo}',
            f'Solicitado {t.cantidad_solicitada}')
        if t.fecha_inicio:
            add(t.fecha_inicio, 'recogido', f'Picking iniciado {t.codigo}',
                quien_id=t.ultimo_operario_id or t.operario_id)
        if t.fecha_completado:
            add(t.fecha_completado, 'recogido', f'Picking completado {t.codigo}',
                f'Recogido {t.cantidad_recogida} de {t.cantidad_solicitada}',
                quien_id=t.ultimo_operario_id or t.operario_id)
        if t.estado == 'BLOQUEADO':
            add(None, 'recogido', f'Picking bloqueado {t.codigo}',
                _BLOQUEOS.get(t.motivo_bloqueo or '', t.motivo_bloqueo), tipo='alerta')
    for x in p.packings:
        add(x.fecha_creacion, 'empacado', f'Empaque creado {x.codigo}')
        if x.fecha_inicio:
            add(x.fecha_inicio, 'empacado', f'Empaque iniciado {x.codigo}',
                quien_id=x.empacador_id)
        if x.fecha_verificado:
            add(x.fecha_verificado, 'empacado', f'Empaque verificado {x.codigo}',
                quien_id=x.cerrado_por_id or x.empacador_id)
        if x.fecha_despachado or x.rm_consec or x.fe_consec:
            docs = []
            if x.rm_consec:
                docs.append(f'{x.rm_tipo or "RM"}-{x.rm_consec}')
            if x.fe_consec:
                docs.append(f'{x.fe_tipo or "FE"}-{x.fe_consec}')
            add(x.fecha_despachado or x.siesa_triggered_at, 'despachado',
                f'Despachado {x.codigo}',
                ('Valor factura ' + str(_r(x.valor_factura))) if x.valor_factura is not None
                else 'Valor de factura sin dato',
                documento={'documentos': docs or None,
                           'resultado': 'ENVIADO' if x.siesa_triggered else 'PENDIENTE'})
        if x.estado == 'CANCELADO':
            add(None, 'empacado', f'Empaque cancelado {x.codigo}', tipo='alerta')
    bultos = Bulto.query.filter(Bulto.tarea_id.in_([x.id for x in p.packings])).all() \
        if p.packings else []
    for b in bultos:
        if b.asignado_ruta_at:
            add(b.asignado_ruta_at, 'despachado', f'Bulto {b.codigo_barras} asignado a ruta',
                f'Ruta {b.ruta_despacho_id}', quien_id=b.asignado_ruta_por_id)
        if b.fecha_cargado:
            add(b.fecha_cargado, 'despachado', f'Bulto {b.codigo_barras} cargado',
                quien_id=b.cargado_por_id)
    for r in p.rutas.values():
        if r.fecha_cierre:
            add(r.fecha_cierre, 'despachado', f'Ruta {r.id} salió')
        if r.fecha_entregada:
            add(r.fecha_entregada, 'entregado', f'Ruta {r.id} cerrada')
        if r.liquidada_en:
            add(r.liquidada_en, 'liquidado', f'Ruta {r.id} liquidada',
                quien_id=r.liquidada_por_id)
    from app.services import motivos_rechazo
    for rc in p.recaudos:
        det = f'{rc.estado_entrega} · {rc.forma_pago or "sin forma de pago"} · ' \
              f'cobrado {_r(rc.monto_cobrado)}'
        if rc.motivo_rechazo:
            det += f' · {motivos_rechazo.etiqueta(rc.motivo_rechazo)}'
        add(rc.fecha_confirmacion, 'entregado', 'Parada confirmada', det,
            quien_id=rc.confirmado_por)
        if rc.editado_en:
            add(rc.editado_en, 'entregado', 'Parada editada', quien_id=rc.editado_por,
                tipo='alerta')
        for doc, nombre in (('nc', 'Nota crédito'), ('rc', 'Recibo de caja'),
                            ('dc', 'Retenciones')):
            res = getattr(rc, f'siesa_{doc}_resultado')
            if res:
                consec = getattr(rc, f'siesa_{doc}_consec', None)
                add(getattr(rc, f'siesa_{doc}_at'),
                    'cobrado' if doc != 'nc' else 'entregado', nombre, None,
                    documento={'documentos': [consec] if consec else None,
                               'resultado': res,
                               'consecutivo_sin_dato': consec is None})
    for d in p.devoluciones:
        add(d.fecha_confirmacion or d.fecha_creacion, 'entregado',
            f'Devolución del cliente {d.codigo}', d.estado,
            documento={'documentos': [f'NCE-{d.siesa_nc_consec}'] if d.siesa_nc_consec else None,
                       'resultado': 'ENVIADO' if d.siesa_nc_triggered else 'PENDIENTE'})

    # La cola hacia Siesa y la bitácora, por entidad.
    refs = ([('TareaPacking', x.id) for x in p.packings]
            + [('RecaudoEntrega', r.id) for r in p.recaudos]
            + [('DevolucionCliente', d.id) for d in p.devoluciones])
    for tipo_ref, rid in refs:
        for j in SiesaJob.query.filter_by(referencia_tipo=tipo_ref, referencia_id=rid):
            add(j.fecha_completado or j.fecha_creacion, None, f'Trabajo Siesa {j.tipo}',
                f'{j.estado} · {j.intentos} intento(s)'
                + (f' · {j.error_ultimo[:200]}' if j.error_ultimo else ''),
                quien_id=j.creado_por_id, tipo='cola')
    entidades = (refs + [('TareaPicking', t.id) for t in p.pickings]
                 + [('Bulto', b.id) for b in bultos]
                 + [('RutaDespacho', r) for r in p.rutas])
    bit = []
    for ent, eid in entidades:
        for a in BitacoraAccion.query.filter_by(entidad=ent, entidad_id=eid):
            bit.append(a)
            add(a.ocurrido_en, None, f'{a.accion.capitalize()} · {ent_legible(ent)} '
                f'{a.entidad_codigo or a.entidad_id}', a.motivo or SIN_MOTIVO,
                quien_id=a.usuario_id, tipo='bitacora')

    ids = {e['quien_id'] for e in ev if e['quien_id']}
    nombres = {u.id: u.nombre for u in Usuario.query.filter(Usuario.id.in_(ids))} if ids else {}
    for e in ev:
        e['quien'] = nombres.get(e['quien_id']) if e['quien_id'] else None
        e['dia'] = dia_operativo_de(e['en']).isoformat() if e['en'] else None
        e['en'] = e['en'].isoformat() if e['en'] else None
    ev.sort(key=lambda e: (e['en'] is None, e['en'] or ''))

    etapas = []
    for i, e in enumerate(ETAPAS):
        etapas.append({'etapa': e, 'titulo': TITULOS[e], 'alcanzada': i <= p.ultimo,
                       'en': p.marcas[e].isoformat() if p.marcas.get(e) else None,
                       'sin_marca': i <= p.ultimo and e in p.implicitas})
    fila = _fila(p, p.ultimo, ahora)
    fila['fuga'] = p.fuga
    fila['detenido'] = p.detenido
    fila['valor_facturado'] = float(p.valor_facturado) if p.valor_facturado is not None else None
    fila['monto_cobrado'] = float(p.monto_cobrado) if p.monto_cobrado is not None else None
    return {
        'pedido': fila,
        'etapas': etapas,
        'eventos': ev,
        'acciones_bitacora': len(bit),
        'meta': {'calculado_en': ahora.isoformat(),
                 'fuentes': {'wms': {'nombre': 'Operación del WMS', 'completa': True,
                                     'motivo': None, 'actualizado_en': ahora.isoformat()}}},
    }


_ENT_LEGIBLE = {
    'TareaPicking': 'picking', 'TareaPacking': 'empaque', 'Bulto': 'bulto',
    'RutaDespacho': 'ruta', 'RecaudoEntrega': 'parada', 'DevolucionCliente': 'devolución',
}


def ent_legible(entidad):
    return _ENT_LEGIBLE.get(entidad, entidad)
