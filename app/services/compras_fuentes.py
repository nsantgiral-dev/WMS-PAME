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
#: Nacional: 10 ± 5 días (2026-09-27, tanda E de compras). Era 5 ± 2, que no
#: era conservador: medido en producción el 27-sep-2026 (solo lectura), las 42
#: OCs abiertas con una entrada parcial tardaron una MEDIANA de 10 días de la
#: OC a la primera entrada (p90 ≈ 26); las 11 cumplidas en 90 días, 4 (p90 17).
#: Un lead time corto subestima el punto de pedido. Se usa hasta que el espejo
#: mida al proveedor (≥ 3 OCs) o al origen (≥ 3 OCs nacionales).
LT_NACIONAL_DIAS = 10
SIGMA_LT_NACIONAL = 5
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
FUENTES_EN_CAMINO = ('OC_SIESA', 'IMPORTACION', 'DECISION_WMS')

#: Una observación de lead time con menos días que esto no mide al proveedor:
#: la OC se registró al recibir (entrada el mismo día de la OC). Ver
#: `observaciones_lead_time`.
DIAS_MIN_OBSERVACION = 1
DESCARTE_OC_AL_RECIBIR = 'oc_registrada_al_recibir'

#: Días de atraso sobre la fecha de entrega desde los que una línea abierta se
#: DECLARA vieja en «en camino» (`COMPRAS_OC_VENCIDA_DIAS`). Supuesto
#: declarado: 90 días es más que cualquier lead time nacional y casi el de
#: China. En QA el 90 % de lo pendiente es de OCs de más de un año.
DIAS_OC_VENCIDA = 90
ENV_OC_VENCIDA = 'COMPRAS_OC_VENCIDA_DIAS'
#: Corte: las líneas con la entrega vencida hace más de estos días NO se suman
#: a «en camino». Default 180 (2026-09-27, decisión por defecto del dueño,
#: declarada): en producción el 100 % de lo pendiente estaba vencido y 50 OCs
#: aprobadas hace más de 180 días sin una sola entrada sumaban 288.156 u que
#: apagaban la compra de los SKU que citan. `nunca` = no cortar (lo de antes).
#: En producción la fecha de entrega es la de la OC (1.276 de 1.276 líneas).
DIAS_OC_EXCLUIR_DEFAULT = 180
ENV_OC_EXCLUIR = 'COMPRAS_OC_EXCLUIR_MAS_DE_DIAS'
_SIN_CORTE = ('nunca', 'no', 'ninguno', 'sin corte')

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
    unidad de INVENTARIO — la que suma `stock_siesa`.

    **Ojo con los nombres de Siesa, que dicen lo contrario de lo que parecen.**
    Verificado en vivo contra Siesa QA el 2026-09-25 (OC 003-OC-28, línea de
    `PAPELSP6948` en paquetes de 12):

        f421_id_unidad_medida = PQ     f421_factor = 12
        f421_cant_pedida      = 36     ← unidad de INVENTARIO (UND)
        f421_cant_pedida_base = 3      ← unidad de la LÍNEA (PQ)
        f421_vlr_bruto        = 3.750  = 3 × 1.250 (el precio es por PQ)

    `API_v2_Items` da `UND` como unidad de inventario del ítem e
    `ItemsUnidadesMedida`, 1 PQ = 12 UND. O sea: `f421_cant_*` ya viene en
    unidad de inventario y `f421_cant_*_base` en la de la línea. La versión
    anterior restaba las `_base` (3 − 0 = 3 paquetes contados como 3 unidades):
    **«en camino» doce veces menor en toda línea PQ**, el déficit inflado en
    lo ya pedido y un contenedor de más (Regla 0: el lado irreversible). En
    las 280 líneas con factor 1 las dos lecturas coinciden; por eso los tests,
    que fabricaban la relación al revés, no lo veían.

    Regla: `f421_cant_pedida − f421_cant_entrada`; si faltan,
    `(pedida_base − entrada_base) × factor`; sin factor, **`None`** (no se
    inventa: se cuenta como línea sin unidad).

    Returns: `(Decimal | None, como)` — `como` ∈ INVENTARIO | FACTOR |
    SIN_UNIDAD_BASE. Nunca negativo: una entrada mayor que lo pedido (exceso)
    no deja «menos que nada» en camino.
    """
    p = _dec(fila.get('f421_cant_pedida'))
    e = _dec(fila.get('f421_cant_entrada'))
    if p is not None and e is not None:
        return max(p - e, Decimal(0)), 'INVENTARIO'
    pb = _dec(fila.get('f421_cant_pedida_base'))
    eb = _dec(fila.get('f421_cant_entrada_base'))
    fac = _dec(fila.get('f421_factor'))
    if pb is not None and eb is not None and fac is not None and fac > 0:
        return max((pb - eb) * fac, Decimal(0)), 'FACTOR'
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

      3. `DECISION_WMS` (2026-09-27) — «lo pedí» registrado en la bandeja
         (`compras_decisiones`) mientras la OC no aparezca en el espejo:
         cuenta hasta lead time + 2σ del proveedor desde el día de la
         decisión. Deja de contarse cuando aparece en el espejo una OC de ese
         SKU del día de la decisión en adelante (o la OC que el comprador
         escribió) — desde ahí la cuenta la fuente 1 —, o cuando vence (y se
         dice). Va a la bodega del CDI. Sin esto, dos personas pedían dos
         veces lo mismo.

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
    try:
        dec = _decisiones_en_camino(filtro_skus, pedidas)
    except Exception as e:     # noqa: BLE001
        logger.error('[COMPRAS] en camino: la fuente DECISION_WMS falló: %s', e)
        errores.append({'fuente': 'DECISION_WMS', 'error': str(e)})
        dec = None

    por_oc = oc['por_sku'] if oc else {}
    vencido = oc['vencido'] if oc else {}
    por_cont = cont['por_sku'] if cont else {}
    sin_unidad = oc['sin_unidad'] if oc else {}
    ocs_de = oc['ocs_de'] if oc else {}
    lineas_de = oc['lineas_de'] if oc else {}
    excluido = oc['excluido'] if oc else {}
    excluidas_de = oc['excluidas_de'] if oc else {}

    por_dec = dec['por_sku'] if dec else {}
    dec_de = dec['detalle'] if dec else {}

    por_sku, detalle = {}, {}
    for ref in (set(por_oc) | set(por_cont) | set(sin_unidad) | set(excluido) | set(por_dec)
                | set(dec_de)):
        cuenta = ref in por_oc or ref in por_cont or ref in sin_unidad or ref in por_dec
        total = (por_oc.get(ref, Decimal(0)) + por_cont.get(ref, Decimal(0))
                 + por_dec.get(ref, Decimal(0)))
        if cuenta:
            por_sku[ref] = float(total)
        detalle[ref] = {
            'oc': float(por_oc.get(ref, 0)),
            'oc_vencida': float(vencido.get(ref, 0)),
            'contenedores': float(por_cont.get(ref, 0)),
            # «Lo pedí» de la bandeja que todavía no aparece como OC.
            'pedido_sin_oc': float(por_dec.get(ref, 0)),
            'decision_pedido': dec_de.get(ref),
            'ocs': sorted(ocs_de.get(ref, ())),
            # Las OCs que cuentan, la más atrasada primero (P1-3): de qué OC es
            # lo «ya pedido» y hace cuántos días debió llegar.
            'lineas_oc': (lineas_de.get(ref) or [])[:5],
            # Lo que NO se cuenta por el corte de antigüedad, y de qué OCs.
            'no_contado_por_viejo': float(excluido.get(ref, 0)),
            'lineas_no_contadas': (excluidas_de.get(ref) or [])[:5],
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
    if dec is not None:
        fuentes['DECISION_WMS'] = {'refs': len(por_dec),
                                   'unidades': round(float(sum(por_dec.values())), 2),
                                   'cubiertos_por_oc': dec['cubiertos'],
                                   'vencidos_sin_oc': dec['vencidos'],
                                   'dias': dec['dias'], 'nota': dec['nota']}
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
            'ocs_vencidas': oc['ocs_vencidas'] if oc else None,
            'corte_antiguedad': oc['corte_antiguedad'] if oc else None,
            'problemas_de_configuracion': oc['problemas'] if oc else [],
            'sync_oc': sync,
        },
    }


def sello_en_camino() -> tuple:
    """Cambia cuando cambia algo de lo que `en_camino` y `lead_time` leen: el
    espejo de OCs, los contenedores, las recepciones, los proveedores y las
    decisiones del comprador. Para la caché del ROP (tanda G)."""
    from sqlalchemy import func
    from app.models.acuerdo_marco import Proveedor
    from app.models.compras_fuentes import OcLineaSiesa
    from app.models.importacion import Contenedor, ItemEnTransito
    from app.models.recepcion import RecepcionMercancia
    from app.services.compras_decisiones import sello as sello_decisiones
    q = db.session.query
    return (
        tuple(q(func.max(OcLineaSiesa.id), func.max(OcLineaSiesa.vista_en),
                func.max(OcLineaSiesa.cerrada_en), func.sum(OcLineaSiesa.pendiente_base)).one()),
        tuple(q(func.max(ItemEnTransito.id), func.count(ItemEnTransito.id)).one()),
        tuple(tuple(x) for x in q(Contenedor.id, Contenedor.estado,
                                  Contenedor.fecha_recepcion_cedi).all()),
        q(func.max(RecepcionMercancia.fecha_confirmacion)).scalar(),
        q(func.max(Proveedor.id)).scalar(),
        sello_decisiones(),
    )


def _dias_env(nombre, defecto):
    """(días, problema). Ausente → `defecto`; ilegible o negativo → `defecto`
    y el problema escrito (no se adivina)."""
    v = (os.getenv(nombre) or '').strip()
    if not v:
        return defecto, None
    try:
        n = int(v)
        if n >= 0:
            return n, None
    except ValueError:
        pass
    return defecto, f'{nombre}={v!r} no es un número de días ≥ 0: se ignora.'


def politica_ocs_viejas() -> dict:
    """Cuándo una OC abierta es «vieja» y si se corta. Una función: la usan
    `en_camino` y la pantalla.

    El corte nace en `DIAS_OC_EXCLUIR_DEFAULT` (180, declarado); `nunca` lo
    apaga. Un valor ILEGIBLE no corta (contar de más achica el déficit: el
    lado que un humano corrige, Regla 0) y se declara."""
    dias, p1 = _dias_env(ENV_OC_VENCIDA, DIAS_OC_VENCIDA)
    crudo = (os.getenv(ENV_OC_EXCLUIR) or '').strip()
    p2 = None
    if not crudo:
        corte, corte_fuente = DIAS_OC_EXCLUIR_DEFAULT, 'DEFAULT_DECLARADO'
    elif crudo.lower() in _SIN_CORTE:
        corte, corte_fuente = None, 'CONFIGURADO'
    else:
        corte, p2 = _dias_env(ENV_OC_EXCLUIR, None)
        corte_fuente = 'CONFIGURADO' if p2 is None else 'ILEGIBLE'
    return {'dias_vencida': dias,
            'dias_vencida_fuente': 'CONFIGURADO' if os.getenv(ENV_OC_VENCIDA, '').strip()
            and not p1 else 'DEFAULT_DECLARADO',
            'excluir_mas_de_dias': corte,
            'excluir_fuente': corte_fuente,
            'problemas': [p for p in (p1, p2) if p]}


def _pendiente_de_ocs_abiertas(filtro_skus, operadas, pedidas) -> dict:
    """Fuente `OC_SIESA` de `en_camino`: el pendiente (unidad de inventario)
    de las líneas de OC abiertas, por SKU, solo a bodegas operadas y pedidas.

    **Una OC abierta con la entrega vencida hace meses probablemente no va a
    llegar** (en QA, el 90 % de lo pendiente es de OCs de más de un año). Se
    suma igual —decisión del dueño: restarla sola sería decidir por él— pero
    se DECLARA con su peso (`ocs_vencidas`); con `COMPRAS_OC_EXCLUIR_MAS_DE_DIAS`
    las más viejas que eso no se suman y también se declara."""
    from app.models.compras_fuentes import OcLineaSiesa
    from app.utils.fecha import dia_operativo

    pol = politica_ocs_viejas()
    hoy = dia_operativo()
    por_sku = defaultdict(Decimal)
    vencido = defaultdict(Decimal)
    ocs_de = defaultdict(set)
    sin_unidad = defaultdict(int)
    fuera_lista_blanca = fuera_de_filtro = sin_bodega = 0
    v_lineas, v_skus, v_mas_vieja = 0, set(), None
    x_lineas, x_unidades, x_skus = 0, Decimal(0), set()
    sin_fecha = 0
    # Por SKU, las OCs que cuentan (con su atraso) y las que no, para que la
    # bandeja diga en palabras de qué OC es lo «ya pedido» (P1-3).
    lineas_de = defaultdict(list)
    excluido = defaultdict(Decimal)
    excluidas_de = defaultdict(list)
    x_por_oc = {}

    filas = (db.session.query(OcLineaSiesa.referencia, OcLineaSiesa.bodega,
                              OcLineaSiesa.pendiente_base, OcLineaSiesa.co,
                              OcLineaSiesa.tipo_docto, OcLineaSiesa.consec_docto,
                              OcLineaSiesa.fecha_entrega, OcLineaSiesa.fecha_oc,
                              OcLineaSiesa.cant_entrada, OcLineaSiesa.estado_oc)
             .filter(OcLineaSiesa.abierta.is_(True))
             .all())
    abiertas_citables = set()
    # ¿Entró algo de la OC? Una OC sin ninguna entrada en 180 días está
    # muerta; una parcial tiene un saldo que nunca se cancela. Se dicen aparte.
    entro_algo = defaultdict(bool)
    for _r, _b, _p, co, tipo, consec, _fe, _fo, entrada, estado in filas:
        clave = f'{co or ""}-{tipo or ""}-{consec or ""}'
        if (entrada is not None and entrada > 0) or estado == 2:
            entro_algo[clave] = True
    for ref, bodega, pendiente, co, tipo, consec, f_entrega, f_oc, _ent, _est in filas:
        ref = (ref or '').strip()
        oc_ref = f'{co or ""}-{tipo or ""}-{consec or ""}'
        abiertas_citables.add(oc_ref)
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
        if pendiente <= 0:
            continue
        pendiente = Decimal(pendiente)
        ref_fecha = f_entrega or f_oc
        atraso = (hoy - ref_fecha).days if ref_fecha else None
        if atraso is None:
            sin_fecha += 1
        linea = {'oc': oc_ref, 'unidades': float(pendiente),
                 'entrega': ref_fecha.isoformat() if ref_fecha else None,
                 'dias_vencida': atraso if atraso is not None and atraso > 0 else 0,
                 'sin_ninguna_entrada': not entro_algo[oc_ref]}
        if (atraso is not None and pol['excluir_mas_de_dias'] is not None
                and atraso > pol['excluir_mas_de_dias']):
            x_lineas += 1
            x_unidades += pendiente
            x_skus.add(ref)
            excluido[ref] += pendiente
            excluidas_de[ref].append(linea)
            o = x_por_oc.setdefault(oc_ref, {'unidades': Decimal(0),
                                             'sin_ninguna_entrada': not entro_algo[oc_ref]})
            o['unidades'] += pendiente
            continue
        por_sku[ref] += pendiente
        ocs_de[ref].add(oc_ref)
        linea['vieja'] = bool(atraso is not None and atraso > pol['dias_vencida'])
        lineas_de[ref].append(linea)
        if atraso is not None and atraso > pol['dias_vencida']:
            vencido[ref] += pendiente
            v_lineas += 1
            v_skus.add(ref)
            v_mas_vieja = ref_fecha if v_mas_vieja is None else min(v_mas_vieja, ref_fecha)

    total = sum(por_sku.values())
    unidades_v = sum(vencido.values())
    pct = round(float(unidades_v / total * 100), 1) if total else None
    ocs_vencidas = {
        'dias': pol['dias_vencida'], 'dias_fuente': pol['dias_vencida_fuente'],
        'lineas': v_lineas, 'unidades': round(float(unidades_v), 2), 'skus': len(v_skus),
        'pct_unidades': pct if v_lineas else (0.0 if total else None),
        'entrega_mas_vieja': v_mas_vieja.isoformat() if v_mas_vieja else None,
        'lineas_sin_fecha_de_entrega': sin_fecha,
        'nota': (f'{pct} % de lo que viene por OCs ({float(unidades_v):,.0f} u en '
                 f'{v_lineas} línea(s)) es de OCs con la entrega vencida hace más de '
                 f'{pol["dias_vencida"]} días (la más vieja, {v_mas_vieja.isoformat()}). '
                 'Se suma igual; si ya no van a llegar, hay que anularlas en Siesa o '
                 f'configurar {ENV_OC_EXCLUIR}.') if v_lineas else None,
    }
    muertas = {k: v for k, v in x_por_oc.items() if v['sin_ninguna_entrada']}
    saldos = {k: v for k, v in x_por_oc.items() if not v['sin_ninguna_entrada']}
    u_muertas = float(sum(v['unidades'] for v in muertas.values()))
    u_saldos = float(sum(v['unidades'] for v in saldos.values()))
    corte = {'dias': pol['excluir_mas_de_dias'], 'fuente': pol['excluir_fuente'],
             'lineas_excluidas': x_lineas, 'ocs_excluidas': len(x_por_oc),
             'unidades_excluidas': round(float(x_unidades), 2), 'skus': len(x_skus),
             'ocs_sin_ninguna_entrada': len(muertas),
             'unidades_sin_ninguna_entrada': round(u_muertas, 2),
             'ocs_con_saldo_parcial': len(saldos),
             # Para administración: la variable que mueve el corte.
             'configurable_con': f'{ENV_OC_EXCLUIR} (días; `nunca` = sin corte)',
             'unidades_saldo_parcial': round(u_saldos, 2),
             'nota': (_nota_corte(pol, x_por_oc, x_unidades, muertas, u_muertas,
                                  saldos, u_saldos) if x_lineas else None)}
    for lst in list(lineas_de.values()) + list(excluidas_de.values()):
        lst.sort(key=lambda x: -(x['dias_vencida'] or 0))
    return {'por_sku': por_sku, 'vencido': vencido, 'ocs_de': ocs_de,
            'lineas_de': lineas_de, 'excluido': excluido, 'excluidas_de': excluidas_de,
            'sin_unidad': sin_unidad,
            'abiertas_citables': abiertas_citables, 'lineas': len(filas),
            'fuera_lista_blanca': fuera_lista_blanca,
            'fuera_de_filtro': fuera_de_filtro, 'sin_bodega': sin_bodega,
            'ocs_vencidas': ocs_vencidas, 'corte_antiguedad': corte,
            'problemas': pol['problemas']}


def _miles(x):
    return f'{float(x):,.0f}'.replace(',', '.')


def _nota_corte(pol, x_por_oc, x_unidades, muertas, u_muertas, saldos, u_saldos):
    """El corte de OCs viejas, en palabras del comprador."""
    partes = []
    if muertas:
        partes.append(f'{len(muertas)} sin ninguna entrada ({_miles(u_muertas)} u)')
    if saldos:
        partes.append(f'{len(saldos)} con el saldo de una entrega parcial ({_miles(u_saldos)} u)')
    return (f'No se cuentan como «ya pedido» {_miles(x_unidades)} u de {len(x_por_oc)} '
            f'orden(es) de compra con la entrega vencida hace más de '
            f'{pol["excluir_mas_de_dias"]} días: {" y ".join(partes)}. Si alguna todavía va '
            f'a llegar, confírmelo con el proveedor; si no, anúlela en Siesa para que '
            f'deje de aparecer.')


def _decisiones_en_camino(filtro_skus, pedidas) -> dict:
    """Fuente `DECISION_WMS` de `en_camino`: lo que el comprador marcó «lo
    pedí» y el espejo todavía no muestra como OC.

    Se deja de contar cuando aparece en el espejo (abierta o ya cumplida) una
    línea de ese SKU con la OC del día de la decisión en adelante, o la OC que
    el comprador escribió: desde ahí la cuenta la fuente `OC_SIESA` (contar
    las dos sería pedir de menos: el lado corregible, pero no hace falta). Un
    «lo pedí» vencido sin OC no se cuenta y se declara: la OC nunca apareció."""
    from app.models.compras_fuentes import OcLineaSiesa
    from app.services import compras_decisiones as cd

    cdi = _bodega_cdi()
    por_sku, detalle = defaultdict(Decimal), {}
    cubiertos = vencidos = 0
    pedidos = cd.pedidos_sin_oc(filtro_skus)
    if pedidos and cdi in pedidas:
        refs = {p['referencia'] for p in pedidos}
        lineas = (db.session.query(OcLineaSiesa.referencia, OcLineaSiesa.fecha_oc,
                                   OcLineaSiesa.consec_docto)
                  .filter(OcLineaSiesa.referencia.in_(refs)).all())
        ocs_de = defaultdict(list)
        for r, f, consec in lineas:
            ocs_de[(r or '').strip()].append((f, consec))
        for p in pedidos:
            ref = p['referencia']
            consec = cd.consec_de(p['oc_siesa'])
            aparecio = any((f is not None and f >= p['dia']) or (consec and c == consec)
                           for f, c in ocs_de.get(ref, ()))
            info = {'decision_id': p['id'], 'unidades': float(p['unidades'] or 0),
                    'dia': p['dia'].isoformat(), 'oc_siesa': p['oc_siesa'],
                    'usuario_nombre': p['usuario_nombre'],
                    'vigente_hasta': p['vigente_hasta'].isoformat(),
                    'vence': p['vigente_hasta'].isoformat()}
            if aparecio:
                cubiertos += 1
                detalle[ref] = dict(info, estado='OC_EN_SIESA')
                continue
            if p['vencida']:
                vencidos += 1
                detalle[ref] = dict(info, estado='VENCIDO_SIN_OC')
                continue
            por_sku[ref] += Decimal(p['unidades'] or 0)
            detalle[ref] = dict(info, estado='CUENTA')
    dias = cd.dias_pedido_en_camino()
    return {'por_sku': por_sku, 'detalle': detalle, 'cubiertos': cubiertos,
            'vencidos': vencidos, 'dias': dias['dias'],
            'nota': (f'{vencidos} «ya se pidió» de la bandeja pasaron lo que tarda en llegar '
                     'sin que la orden apareciera en Siesa: ya no se cuentan como en camino '
                     'y la línea vuelve marcada. Revise si la orden se hizo.')
            if vencidos else None}


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
    completa = (ok or {}).get('inicio') if ok else None
    hace_min = None
    if completa:
        try:
            hace_min = int((datetime.utcnow() - datetime.fromisoformat(
                str(completa).replace('Z', '').split('+')[0])).total_seconds() // 60)
        except (TypeError, ValueError):
            hace_min = None
    return {
        'nunca_corrio': ult is None,
        'completa_hace_min': hace_min,
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

    **Una entrada el mismo día de la OC no es una observación** (`descartadas
    ['oc_registrada_al_recibir']`, también por proveedor): la OC se digitó al
    recibir la mercancía y los «0 días» miden eso, no al proveedor. Un
    proveedor que solo tiene OCs así cae al origen y, sin muestra, al default
    declarado.
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
    por_proveedor = defaultdict(lambda: defaultdict(int))
    for k, o in ocs.items():
        entrada, fuente = (wms[k], 'WMS_RECEPCION') if k in wms else (o['siesa'], 'SIESA')
        if entrada is None:
            descartadas['sin_entrada_todavia'] += 1
            continue
        dias = (entrada - o['fecha_oc']).days
        if dias < 0:
            descartadas['entrada_antes_de_la_oc'] += 1
            por_proveedor[o['proveedor_codigo']]['entrada_antes_de_la_oc'] += 1
            continue
        if dias < DIAS_MIN_OBSERVACION:
            # La OC se registró el mismo día que entró la mercancía: se hizo
            # AL RECIBIR (verificado en QA: DISPAPELES, 24 de 24 con 0 días,
            # creada y cumplida con minutos de diferencia). No mide cuánto
            # tarda el proveedor sino cuánto tardó alguien en digitarla.
            # Publicarla como «0 días, MEDIDO, confianza ALTA» achica el ROP
            # (LT 0 → ni demanda de reposición ni colchón): se cuenta aparte.
            descartadas[DESCARTE_OC_AL_RECIBIR] += 1
            por_proveedor[o['proveedor_codigo']][DESCARTE_OC_AL_RECIBIR] += 1
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
            'descartadas': dict(descartadas),
            'descartadas_por_proveedor': {p: dict(v) for p, v in por_proveedor.items()}}


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
      3. el default declarado (`default_lead_time`: 10±5 nacional, configurable
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
    aviso_desc = None
    if proveedor:
        dias = [o['dias'] for o in obs['por_oc'] if o['proveedor_codigo'] == proveedor]
        n_prov = len(dias)
        desc = (obs.get('descartadas_por_proveedor') or {}).get(proveedor) or {}
        n_al_recibir = desc.get(DESCARTE_OC_AL_RECIBIR, 0)
        if n_al_recibir:
            extra['descartadas_al_recibir'] = n_al_recibir
            aviso_desc = (f'{n_al_recibir} OC(s) de {proveedor} se registraron el mismo día '
                          'que entró la mercancía: no miden al proveedor y no se usan.')
        if n_prov >= N_MIN_PARCIAL:
            r = _medido(dias, 'PROVEEDOR', piso, **extra)
            if aviso_desc:
                r['nota'] = f'{aviso_desc} {r["nota"]}' if r['nota'] else aviso_desc
            return r

    pool = (list(obs['contenedores']) if es_china
            else [o['dias'] for o in obs['por_oc'] if o['nacional']])
    if len(pool) >= N_MIN_PARCIAL:
        r = _medido(pool, 'ORIGEN', piso, **extra)
        if proveedor:
            aviso = (f'El proveedor {proveedor} tiene {n_prov} OC(s) medida(s) '
                     f'(mínimo {N_MIN_PARCIAL}): se usa el del origen.')
            if aviso_desc:
                aviso = f'{aviso_desc} {aviso}'
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
                f'SUPUESTO'
                + ('' if es_china else
                   ' (en producción, el 27-sep-2026, 42 órdenes con entrada parcial '
                   'tardaron una mediana de 10 días)')
                + ('' if es_china else
                   f' (configurable con {ENV_LT_NACIONAL} / {ENV_SIGMA_LT_NACIONAL})')
                + '.')
    if aviso_desc:
        nota = f'{aviso_desc} {nota}'
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
    desc = obs.get('descartadas_por_proveedor') or {}
    # También los que solo tienen OCs descartadas: que no aparezcan escondería
    # justo al proveedor cuyo «0 días» se dejó de creer.
    codigos = sorted({o['proveedor_codigo'] for o in obs['por_oc'] if o['proveedor_codigo']}
                     | {c for c, d in desc.items() if c and d.get(DESCARTE_OC_AL_RECIBIR)})
    salida = []
    for c in codigos:
        nacional = all(o['nacional'] for o in obs['por_oc'] if o['proveedor_codigo'] == c)
        r = lead_time(proveedor=c, origen=ORIGEN_NACIONAL if nacional else ORIGEN_CHINA,
                      observaciones=obs)
        r = {k: v for k, v in r.items() if k != 'lead_times'}
        r['n_proveedor'] = sum(1 for o in obs['por_oc'] if o['proveedor_codigo'] == c)
        r['n_descartadas_al_recibir'] = (desc.get(c) or {}).get(DESCARTE_OC_AL_RECIBIR, 0)
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
    por la unidad de la LÍNEA (`f421_id_unidad_medida`; verificado en vivo:
    `f421_vlr_bruto` = `f421_cant_pedida_base` × precio). Precio ÷ factor. Sin
    factor no se convierte — no se inventa."""
    p = _dec(linea.precio_unitario)
    fac = _dec(linea.factor)
    if p is None or p <= 0 or fac is None or fac <= 0:
        return None
    return p / fac


