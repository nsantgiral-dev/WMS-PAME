"""
Cómo se interpreta la condición de pago de un pedido. **Una función.**

`f430_id_cond_pago` viene del pedido de Siesa y puede llegar vacío cuando el
maestro del cliente está incompleto. Hasta el 2026-08-13 ese vacío se
interpretaba en **dos sitios**, los dos hacia contado:

  · `connekta_gateway.trigger_factura_desde_remision` — al facturar caía a
    `SIESA_COND_PAGO_VENTAS`, **que es el código de contado**. Hoy cae a
    `SIESA_COND_PAGO_RUTA` (crédito a un día): ver `aprobable_en_ruta`.
  · `ruta_service._valor_y_cond_pago` — `es_contado = (not cond_pago) or ...`,
    que con vacío da **True**.

El segundo llega a la pantalla del conductor: `rutas.js` solo muestra el modo
CRÉDITO cuando `es_contado === false` **confirmado**. Con vacío mostraba «Valor
a Cobrar» — es decir, **al conductor se le pedía cobrarle a un cliente cuya
condición de pago nadie conocía**.

## Por qué «no sé» no puede colapsar a «contado»

Un vacío significa tres cosas distintas que no se distinguen entre sí: el
maestro está incompleto, la consulta falló, o el cliente de verdad es de
contado. Tratarlas igual es la Regla 0 al revés — el lado conservador acá no es
cobrar, es **no afirmar**.

El daño en pantalla es acotado: el conductor cobra de más y se devuelve. El
daño del otro lado no lo era — el vacío emitía una factura de contado, y el
2026-08-13 se probó en producción que **Siesa no la aprueba**. Quedaba el
inventario descargado, la factura en Elaboración y la liquidación sin CxC
contra la cual cruzar el recibo. Un campo vacío rompía el ciclo entero.

## Lo que NO hace esta función

No decide qué hacer con cada caso: eso depende del llamador. La pantalla puede
mostrar «no sé» y dejar que el conductor elija; el gateway **no puede** mandar
null y tiene que caer a algo. Lo que sí es una sola política es **cómo se lee
el vacío**, y esa es la que vivía duplicada.
"""
import logging

logger = logging.getLogger(__name__)

CONTADO = 'contado'
CREDITO = 'credito'
#: Ni contado ni crédito: **no se sabe**. No es un tercer tipo de venta, es la
#: ausencia del dato — y se declara en vez de resolverse hacia un lado.
AUSENTE = 'ausente'


def clasificar(cond_pago_siesa, cond_pago_contado) -> str:
    """`contado` | `credito` | `ausente` a partir de `f430_id_cond_pago`.

    `cond_pago_contado` es el código que la empresa usa para contado
    (`SIESA_COND_PAGO_VENTAS`). Se configura para **reconocerlo, no para
    emitirlo** — ver `aprobable_en_ruta`.
    """
    valor = (cond_pago_siesa or '').strip()
    if not valor:
        return AUSENTE
    return CONTADO if valor == (cond_pago_contado or '').strip() else CREDITO


def aprobable_en_ruta(cond_pago, cond_pago_contado) -> bool:
    """`False` si esa condición produce una factura que Siesa **no va a aprobar**.

    Probado en producción el 2026-08-13: dos facturas de contado, por $263.963
    y $14.200, quedaron **en Elaboración** con el mensaje *«el valor de la
    cartera debe ser igual al valor de las CxC»*. Es el mismo mensaje de la
    Regla 21 — no es una rareza de la nota crédito, es el invariante de
    aprobación de Siesa: **un documento no se aprueba si su cartera no cuadra.**
    Una FE de contado exige el recaudo en el mismo documento, y en ruta ese
    recaudo no existe todavía: lo hace el conductor horas después.

    Por eso la factura de ruta nace **a crédito de un día** y el recibo de caja
    del conductor la salda. No es un rodeo: es lo que físicamente pasa.

    Y por eso `f462_id_caja` del 142943 se manda vacío. Llenarlo haría que
    Siesa registrara el ingreso al facturar —plata que nadie ha recibido— y
    otra vez cuando llegue el RC de la liquidación.

    Vive acá y no en el gateway porque la misma pregunta la hace el desglose
    para contar cuántos pedidos de ruta vienen con una condición que no sirve.
    """
    return clasificar(cond_pago, cond_pago_contado) != CONTADO


def es_contado_o_none(cond_pago_siesa, cond_pago_contado):
    """`True` | `False` | `None` — **contado documental**, no cobro en la puerta.

    `None` es el caso que antes se colapsaba a `True`. El frontend ya sabe
    manejarlo: cae al campo libre en vez de mostrar un valor a cobrar que nadie
    confirmó.

    ⚠️ Para decidir qué ve el conductor, la función es
    `cobra_en_la_puerta` — no ésta. Ver el bloque de abajo.
    """
    clase = clasificar(cond_pago_siesa, cond_pago_contado)
    if clase == AUSENTE:
        return None
    return clase == CONTADO


