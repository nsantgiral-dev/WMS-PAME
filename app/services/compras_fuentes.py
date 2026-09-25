"""
Compras: las fuentes — «en camino», lead time y precio de compra.

Una política, una función (Regla 0, corolario). Cada pregunta de abajo tiene
UNA respuesta en el repo y vive acá:

  · ¿cuánto de este SKU viene en camino?      → `en_camino(skus, bodegas)`
  · ¿cuánto tarda este proveedor / origen?     → `lead_time(proveedor, origen)`
  · ¿cuánto queda por entrar de esta línea?    → `pendiente_de_linea(fila)`
  · ¿a cuánto lo compramos la última vez?      → `precios_oc(refs)`
  · ¿la OC respetó el acuerdo marco?           → `precio_oc_vs_acuerdo()`

`tests/test_compras_fuentes_trinquetes.py` impide, por AST, que otro módulo
vuelva a sumar cantidades en tránsito o a elegir un lead time por su cuenta.

── Por qué existe ────────────────────────────────────────────────────────────
El armador calculaba `posicion = stock + en_transito − comprometido − …` y el
término de tránsito leía `ItemEnTransito`, que **no tenía ningún escritor**:
valía 0 siempre. Con OCs abiertas por cientos de unidades, el déficit salía
inflado exactamente en lo ya pedido — y el déficit dimensiona el contenedor,
irreversible 120 días (Regla 0).

El lead time era una constante (5 días nacional, 105 China) sin importar el
proveedor. El de China se medía de `contenedores`, que tampoco tenía escritor.

── Reglas ─────────────────────────────────────────────────────────────────────
- **Lista blanca de bodegas** (`_BODEGAS_PV`): «en camino» a AV1/TRA1 no es
  mercancía comprable, igual que su stock no lo es. Una bodega que el llamador
  pida y no esté operada se ignora y se declara.
- **Ante dato ausente, declararlo**: una línea sin unidad base no se suma con un
  número inventado; se cuenta. Un lead time sin muestra suficiente cae al
  default y lo dice (`fuente`, `n`, `confianza`).
- **Nunca dos veces lo mismo**: un contenedor que cita su OC
  (`ItemEnTransito.oc_referencia`) no se suma encima del pendiente de esa OC.
  Uno que no la cita, sobre un SKU que además tiene OC abierta, se suma y se
  marca `solapamiento_posible`: contar de más achica el déficit, que es el lado
  que un humano corrige mañana; contar de menos arma un contenedor de más.
"""
import logging
import os
import statistics
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.extensions import db

logger = logging.getLogger(__name__)

# ── Defaults DECLARADOS del lead time (vivían en armador_service) ─────────────
# Conservadores a propósito. `armador_service` los re-exporta por compatibilidad;
# ningún otro módulo los lee para decidir (trinquete).
LT_NACIONAL_DIAS = 5
SIGMA_LT_NACIONAL = 2
LT_CHINA_DIAS = 105
SIGMA_LT_CHINA = 15  # conservador — se reemplaza con ≥3 contenedores medidos

#: Con menos observaciones que esto, el lead time es el default. Con al menos
#: `N_MIN_MEDIDO`, la fuente es MEDIDO; entre los dos, PARCIAL. Es el mismo
#: criterio que ya usaba `calcular_sigma_lt_real` para los contenedores.
N_MIN_PARCIAL = 3
N_MIN_MEDIDO = 6

#: Estados de `ItemEnTransito` que todavía no llegaron y ya están comprados.
#: `EN_PRODUCCION` cuenta (la compra está hecha); `BORRADOR` no (un contenedor
#: en armado no es una compra) y `RECIBIDO` tampoco (ya está en el stock).
ESTADOS_CONTENEDOR_EN_CAMINO = ('EN_PRODUCCION', 'NAVEGANDO', 'EN_PUERTO',
                                'NACIONALIZACION', 'EN_RUTA_CEDI')

#: `f420_ind_estado` de una OC anulada (spec API_v2_Compras_Ordenes).
ESTADO_OC_ANULADA = 9

ORIGEN_CHINA = 'CHINA'
ORIGEN_NACIONAL = 'NACIONAL'


def _dec(v):
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _bodega_cdi() -> str:
    return (os.getenv('CONNEKTA_BODEGA') or 'NB1').strip() or 'NB1'


# ══════════════════════════════════════════════════════════════════════════════
# Pendiente de una línea de OC
# ══════════════════════════════════════════════════════════════════════════════