def _cantidad_base(linea):
    """Lo pedido en la línea, en unidad de INVENTARIO (la de `_precio_base`):
    `cant_pedida` —que en la API de Siesa ya es unidad de inventario, ver
    `pendiente_de_linea`—; sin ella, `cant_pedida_base × factor` (la `_base`
    es la unidad de la línea). Precio base × esta cantidad = valor bruto de
    la línea (verificado: 1.250/12 × 36 = 3.750 en la OC 003-OC-28)."""
    p = _dec(linea.cant_pedida)
    if p is not None:
        return p
    pb, fac = _dec(linea.cant_pedida_base), _dec(linea.factor)
    if pb is not None and fac is not None and fac > 0:
        return pb * fac
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
    from app.models.acuerdo_marco import Proveedor
    lineas = q.all()
    moneda_prov = {}
    if any(not (l.moneda or '').strip() for l in lineas):
        moneda_prov = {c: (m or '').strip().upper() for c, m in db.session.query(
            Proveedor.codigo, Proveedor.moneda).filter(Proveedor.moneda.isnot(None)).all()}
    salida = []
    for l in lineas:
        if l.estado_oc == ESTADO_OC_ANULADA or (l.ind_obsequio or 0) == 1:
            continue
        p = _precio_base(l)
        if p is None:
            continue
        moneda, moneda_fuente = _moneda_de_linea(l, moneda_prov)
        salida.append({
            'referencia': (l.referencia or '').strip(), 'precio_base': p,
            'moneda': moneda, 'moneda_fuente': moneda_fuente,
            'cantidad_base': _cantidad_base(l), 'fecha_oc': l.fecha_oc,
            'proveedor_codigo': l.proveedor_codigo, 'oc': l.oc_referencia,
        })
    return salida