# ── Contado documental ≠ cobro en la puerta ───────────────────────────────
#
# Son **dos preguntas distintas** y solo coinciden en C01. Durante meses las
# contestó una sola función, y por eso el conductor no veía el cobro:
#
#                                    C01   C02   C04
#     ¿la FE trae su propio recaudo?  sí    no    no    ← clasificar()
#     ¿el conductor cobra?            sí    sí    no    ← cobra_en_la_puerta()
#
# La ruta factura en C02 **precisamente porque** C01 es imposible: una FE de
# contado no se aprueba sin el recaudo dentro del documento, y ese recaudo lo
# hace el conductor horas después (ver `aprobable_en_ruta`). Es decir: el
# motivo por el que C02 no es contado documental es el mismo por el que **sí**
# se cobra en la puerta. Una función no puede contestar las dos.
#
# `clasificar` no se toca: gobierna si la factura se puede aprobar. Cambiarla
# para arreglar la pantalla rompería la emisión.

def cond_pago_efectiva(cond_pago_siesa, cond_pago_ruta):
    """La condición con la que el documento **sale de verdad**, no la que el
    pedido declara. `''` si no se puede determinar.

    El pedido puede venir sin condición —maestro del tercero incompleto— y el
    gateway no puede emitir `null`: cae a la condición de ruta. Esa caída es
    una decisión con consecuencia, y hasta el 2026-08-14 estaba escrita **dos
    veces con resultados opuestos**:

        pedido sin condición
          ├── FE       → cae a C02  → «la salda el RC del conductor» (dice la alerta)
          └── pantalla → se queda en ausente → LIBRE → no se le pide cobrar

    La factura salía en una condición que exige cobro y la pantalla no lo
    pedía. Es el mismo defecto que `cobra_en_la_puerta` arregla,
    reintroducido por el caso ausente — y es el corolario de la Regla 0: **una
    política, una función.** El mismo fallback en dos sitios diverge.

    No es afirmar sobre un dato ausente: es leer lo que ya se emitió. La
    ausencia sigue declarándose por su canal (`registrar_ausencia` y la alerta
    `DATA_MAESTRA_COND_PAGO`) — lo que deja de pasar es que el documento diga
    una cosa y la pantalla otra.
    """
    return (cond_pago_siesa or '').strip() or (cond_pago_ruta or '').strip()


def cobra_en_la_puerta(cond_pago_siesa, cond_pago_contado=None, cond_pago_ruta=None) -> bool:
    """`True` | `False` — ¿el conductor cobra en esta parada?

    **Desde el 2026-09-24 es un atajo de `cobro_contraentrega`**, no una
    política propia (ver el bloque «Contado contraentrega vs crédito real»
    más abajo). Antes cobraba SOLO con el código de contado o exactamente
    `SIESA_COND_PAGO_RUTA`: una factura C03 (8 días) salía en pantalla como
    «💳 no se cobra», el conductor la entregaba sin pedir plata y la
    liquidación no hacía nada. La regla del dueño es por **días**: ≤ 15 es
    contado contraentrega, > 15 crédito real.

    Ya no devuelve `None`: un dato ausente **se cobra** (declarado con su
    origen en `cobro_contraentrega`). `cond_pago_contado`/`cond_pago_ruta` se
    aceptan por compatibilidad y no deciden nada — el vacío se lee igual que
    antes a efectos del cobro (la FE sale en la condición de ruta, que se
    cobra), pero ahora con origen `SUPUESTO_AUSENTE` en vez de un `True`
    indistinguible de un dato leído.
    """
    return cobro_contraentrega(cond_pago_siesa)['cobrar']


def registrar_ausencia(contexto: str, tercero: str = ''):
    """Deja rastro cuando el dato falta. **No es cosmético.**

    La pregunta «¿esto pasa alguna vez?» se estuvo discutiendo con cifras de
    otro sistema —conteos de facturas— que no pueden detectarlo: el fallback
    rellena el campo antes de emitir, así que toda factura sale con condición.
    Contar facturas mira el único lugar donde la evidencia está garantizada
    limpia.

    Lo que sí lo detecta es esto.
    """
    logger.warning(
        '[COND_PAGO] ausente en %s%s — no se asume contado',
        contexto, f' (tercero {tercero})' if tercero else '',
    )


# ── Modo de la pantalla del conductor ─────────────────────────────────────
#: Los tres modos que puede mostrar la parada. Vivían calculados en
#: `rutas.js:1908` y no se podían contar: el desglose sabía qué eligió el
#: conductor, no qué opciones tenía enfrente.
CREDITO_PANTALLA = 'CREDITO'   # no se cobra en la puerta, no se pregunta monto
DINAMICO = 'DINAMICO'          # contado confirmado y valor conocido → Total/Parcial
LIBRE = 'LIBRE'                # **el conductor elige sin restricción**


