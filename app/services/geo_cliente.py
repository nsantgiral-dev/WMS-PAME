"""
La política de dónde queda un cliente. **Una política, una función.**

Este módulo contesta tres preguntas y ninguna más:

1. **¿Qué llegó del teléfono?** — `leer_captura_del_conductor`. Traduce el
   bloque `geo` del payload de `confirmar_parada` a una coordenada con
   procedencia, o a un «no sé» con motivo. Nunca a 0,0.
2. **¿Cuál de todas las capturas es la del maestro?** —
   `elegir_coordenada_del_maestro`. Es la única función del repo que contesta
   esto, y está escrita acá y no dentro de una consulta a propósito: repartida
   en un `ORDER BY ... LIMIT 1` no se puede leer, no se puede probar y no se
   puede cambiar sin cambiarla en dos sitios. Es la Regla 0 del WMS —«una
   política, una función»— aplicada antes de que haya con qué divergir.
3. **¿Cuánto llevamos?** — `cobertura`. El número que hace que esto valga algo:
   cuántos clientes ya tienen coordenada y cuántos no.

## Por qué la mediana, y no «la más reciente» ni «la más precisa»

Las tres son defendibles y hay que elegir una. Se eligió la **mediana por
coordenada** de las capturas fiables:

· **«La más reciente»** deja el maestro a merced de la última confirmación, y
  la última confirmación es justamente la que puede venir de una corrección
  hecha en la oficina. Un solo evento raro mueve el punto.
· **«La más precisa»** premia lo que el teléfono *dice* de sí mismo, no lo que
  midió. Un dispositivo que reporta 5 m de precisión y está a 300 m de la
  puerta gana sobre cinco capturas concordantes de 40 m.
· **La mediana** necesita que la MITAD de las capturas se equivoquen en la
  misma dirección para moverse. Con dos capturas es el punto medio (que es lo
  honesto: no hay con qué desempatar); con una es esa misma, sin fingir
  robustez que no tiene — por eso `capturas_consideradas` viaja pegado al dato.

Es mediana **por coordenada**, no mediana geométrica: se toma la mediana de las
latitudes y la de las longitudes por separado. A las distancias de esta tabla
—cuadras dentro de un municipio— la diferencia entre las dos es de centímetros,
y la geométrica exige iterar. Se dice acá para que nadie lea «mediana» y
suponga otra cosa.

`corregida_a_mano` gana sobre todo lo anterior: si un humano ya juzgó dónde
queda la tienda, promediarlo contra capturas de GPS sería descartar el único
dato que sí tiene autoridad.

## Lo que este módulo NO hace, y es deliberado

**No rutea, no optimiza y no entrena nada.** El plan es explícito: *«No hay
nada que entrenar hasta tener las dos cosas. El primer paso honesto es capturar
la coordenada y esperar.»* La condición de disparo está escrita en
`docs/flota/ESTADO.md`.

**No geocodifica direcciones.** No hay direcciones que geocodificar (ver el
encabezado de `app/models/geo_entrega.py`), y las dos herramientas gratuitas
obvias están descartadas por licencia: Nominatim prohíbe explícitamente el
seguimiento de vehículos y el demo de OSRM es solo no comercial.
"""
import logging
import math
import re
import unicodedata
from datetime import datetime
from typing import NamedTuple, Optional, Sequence

logger = logging.getLogger(__name__)

# ── Procedencia (Regla 13) ───────────────────────────────────────────────────

#: El conductor tocó «Estoy aquí» y el navegador entregó una posición.
GPS_CONDUCTOR = 'gps_conductor'
#: Un humano fijó el punto a mano. **Hoy no tiene puerta**: ningún endpoint ni
#: pantalla escribe este valor, y está declarado como deuda con su condición de
#: disparo en `docs/flota/ESTADO.md`. Vive en el catálogo y en la elección
#: porque la política de «qué gana sobre qué» se escribe una vez o se escribe
#: mal: agregarla después obligaría a re-decidir la precedencia con el maestro
#: ya poblado, que es cuando cambiarla cuesta.
CORREGIDA_A_MANO = 'corregida_a_mano'
#: No se sabe. **No es 0,0.**
SIN_DATO = 'sin_dato'

