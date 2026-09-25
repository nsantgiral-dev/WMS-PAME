"""
Compras: las fuentes — «en camino», lead time y precio de compra.

Una política, una función (Regla 0, corolario). Cada pregunta de abajo tiene
UNA respuesta en el repo y vive acá:

  · ¿cuánto de este SKU viene en camino?      → `en_camino(skus, bodegas)`
  · ¿cuánto tarda este proveedor / origen?     → `lead_time(proveedor, origen)`
  · ¿cuánto queda por entrar de esta línea?    → `pendiente_de_linea(fila)`
  · ¿a cuánto lo compramos la última vez?      → `precios_oc(refs)` (en COP)
  · ¿a cuánto dice cada OC? (una lectura)      → `lineas_precio_oc(...)` — la
    usan el costo y la deriva contra el acuerdo marco
    (`compras_inteligencia_service.detectar_deriva`, el único comparador)

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
# Conservadores a propósito. Un solo juego: `default_lead_time` los lee (y las
# variables de entorno de nacional); `armador_service` los re-exporta por
# compatibilidad y ningún otro módulo los lee para decidir (trinquete).
LT_NACIONAL_DIAS = 5
SIGMA_LT_NACIONAL = 2
LT_CHINA_DIAS = 105
SIGMA_LT_CHINA = 15  # conservador — piso de lo medido hasta 6 contenedores (D9)

#: Variables de entorno del lead time nacional (del frente del motor, D14).
ENV_LT_NACIONAL = 'ROP_LT_NACIONAL_DIAS'
ENV_SIGMA_LT_NACIONAL = 'ROP_SIGMA_LT_NACIONAL'

#: Con menos observaciones que esto, el lead time es el default. Con al menos
#: `N_MIN_MEDIDO`, la fuente es MEDIDO; entre los dos, PARCIAL — y PARCIAL no
#: baja del default (D9, ver `_medido`).
N_MIN_PARCIAL = 3
N_MIN_MEDIDO = 6

#: Estados en los que la mercancía YA ESTÁ PEDIDA y todavía no llegó — de un
#: contenedor o, sin contenedor, del ítem. `EN_PRODUCCION` cuenta (un
#: contenedor en fábrica es plata comprometida: no contarlo pediría dos veces
#: lo mismo); `BORRADOR` no (un contenedor en armado no es una compra) y
#: `RECIBIDO` tampoco (ya está en el stock).
ESTADOS_EN_CAMINO = ('EN_PRODUCCION', 'NAVEGANDO', 'EN_PUERTO',
                     'NACIONALIZACION', 'EN_RUTA_CEDI')

#: Las fuentes de «lo que ya viene». No es un registro de funciones: la de
#: contenedores depende de la de OCs para no contar dos veces lo mismo.
FUENTES_EN_CAMINO = ('OC_SIESA', 'IMPORTACION')

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
    """LO QUE YA VIENE, por SKU, a las bodegas operadas. **La única función del
    repo que lo calcula** (Regla 0, corolario): el Armador (ROP, contenedor) y
    el pedido de temporada lo leen por `armador_service.posicion_inventario`.

    Dos fuentes (`FUENTES_EN_CAMINO`), sumadas sin contar dos veces lo mismo:
      1. `OC_SIESA` — pendiente de las líneas de OC **abiertas** de Siesa
         (`oc_linea_siesa`).
      2. `IMPORTACION` — ítems de contenedor sin recibir (`ItemEnTransito`).
         Cuenta si su CONTENEDOR está en `ESTADOS_EN_CAMINO` (incluido
         EN_PRODUCCION: una compra hecha en fábrica), o —sin contenedor— si el
         ítem mismo lo está. BORRADOR no es una compra; RECIBIDO ya está en el
         stock. Si el ítem cita su OC y esa OC sigue abierta, ya está en (1);
         si cita una OC cerrada, ya entró o se anuló: no se suma.

    Una fuente que revienta **no suma cero en silencio**: va a
    `fuentes_con_error` y `completo` queda False. «Viene 0» y «no sé qué
    viene» empujan la compra al mismo lado, y solo el segundo es cierto.

    Args:
        skus: iterable de `codigo_siesa`; None = todos.
        bodegas: iterable de bodegas; None = `_BODEGAS_PV`. Lo que no esté en
            `_BODEGAS_PV` se ignora y se declara — nunca se suma AV1/TRA1.

    Returns:
        {'por_sku': {ref: float}, 'detalle': {ref: {...}},
         'declaracion': {fuentes, fuentes_con_error, completo, hay_dato, nota,
                         sync_oc, bodegas, …contadores}}
    """
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

    errores = []
    try:
        oc = _pendiente_de_ocs_abiertas(filtro_skus, operadas, pedidas)
    except Exception as e:     # noqa: BLE001 — se declara, no se calla
        logger.error('[COMPRAS] en camino: la fuente OC_SIESA falló: %s', e)
        errores.append({'fuente': 'OC_SIESA', 'error': str(e)})
        oc = None
    try:
        cont = _contenedores_en_camino(
            filtro_skus, pedidas, oc['abiertas_citables'] if oc else None)
    except Exception as e:     # noqa: BLE001
        logger.error('[COMPRAS] en camino: la fuente IMPORTACION falló: %s', e)
        errores.append({'fuente': 'IMPORTACION', 'error': str(e)})
        cont = None

    por_oc = oc['por_sku'] if oc else {}
    por_cont = cont['por_sku'] if cont else {}
    sin_unidad = oc['sin_unidad'] if oc else {}
    ocs_de = oc['ocs_de'] if oc else {}

    por_sku, detalle = {}, {}
    for ref in set(por_oc) | set(por_cont) | set(sin_unidad):
        total = por_oc.get(ref, Decimal(0)) + por_cont.get(ref, Decimal(0))
        por_sku[ref] = float(total)
        detalle[ref] = {
            'oc': float(por_oc.get(ref, 0)),
            'contenedores': float(por_cont.get(ref, 0)),
            'ocs': sorted(ocs_de.get(ref, ())),
            'lineas_sin_unidad_base': sin_unidad.get(ref, 0),
            'solapamiento_posible': bool(por_oc.get(ref) and por_cont.get(ref)),
        }

    sync = frescura_oc()
    oc_sabe = bool(sync.get('completa_utc')) and oc is not None
    fuentes = {}
    if oc is not None:
        fuentes['OC_SIESA'] = {'refs': len(por_oc),
                               'unidades': round(float(sum(por_oc.values())), 2),
                               'espejo_completo': oc_sabe}
    if cont is not None:
        fuentes['IMPORTACION'] = {'refs': len(por_cont),
                                  'unidades': round(float(sum(por_cont.values())), 2)}
    hay_dato = oc_sabe or bool(por_cont)

    return {
        'por_sku': por_sku,
        'detalle': detalle,
        'declaracion': {
            'fuentes': fuentes,
            'fuentes_con_error': errores,
            'completo': not errores,
            'hay_dato': hay_dato,
            'nota': (None if hay_dato else
                     'Ninguna fuente de «en camino» tiene datos: nunca hubo una '
                     'sincronización completa de OCs de Siesa y no hay contenedores '
                     'registrados en camino. El término en tránsito de la posición '
                     'vale 0 porque NO SE SABE, no porque no venga nada.'),
            'bodegas': sorted(pedidas),
            'bodegas_ignoradas_no_operadas': ignoradas,
            'lineas_oc_abiertas': oc['lineas'] if oc else None,
            'lineas_sin_unidad_base': sum(sin_unidad.values()),
            'lineas_fuera_de_lista_blanca': oc['fuera_lista_blanca'] if oc else None,
            'lineas_fuera_del_filtro_de_bodega': oc['fuera_de_filtro'] if oc else None,
            'lineas_sin_bodega': oc['sin_bodega'] if oc else None,
            'contenedor_cubierto_por_su_oc': cont['cubiertos_por_oc'] if cont else None,
            'contenedor_cita_oc_cerrada': cont['oc_citada_cerrada'] if cont else None,
            'contenedor_oc_no_verificable': cont['oc_no_verificable'] if cont else None,
            'contenedor_fuera_de_bodegas': cont['fuera'] if cont else None,
            'skus_con_solapamiento_posible': sum(
                1 for d in detalle.values() if d['solapamiento_posible']),
            'sync_oc': sync,
        },
    }


def _pendiente_de_ocs_abiertas(filtro_skus, operadas, pedidas) -> dict:
    """Fuente `OC_SIESA` de `en_camino`: el pendiente (unidad base) de las
    líneas de OC abiertas, por SKU, solo a bodegas operadas y pedidas."""
    from app.models.compras_fuentes import OcLineaSiesa

    por_sku = defaultdict(Decimal)
    ocs_de = defaultdict(set)
    sin_unidad = defaultdict(int)
    fuera_lista_blanca = fuera_de_filtro = sin_bodega = 0

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
            por_sku[ref] += Decimal(pendiente)
            ocs_de[ref].add(f'{co or ""}-{tipo or ""}-{consec or ""}')
    return {'por_sku': por_sku, 'ocs_de': ocs_de, 'sin_unidad': sin_unidad,
            'abiertas_citables': abiertas_citables, 'lineas': len(filas),
            'fuera_lista_blanca': fuera_lista_blanca,
            'fuera_de_filtro': fuera_de_filtro, 'sin_bodega': sin_bodega}


def _contenedores_en_camino(filtro_skus, pedidas, abiertas_citables) -> dict:
    """Fuente `IMPORTACION` de `en_camino`: ítems de contenedor comprados y sin
    recibir. `abiertas_citables=None` = la fuente de OCs falló: un ítem que
    cita su OC no se puede verificar y **se suma** (contar de más achica el
    déficit, el lado que un humano corrige; Regla 0), declarado."""
    from sqlalchemy import and_, or_
    from app.models.importacion import Contenedor, ItemEnTransito
    from app.models.producto import Producto

    por_sku = defaultdict(Decimal)
    cubiertos_por_oc = oc_citada_cerrada = oc_no_verificable = fuera = 0
    items = (db.session.query(Producto.codigo_siesa, ItemEnTransito.cantidad,
                              ItemEnTransito.oc_referencia,
                              ItemEnTransito.bodega_destino)
             .join(Producto, ItemEnTransito.producto_id == Producto.id)
             .outerjoin(Contenedor, ItemEnTransito.contenedor_id == Contenedor.id)
             .filter(ItemEnTransito.estado != 'RECIBIDO')
             .filter(or_(
                 Contenedor.estado.in_(ESTADOS_EN_CAMINO),
                 and_(ItemEnTransito.contenedor_id.is_(None),
                      ItemEnTransito.estado.in_(ESTADOS_EN_CAMINO)),
             ))
             .all())
    for ref, cantidad, oc_ref, bodega in items:
        ref = (ref or '').strip()
        if not ref or (filtro_skus is not None and ref not in filtro_skus):
            continue
        bodega = (bodega or '').strip() or _bodega_cdi()
        if bodega not in pedidas:
            fuera += 1
            continue
        oc_ref = (oc_ref or '').strip()
        if oc_ref:
            if abiertas_citables is None:
                oc_no_verificable += 1
            elif oc_ref in abiertas_citables:
                cubiertos_por_oc += 1     # ya está en el pendiente de la OC
                continue
            else:
                # La OC que cita ya no está abierta: o entró (y está en stock)
                # o se anuló. Sumarla sería contar dos veces lo que ya entró.
                oc_citada_cerrada += 1
                continue
        por_sku[ref] += Decimal(cantidad or 0)
    return {'por_sku': por_sku, 'cubiertos_por_oc': cubiertos_por_oc,
            'oc_citada_cerrada': oc_citada_cerrada,
            'oc_no_verificable': oc_no_verificable, 'fuera': fuera}


def resumen_lineas_abiertas() -> dict:
    """Cuántas líneas abiertas hay, cuántas con pendiente y cuántas sin unidad
    base. Para el estado del sync: el pendiente solo se lee acá."""
    from app.models.compras_fuentes import OcLineaSiesa
    q = OcLineaSiesa.query.filter(OcLineaSiesa.abierta.is_(True))
    return {'abiertas': q.count(),
            'con_pendiente': q.filter(OcLineaSiesa.pendiente_base > 0).count(),
            'sin_unidad_base': q.filter(OcLineaSiesa.pendiente_base.is_(None)).count()}


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


def _numero_env(nombre, defecto, minimo):
    try:
        v = float(os.environ[nombre])
        if v >= minimo:
            return v, True
    except (KeyError, TypeError, ValueError):
        pass
    return float(defecto), False


def default_lead_time(origen: str = None) -> dict:
    """El default DECLARADO por origen — el último escalón de `lead_time` y el
    piso conservador de la regla D9. Nacional es configurable
    (`ROP_LT_NACIONAL_DIAS` / `ROP_SIGMA_LT_NACIONAL`); China no (sale de los
    contenedores). Un valor ilegible o negativo cae al default, declarado.

    Returns: {lt_dias, sigma_lt, fuente: CONFIGURADO | DEFAULT_CONSERVADOR}
    """
    if (origen or '').strip().upper() == ORIGEN_CHINA:
        return {'lt_dias': float(LT_CHINA_DIAS), 'sigma_lt': float(SIGMA_LT_CHINA),
                'fuente': 'DEFAULT_CONSERVADOR'}
    lt, c1 = _numero_env(ENV_LT_NACIONAL, LT_NACIONAL_DIAS, minimo=1)
    slt, c2 = _numero_env(ENV_SIGMA_LT_NACIONAL, SIGMA_LT_NACIONAL, minimo=0)
    return {'lt_dias': lt, 'sigma_lt': slt,
            'fuente': 'CONFIGURADO' if (c1 or c2) else 'DEFAULT_CONSERVADOR'}


def _medido(dias, nivel, piso, **extra):
    """Lo medido, con la regla D9: con 3 a 5 observaciones se usa **el mayor**
    entre lo medido y el piso conservador (`default_lead_time`), para la media
    y para la σ — tres contenedores de 118/120/122 días no reemplazan σ=15 por
    σ=2: la muestra no alcanza ni para estimar una varianza. Desde
    `N_MIN_MEDIDO`, lo medido manda."""
    n = len(dias)
    lt_m = statistics.mean(dias)
    sigma_m = statistics.stdev(dias) if n > 1 else piso['sigma_lt']
    if n >= N_MIN_MEDIDO:
        fuente, lt, sigma, nota = 'MEDIDO', lt_m, sigma_m, None
    else:
        fuente = 'PARCIAL'
        lt = max(lt_m, piso['lt_dias'])
        sigma = max(sigma_m, piso['sigma_lt'])
        nota = (f'{n} observaciones: se usa el MAYOR entre lo medido (LT {lt_m:.1f}, '
                f'σ {sigma_m:.1f}) y el conservador (LT {piso["lt_dias"]:g}, '
                f'σ {piso["sigma_lt"]:g}) hasta tener {N_MIN_MEDIDO} (D9).')
    return {
        'lt_dias': round(lt, 1), 'lt_medio': round(lt, 1),
        'sigma_lt': round(sigma, 1), 'lt_medido': round(lt_m, 1),
        'sigma_lt_medida': round(sigma_m, 1), 'n': n, 'lead_times': list(dias),
        'fuente': fuente, 'confianza': 'ALTA' if fuente == 'MEDIDO' else 'MEDIA',
        'nivel': nivel, 'nota': nota, **extra,
    }


def lead_time(proveedor: str = None, origen: str = None,
              observaciones: dict = None) -> dict:
    """El lead time a usar. **La única función del repo que lo decide.**

    Cascada, de lo más específico a lo más general:
      1. el proveedor (`Proveedor.codigo` = `f200_id_prov`), si tiene ≥3 OCs medidas;
      2. el origen: CHINA → contenedores; NACIONAL → OCs en COP de proveedores
         no chinos;
      3. el default declarado (`default_lead_time`: 5±2 nacional, configurable
         con `ROP_LT_NACIONAL_DIAS` / `ROP_SIGMA_LT_NACIONAL`; 105±15 China).
    En los niveles medidos rige D9 (ver `_medido`): con < 6 observaciones, el
    mayor entre lo medido y el default.

    `observaciones` se pasa cuando se consulta muchas veces seguidas (el ROP,
    un SKU por fila) para no releer la base en cada llamada.

    Returns: {lt_dias, lt_medio, sigma_lt, n, fuente (MEDIDO|PARCIAL|
              CONFIGURADO|DEFAULT_CONSERVADOR), confianza (ALTA|MEDIA|NINGUNA),
              nivel (PROVEEDOR|ORIGEN|DEFAULT), proveedor, origen, lead_times,
              lt_medido, sigma_lt_medida (solo medidos), nota}
    """
    obs = observaciones if observaciones is not None else observaciones_lead_time()
    org = (origen or ORIGEN_NACIONAL).strip().upper()
    es_china = org == ORIGEN_CHINA
    piso = default_lead_time(ORIGEN_CHINA if es_china else ORIGEN_NACIONAL)
    extra = {'proveedor': proveedor, 'origen': ORIGEN_CHINA if es_china else ORIGEN_NACIONAL}

    n_prov = 0
    if proveedor:
        dias = [o['dias'] for o in obs['por_oc'] if o['proveedor_codigo'] == proveedor]
        n_prov = len(dias)
        if n_prov >= N_MIN_PARCIAL:
            return _medido(dias, 'PROVEEDOR', piso, **extra)

    pool = (list(obs['contenedores']) if es_china
            else [o['dias'] for o in obs['por_oc'] if o['nacional']])
    if len(pool) >= N_MIN_PARCIAL:
        r = _medido(pool, 'ORIGEN', piso, **extra)
        if proveedor:
            aviso = (f'El proveedor {proveedor} tiene {n_prov} OC(s) medida(s) '
                     f'(mínimo {N_MIN_PARCIAL}): se usa el del origen.')
            r['nota'] = f'{aviso} {r["nota"]}' if r['nota'] else aviso
        return r

    que = 'contenedores' if es_china else 'OCs nacionales'
    lt_def, sigma_def = piso['lt_dias'], piso['sigma_lt']
    if piso['fuente'] == 'CONFIGURADO':
        nota = (f'Solo {len(pool)} {que} con fechas completas (mínimo {N_MIN_PARCIAL}): '
                f'se usa el lead time CONFIGURADO ({ENV_LT_NACIONAL} / '
                f'{ENV_SIGMA_LT_NACIONAL}) {lt_def:g}±{sigma_def:g} días.')
    else:
        nota = (f'Solo {len(pool)} {que} con fechas completas (mínimo {N_MIN_PARCIAL}) '
                f'— usando el default declarado {lt_def:g}±{sigma_def:g} días, un '
                f'SUPUESTO que nadie midió'
                + ('' if es_china else
                   f' (configurable con {ENV_LT_NACIONAL} / {ENV_SIGMA_LT_NACIONAL})')
                + '.')
    return {
        'lt_dias': lt_def, 'lt_medio': lt_def, 'sigma_lt': sigma_def,
        'n': n_prov if proveedor else len(pool), 'lead_times': [],
        'fuente': piso['fuente'], 'confianza': 'NINGUNA', 'nivel': 'DEFAULT',
        'nota': nota, **extra,
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
    """Precio por unidad de INVENTARIO, en la moneda de la OC: el de la OC es
    por su unidad (`f421_id_unidad_medida`). Sin factor no se convierte — no se
    inventa."""
    p = _dec(linea.precio_unitario)
    fac = _dec(linea.factor)
    if p is None or p <= 0 or fac is None or fac <= 0:
        return None
    return p / fac


def _cantidad_base(linea):
    """Lo pedido en la línea, en unidad base (lo que se paga al precio de la OC)."""
    pb = _dec(linea.cant_pedida_base)
    if pb is not None:
        return pb
    p, fac = _dec(linea.cant_pedida), _dec(linea.factor)
    if p is not None and fac is not None and fac > 0:
        return p * fac
    return None


def lineas_precio_oc(refs=None, desde=None, proveedor: str = None) -> list:
    """Las líneas de OC que dicen a cuánto se compró. **La única lectura del
    precio de una OC**: la usan el costo (`precios_oc` → capa `OC_SIESA`) y la
    deriva contra el acuerdo (`compras_inteligencia_service.
    precios_de_compra_recibidos`).

    Una por línea, por unidad base y **en la moneda de la OC** (la conversión a
    pesos es de `costo_service.a_cop`, una política). Fuera: anuladas,
    obsequios (`f421_ind_obsequio=1`) y líneas sin precio o sin factor.

    Returns: [{referencia, precio_base (Decimal), moneda, cantidad_base
               (Decimal | None), fecha_oc (date), proveedor_codigo, oc}]
    """
    from app.models.compras_fuentes import OcLineaSiesa
    q = OcLineaSiesa.query.filter(OcLineaSiesa.fecha_oc.isnot(None))
    if refs is not None:
        refs = [r for r in refs if r]
        if not refs:
            return []
        q = q.filter(OcLineaSiesa.referencia.in_(refs))
    if desde is not None:
        q = q.filter(OcLineaSiesa.fecha_oc >= desde)
    if proveedor:
        q = q.filter(OcLineaSiesa.proveedor_codigo == proveedor)
    salida = []
    for l in q.all():
        if l.estado_oc == ESTADO_OC_ANULADA or (l.ind_obsequio or 0) == 1:
            continue
        p = _precio_base(l)
        if p is None:
            continue
        salida.append({
            'referencia': (l.referencia or '').strip(), 'precio_base': p,
            'moneda': (l.moneda or 'COP').strip().upper() or 'COP',
            'cantidad_base': _cantidad_base(l), 'fecha_oc': l.fecha_oc,
            'proveedor_codigo': l.proveedor_codigo, 'oc': l.oc_referencia,
        })
    return salida


def precios_oc(refs, proveedor: str = None) -> dict:
    """El precio de la OC más reciente por SKU, **en COP** y por unidad base.

    La moneda se lleva a pesos con `costo_service.a_cop` —la misma política de
    los acuerdos y las cotizaciones (D4)—: USD como FOB × TRM × factor de
    nacionalización, declarado en `conversion`. Otra moneda no se convierte:
    esa línea se excluye y se cuenta; un SKU que solo tiene líneas así sale con
    `costo: None` y `motivo_exclusion` (la capa de costo lo salta).

    A igual fecha, el MÁS ALTO (misma regla de `costo_service`: subestimar el
    costo empuja a comprar de más, que es el lado irreversible).

    Returns: {ref: {costo, fuente='OC_SIESA', fecha_costo, proveedor, oc,
                    moneda, conversion, lineas_otra_moneda}}
    """
    from app.services.costo_service import a_cop
    salida, excluidas, motivo = {}, defaultdict(int), {}
    for l in lineas_precio_oc(refs, proveedor=proveedor):
        ref = l['referencia']
        p, conversion = a_cop(l['precio_base'], l['moneda'])
        if p is None or p <= 0:
            excluidas[ref] += 1
            motivo[ref] = (conversion or {}).get('motivo')
            continue
        prev = salida.get(ref)
        if (prev is None or l['fecha_oc'] > prev['_fecha']
                or (l['fecha_oc'] == prev['_fecha'] and p > prev['costo'])):
            salida[ref] = {
                'costo': round(float(p), 4), 'fuente': 'OC_SIESA',
                'fecha_costo': l['fecha_oc'].isoformat(), '_fecha': l['fecha_oc'],
                'proveedor': l['proveedor_codigo'], 'oc': l['oc'],
                'moneda': l['moneda'], 'conversion': conversion,
            }
    for ref, n in excluidas.items():
        if ref in salida:
            salida[ref]['lineas_otra_moneda'] = n
        else:
            salida[ref] = {'costo': None, 'fuente': 'OC_SIESA',
                           'lineas_otra_moneda': n,
                           'motivo_exclusion': motivo.get(ref) or 'Moneda sin conversión.'}
    for v in salida.values():
        v.pop('_fecha', None)
    return salida