def _moneda_de_linea(linea, moneda_prov):
    """La moneda de la OC; si la OC no la trae, la del proveedor en el maestro
    de Siesa (`Proveedor.moneda`, m047); si tampoco, COP **declarado**
    (`SUPUESTA_COP`). Returns: (moneda, fuente ∈ OC | PROVEEDOR | SUPUESTA_COP)."""
    m = (linea.moneda or '').strip().upper()
    if m:
        return m, 'OC'
    m = moneda_prov.get(linea.proveedor_codigo)
    if m:
        return m, 'PROVEEDOR'
    return 'COP', 'SUPUESTA_COP'


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
                'moneda': l['moneda'], 'moneda_fuente': l['moneda_fuente'],
                'conversion': conversion,
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


# ══════════════════════════════════════════════════════════════════════════════
# Lo que el comprador necesita para armar una OC (2026-09-25, la bandeja)
# ══════════════════════════════════════════════════════════════════════════════

def empaque_de_compra(refs) -> dict:
    """¿En qué empaque se le compra este SKU al proveedor? — por SKU, con su
    procedencia. La cantidad a pedir se redondea a este empaque
    (`armador_service.pedido_en_empaques`).

    Cascada, de lo más cercano a la compra a lo más lejano:
      1. `OC_SIESA`: la unidad y el factor de la línea de la OC más reciente
         (lo que el proveedor factura: una caja de 12, una paca de 500).
      2. `MAESTRO_SIESA`: `Producto.factor_conversion`/`unidad_empaque` que
         trae el sync del catálogo (el empaque de venta, no necesariamente el
         de compra — declarado).
      3. `SIN_EMPAQUE`: se pide por unidad y se dice.

    El MOQ del proveedor nacional no vive en ninguna fuente: `moq_empaques=1`
    con `moq_fuente='SIN_DATO'` (Regla 0: no se inventa un mínimo).

    Returns: {ref: {unidades_por_empaque, unidad, moq_empaques, moq_fuente,
                    fuente, oc, nota}}
    """
    from app.models.compras_fuentes import OcLineaSiesa
    from app.models.producto import Producto
    refs = [r for r in (refs or []) if r]
    if not refs:
        return {}
    mejor = {}
    q = (OcLineaSiesa.query
         .filter(OcLineaSiesa.referencia.in_(refs), OcLineaSiesa.fecha_oc.isnot(None)))
    for l in q.all():
        if l.estado_oc == ESTADO_OC_ANULADA or (l.ind_obsequio or 0) == 1:
            continue
        fac = _dec(l.factor)
        if fac is None or fac <= 0:
            continue
        ref = (l.referencia or '').strip()
        clave = (l.fecha_oc, l.consec_docto or 0)
        if ref not in mejor or clave > mejor[ref][0]:
            mejor[ref] = (clave, l)
    maestro = {p.codigo_siesa: p for p in
               Producto.query.filter(Producto.codigo_siesa.in_(refs)).all()}
    salida = {}
    for ref in refs:
        base = {'moq_empaques': 1, 'moq_fuente': 'SIN_DATO', 'oc': None, 'nota': None}
        if ref in mejor:
            l = mejor[ref][1]
            u = int(_dec(l.factor))
            salida[ref] = dict(base, unidades_por_empaque=max(1, u),
                               unidad=(l.unidad_medida or '').strip() or 'UND',
                               fuente='OC_SIESA', oc=l.oc_referencia)
            continue
        p = maestro.get(ref)
        fc = int(getattr(p, 'factor_conversion', 1) or 1) if p else 1
        if p is not None and fc > 1:
            salida[ref] = dict(base, unidades_por_empaque=fc,
                               unidad=(p.unidad_empaque or '').strip() or 'EMPAQUE',
                               fuente='MAESTRO_SIESA',
                               nota='Empaque del catálogo de Siesa: puede no ser el '
                                    'del proveedor.')
            continue
        salida[ref] = dict(base, unidades_por_empaque=1,
                           unidad=((p.unidad_medida if p else None) or 'UND'),
                           fuente='SIN_EMPAQUE',
                           nota='Sin empaque conocido: se pide por unidad.')
    return salida