FUENTES = (GPS_CONDUCTOR, CORREGIDA_A_MANO, SIN_DATO)

# ── Por qué no hay coordenada, del lado del dispositivo ──────────────────────
#
# Los cinco primeros los declara el navegador (`GeolocationPositionError` y la
# ausencia de la API). Los dos últimos los deriva el servidor, y son distintos:
# uno dice «el conductor no colaboró», el otro «llegó algo y no me lo creo».

PERMISO_DENEGADO = 'permiso_denegado'
SIN_SENAL = 'sin_senal'
NO_SOPORTADO = 'no_soportado'
TIMEOUT_GPS = 'timeout'
NO_SE_PIDIO = 'no_se_pidio'
#: Llegó una coordenada fuera de Colombia. Incluye 0,0 y los ejes cambiados.
FUERA_DE_RANGO = 'fuera_de_rango'
#: Llegó «sin dato» sin decir por qué. Se guarda como motivo propio en vez de
#: dejarlo en NULL: «no sé por qué no sé» es una respuesta, y NULL sería otra
#: ausencia muda dentro de la columna que existe para no tenerlas.
NO_DECLARADO = 'no_declarado'

MOTIVOS_SIN_DATO = (PERMISO_DENEGADO, SIN_SENAL, NO_SOPORTADO, TIMEOUT_GPS,
                    NO_SE_PIDIO, FUERA_DE_RANGO, NO_DECLARADO)

# ── Por qué el maestro no eligió punto ───────────────────────────────────────

SIN_CAPTURAS = 'sin_capturas'
PRECISION_INSUFICIENTE = 'precision_insuficiente'
CAPTURAS_DISPERSAS = 'capturas_dispersas'

MOTIVOS_SIN_MAESTRO = (SIN_CAPTURAS, PRECISION_INSUFICIENTE, CAPTURAS_DISPERSAS)

# ── Umbrales, con su motivo ──────────────────────────────────────────────────

#: Radio de incertidumbre máximo, en metros, para que una captura entre a la
#: elección. 100 m es el orden de magnitud de un GPS de teléfono bajo techo o
#: entre edificios; por encima de eso el navegador está triangulando por wifi o
#: por antena y el punto puede caer en otro barrio.
#:
#: **No descarta la fila**: la captura se guarda igual. Descarta su voto.
PRECISION_MAXIMA_M = 100.0

#: A qué distancia de la mediana una captura deja de ser «la misma tienda».
#: 500 m es más que cualquier error razonable de GPS urbano y menos que la
#: distancia entre dos negocios distintos con la misma razón social. Se usa
#: para **contar** capturas incoherentes, no para borrarlas.
RADIO_COHERENCIA_M = 500.0

#: Radio de la Tierra en metros (esfera). A las distancias de este módulo
#: —metros a kilómetros— la diferencia contra el elipsoide es despreciable, y
#: usarla sería precisión fingida sobre puntos con 40 m de incertidumbre.
RADIO_TIERRA_M = 6_371_000.0

#: Cuántos clientes con coordenada hacen falta antes de que «ruteo» signifique
#: algo. **No es un número de arte**: una ruta urbana de PAME tiene del orden
#: de 15-25 paradas, y optimizar el orden de una ruta en la que se conoce la
#: mitad de los puntos produce una secuencia que el conductor descarta al
#: segundo desvío. 150 clientes es el orden de magnitud en que la mayoría de
#: las paradas de una ruta cualquiera ya tiene punto. Condición de disparo
#: declarada en `docs/flota/ESTADO.md`; hasta llegar ahí no se construye ruteo.
UMBRAL_PARA_RUTEAR = 150