def modo_pantalla(se_cobra_en_puerta, hay_valor_conocido: bool) -> str:
    """Qué modo de pago ve el conductor en esa parada.

    El primer argumento es **`cobra_en_la_puerta`**, no `es_contado`. Hasta
    el 2026-08-14 recibía el segundo, y como toda venta de ruta es C02 —que no
    es contado documental— **ninguna parada pedía cobrar**. Ver el bloque
    «Contado documental ≠ cobro en la puerta» arriba.

    `LIBRE` es donde vive el riesgo y por eso hay que poder contarlo: sobre una
    parada sin condición conocida, `confirmar_parada` **no bloquea** el crédito
    —no saber no es evidencia (Regla 0)— así que el conductor puede marcar
    CREDITO sobre algo que sí había que cobrar.

    Un pedido con `cond_pago` vacío cae acá en LIBRE, porque no hay `True`
    confirmado. Del lado de la factura ese mismo vacío ya no hace daño —cae a
    la condición de ruta— pero la parada sigue quedando sin restricción.

    Es `True` | `False` | `None`. Solo el `False` **confirmado** muestra
    CRÉDITO: ante `None` no se afirma nada y se deja elegir, que es lo correcto
    — pero hay que saber cuántas veces pasa.
    """
    if se_cobra_en_puerta is False:
        return CREDITO_PANTALLA
    if se_cobra_en_puerta is True and hay_valor_conocido:
        return DINAMICO
    return LIBRE


# ═════════════════════════════════════════════════════════════════════════════
# Contado contraentrega vs crédito real (2026-09-24)
# ═════════════════════════════════════════════════════════════════════════════
#
# REGLA DEL DUEÑO: las facturas de ruta salen en Siesa como «crédito» corto
# solo por lo que tarda la ruta; en la calle se cobran **contraentrega**. Toda
# condición con ≤ 15 días de crédito se trata en el WMS SOLO como contado: el
# conductor la cobra y la liquidación la exige. > 15 días es crédito real y se
# entrega sin cobrar.
#
# «Hasta 15 días las condiciones de pago son de contado, después es crédito.
# Si digamos tiene 1 día, debes tener en cuenta que toca esperar el despacho y
# también el tiempo de ruta.» — por eso el vencimiento de la FE de contado es
# la ventana entera (15 días) y no el día de la condición (`vencimiento_fe`).
#
# **Una política, una función.** `cobro_contraentrega` es la única que
# contesta «¿se cobra?». La pantalla, la guarda del servidor, la liquidación,
# la reconciliación, la analítica y la auditoría la leen — ninguna compara
# `forma_pago == 'CREDITO'` ni un código crudo por su cuenta (trinquete:
# `tests/test_contado_contraentrega.py`).
#
# El conservador es COBRAR: `cobrar=False` solo cuando los días se CONOCEN y
# superan el umbral. Un dato ausente, un código que la tabla no conoce o una
# tabla vacía se cobran, con el origen declarado. Cobrar de más en la puerta
# se corrige («no pagó» devuelve la mercancía, o crédito autorizado en la
# liquidación con razón escrita); entregar sin cobrar lo que era contado es
# crédito que nadie evaluó.

import json as _json
import os as _os
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone

#: Umbral por defecto: hasta 15 días inclusive es contado contraentrega.
UMBRAL_DEFECTO = 15

# Orígenes de una clasificación. Solo MAESTRO afirma algo leído.
MAESTRO = 'MAESTRO'                            # código conocido, días de la tabla
SUPUESTO_AUSENTE = 'SUPUESTO_AUSENTE'          # no hay código (no se pudo leer o vino vacío)
SUPUESTO_DESCONOCIDO = 'SUPUESTO_DESCONOCIDO'  # hay código y la tabla no lo tiene
SIN_MAESTRO = 'SIN_MAESTRO'                    # no hay tabla de días contra la cual leer
ORIGENES = (MAESTRO, SUPUESTO_AUSENTE, SUPUESTO_DESCONOCIDO, SIN_MAESTRO)
#: No es una venta: un traslado entre sedes viaja en la ruta y no se cobra.
#: No se guarda (no está en el CHECK): se deriva del tipo de documento.
NO_APLICA = 'NO_APLICA'


def _es_traslado(tarea) -> bool:
    return (getattr(tarea, 'tipo_documento', None) or '').upper() == 'TRASLADO'

#: Formas de pago que declaran «no entró plata». Sobre una parada de contado
#: son crédito que el conductor no puede otorgar.
FORMAS_QUE_NO_COBRAN = ('CREDITO', 'EXENTO')

#: El PWA que sabe ofrecer el select sin crédito ni exento en contado manda
#: `version_formulario >= 3`. Un ítem viejo de la cola no se rechaza por esto
#: (quedaría trabado en el teléfono): llega como `credito_no_autorizado` a la
#: liquidación, que no lo deja pasar sin autorización.
VERSION_FORMULARIO_CONTADO = 3