def pendiente_de_linea(fila: dict):
    """Lo que falta por entrar de una línea de `API_v2_Compras_Ordenes`, en
    unidad de INVENTARIO (base).

    `f421_cant_pedida`/`f421_cant_entrada` vienen en la unidad de la OC
    (`f421_id_unidad_medida`: una caja, una paca); las `_base` en la unidad del
    inventario, que es la que suma `stock_siesa`. Sumar cajas con unidades
    sería el error de unidades que el armador no puede ver.

    Returns: `(Decimal | None, como)` — `como` ∈ BASE | FACTOR | SIN_UNIDAD_BASE.
    Nunca negativo: una entrada mayor que lo pedido (exceso) no deja «menos que
    nada» en camino.
    """
    pb = _dec(fila.get('f421_cant_pedida_base'))
    eb = _dec(fila.get('f421_cant_entrada_base'))
    if pb is not None and eb is not None:
        return max(pb - eb, Decimal(0)), 'BASE'
    p = _dec(fila.get('f421_cant_pedida'))
    e = _dec(fila.get('f421_cant_entrada'))
    fac = _dec(fila.get('f421_factor'))
    if p is not None and e is not None and fac is not None and fac > 0:
        return max((p - e) * fac, Decimal(0)), 'FACTOR'
    return None, 'SIN_UNIDAD_BASE'


# ══════════════════════════════════════════════════════════════════════════════
# En camino
# ══════════════════════════════════════════════════════════════════════════════

def en_camino(skus=None, bodegas=None) -> dict:
    """Cuánto de cada SKU viene en camino a las bodegas operadas.

    Dos fuentes, sumadas sin contar dos veces lo mismo:
      1. Pendiente de las líneas de OC **abiertas** de Siesa (`oc_linea_siesa`).
      2. Ítems de contenedor sin recibir (`ItemEnTransito`) — la carga manual
         del contenedor. Si citan su OC y esa OC sigue abierta, ya están en (1).

    Args:
        skus: iterable de `codigo_siesa`; None = todos.
        bodegas: iterable de bodegas; None = `_BODEGAS_PV`. Lo que no esté en
            `_BODEGAS_PV` se ignora y se declara — nunca se suma AV1/TRA1.

    Returns:
        {'por_sku': {ref: float}, 'detalle': {ref: {...}}, 'declaracion': {...}}
    """
    from app.models.compras_fuentes import OcLineaSiesa
    from app.models.importacion import ItemEnTransito
    from app.models.producto import Producto
    from app.services.inventario_siesa_service import _BODEGAS_PV

    operadas = list(_BODEGAS_PV)
    if bodegas is None:
        pedidas = set(operadas)
        ignoradas = []
    else:
        bodegas = [str(b).strip() for b in bodegas if b]
        pedidas = {b for b in bodegas if b in operadas}
        ignoradas = sorted({b for b in bodegas if b not in operadas})
    filtro_skus = None if skus is None else {str(s).strip() for s in skus if s}

    oc = defaultdict(Decimal)
    ocs_de = defaultdict(set)
    sin_unidad = defaultdict(int)
    fuera_lista_blanca = 0
    fuera_de_filtro = 0
    sin_bodega = 0

    filas = (db.session.query(OcLineaSiesa.referencia, OcLineaSiesa.bodega,
                              OcLineaSiesa.pendiente_base, OcLineaSiesa.co,
                              OcLineaSiesa.tipo_docto, OcLineaSiesa.consec_docto)
             .filter(OcLineaSiesa.abierta.is_(True))
             .all())
    abiertas_citables = set()
    for ref, bodega, pendiente, co, tipo, consec in filas:
        ref = (ref or '').strip()
        abiertas_citables.add(f'{co or ""}-{tipo or ""}-{consec or ""}')
        if not ref or (filtro_skus is not None and ref not in filtro_skus):
            continue
        bodega = (bodega or '').strip()
        if not bodega:
            sin_bodega += 1
            continue
        if bodega not in operadas:
            fuera_lista_blanca += 1
            continue
        if bodega not in pedidas:
            fuera_de_filtro += 1
            continue
        if pendiente is None:
            sin_unidad[ref] += 1
            continue
        if pendiente > 0:
            oc[ref] += Decimal(pendiente)
            ocs_de[ref].add(f'{co or ""}-{tipo or ""}-{consec or ""}')

    cont = defaultdict(Decimal)
    cubiertos_por_oc = 0
    oc_citada_cerrada = 0
    cont_fuera = 0
    items = (db.session.query(Producto.codigo_siesa, ItemEnTransito.cantidad,
                              ItemEnTransito.oc_referencia,
                              ItemEnTransito.bodega_destino)
             .join(Producto, ItemEnTransito.producto_id == Producto.id)
             .filter(ItemEnTransito.estado.in_(ESTADOS_CONTENEDOR_EN_CAMINO))
             .all())
    for ref, cantidad, oc_ref, bodega in items:
        ref = (ref or '').strip()
        if not ref or (filtro_skus is not None and ref not in filtro_skus):
            continue
        bodega = (bodega or '').strip() or _bodega_cdi()
        if bodega not in pedidas:
            cont_fuera += 1
            continue
        oc_ref = (oc_ref or '').strip()
        if oc_ref:
            if oc_ref in abiertas_citables:
                cubiertos_por_oc += 1     # ya está en el pendiente de la OC
                continue
            # La OC que cita ya no está abierta: o entró (y está en stock) o
            # se anuló. Sumarla sería contar dos veces lo que ya entró.
            oc_citada_cerrada += 1
            continue
        cont[ref] += Decimal(cantidad or 0)

    por_sku, detalle = {}, {}
    for ref in set(oc) | set(cont) | set(sin_unidad):
        total = oc.get(ref, Decimal(0)) + cont.get(ref, Decimal(0))
        por_sku[ref] = float(total)
        detalle[ref] = {
            'oc': float(oc.get(ref, 0)),
            'contenedores': float(cont.get(ref, 0)),
            'ocs': sorted(ocs_de.get(ref, ())),
            'lineas_sin_unidad_base': sin_unidad.get(ref, 0),
            'solapamiento_posible': bool(oc.get(ref) and cont.get(ref)),
        }

    return {
        'por_sku': por_sku,
        'detalle': detalle,
        'declaracion': {
            'bodegas': sorted(pedidas),
            'bodegas_ignoradas_no_operadas': ignoradas,
            'lineas_oc_abiertas': len(filas),
            'lineas_sin_unidad_base': sum(sin_unidad.values()),
            'lineas_fuera_de_lista_blanca': fuera_lista_blanca,
            'lineas_fuera_del_filtro_de_bodega': fuera_de_filtro,
            'lineas_sin_bodega': sin_bodega,
            'contenedor_cubierto_por_su_oc': cubiertos_por_oc,
            'contenedor_cita_oc_cerrada': oc_citada_cerrada,
            'contenedor_fuera_de_bodegas': cont_fuera,
            'skus_con_solapamiento_posible': sum(
                1 for d in detalle.values() if d['solapamiento_posible']),
            'sync_oc': frescura_oc(),
        },
    }