def proveedores_info(codigos) -> dict:
    """{codigo: {codigo, nombre, nit, pais, fuente}} — el maestro de
    proveedores, con la razón social y el NIT de la OC más reciente cuando el
    maestro no la tiene (el espejo trae los dos)."""
    from app.models.acuerdo_marco import Proveedor
    from app.models.compras_fuentes import OcLineaSiesa
    codigos = [c for c in (codigos or []) if c]
    if not codigos:
        return {}
    salida = {}
    for p in Proveedor.query.filter(Proveedor.codigo.in_(codigos)).all():
        salida[p.codigo] = {'codigo': p.codigo, 'nombre': p.nombre, 'nit': p.nit,
                            'pais': p.pais, 'fuente': p.fuente or 'MAESTRO'}
    faltan = [c for c in codigos if c not in salida or not salida[c].get('nit')]
    if faltan:
        filas = (db.session.query(OcLineaSiesa.proveedor_codigo, OcLineaSiesa.proveedor_nombre,
                                  OcLineaSiesa.proveedor_nit, OcLineaSiesa.fecha_oc)
                 .filter(OcLineaSiesa.proveedor_codigo.in_(faltan))
                 .order_by(OcLineaSiesa.fecha_oc.desc()).all())
        for cod, nom, nit, _f in filas:
            d = salida.setdefault(cod, {'codigo': cod, 'nombre': None, 'nit': None,
                                        'pais': None, 'fuente': 'SIESA_OC'})
            d['nombre'] = d['nombre'] or nom
            d['nit'] = d['nit'] or nit
    return salida