# ── La tabla código → días ────────────────────────────────────────────────
#
# COPIA del reporte «CONDICIONES DE PAGO» de Siesa
# (`docs/siesa-specs/Condiciones de pago.pdf`, generado el 17/04/2026 10:16
# a. m. por NELLY CARMENSA FIGUEROA ANACONA, ordenado por código). Se copió la
# columna **«Dias Vcto»**, NUNCA la descripción: P08 dice «40 DIAS» y vence a
# 180; P09 «210 DIAS» → 180; P10 «240 DIAS» → 180. Es la columna la que
# Siesa usa para vencer.
#
# ⚠️ Es una copia que puede quedar vieja: si alguien crea o cambia una
# condición en Siesa, esta tabla no se entera. Por eso:
#   · `SIESA_COND_PAGO_DIAS` (JSON `{"C10": 20}`) la sobreescribe por código;
#   · `CONNEKTA_CONSULTA_COND_PAGO` (sin default) es el gancho para una
#     consulta dinámica del maestro (probablemente `t208_mm_condiciones_pago`;
#     hoy no hay API registrada — 401). Si está configurada y responde, MANDA,
#     y el health declara en qué difiere de esta copia.
DIAS_POR_CODIGO_PDF = {
    'C01': 0, 'C02': 1, 'C03': 8, 'C04': 30, 'C05': 45, 'C06': 60,
    'C07': 75, 'C08': 90, 'C09': 120,
    'P01': 30, 'P02': 45, 'P03': 60, 'P04': 75, 'P05': 90, 'P06': 120,
    'P07': 180, 'P08': 180, 'P09': 180, 'P10': 180,
}
PROCEDENCIA_TABLA = ('docs/siesa-specs/Condiciones de pago.pdf — reporte Siesa '
                     '17/04/2026, columna «Dias Vcto». Copia que puede quedar vieja.')

#: Lo último que respondió la consulta dinámica, si está configurada.
#: `{'tabla': dict, 'en': datetime, 'error': str|None}`. Se llena solo con
#: `refrescar_tabla_desde_siesa()` — ninguna decisión de cobro sale a la red.
_CACHE_CONSULTA = {'tabla': None, 'en': None, 'error': None}
_TTL_CONSULTA = _timedelta(hours=6)


def _norm(codigo) -> str:
    return str(codigo or '').strip().upper()


def umbral_dias() -> tuple:
    """`(umbral, problema)`. Un valor inválido NO se aplica: se usa 15 y se
    declara el problema (health). Nunca negativo."""
    crudo = _os.getenv('COBRO_CONTRAENTREGA_MAX_DIAS')
    if crudo is None:
        return UMBRAL_DEFECTO, None
    try:
        v = int(str(crudo).strip())
        if v < 0:
            raise ValueError
        return v, None
    except (TypeError, ValueError):
        return UMBRAL_DEFECTO, (f'COBRO_CONTRAENTREGA_MAX_DIAS={crudo!r} no es un entero '
                                f'>= 0: se usa {UMBRAL_DEFECTO}')


def _tabla_env() -> tuple:
    """`(dict, problemas)` de `SIESA_COND_PAGO_DIAS`. Entradas inválidas no se
    aplican y se declaran; un JSON ilegible no se aplica entero."""
    crudo = _os.getenv('SIESA_COND_PAGO_DIAS')
    if crudo is None or not str(crudo).strip():
        return {}, ([] if crudo is None else
                    ['SIESA_COND_PAGO_DIAS declarada vacía: se ignora'])
    try:
        datos = _json.loads(crudo)
    except ValueError:
        return {}, ['SIESA_COND_PAGO_DIAS no es JSON válido: se ignora entera']
    if not isinstance(datos, dict):
        return {}, ['SIESA_COND_PAGO_DIAS no es un objeto {codigo: dias}: se ignora']
    tabla, problemas = {}, []
    for k, v in datos.items():
        try:
            if isinstance(v, bool):
                raise ValueError
            d = int(v)
            if d < 0 or float(v) != d:
                raise ValueError
            tabla[_norm(k)] = d
        except (TypeError, ValueError):
            problemas.append(f'SIESA_COND_PAGO_DIAS[{k!r}]={v!r} no son días enteros >= 0: se ignora')
    return tabla, problemas


def consulta_cond_pago() -> str:
    """Nombre de la consulta dinámica del maestro de condiciones. Sin default:
    su nombre no está en el repo hasta que alguien la registre en Connekta."""
    return (_os.getenv('CONNEKTA_CONSULTA_COND_PAGO') or '').strip()


def tabla_dias() -> tuple:
    """`(tabla, fuente, problemas)`. Fuente: `CONSULTA` (la consulta dinámica
    respondió y manda), `ENV` (la copia con sobreescrituras) o `CODIGO`."""
    env, problemas = _tabla_env()
    if consulta_cond_pago() and _CACHE_CONSULTA['tabla']:
        return dict(_CACHE_CONSULTA['tabla']), 'CONSULTA', problemas
    tabla = dict(DIAS_POR_CODIGO_PDF)
    tabla.update(env)
    return tabla, ('ENV' if env else 'CODIGO'), problemas


def dias_credito(codigo):
    """Días de crédito de la condición según la tabla vigente. `None` si no se
    conoce (código vacío o que la tabla no tiene) — nunca 0 por defecto."""
    c = _norm(codigo)
    if not c:
        return None
    tabla, _f, _p = tabla_dias()
    return tabla.get(c)