class Captura(NamedTuple):
    """Lo que se leyó del payload del conductor, ya juzgado.

    QUÉ AFIRMA: que el bloque `geo` que llegó dice esto. QUÉ NO AFIRMA: que la
    posición sea correcta — solo que es sintácticamente creíble y cae dentro de
    Colombia.
    """
    lat: Optional[float]
    lon: Optional[float]
    precision_m: Optional[float]
    fuente: str
    motivo_sin_dato: Optional[str]


class Eleccion(NamedTuple):
    """El veredicto del maestro para un cliente."""
    lat: Optional[float]
    lon: Optional[float]
    precision_m: Optional[float]
    fuente: str
    motivo_sin_maestro: Optional[str]
    #: Cuántas capturas tuvieron voto.
    consideradas: int
    #: Cuántas existían y no votaron (precisión desconocida o mala, o lejanas
    #: de la mediana). Es el denominador que hace legible el número de arriba:
    #: «1 de 1» y «1 de 9» son maestros muy distintos.
    descartadas: int


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La clave del cliente
# ═════════════════════════════════════════════════════════════════════════════

_ESPACIOS = re.compile(r'\s+')


def clave_de_cliente(cliente, municipio) -> Optional[str]:
    """`RAZON SOCIAL|MUNICIPIO` normalizado, o `None` si no hay con qué.

    QUÉ AFIRMA: que dos paradas con la misma razón social escrita con distinta
    caja, tildes o espacios caen en el mismo cliente. QUÉ NO AFIRMA que sean el
    mismo negocio.

    **La clave correcta sería el NIT y el WMS no lo tiene.** Verificado por
    grep el 2026-09-02: ningún modelo persiste el NIT del cliente;
    `pedidos_sync_service` guarda `f200_razon_social_pedido_fact` y nada más, y
    el NIT solo aparece en vuelo dentro de `liquidacion_service.py:254`, leído
    de la cabecera de Siesa. Queda como deuda declarada — no se inventa.

    **El municipio entra en la clave**, y esa es la decisión que hace falta
    justificar. Sin él, dos tiendas homónimas en Neiva y en Pitalito colapsan en
    un cliente y la mediana da un punto en la carretera entre las dos: un
    maestro **activamente equivocado**. Con él, el mismo negocio cuyo municipio
    cambie de escritura se parte en dos filas: dos maestros, los dos
    aproximadamente correctos. Regla 0 — la fragmentación es el lado
    conservador; la colisión no.
    """
    nombre = _normalizar(cliente)
    if not nombre:
        return None
    return f'{nombre}|{_normalizar(municipio)}'[:220]


def _normalizar(texto) -> str:
    s = (texto or '').strip().upper()
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return _ESPACIOS.sub(' ', s)


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Qué llegó del teléfono
# ═════════════════════════════════════════════════════════════════════════════