def ocs_abiertas(hoy=None) -> dict:
    """Las OCs abiertas del espejo, por proveedor, con lo que falta por entrar
    de cada línea (`pendiente_base`, en unidad de inventario) y si ya pasó su
    fecha de entrega. Lectura del espejo: sin sincronización completa lo dice
    (`frescura`).

    Una línea está **atrasada** si le falta mercancía y su `f421_fecha_entrega`
    ya pasó (día Bogotá). Una línea sin fecha de entrega no es «al día»: se
    cuenta en `sin_fecha_entrega`.

    Returns: {proveedores: [{codigo, nombre, nit, ocs: [{oc, fecha_oc,
              fecha_entrega, dias_atraso, atrasada, moneda, lineas: [...]}],
              atrasadas}], total_ocs, atrasadas, sin_fecha_entrega,
              lineas_sin_unidad_base, frescura}
    """
    from app.models.compras_fuentes import OcLineaSiesa
    from app.models.producto import Producto
    from app.utils.fecha import dia_operativo
    hoy = hoy or dia_operativo()
    lineas = (OcLineaSiesa.query.filter(OcLineaSiesa.abierta.is_(True))
              .order_by(OcLineaSiesa.fecha_oc, OcLineaSiesa.consec_docto).all())
    refs = sorted({(l.referencia or '').strip() for l in lineas if l.referencia})
    nombres = {p.codigo_siesa: p.nombre for p in
               Producto.query.filter(Producto.codigo_siesa.in_(refs)).all()} if refs else {}
    por_oc, sin_fecha, sin_base = {}, 0, 0
    for l in lineas:
        if l.estado_oc == ESTADO_OC_ANULADA:
            continue
        pend = _dec(l.pendiente_base)
        if pend is None:
            sin_base += 1
        elif pend <= 0:
            continue
        ref = (l.referencia or '').strip()
        atrasada = bool(pend is not None and pend > 0 and l.fecha_entrega
                        and l.fecha_entrega < hoy)
        if l.fecha_entrega is None:
            sin_fecha += 1
        oc = por_oc.setdefault(l.oc_referencia, {
            'oc': l.oc_referencia, 'proveedor_codigo': l.proveedor_codigo,
            'proveedor_nombre': l.proveedor_nombre, 'proveedor_nit': l.proveedor_nit,
            'fecha_oc': l.fecha_oc.isoformat() if l.fecha_oc else None,
            'moneda': (l.moneda or 'COP').strip().upper() or 'COP',
            'estado_oc': l.estado_oc, 'lineas': [], '_entregas': []})
        precio = _precio_base(l)
        oc['lineas'].append({
            'referencia': ref, 'nombre': nombres.get(ref),
            'bodega': l.bodega,
            'pendiente_unidades': float(pend) if pend is not None else None,
            'fecha_entrega': l.fecha_entrega.isoformat() if l.fecha_entrega else None,
            'atrasada': atrasada,
            'dias_atraso': (hoy - l.fecha_entrega).days if atrasada else 0,
            'precio_unidad': float(precio) if precio is not None else None,
            'valor_pendiente': (float(precio * pend) if precio is not None and pend is not None
                                else None),
        })
        if l.fecha_entrega:
            oc['_entregas'].append(l.fecha_entrega)
    proveedores = {}
    for oc in por_oc.values():
        entregas = oc.pop('_entregas')
        oc['fecha_entrega'] = min(entregas).isoformat() if entregas else None
        oc['atrasada'] = any(x['atrasada'] for x in oc['lineas'])
        oc['dias_atraso'] = max((x['dias_atraso'] for x in oc['lineas']), default=0)
        vals = [x['valor_pendiente'] for x in oc['lineas']]
        oc['valor_pendiente'] = sum(v for v in vals if v is not None) if any(
            v is not None for v in vals) else None
        oc['valor_es_cota_inferior'] = any(v is None for v in vals)
        p = proveedores.setdefault(oc['proveedor_codigo'], {
            'codigo': oc['proveedor_codigo'], 'nombre': oc['proveedor_nombre'],
            'nit': oc['proveedor_nit'], 'ocs': []})
        p['ocs'].append(oc)
    lista = list(proveedores.values())
    for p in lista:
        p['atrasadas'] = sum(1 for o in p['ocs'] if o['atrasada'])
        p['ocs'].sort(key=lambda o: (not o['atrasada'], o['fecha_entrega'] or '9999'))
    lista.sort(key=lambda p: (-p['atrasadas'], (p['nombre'] or '')))
    return {
        'proveedores': lista,
        'total_ocs': len(por_oc),
        'atrasadas': sum(1 for o in por_oc.values() if o['atrasada']),
        'sin_fecha_entrega': sin_fecha,
        'lineas_sin_unidad_base': sin_base,
        'frescura': frescura_oc(),
    }