def frescura_oc() -> dict:
    """¿De cuándo es el espejo de OCs? Leído de `registros_sync`, no del
    proceso. `completa_utc` es la última corrida con paginación completa: solo
    esa puede afirmar que una OC que no apareció se cerró."""
    from app.services import registro_sync_service as _reg
    ult = _reg.ultimo('compras_oc')
    ok = _reg.ultimo_ok('compras_oc')
    if isinstance(ult, dict) and '_error_lectura' in ult:
        return {'error_lectura': ult['_error_lectura']}
    return {
        'nunca_corrio': ult is None,
        'ultima_utc': (ult or {}).get('inicio'),
        'ultima_ok': (ult or {}).get('ok'),
        'completa_utc': (ok or {}).get('inicio') if ok else None,
        'nota': (None if ok else
                 'Nunca hubo una sincronización completa de OCs: «en camino» '
                 'desde Siesa vale 0 porque no se sabe, no porque no haya nada '
                 'pedido.'),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Lead time
# ══════════════════════════════════════════════════════════════════════════════

def _fecha_de(v):
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v


def observaciones_lead_time() -> dict:
    """Las mediciones de las que sale todo lead time. Una por OC.

    fecha de la OC (`f420_fecha`) → primera entrada:
      · la confirmación de la recepción en el WMS (`recepciones.fecha_confirmacion`,
        la fecha FÍSICA), si la OC se recibió por el muelle;
      · si no, la primera marca de Siesa: `f420_fecha_ts_parcial` o
        `f420_fecha_ts_cumplido` (lo que ocurra primero). **No verificado en
        vivo** que `ts_parcial` sea la primera entrada — `qa_compras_fuentes_real`
        lo mira.
    Para China, además, los contenedores con fecha de OC y de recepción en CDI.
    """
    from app.models.compras_fuentes import OcLineaSiesa
    from app.models.importacion import Contenedor
    from app.models.recepcion import RecepcionMercancia
    from app.models.acuerdo_marco import Proveedor
    from app.utils.fecha import dia_operativo_de

    ocs = {}
    for l in OcLineaSiesa.query.filter(
            OcLineaSiesa.fecha_oc.isnot(None)).all():
        if l.estado_oc == ESTADO_OC_ANULADA:
            continue
        k = ((l.co or '').strip(), (l.tipo_docto or '').strip().upper(),
             l.consec_docto)
        o = ocs.setdefault(k, {'proveedor_codigo': None, 'fecha_oc': l.fecha_oc,
                               'siesa': None, 'moneda': l.moneda})
        o['proveedor_codigo'] = o['proveedor_codigo'] or l.proveedor_codigo
        if l.fecha_oc < o['fecha_oc']:
            o['fecha_oc'] = l.fecha_oc
        for ts in (l.fecha_parcial, l.fecha_cumplido):
            d = _fecha_de(ts)
            if d and (o['siesa'] is None or d < o['siesa']):
                o['siesa'] = d

    wms = {}
    for co, tipo, consec, conf in db.session.query(
            RecepcionMercancia.co_oc_siesa, RecepcionMercancia.tipo_docto_oc_siesa,
            RecepcionMercancia.consec_docto_oc_siesa,
            RecepcionMercancia.fecha_confirmacion).filter(
            RecepcionMercancia.fecha_confirmacion.isnot(None)).all():
        try:
            k = ((co or '').strip(), (tipo or '').strip().upper(), int(consec))
        except (TypeError, ValueError):
            continue
        d = dia_operativo_de(conf)
        if k not in wms or d < wms[k]:
            wms[k] = d

    paises = {p.codigo: (p.pais or '').upper()
              for p in Proveedor.query.all() if p.codigo}

    por_oc, descartadas = [], defaultdict(int)
    for k, o in ocs.items():
        entrada, fuente = (wms[k], 'WMS_RECEPCION') if k in wms else (o['siesa'], 'SIESA')
        if entrada is None:
            descartadas['sin_entrada_todavia'] += 1
            continue
        dias = (entrada - o['fecha_oc']).days
        if dias < 0:
            descartadas['entrada_antes_de_la_oc'] += 1
            continue
        moneda = (o['moneda'] or '').strip().upper()
        por_oc.append({
            'oc': f'{k[0]}-{k[1]}-{k[2]}',
            'proveedor_codigo': o['proveedor_codigo'],
            'fecha_oc': o['fecha_oc'].isoformat(),
            'fecha_entrada': entrada.isoformat(),
            'fuente_entrada': fuente,
            'dias': dias,
            'nacional': (moneda in ('', 'COP')
                         and paises.get(o['proveedor_codigo'], '') != ORIGEN_CHINA),
        })

    contenedores = [c.lead_time_real for c in Contenedor.query.filter(
        Contenedor.fecha_oc.isnot(None),
        Contenedor.fecha_recepcion_cedi.isnot(None)).all()
        if c.lead_time_real]

    return {'por_oc': por_oc, 'contenedores': contenedores,
            'descartadas': dict(descartadas)}


def _medido(dias, nivel, sigma_default, **extra):
    n = len(dias)
    lt = statistics.mean(dias)
    sigma = statistics.stdev(dias) if n > 1 else sigma_default
    fuente = 'MEDIDO' if n >= N_MIN_MEDIDO else 'PARCIAL'
    return {
        'lt_dias': round(lt, 1), 'lt_medio': round(lt, 1),
        'sigma_lt': round(sigma, 1), 'n': n, 'lead_times': list(dias),
        'fuente': fuente, 'confianza': 'ALTA' if fuente == 'MEDIDO' else 'MEDIA',
        'nivel': nivel, 'nota': None, **extra,
    }


def lead_time(proveedor: str = None, origen: str = None,
              observaciones: dict = None) -> dict:
    """El lead time a usar. **La única función del repo que lo decide.**

    Cascada, de lo más específico a lo más general:
      1. el proveedor (`Proveedor.codigo` = `f200_id_prov`), si tiene ≥3 OCs medidas;
      2. el origen: CHINA → contenedores; NACIONAL → OCs en COP de proveedores
         no chinos;
      3. el default declarado (5±2 nacional, 105±15 China).

    `observaciones` se pasa cuando se consulta muchas veces seguidas (el ROP,
    un SKU por fila) para no releer la base en cada llamada.

    Returns: {lt_dias, lt_medio, sigma_lt, n, fuente (MEDIDO|PARCIAL|
              DEFAULT_CONSERVADOR), confianza (ALTA|MEDIA|NINGUNA), nivel
              (PROVEEDOR|ORIGEN|DEFAULT), proveedor, origen, lead_times, nota}
    """
    obs = observaciones if observaciones is not None else observaciones_lead_time()
    org = (origen or ORIGEN_NACIONAL).strip().upper()
    es_china = org == ORIGEN_CHINA
    lt_def, sigma_def = ((LT_CHINA_DIAS, SIGMA_LT_CHINA) if es_china
                         else (LT_NACIONAL_DIAS, SIGMA_LT_NACIONAL))
    extra = {'proveedor': proveedor, 'origen': ORIGEN_CHINA if es_china else ORIGEN_NACIONAL}

    n_prov = 0
    if proveedor:
        dias = [o['dias'] for o in obs['por_oc'] if o['proveedor_codigo'] == proveedor]
        n_prov = len(dias)
        if n_prov >= N_MIN_PARCIAL:
            return _medido(dias, 'PROVEEDOR', sigma_def, **extra)

    pool = (list(obs['contenedores']) if es_china
            else [o['dias'] for o in obs['por_oc'] if o['nacional']])
    if len(pool) >= N_MIN_PARCIAL:
        r = _medido(pool, 'ORIGEN', sigma_def, **extra)
        if proveedor:
            r['nota'] = (f'El proveedor {proveedor} tiene {n_prov} OC(s) medida(s) '
                         f'(mínimo {N_MIN_PARCIAL}): se usa el del origen.')
        return r

    que = 'contenedores' if es_china else 'OCs nacionales'
    return {
        'lt_dias': lt_def, 'lt_medio': lt_def, 'sigma_lt': sigma_def,
        'n': n_prov if proveedor else len(pool), 'lead_times': [],
        'fuente': 'DEFAULT_CONSERVADOR', 'confianza': 'NINGUNA', 'nivel': 'DEFAULT',
        'nota': (f'Solo {len(pool)} {que} con fechas completas (mínimo '
                 f'{N_MIN_PARCIAL}) — usando el default declarado '
                 f'{lt_def}±{sigma_def} días.'),
        **extra,
    }


def lead_times_por_proveedor(observaciones: dict = None) -> list:
    """Una fila por proveedor con OCs medidas, con su veredicto de `lead_time`.
    Para la pantalla de fuentes: n y confianza a la vista."""
    obs = observaciones if observaciones is not None else observaciones_lead_time()
    codigos = sorted({o['proveedor_codigo'] for o in obs['por_oc'] if o['proveedor_codigo']})
    salida = []
    for c in codigos:
        nacional = all(o['nacional'] for o in obs['por_oc'] if o['proveedor_codigo'] == c)
        r = lead_time(proveedor=c, origen=ORIGEN_NACIONAL if nacional else ORIGEN_CHINA,
                      observaciones=obs)
        r = {k: v for k, v in r.items() if k != 'lead_times'}
        r['n_proveedor'] = sum(1 for o in obs['por_oc'] if o['proveedor_codigo'] == c)
        salida.append(r)
    return salida


def proveedor_habitual(refs=None) -> dict:
    """`{ref: proveedor_codigo}` — el de la OC más reciente de cada SKU.

    Es lo que el ROP necesita para pedir el lead time del proveedor y no el
    promedio de todos. Ante varias OCs el mismo día gana la de consecutivo
    mayor (la última registrada)."""
    from app.models.compras_fuentes import OcLineaSiesa
    q = (db.session.query(OcLineaSiesa.referencia, OcLineaSiesa.proveedor_codigo,
                          OcLineaSiesa.fecha_oc, OcLineaSiesa.consec_docto)
         .filter(OcLineaSiesa.proveedor_codigo.isnot(None),
                 OcLineaSiesa.fecha_oc.isnot(None)))
    filtro = None if refs is None else set(refs)
    mejor = {}
    for ref, prov, fecha, consec in q.all():
        ref = (ref or '').strip()
        if not ref or (filtro is not None and ref not in filtro):
            continue
        clave = (fecha, consec or 0)
        if ref not in mejor or clave > mejor[ref][0]:
            mejor[ref] = (clave, prov)
    return {r: v[1] for r, v in mejor.items()}


# ══════════════════════════════════════════════════════════════════════════════
# Precio de compra según las OCs
# ══════════════════════════════════════════════════════════════════════════════

def _precio_base(linea):
    """Precio por unidad de INVENTARIO: el de la OC es por su unidad
    (`f421_id_unidad_medida`). Sin factor no se convierte — no se inventa."""
    p = _dec(linea.precio_unitario)
    fac = _dec(linea.factor)
    if p is None or p <= 0 or fac is None or fac <= 0:
        return None
    return p / fac


def precios_oc(refs, proveedor: str = None) -> dict:
    """El precio de la OC más reciente por SKU, en COP y por unidad base.

    Solo COP: una OC en USD no es un costo en pesos sin la tasa del día de
    nacionalizar, y convertirla con la del documento sería inventar el costo de
    importación. Obsequios (`f421_ind_obsequio=1`) y anuladas fuera.

    A igual fecha, el MÁS ALTO (misma regla de `costo_service`: subestimar el
    costo empuja a comprar de más, que es el lado irreversible).

    Returns: {ref: {costo, fuente='OC_SIESA', fecha_costo, proveedor, oc}}
    """
    from app.models.compras_fuentes import OcLineaSiesa
    refs = [r for r in (refs or []) if r]
    if not refs:
        return {}
    q = OcLineaSiesa.query.filter(OcLineaSiesa.referencia.in_(refs),
                                  OcLineaSiesa.fecha_oc.isnot(None))
    if proveedor:
        q = q.filter(OcLineaSiesa.proveedor_codigo == proveedor)
    salida = {}
    for l in q.all():
        if l.estado_oc == ESTADO_OC_ANULADA or (l.ind_obsequio or 0) == 1:
            continue
        if (l.moneda or '').strip().upper() not in ('COP',):
            continue
        p = _precio_base(l)
        if p is None:
            continue
        prev = salida.get(l.referencia)
        if (prev is None or l.fecha_oc > prev['_fecha']
                or (l.fecha_oc == prev['_fecha'] and p > Decimal(str(prev['costo'])))):
            salida[l.referencia] = {
                'costo': float(round(p, 4)), 'fuente': 'OC_SIESA',
                'fecha_costo': l.fecha_oc.isoformat(), '_fecha': l.fecha_oc,
                'proveedor': l.proveedor_codigo, 'oc': l.oc_referencia,
            }
    for v in salida.values():
        v.pop('_fecha', None)
    return salida


def precio_oc_vs_acuerdo() -> list:
    """Cada acuerdo marco vigente contra la OC más reciente del MISMO proveedor
    y SKU. Es el insumo de `detectar_deriva` (compras_inteligencia_service): la
    deriva se mide contra lo que se pidió en la OC, que existe desde que se
    aprueba, y no solo contra la recepción.

    Returns: [{producto_id, referencia, proveedor_codigo, precio_pactado,
               precio_oc, diferencia_pct, oc, fecha_oc}]
    """
    from app.models.acuerdo_marco import AcuerdoMarco
    from app.utils.fecha import dia_operativo
    hoy = dia_operativo()
    salida = []
    acuerdos = AcuerdoMarco.query.filter(AcuerdoMarco.activo.is_(True),
                                         AcuerdoMarco.vigencia_desde <= hoy,
                                         AcuerdoMarco.vigencia_hasta >= hoy).all()
    for a in acuerdos:
        if (a.moneda or 'COP').upper() != 'COP' or not a.producto or not a.proveedor:
            continue
        ref = a.producto.codigo_siesa
        prov = a.proveedor.codigo
        p = precios_oc([ref], proveedor=prov).get(ref)
        if not p:
            continue
        pactado = float(a.precio_unitario)
        salida.append({
            'producto_id': a.producto_id, 'referencia': ref,
            'proveedor_codigo': prov, 'precio_pactado': pactado,
            'precio_oc': p['costo'],
            'diferencia_pct': (round((p['costo'] - pactado) / pactado * 100, 2)
                               if pactado > 0 else None),
            'oc': p['oc'], 'fecha_oc': p['fecha_costo'],
        })
    return salida