def dentro_de_colombia(lat: float, lon: float) -> bool:
    """La caja del CHECK, en Python, leyendo las MISMAS constantes.

    Se importa de `app.models.geo_entrega` en vez de repetir los cuatro
    números: el modelo escribe el CHECK con ellos, y dos copias de un límite
    divergen — es literalmente la Regla 0 en cuatro literales.
    """
    from app.models.geo_entrega import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def leer_captura_del_conductor(geo) -> Optional[Captura]:
    """Traduce el bloque `geo` del payload a una `Captura`, o a `None`.

    QUÉ AFIRMA el retorno:

    · `None` — **no vino nada**. El cliente es viejo (caché del service worker)
      o la confirmación entró por una vía que no captura. No se escribe fila:
      «esta confirmación es de antes de que esto existiera» y «se preguntó y no
      se pudo» son cosas distintas, y guardar `sin_dato` para las dos borra esa
      diferencia justo en el número que mide la adopción.
    · Una `Captura` con `fuente='sin_dato'` — se preguntó y no se pudo, con
      motivo.
    · Una `Captura` con coordenada — el navegador entregó una posición creíble.

    QUÉ NO AFIRMA: nada sobre si el conductor estaba en la puerta del cliente.

    **Nunca levanta excepción.** Un payload deforme produce `sin_dato` con
    motivo, no un 400: la entrega no se traba por la geografía. Es el mismo
    criterio con el que `confirmar_parada` ya trata la condición de pago que no
    se alcanzó a anotar.
    """
    if not isinstance(geo, dict):
        return None

    lat, lon = _numero(geo.get('lat')), _numero(geo.get('lon'))
    if lat is None or lon is None:
        return Captura(None, None, None, SIN_DATO, _motivo_declarado(geo))

    if not dentro_de_colombia(lat, lon):
        # Incluye 0,0 (Golfo de Guinea) y los ejes cambiados. Se declara
        # `fuera_de_rango` en vez de descartar en silencio: si esto empieza a
        # aparecer, es un bug del cliente y hay que poder contarlo.
        logger.warning('[GEO] coordenada fuera de Colombia descartada: %s,%s', lat, lon)
        return Captura(None, None, None, SIN_DATO, FUERA_DE_RANGO)

    precision = _numero(geo.get('precision_m'))
    if precision is not None and precision <= 0:
        # Cero metros de incertidumbre no existe. Se trata como «no lo reportó»
        # —que es la verdad— y la captura queda sin voto en el maestro.
        precision = None

    fuente = geo.get('fuente')
    fuente = fuente if fuente in (GPS_CONDUCTOR, CORREGIDA_A_MANO) else GPS_CONDUCTOR
    return Captura(lat, lon, precision, fuente, None)