def _clave_oc(co, tipo, consec):
    try:
        consec = int(str(consec).strip())
    except (TypeError, ValueError):
        consec = (str(consec or '')).strip()
    return f'{(co or "").strip()}-{(tipo or "").strip()}-{consec}'


def llegadas_recientes(dias: int = 30) -> dict:
    """Lo que llegó en los últimos `dias` (día Bogotá). Dos fuentes, y dice cuál
    (P2-3, 2026-09-27):

      1. **Siesa** (`ENTRADA_SIESA`): las OCs del espejo con una marca de
         entrada en la ventana — `f420_fecha_ts_cumplido` (se completó) o
         `f420_fecha_ts_parcial` (primera entrada). En producción las entradas
         se hacen en Siesa: sin esta fuente la sección salía vacía mientras la
         mercancía sí entraba. La cantidad es lo ENTRADO ACUMULADO de la OC
         (`f421_cant_entrada`, unidad de inventario), no solo lo de la ventana:
         Siesa no guarda la historia de entradas en esta consulta, y se dice.
      2. **El muelle del WMS** (`MUELLE_WMS`): recepciones CONFIRMADAS, con la
         fecha física. Una OC recibida por el muelle aparece una vez: gana el
         muelle (su fecha es la del recibo, la de Siesa la del documento).
    """
    from datetime import timedelta
    from sqlalchemy import or_
    from app.models.compras_fuentes import OcLineaSiesa
    from app.models.recepcion import EstadoRecepcion, RecepcionMercancia
    from app.utils.fecha import dia_operativo, dia_operativo_de
    dias = int(dias)
    desde_dia = dia_operativo() - timedelta(days=dias)
    desde = datetime.utcnow() - timedelta(days=dias)

    filas = (RecepcionMercancia.query
             .filter(RecepcionMercancia.estado == EstadoRecepcion.CONFIRMADA,
                     RecepcionMercancia.fecha_confirmacion >= desde)
             .order_by(RecepcionMercancia.fecha_confirmacion.desc()).limit(200).all())
    recepciones = [{
        'codigo': r.codigo, 'oc': r.numero_oc_siesa,
        'proveedor_codigo': r.proveedor_codigo, 'proveedor_nombre': r.proveedor_nombre,
        'dia': dia_operativo_de(r.fecha_confirmacion).isoformat(),
        'parcial': bool(r.es_parcial),
        'lineas': len(r.items),
        'unidades': sum(int(i.cantidad_recibida or 0) for i in r.items),
        'fuente': 'MUELLE_WMS',
    } for r in filas]
    del_muelle = {_clave_oc(r.co_oc_siesa, r.tipo_docto_oc_siesa, r.consec_docto_oc_siesa)
                  for r in filas if r.consec_docto_oc_siesa}

    # Siesa escribe sus marcas en hora local (Bogotá): el día es la fecha.
    desde_ts = datetime.combine(desde_dia, datetime.min.time())
    lineas = (OcLineaSiesa.query
              .filter(or_(OcLineaSiesa.fecha_cumplido >= desde_ts,
                          OcLineaSiesa.fecha_parcial >= desde_ts))
              .all())
    por_oc = {}
    for l in lineas:
        if l.estado_oc == ESTADO_OC_ANULADA:
            continue
        clave = l.oc_referencia
        if clave in del_muelle:
            continue
        cumplida = bool(l.fecha_cumplido and l.fecha_cumplido >= desde_ts)
        marca = l.fecha_cumplido if cumplida else l.fecha_parcial
        o = por_oc.setdefault(clave, {
            'oc': clave, 'proveedor_codigo': l.proveedor_codigo,
            'proveedor_nombre': l.proveedor_nombre, 'dia': marca.date().isoformat(),
            'completa': cumplida, 'parcial': not cumplida,
            'lineas': 0, 'unidades': 0.0, 'lineas_sin_unidad': 0,
            'fuente': 'ENTRADA_SIESA'})
        entrada = _dec(l.cant_entrada)
        if entrada is None:
            o['lineas_sin_unidad'] += 1
        elif entrada > 0:
            o['lineas'] += 1
            o['unidades'] += float(entrada)
    entradas = sorted(por_oc.values(), key=lambda x: x['dia'], reverse=True)[:200]
    return {
        'dias': dias,
        'recepciones': recepciones,
        'entradas_siesa': entradas,
        'fuentes': {
            'ENTRADA_SIESA': {'ocs': len(entradas), 'frescura': frescura_oc()},
            'MUELLE_WMS': {'recepciones': len(recepciones)},
        },
        'nota_siesa': ('Según las órdenes de compra sincronizadas con Siesa: la fecha es la '
                       'de la primera entrada o la de la que completó la orden, y las '
                       'unidades son todo lo entrado a esa orden hasta hoy.'),
    }