def cobro_contraentrega(codigo) -> dict:
    """**LA** respuesta a «¿el conductor cobra esta factura al entregarla?».

    `{cobrar, dias, codigo, origen, umbral}`:
      · días conocidos ≤ umbral → cobrar, origen MAESTRO (C01 0, C02 1, C03 8);
      · días conocidos  > umbral → NO cobrar, origen MAESTRO (C04 30, P01…);
      · sin código              → cobrar, SUPUESTO_AUSENTE;
      · código fuera de tabla   → cobrar, SUPUESTO_DESCONOCIDO;
      · tabla vacía             → cobrar, SIN_MAESTRO.

    Sin red: lee la tabla que ya está en memoria. La consulta dinámica, si
    existe, se refresca en los momentos con señal (`refrescar_tabla_desde_siesa`).
    """
    umbral, _ = umbral_dias()
    c = _norm(codigo)
    tabla, _fuente, _p = tabla_dias()
    if not c:
        return {'cobrar': True, 'dias': None, 'codigo': c or None,
                'origen': SUPUESTO_AUSENTE, 'umbral': umbral}
    if not tabla:
        return {'cobrar': True, 'dias': None, 'codigo': c,
                'origen': SIN_MAESTRO, 'umbral': umbral}
    dias = tabla.get(c)
    if dias is None:
        return {'cobrar': True, 'dias': None, 'codigo': c,
                'origen': SUPUESTO_DESCONOCIDO, 'umbral': umbral}
    return {'cobrar': dias <= umbral, 'dias': dias, 'codigo': c,
            'origen': MAESTRO, 'umbral': umbral}


def vencimiento_fe(fecha_factura, codigo) -> str:
    """`F353_FECHA_VCTO` de la FE de ruta, `YYYYMMDD` (Regla 5).

    Decisión del dueño (2026-09-24): «si tiene 1 día, toca esperar el despacho
    y también el tiempo de ruta». Contado contraentrega (incluido el supuesto)
    vence en `fecha_factura + umbral` (15): la ventana entera que el dueño
    llama contado. C02 = 1 día vencería antes de que el camión salga, y la
    factura computaría mora por el tiempo de ruta. Crédito real vence a los
    días de su condición (C04 +30, C05 +45…).

    Hasta ese día el gateway mandaba `hoy + 30` fijo para TODA factura, y
    Siesa lo respeta: por eso la cartera del WMS no servía para saber la
    condición. Las FE ya emitidas quedan con +30 (sin backfill).

    `fecha_factura`: `date`, `datetime` o `'YYYYMMDD'` (la fecha Bogotá con
    la que sale `F350_FECHA`; el caller la toma de `app/utils/fecha.py`).
    """
    if isinstance(fecha_factura, _datetime):
        base = fecha_factura.date()
    elif isinstance(fecha_factura, _date):
        base = fecha_factura
    else:
        base = _datetime.strptime(str(fecha_factura).strip(), '%Y%m%d').date()
    cobro = cobro_contraentrega(codigo)
    dias = cobro['umbral'] if cobro['cobrar'] else cobro['dias']
    return (base + _timedelta(days=int(dias))).strftime('%Y%m%d')


def forma_no_cobra(forma_pago) -> bool:
    """`CREDITO`/`EXENTO`: la parada declara que no entró plata."""
    return _norm(forma_pago) in FORMAS_QUE_NO_COBRAN


def formulario_sabe_de_contado(data: dict) -> bool:
    try:
        return int((data or {}).get('version_formulario') or 0) >= VERSION_FORMULARIO_CONTADO
    except (TypeError, ValueError):
        return False


def regla_anterior_rechaza_credito(forma_pago, codigo, cond_pago_contado, cond_pago_ruta) -> bool:
    """Lo único que la guarda rechazaba ANTES del 2026-09-24, para los ítems
    de la cola armados por un PWA viejo: `CREDITO` sobre el código de contado
    o sobre la condición de ruta (y el vacío, que factura en ruta). Un ítem
    viejo que la regla nueva rechazaría y la vieja no, pasa — y llega a la
    liquidación como `credito_no_autorizado`. No se endurece hacia atrás: un
    ítem rechazado en la cola queda trabado en el teléfono para siempre."""
    if _norm(forma_pago) != 'CREDITO':
        return False
    c = _norm(codigo)
    contado, ruta = _norm(cond_pago_contado), _norm(cond_pago_ruta)
    if codigo is None:
        return False
    if not c:
        return bool(ruta)
    return c == contado or (bool(ruta) and c == ruta)


# ── Snapshot en la tarea y en el recaudo ──────────────────────────────────
#
# La confirmación tiene que funcionar sin señal: nada de lo de abajo va a la
# red. La clasificación se escribe en la tarea lo antes posible (al crear el
# packing desde la historia del pedido, al emitir la FE con la condición que
# salió de verdad, y como respaldo al despachar y al listar paradas) y se
# CONGELA en el recaudo al confirmar.

def codigo_vigente(tarea):
    """La condición que manda: la de la FE si se conoce (lo que se emitió de
    verdad), si no la del pedido. `None` = no se leyó nada."""
    if tarea is None:
        return None
    fe = getattr(tarea, 'cond_pago_fe', None)
    if fe:
        return fe
    return getattr(tarea, 'cond_pago', None)