def _numero(v) -> Optional[float]:
    """`float` o `None`. Un booleano NO es un número acá.

    `isinstance(True, int)` es `True` en Python, así que sin este guard un
    `lat: true` entraría como latitud 1.0 — una coordenada perfectamente
    válida dentro de Colombia.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _motivo_declarado(geo: dict) -> str:
    m = geo.get('motivo')
    return m if m in MOTIVOS_SIN_DATO else NO_DECLARADO


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Cuál es la del maestro — LA política
# ═════════════════════════════════════════════════════════════════════════════

def distancia_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine sobre esfera, en metros. Ver `RADIO_TIERRA_M`."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * RADIO_TIERRA_M * math.asin(min(1.0, math.sqrt(a)))


def mediana(valores: Sequence[float]) -> float:
    """La mediana. Con longitud par, el promedio de los dos del medio."""
    orden = sorted(valores)
    n = len(orden)
    medio = n // 2
    return orden[medio] if n % 2 else (orden[medio - 1] + orden[medio]) / 2.0


def elegir_coordenada_del_maestro(capturas) -> Eleccion:
    """De N capturas de un cliente, el punto del maestro. **La única política.**

    QUÉ AFIRMA la elección: que con estas capturas y estos umbrales, ese es el
    punto. QUÉ NO AFIRMA: que la tienda esté ahí — ver `ClienteGeo`.

    Los pasos, en orden, y cada uno con su motivo:

    1. **Una corrección a mano gana**, la más reciente. Un humano que fijó el
       punto ya juzgó; promediarlo contra GPS sería descartar el único dato con
       autoridad. Si hay varias, la última — porque corregir dos veces es
       corregir la corrección.
    2. Entre las de GPS, solo votan las que tienen **precisión conocida y
       ≤ `PRECISION_MAXIMA_M`**. Precisión desconocida no es precisión buena.
    3. **Sin votantes no hay maestro**, y el motivo distingue los dos casos:
       `sin_capturas` (nunca se capturó nada) de `precision_insuficiente`
       (se capturó y ninguna sirve). Se arreglan distinto: el primero pidiéndole
       al conductor que toque el botón, el segundo mirando el dispositivo.
    4. **Mediana por coordenada** de las votantes.
    5. Si **la mitad o más** de las votantes queda a más de
       `RADIO_COHERENCIA_M` de esa mediana, no hay maestro: `capturas_dispersas`.
       Mitad y mitad no es ruido de GPS — es evidencia de que bajo una misma
       clave hay dos lugares (dos tiendas homónimas que el municipio no separó,
       o un cliente que se mudó). Un punto en el medio de los dos sería un
       maestro que manda al conductor a ninguna de las dos direcciones, que es
       **peor que no tener maestro**. Regla 0.

    Con una sola captura el paso 5 no descarta nada (su distancia a sí misma es
    cero) y eso es correcto: una captura sola no es incoherente, es poca. Lo
    poco que es se lee en `consideradas`, no en un veredicto inventado.

    Acepta cualquier objeto con `lat`, `lon`, `precision_m`, `fuente` y
    —opcionalmente— `capturado_en`: en producción son filas de `EntregaGeo`, y
    en los tests conviene poder construir el caso sin base de datos. Ese es el
    motivo de que la política viva acá y no dentro de una consulta.
    """
    con_punto = [c for c in capturas if c.lat is not None and c.lon is not None]

    manuales = [c for c in con_punto if c.fuente == CORREGIDA_A_MANO]
    if manuales:
        elegida = max(manuales,
                      key=lambda c: (getattr(c, 'capturado_en', None) or datetime.min))
        return Eleccion(
            lat=float(elegida.lat), lon=float(elegida.lon),
            precision_m=(float(elegida.precision_m)
                         if elegida.precision_m is not None else None),
            fuente=CORREGIDA_A_MANO, motivo_sin_maestro=None,
            consideradas=len(manuales),
            descartadas=len(con_punto) - len(manuales))

    votantes = [c for c in con_punto
                if c.fuente == GPS_CONDUCTOR
                and c.precision_m is not None
                and float(c.precision_m) <= PRECISION_MAXIMA_M]

    if not votantes:
        motivo = PRECISION_INSUFICIENTE if con_punto else SIN_CAPTURAS
        return Eleccion(None, None, None, SIN_DATO, motivo,
                        consideradas=0, descartadas=len(con_punto))

    lat_m = mediana([float(c.lat) for c in votantes])
    lon_m = mediana([float(c.lon) for c in votantes])

    lejanas = [c for c in votantes
               if distancia_m(float(c.lat), float(c.lon), lat_m, lon_m)
               > RADIO_COHERENCIA_M]
    if len(lejanas) * 2 >= len(votantes):
        return Eleccion(None, None, None, SIN_DATO, CAPTURAS_DISPERSAS,
                        consideradas=0,
                        descartadas=len(con_punto))

    return Eleccion(
        lat=lat_m, lon=lon_m,
        precision_m=mediana([float(c.precision_m) for c in votantes]),
        fuente=GPS_CONDUCTOR, motivo_sin_maestro=None,
        consideradas=len(votantes),
        descartadas=len(con_punto) - len(votantes) + len(lejanas))


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Escritura — la captura y el maestro
# ═════════════════════════════════════════════════════════════════════════════

def registrar_captura(recaudo_id: int, cliente, municipio, geo,
                      ahora: datetime = None) -> Optional[str]:
    """Guarda la captura de una parada y recalcula el maestro de ese cliente.

    Devuelve una etiqueta de lo que pasó (`'capturada'`, `'sin_dato'`,
    `'ya_capturada'`, `'sin_cliente'`, `'no_vino'`) — para el log y para los
    tests, no para el conductor.

    QUÉ AFIRMA: que si devuelve `'capturada'`, hay una fila con coordenada y el
    maestro se recalculó. QUÉ NO AFIRMA: que el maestro haya cambiado — puede
    que la captura no llegara a votar.

    **`'ya_capturada'` es el caso que hay que entender.** Re-confirmar una
    parada está permitido a propósito, y lo que se corrige ahí son montos,
    fotos y motivos. La geografía no: la segunda confirmación puede ser de la
    oficina al día siguiente, y pisar la primera movería el maestro hacia el CD
    poquito a poco, sin que nada falle. Una fila que ya tiene coordenada no se
    toca; una que quedó en `sin_dato` sí se puede completar, porque ahí no hay
    nada que perder.

    **No hace `commit`.** El que llama decide cuándo, y en `confirmar_parada`
    eso ocurre en una transacción aparte, después de que la entrega ya está
    guardada — una coordenada perdida cuesta una captura; una entrega trabada
    en la calle no la desbloquea nadie.
    """
    from app.extensions import db
    from app.models.geo_entrega import EntregaGeo

    captura = leer_captura_del_conductor(geo)
    if captura is None:
        return 'no_vino'

    clave = clave_de_cliente(cliente, municipio)
    if clave is None:
        # Sin razón social no hay a qué cliente sumarle esto. La fila del
        # evento existiría, pero `cliente_clave` es NOT NULL a propósito: una
        # captura que no puede alimentar ningún maestro no es un dato, es
        # ruido con formato.
        logger.warning('[GEO] captura sin cliente en recaudo %s — descartada', recaudo_id)
        return 'sin_cliente'

    ahora = ahora or datetime.utcnow()
    fila = EntregaGeo.query.filter_by(recaudo_id=recaudo_id).first()
    if fila is not None and fila.lat is not None:
        return 'ya_capturada'

    if fila is None:
        fila = EntregaGeo(recaudo_id=recaudo_id)
        db.session.add(fila)

    fila.cliente_clave = clave
    fila.lat = captura.lat
    fila.lon = captura.lon
    fila.precision_m = captura.precision_m
    fila.fuente = captura.fuente
    fila.motivo_sin_dato = captura.motivo_sin_dato
    fila.capturado_en = ahora

    db.session.flush()
    recalcular_maestro(clave, cliente=cliente, municipio=municipio, ahora=ahora)
    return 'capturada' if captura.lat is not None else 'sin_dato'


def recalcular_maestro(clave: str, cliente=None, municipio=None,
                       ahora: datetime = None):
    """Vuelve a correr la elección sobre TODAS las capturas de un cliente.

    Se recalcula entero y no incremental a propósito: la mediana no es
    incremental, y un maestro «actualizado» con la última captura sería la
    política de «la más reciente» disfrazada de mediana — el peor de los dos
    mundos, porque el nombre diría una cosa y el número otra.
    """
    from app.extensions import db
    from app.models.geo_entrega import ClienteGeo, EntregaGeo

    capturas = EntregaGeo.query.filter_by(cliente_clave=clave).all()
    eleccion = elegir_coordenada_del_maestro(capturas)

    maestro = db.session.get(ClienteGeo, clave)
    if maestro is None:
        maestro = ClienteGeo(cliente_clave=clave)
        db.session.add(maestro)

    if cliente:
        maestro.cliente_nombre = str(cliente)[:200]
    if municipio:
        maestro.municipio = str(municipio)[:100]
    maestro.lat = eleccion.lat
    maestro.lon = eleccion.lon
    maestro.precision_m = eleccion.precision_m
    maestro.fuente = eleccion.fuente
    maestro.motivo_sin_maestro = eleccion.motivo_sin_maestro
    maestro.capturas_consideradas = eleccion.consideradas
    maestro.capturas_descartadas = eleccion.descartadas
    maestro.elegido_en = ahora or datetime.utcnow()
    db.session.flush()
    return maestro


def maestro_de(cliente, municipio):
    """El `ClienteGeo` de una parada, o `None` si nunca se capturó nada.

    `None` significa **«nadie estuvo ahí todavía»** y la pantalla lo dice así.
    Una fila con `fuente='sin_dato'` significa otra cosa —«se intentó y no se
    pudo elegir»— y también lo dice. Colapsar las dos en un `if not geo` es el
    defecto que la Regla 4 persigue.
    """
    from app.extensions import db
    from app.models.geo_entrega import ClienteGeo
    clave = clave_de_cliente(cliente, municipio)
    return db.session.get(ClienteGeo, clave) if clave else None


# ═════════════════════════════════════════════════════════════════════════════
# 5 · La medida — sin ella, en tres meses nadie sabe si sirvió
# ═════════════════════════════════════════════════════════════════════════════

def cobertura() -> dict:
    """Cuántos clientes ya tienen coordenada y cuántos no.

    QUÉ AFIRMA: el estado de la base **en el momento de la consulta**, con su
    denominador explícito. QUÉ NO AFIRMA: nada sobre calidad del punto — un
    cliente con una sola captura de 90 m cuenta igual que uno con seis de 12 m.
    Por eso viaja también `con_una_sola_captura`.

    **El denominador son los clientes visitados**, no los que existen: un
    cliente al que nunca se le entregó no tuvo oportunidad de tener coordenada,
    y meterlo en el denominador haría que la cifra bajara al crecer el negocio
    — una métrica que empeora cuando las cosas van bien deja de mirarse.
    Regla 11 del módulo: antes de publicar, preguntarse cómo la maximiza quien
    no quiere hacer el trabajo. Acá la única forma de subirla es tocar el botón
    en la puerta del cliente, que es exactamente el trabajo.

    Regla 13 del módulo: el número sale con su fecha y su base. Quien lo
    publique en `ESTADO.md` tiene con qué.
    """
    from app.models.geo_entrega import ClienteGeo, EntregaGeo

    visitados = {c.cliente_clave for c in EntregaGeo.query.with_entities(
        EntregaGeo.cliente_clave).distinct()}

    maestros = ClienteGeo.query.all()
    con_coordenada = [m for m in maestros
                      if m.lat is not None and m.cliente_clave in visitados]
    sin_coordenada = [m for m in maestros
                      if m.lat is None and m.cliente_clave in visitados]

    por_motivo_maestro = {}
    for m in sin_coordenada:
        k = m.motivo_sin_maestro or NO_DECLARADO
        por_motivo_maestro[k] = por_motivo_maestro.get(k, 0) + 1

    capturas = EntregaGeo.query.all()
    por_motivo_captura = {}
    for c in capturas:
        if c.motivo_sin_dato:
            por_motivo_captura[c.motivo_sin_dato] = \
                por_motivo_captura.get(c.motivo_sin_dato, 0) + 1

    ultima = max((c.capturado_en for c in capturas if c.capturado_en),
                 default=None)

    return {
        'medido_en': datetime.utcnow().isoformat(),
        'clientes_visitados': len(visitados),
        'con_coordenada': len(con_coordenada),
        'sin_coordenada': len(sin_coordenada),
        # Un maestro de una sola captura no está mal: está solo. Se cuenta
        # aparte para que «180 clientes con coordenada» no se lea como 180
        # puntos verificados.
        'con_una_sola_captura': sum(1 for m in con_coordenada
                                    if (m.capturas_consideradas or 0) <= 1),
        'capturas_totales': len(capturas),
        'capturas_con_punto': sum(1 for c in capturas if c.lat is not None),
        'motivos_sin_captura': por_motivo_captura,
        'motivos_sin_maestro': por_motivo_maestro,
        'ultima_captura': ultima.isoformat() if ultima else None,
        # El umbral a partir del cual «ruteo» dejaría de ser una palabra. Viaja
        # con el número para que la pregunta «¿ya alcanza?» se conteste en la
        # misma pantalla y no de memoria. Ver `docs/flota/ESTADO.md`.
        'umbral_para_rutear': UMBRAL_PARA_RUTEAR,
    }