def cobro_de_tarea(tarea) -> dict:
    """La clasificación de la tarea SIN red: el snapshot si existe; si no, la
    política sobre lo anotado; si no, SUPUESTO_AUSENTE (se cobra). Un
    traslado entre sedes no es una venta: NO_APLICA, no se cobra."""
    if _es_traslado(tarea):
        return {'cobrar': False, 'dias': None, 'codigo': None,
                'origen': NO_APLICA, 'umbral': umbral_dias()[0]}
    # Se recalcula sobre los CÓDIGOS anotados (sin red, barato) en vez de leer
    # las columnas del snapshot: así un cambio de tabla o de umbral aplica ya,
    # y el snapshot no puede quedar desfasado de los códigos que lo produjeron.
    # Las columnas son la copia persistida para reportes y auditoría.
    return cobro_contraentrega(codigo_vigente(tarea))


def anotar_en_tarea(tarea, *, cond_fe=None, cond_pedido=None) -> dict:
    """Escribe el snapshot en la tarea (sin commit). Devuelve la clasificación.

    · `cond_fe`: la condición con la que la FE salió (`f461_id_cond_pago`, o
      la que el gateway emitió). **Gana siempre**.
    · `cond_pedido`: `f430_id_cond_pago`. Solo llena `tarea.cond_pago` si
      estaba sin leer (`None`): la primera lectura del pedido es la que se
      anotó, y un cambio posterior en Siesa no reescribe la factura.
    · Si la FE difiere del pedido, gana la FE y se declara (log + campo
      `difiere_del_pedido` en el retorno, que la pantalla y el desglose leen).
    """
    if tarea is None:
        return cobro_contraentrega(None)
    if _es_traslado(tarea):
        return {**cobro_de_tarea(tarea), 'difiere_del_pedido': False}
    if cond_pedido is not None and getattr(tarea, 'cond_pago', None) is None:
        tarea.cond_pago = str(cond_pedido).strip()[:10]
    fe = _norm(cond_fe)
    if fe:
        tarea.cond_pago_fe = fe[:10]
    cobro = cobro_contraentrega(codigo_vigente(tarea))
    ped = _norm(getattr(tarea, 'cond_pago', None))
    fe_anotada = _norm(getattr(tarea, 'cond_pago_fe', None))
    difiere = bool(fe_anotada and ped and ped != fe_anotada)
    if difiere:
        logger.warning('[COND_PAGO] tarea %s: la FE salió en %s y el pedido declara %s — '
                       'manda la FE', getattr(tarea, 'id', None), fe_anotada, ped)
    cambio = (getattr(tarea, 'cobro_contraentrega', None) != cobro['cobrar']
              or getattr(tarea, 'dias_credito', None) != cobro['dias']
              or getattr(tarea, 'clasif_origen', None) != cobro['origen'])
    if cambio:
        tarea.cobro_contraentrega = cobro['cobrar']
        tarea.dias_credito = cobro['dias']
        tarea.clasif_origen = cobro['origen']
        tarea.clasif_en = _datetime.utcnow()
    return {**cobro, 'difiere_del_pedido': difiere}


def cobro_de_recaudo(recaudo, tarea=None) -> dict:
    """La clasificación con la que se juzga una parada ya confirmada: la
    congelada en el recaudo; si no hay (parada anterior a esto, o confirmada
    sobre un supuesto), la de la tarea hoy."""
    tarea = tarea if tarea is not None else getattr(recaudo, 'tarea', None)
    base = cobro_de_tarea(tarea)
    congelado = getattr(recaudo, 'cobro_contraentrega', None)
    if congelado is not None:
        return {**base, 'cobrar': bool(congelado), 'congelado': True}
    return {**base, 'congelado': False}


def credito_autorizado(recaudo) -> bool:
    return getattr(recaudo, 'credito_autorizado_en', None) is not None


#: Desde cuándo rige la regla ≤ 15 días: el commit d5010187 (m043contado).
#: Es la fecha MÁS TEMPRANA posible del despliegue: ninguna parada confirmada
#: antes pudo pasar por la guarda nueva ni congelar su clasificación. Una
#: confirmada entre este instante y el despliegue real se juzga con la regla
#: nueva (se muestra de más, no de menos — Regla 0).
REGLA_CONTADO_DESDE = '2026-09-24T17:35:31-05:00'


def regla_contado_desde():
    """`REGLA_CONTADO_DESDE` en UTC naive (el marco de `fecha_confirmacion`)."""
    return (_datetime.fromisoformat(REGLA_CONTADO_DESDE)
            .astimezone(_timezone.utc).replace(tzinfo=None))


def anterior_a_la_regla(recaudo) -> bool:
    """¿Se confirmó antes de que existiera la regla de contado?

    Sí solo si **no tiene snapshot** (`cobro_contraentrega` NULL: la regla
    nueva lo congela al confirmar sobre una clasificación leída) **y** su
    `fecha_confirmacion` es anterior a `REGLA_CONTADO_DESDE`. Sin fecha → no
    (no saber cuándo no la vuelve vieja). La regla no es retroactiva: a esas
    paradas se les aplica la de antes (`regla_anterior_rechaza_credito`)."""
    if getattr(recaudo, 'cobro_contraentrega', None) is not None:
        return False
    cuando = getattr(recaudo, 'fecha_confirmacion', None)
    if not isinstance(cuando, _datetime):
        return False
    if cuando.tzinfo is not None:
        cuando = cuando.astimezone(_timezone.utc).replace(tzinfo=None)
    return cuando < regla_contado_desde()


def _trato_regla_anterior(recaudo, tarea) -> str:
    """El trato de una parada anterior a la regla: la regla de ENTONCES.
    Solo es no autorizado lo que esa regla ya rechazaba (CREDITO sobre el
    código de contado o de ruta); lo demás que no trajo plata es crédito, y un
    ENTREGADO con forma que cobra y $0 es contado sin recibo (como era)."""
    from app.services.connekta_gateway import connekta
    fp = _norm(getattr(recaudo, 'forma_pago', None))
    if not forma_no_cobra(fp):
        return TRATO_CONTADO
    tarea = tarea if tarea is not None else getattr(recaudo, 'tarea', None)
    if regla_anterior_rechaza_credito(fp, codigo_vigente(tarea),
                                      connekta.cond_pago_ventas, connekta.cond_pago_ruta):
        return TRATO_NO_AUTORIZADO
    return TRATO_CREDITO


#: Cómo trata la liquidación una parada entregada. Una función, no un
#: `forma_pago == 'CREDITO'` en cada sitio.
TRATO_CONTADO = 'CONTADO'                  # entró plata: RC (y DC si hay retención)
TRATO_CREDITO = 'CREDITO'                  # crédito real o autorizado: sin RC
TRATO_NO_AUTORIZADO = 'CREDITO_NO_AUTORIZADO'  # contado sin plata y sin autorización


def trato_de_cobro(recaudo, tarea=None) -> str:
    """Solo tiene sentido en ENTREGADO/PARCIAL (el que llama lo filtra).

    · forma que cobra y monto > 0 → CONTADO (aunque el cliente sea de crédito:
      si pagó, hay recibo).
    · autorizado por la oficina → CREDITO.
    · crédito real (días conocidos > umbral) → CREDITO.
    · contado (incluido supuesto) con CREDITO/EXENTO, o ENTREGADO con $0 →
      CREDITO_NO_AUTORIZADO: nunca al contador `credito`.
    · PARCIAL contado con forma que cobra y $0 → CONTADO (no hay RC que
      mandar; la NC de lo devuelto sigue su camino) — la guarda del servidor
      ya exige monto > 0 en un parcial.
    · **No es retroactiva**: una parada confirmada antes de la regla
      (`anterior_a_la_regla`) se juzga con la regla de entonces
      (`_trato_regla_anterior`). Sin esto, cada crédito viejo aparecía como
      no autorizado y trababa la liquidación de rutas ya cerradas.
    """
    from app.models.recaudo_entrega import EstadoEntrega
    fp = _norm(getattr(recaudo, 'forma_pago', None))
    monto = float(getattr(recaudo, 'monto_cobrado', None) or 0)
    if not forma_no_cobra(fp) and monto > 0:
        return TRATO_CONTADO
    if credito_autorizado(recaudo):
        return TRATO_CREDITO
    if anterior_a_la_regla(recaudo):
        return _trato_regla_anterior(recaudo, tarea)
    cobro = cobro_de_recaudo(recaudo, tarea)
    if not cobro['cobrar']:
        return TRATO_CREDITO
    if forma_no_cobra(fp):
        return TRATO_NO_AUTORIZADO
    if getattr(recaudo, 'estado_entrega', None) == EstadoEntrega.ENTREGADO:
        return TRATO_NO_AUTORIZADO
    return TRATO_CONTADO


def credito_no_autorizado(recaudo, tarea=None) -> bool:
    """Parada de contado entregada que no trajo plata y nadie autorizó."""
    from app.models.recaudo_entrega import EstadoEntrega
    if getattr(recaudo, 'estado_entrega', None) not in (EstadoEntrega.ENTREGADO,
                                                         EstadoEntrega.PARCIAL):
        return False
    return trato_de_cobro(recaudo, tarea) == TRATO_NO_AUTORIZADO


def etiqueta_conductor(cobro: dict) -> dict:
    """El texto que ve el conductor. `{'texto', 'tono'}` — tono `ok`/`warn`/`info`."""
    cod, dias, origen = cobro.get('codigo'), cobro.get('dias'), cobro.get('origen')
    if origen == NO_APLICA:
        return {'texto': 'Traslado entre sedes — no se cobra', 'tono': 'info'}
    if not cobro.get('cobrar'):
        return {'texto': f'Crédito {dias} días — no se cobra', 'tono': 'info'}
    if origen == MAESTRO:
        plural = 'día' if dias == 1 else 'días'
        if dias == 0:
            return {'texto': f'Contado contraentrega · {cod} — cobrá al entregar', 'tono': 'ok'}
        return {'texto': f'Contado contraentrega · {cod} ({dias} {plural}) — cobrá al entregar',
                'tono': 'ok'}
    if origen == SUPUESTO_DESCONOCIDO:
        return {'texto': f'Condición {cod} sin días conocidos: cobrá al entregar', 'tono': 'warn'}
    if origen == SIN_MAESTRO:
        return {'texto': 'Sin tabla de condiciones de pago: cobrá al entregar', 'tono': 'warn'}
    return {'texto': 'Sin condición de pago: cobrá al entregar', 'tono': 'warn'}


# ── La consulta dinámica del maestro (gancho) ─────────────────────────────

def _filas_a_tabla(filas) -> dict:
    """Lee filas de una consulta del maestro sin contrato conocido: busca la
    columna del código y la de días de vencimiento por nombre. Una fila sin
    las dos se descarta (no se inventa un 0)."""
    tabla = {}
    for f in filas or []:
        if not isinstance(f, dict):
            continue
        claves = {str(k).lower(): v for k, v in f.items()}
        cod = next((claves[k] for k in ('f208_id', 'codigo', 'id', 'f208_codigo', 'cod')
                    if claves.get(k) not in (None, '')), None)
        dias = next((claves[k] for k in ('f208_dias_vcto', 'dias_vcto', 'dias')
                     if claves.get(k) not in (None, '')), None)
        if cod is None or dias is None:
            continue
        try:
            tabla[_norm(cod)] = int(float(dias))
        except (TypeError, ValueError):
            continue
    return tabla


def refrescar_tabla_desde_siesa(forzar: bool = False) -> dict:
    """Trae el maestro por la consulta dinámica, si está configurada. Nunca
    levanta: un fallo deja la copia en código y se declara en `error`."""
    nombre = consulta_cond_pago()
    if not nombre:
        return {'configurada': False}
    ahora = _datetime.utcnow()
    if (not forzar and _CACHE_CONSULTA['en'] is not None
            and ahora - _CACHE_CONSULTA['en'] < _TTL_CONSULTA):
        return {'configurada': True, 'filas': len(_CACHE_CONSULTA['tabla'] or {}),
                'error': _CACHE_CONSULTA['error'], 'cache': True}
    try:
        from app.services.connekta_gateway import connekta
        res = connekta._get(nombre, params_extra={'paginacion': 'numPag=1|tamPag=100'},
                            url=connekta.url_get_dinamico)
        det = (res or {}).get('detalle', {}) or {}
        tabla = _filas_a_tabla(det.get('Datos') or det.get('Table') or [])
        _CACHE_CONSULTA.update(tabla=tabla or None, en=ahora,
                               error=None if tabla else 'la consulta no trajo filas legibles')
    except Exception as e:  # noqa: BLE001 — se declara, no se levanta
        _CACHE_CONSULTA.update(en=ahora, error=str(e)[:200])
    return {'configurada': True, 'filas': len(_CACHE_CONSULTA['tabla'] or {}),
            'error': _CACHE_CONSULTA['error'], 'cache': False}


def estado_politica(refrescar: bool = False) -> dict:
    """Para `/api/health/siesa`: umbral, tabla vigente, su fuente, problemas de
    configuración y, si la consulta existe, en qué difiere de la copia."""
    consulta = refrescar_tabla_desde_siesa() if refrescar else {
        'configurada': bool(consulta_cond_pago())}
    umbral, prob_umbral = umbral_dias()
    tabla, fuente, problemas = tabla_dias()
    if prob_umbral:
        problemas = [prob_umbral] + problemas
    copia = dict(DIAS_POR_CODIGO_PDF)
    copia.update(_tabla_env()[0])
    divergencias = []
    if _CACHE_CONSULTA['tabla']:
        viva = _CACHE_CONSULTA['tabla']
        for c in sorted(set(viva) | set(copia)):
            if viva.get(c) != copia.get(c):
                divergencias.append({'codigo': c, 'siesa': viva.get(c), 'copia': copia.get(c)})
    return {
        'umbral_dias': umbral,
        'fuente_tabla': fuente,
        'procedencia_copia': PROCEDENCIA_TABLA,
        'tabla': tabla,
        'contado': sorted(c for c, d in tabla.items() if d <= umbral),
        'credito': sorted(c for c, d in tabla.items() if d > umbral),
        'problemas': problemas,
        'consulta': {**consulta, 'nombre': consulta_cond_pago() or None},
        'divergencias_con_siesa': divergencias,
    }


def cond_pago_de_historia(pedido_clave):
    """`f430_id_cond_pago` que el sync de pedidos ya guardó en
    `pedidos_historia` (sin red). `None` si el pedido no está o no la trae."""
    if not pedido_clave:
        return None
    try:
        from app.models.pedido_historia import PedidoHistoria
        fila = (PedidoHistoria.query
                .filter(PedidoHistoria.pedido_clave == pedido_clave,
                        PedidoHistoria.cond_pago.isnot(None))
                .order_by(PedidoHistoria.id.desc()).first())
        return fila.cond_pago if fila is not None else None
    except Exception as e:  # noqa: BLE001 — anotar temprano no bloquea nada
        logger.warning('[COND_PAGO] historia de %s ilegible: %s', pedido_clave, e)
        return None


def anotar_desde_historia(tarea) -> bool:
    """Primer snapshot, al crear el packing: la condición del pedido desde la
    historia del sync. No escribe nada si no hay dato (no se inventa)."""
    cond = cond_pago_de_historia(getattr(tarea, 'pedido_clave', None))
    if cond is None:
        return False
    anotar_en_tarea(tarea, cond_pedido=cond)
    return True
