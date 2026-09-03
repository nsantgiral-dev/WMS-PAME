"""
La plata que sale — vocabulario y cálculo. Sin I/O, sin framework.

`costo_por_kilometro` tiene canon en `docs/flota/canones/costo_por_kilometro.md`.
Los tests salen de ese documento, no de este archivo.

## Qué hace este módulo y qué NO hace

Hace **aritmética sobre hechos ya registrados**: repartir un gasto de período
sobre una ventana, dividir pesos entre kilómetros, dividir kilómetros entre
galones. Nada de acá lee la base ni decide permisos.

No hace **contabilidad**. La decisión del §4 del plan («Dónde vive el dinero»)
es que el valor, el proveedor, el impuesto y la causación viven en Siesa, y que
el puente es una **referencia** (`documento_numero`), no una copia. El `valor`
que entra acá existe para poder dividirlo entre kilómetros — para contestar
*«¿cuánto me cuesta el kilómetro del TGZ653?»*, que el ERP no contesta hoy **no
porque le falte la plata: le falta el kilómetro**. Un número de este módulo no
cuadra con un balance y no debe usarse para eso.

No nombra a nadie. Ningún cálculo de acá toma un conductor como entrada ni lo
devuelve: la regla 2 del módulo no es una advertencia de redacción, es una
restricción de firma. Quién manejaba está en la custodia, y se cruza a mano.
"""
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence, Tuple, Union

from flota.dominio.errores import ErrorFlota
from flota.dominio.valores import SIN_DATO


class CalculoImposible(ErrorFlota):
    """La pregunta está mal hecha y responderla con un número la volvería
    una respuesta mal creída. Es el mismo criterio que
    `dias_hallazgo_abierto` sobre un hallazgo de línea base."""


# ── Vocabularios ─────────────────────────────────────────────────────────────
#
# Viven acá y la tabla los sigue, igual que `TIPOS_DOCUMENTO` en `valores.py` y
# `EstadoHallazgo` en `hallazgo.py`. Un enum de Python que la base no conoce es
# una convención; un CHECK armado desde esta tupla es una garantía.

#: Qué clase de plata es. **Catálogo cerrado, y por eso generoso.**
#:
#: `TIPOS_DOCUMENTO` se cerró en cuatro y los cuatro que el dueño pidió después
#: —impuesto vehicular, todo riesgo, RCE, peritaje— **dan 400**. Esa lección
#: está a dos meses de distancia y se aplica acá de dos formas: el catálogo
#: nace cubriendo lo que el plan nombra *y* lo que la sección de documentos
#: reportó como faltante, y existe `otro` con descripción obligatoria para que
#: un gasto real nunca se quede sin poder registrarse.
#:
#: `otro` no es la salida cómoda: cuesta escribir qué fue (CHECK en la base). Si
#: aparece tres veces la misma palabra en esa descripción, eso es una categoría
#: pidiendo existir, y se agrega **acá**.
CATEGORIAS_GASTO = (
    'combustible',
    'mantenimiento',
    'repuesto',
    'llanta',
    'soat',
    'rtm',
    'impuesto_vehicular',
    'seguro',
    'peaje',
    'lavado',
    'parqueadero',
    'multa',
    'otro',
)

#: Categorías cuyo gasto **cubre un período** y no el día en que se pagó.
#:
#: «El SOAT no se carga al CPK del día que se pagó» es la decisión del plan. La
#: lista es de categorías y no un booleano que teclea quien registra: si fuera
#: una casilla, el camino barato sería no marcarla y el gasto entero caería
#: sobre un día (regla 11).
CATEGORIAS_CON_PERIODO = ('soat', 'rtm', 'impuesto_vehicular', 'seguro')

#: De dónde salió la plata. **Es la pregunta que el dueño no contestó todavía**
#: (¿tanquean con tarjeta/convenio o con efectivo del conductor?), modelada con
#: palabras para que la tabla aguante las dos respuestas sin migrar.
#:
#: `sin_dato` está en el vocabulario y **no es el default**: hay que escribirlo.
#: Si el registro es efectivo del conductor, es además una legalización de
#: gasto y quién aprueba cambia — esa consecuencia se decide cuando la respuesta
#: llegue, y hasta entonces la columna dice honestamente que no se sabe.
ORIGENES_COSTO = ('tarjeta_convenio', 'credito_proveedor',
                  'efectivo_conductor', 'sin_dato')

#: Cómo quedó el tanque. **Sin default, y las tres se eligen a mano.**
#:
#: El rendimiento solo es calculable de tanque lleno a tanque lleno: sobre uno
#: parcial, dividir km entre galones da un número que varía con lo que quedaba
#: adentro. Un `lleno` puesto por inercia no produce un error — produce una
#: ventana basura que infla o hunde la métrica sin que nada se vea raro.
#:
#: Es la regla 1 del módulo (ningún ítem tiene valor por defecto) aplicada al
#: único campo de esta fase que decide si una medición existe.
ESTADOS_TANQUE = ('lleno', 'parcial', 'sin_dato')

#: Marcas de confianza de un tramo de odómetro.
#:
#: **No las produce este módulo.** Otro agente está agregando `confianza` a
#: `flota_lectura_odometro` y una función pura en `flota/dominio/odometro.py`
#: que decide la marca de un tramo. Acá solo se declara el vocabulario que el
#: CPK sabe consumir, para no escribir una segunda política de confianza que
#: divergiría de la primera (regla 0, corolario).
#:
#: Ver `costo_por_kilometro` para el contrato exacto y el enganche declarado.
MARCAS_TRAMO = ('verificada', 'declarada', 'dudosa')


def exige_periodo(categoria: str) -> bool:
    """¿Esta categoría cubre un período, o se consume el día que ocurrió?

    Total sobre el vocabulario y **sin `.get(categoria, False)`**: una categoría
    desconocida tiene que reventar. Un default `False` haría que una categoría
    nueva de las periodificables —el todo riesgo, el peritaje— cargara su año
    entero sobre un día, en silencio y con cara de número medido. Regla 5.
    """
    if categoria not in CATEGORIAS_GASTO:
        raise ValueError(
            f'categoría de gasto desconocida: {categoria!r}. '
            f'Conocidas: {", ".join(CATEGORIAS_GASTO)}.'
        )
    return categoria in CATEGORIAS_CON_PERIODO


# ── El reparto: una función con nombre, no una línea dentro de una consulta ───

def dias_del_periodo(desde: date, hasta: date) -> int:
    """Días **calendario, ambos extremos incluidos**.

    Un SOAT del 1-ene al 31-dic cubre 365 días, no 364. La inclusión de los dos
    extremos es una decisión del canon y está acá y en ningún otro lado: escrita
    dos veces, la segunda copia usa `.days` pelado y el año pierde un día sin
    que nada falle.
    """
    if hasta < desde:
        raise ValueError(
            f'período invertido: {hasta} termina antes de {desde}. '
            f'No se reparte hacia atrás.'
        )
    return (hasta - desde).days + 1


def imputar_a_ventana(*, valor: Decimal, periodo_desde: date, periodo_hasta: date,
                      ventana_desde: date, ventana_hasta: date) -> Decimal:
    """Cuánto de este gasto le toca a esta ventana. La regla de reparto.

    QUÉ AFIRMA: la porción del gasto que corresponde a los días de la ventana,
    repartida **linealmente por día calendario** sobre el período que el gasto
    declara cubrir.

    QUÉ NO AFIRMA:

    · **Que el gasto se haya consumido así.** Un SOAT no se gasta un poco cada
      día: se paga entero y protege todo el año. El reparto lineal es una
      convención para que el CPK de marzo no dependa de en qué mes se firmó la
      póliza — no una descripción de la realidad física.
    · **Que sea la causación contable.** Eso vive en Siesa (§4 del plan). Un
      número de acá no cuadra con un balance.
    · **Nada sobre el uso del vehículo.** Reparte por días, no por kilómetros.
      Un camión parado dos semanas recibe la misma porción de SOAT que uno que
      no paró — y eso está bien: el seguro corrió igual.

    Vive en una función con nombre y su canon, y **no escrita dentro de la
    consulta del tablero**, por el motivo de siempre: la copia que se queda
    dentro del SQL es la que nadie encuentra cuando la regla cambia, y es la que
    la gente mira.

    Sin solape devuelve **`Decimal('0')` y eso sí es un cero legítimo**: el SOAT
    de este año aporta exactamente nada al CPK de un mes del año pasado. No se
    confunde con `SIN_DATO` —«no se puede calcular»—, que es lo que devuelve el
    CPK cuando no hay kilómetros.
    """
    if ventana_hasta < ventana_desde:
        raise ValueError(
            f'ventana invertida: {ventana_hasta} termina antes de {ventana_desde}.')
    dias = dias_del_periodo(periodo_desde, periodo_hasta)

    inicio = max(periodo_desde, ventana_desde)
    fin = min(periodo_hasta, ventana_hasta)
    if fin < inicio:
        return Decimal('0')
    solapados = (fin - inicio).days + 1
    # Se multiplica ANTES de dividir: `valor / dias * solapados` arrastra a cada
    # término el error de una división que ya se hizo, en vez de redondear una
    # sola vez al final.
    #
    # **Corregido el 2026-09-02 contra la medición** (canon §8): la versión
    # anterior de este comentario afirmaba que «con Decimal y en este orden los
    # doce meses suman exacto». No es cierto en general. Repartiendo sobre los
    # 12 meses de 2026 con Decimal de 28 dígitos: $730.000 y $1.860.000 —los dos
    # divisibles entre 365— suman exacto en LOS DOS órdenes, y $1.000.000 no
    # suma exacto en ninguno (999.999,999…998 acá, …999 al revés). Lo que decide
    # la exactitud es que el valor sea divisible entre los días del período. El
    # orden sigue siendo éste porque arrastra un redondeo en vez de dos; la
    # justificación se ajustó a lo que la medición aguanta.
    return Decimal(valor) * Decimal(solapados) / Decimal(dias)


# ── El CPK ───────────────────────────────────────────────────────────────────

def costo_por_kilometro(
    *,
    pesos_imputados: Decimal,
    km_recorridos: int,
    hubo_gastos: bool,
    marca_tramo: str,
) -> Tuple[Union[Decimal, str], str]:
    """Pesos por kilómetro de un vehículo en una ventana, **con su marca**.

    Devuelve `(valor, marca)`. El valor es un `Decimal` o `SIN_DATO`; la marca
    siempre viaja, incluso cuando el valor no se pudo calcular — quien lo pinta
    tiene que poder decir por qué.

    QUÉ AFIRMA: cuánto costó cada kilómetro que este vehículo recorrió en esta
    ventana, con el gasto de período ya repartido.

    QUÉ NO AFIRMA — y las cuatro importan porque el número va a un tablero:

    · **No compara vehículos.** Un NHR y un motocarro no se comparan; el plan lo
      dice y la firma lo respeta: esta función recibe UN vehículo y no sabe que
      existen otros. «¿Qué vehículo se come la plata?» se contesta en pesos por
      mes, no en CPK.
    · **No mide a nadie.** No recibe conductor. Quién manejaba está en la
      custodia (regla 2).
    · **No es el costo total de poseer el vehículo.** Es lo que se registró:
      depreciación, financiación y el tiempo de quien gestiona quedan fuera.
    · **No es contabilidad.** Ver el encabezado del módulo.

    ## Los tres casos que devuelven `SIN_DATO` y ninguno devuelve cero

    | Caso | Por qué no es cero |
    |---|---|
    | `km_recorridos <= 0` | Un CPK de cero kilómetros no es cero pesos por kilómetro: es que no se puede dividir. Es el mismo criterio de `promedio_del_indicador` sobre cero elementos |
    | Sin ningún gasto registrado (`hubo_gastos=False`) | «Nadie registró nada» y «no costó nada» se ven idénticos en un cero, y el primero es el estado real de esta fase — la operación viene perdiendo las facturas. Un CPK de $0 se leería como un vehículo gratis |
    | `marca_tramo` = `SIN_DATO` | Los dos extremos del tramo son dudosos: no se sabe cuántos kilómetros recorrió. **Nunca un promedio que rellena** |

    `hubo_gastos=True` con `pesos_imputados = 0` **sí** devuelve cero, y es un
    cero medido: hubo gastos registrados y ninguno cae dentro de la ventana (el
    SOAT del año pasado, por ejemplo). Los dos ceros existen y son distintos.

    ## `marca_tramo` — el enganche con la confianza del odómetro

    **Esta función no decide la marca: la recibe.** La política de confianza
    (`declarada | dudosa | verificada`) es de otro agente y va en
    `flota/dominio/odometro.py`, junto con la columna `confianza` de
    `flota_lectura_odometro`. Escribir acá una segunda es la regla 0 otra vez, y
    la copia que diverja sería la que decide si un número se publica.

    Contrato que el CPK le pide a esa función, declarado para que las dos mitades
    digan lo mismo:

    · devuelve una de `MARCAS_TRAMO` cuando el tramo se puede usar;
    · devuelve `SIN_DATO` cuando **los dos extremos** son dudosos.

    Con un solo extremo dudoso el tramo vale `'dudosa'`: se devuelve el número
    **y** la marca, que es exactamente lo que el plan pide. Rellenar con un
    promedio de la flota sería inventar el kilometraje que falta.

    Hoy la columna no existe y el adaptador pasa `'declarada'`. **No es un
    default optimista: es el hecho.** Las 26 lecturas de producción las declaró
    una persona, ninguna se verificó contra su foto y ninguna está marcada como
    dudosa — `declarada` es literalmente lo que son. El día que la columna
    exista, el adaptador llama a la función y este contrato no cambia.
    """
    if marca_tramo == SIN_DATO:
        return SIN_DATO, SIN_DATO
    if marca_tramo not in MARCAS_TRAMO:
        raise ValueError(
            f'marca de tramo desconocida: {marca_tramo!r}. '
            f'Conocidas: {", ".join(MARCAS_TRAMO)}, o SIN_DATO cuando los dos '
            f'extremos son dudosos.'
        )
    if km_recorridos <= 0:
        return SIN_DATO, marca_tramo
    if not hubo_gastos:
        return SIN_DATO, marca_tramo
    return Decimal(pesos_imputados) / Decimal(km_recorridos), marca_tramo


# ── Combustible ──────────────────────────────────────────────────────────────

def precio_por_galon(*, valor: Decimal, galones: Decimal) -> Union[Decimal, str]:
    """Lo que costó el galón en ese tanqueo.

    **Se calcula, no se guarda.** Es la misma decisión que el plan toma para
    `km_acumulado` de una llanta: con `valor` y `galones` ya en la fila, una
    tercera columna con el precio es un dato que puede contradecir a los otros
    dos, y el día que alguien corrija el valor de una factura mal digitada el
    precio queda apuntando al número viejo. Denormalizar es garantizar
    divergencia.

    Lo que el plan pide —«precio del galón por estación»— sigue estando: sale de
    esta función, agrupado por `flota_tanqueo.estacion`.

    Cero galones devuelve `SIN_DATO`, no cero pesos: un tanqueo de cero galones
    no es combustible gratis, es una fila mal cargada.
    """
    if Decimal(galones) <= 0:
        return SIN_DATO
    return Decimal(valor) / Decimal(galones)


def excede_capacidad(*, galones: Decimal,
                     capacidad_galones: Optional[Decimal]) -> Union[bool, str]:
    """El primer detector, el que **no necesita umbral ni canon ni historia**.

    QUÉ AFIRMA: que en el tanque entraron más galones de los que el tanque, según
    su ficha, puede contener. Un tanque de 15 galones que recibe 22 no es error
    de medición.

    QUÉ NO AFIRMA — y esto no es prudencia, es la regla 2:

    > **No afirma que alguien se haya llevado nada.** Afirma que dos datos no
    > pueden ser los dos ciertos: la capacidad de la ficha o los galones
    > registrados. Puede ser una capacidad mal levantada, un tanque auxiliar que
    > la ficha no conoce, dos vehículos tanqueados en la misma factura, un dedo
    > en el teclado. Todas se investigan igual de rápido, y ninguna se investiga
    > mejor si el sistema ya dictó sentencia.

    Sin capacidad en la ficha devuelve **`SIN_DATO`, jamás `False`**. Es el
    corazón del detector: `False` significaría «se revisó y está bien», y lo que
    pasa es que no hay contra qué revisar. Un vehículo sin capacidad declarada
    saldría limpio para siempre, que es exactamente cómo un detector se apaga sin
    que nadie lo note.
    """
    if capacidad_galones is None:
        return SIN_DATO
    return Decimal(galones) > Decimal(capacidad_galones)


def ventanas_lleno_a_lleno(tanqueos: Sequence[dict]) -> list:
    """Los tramos sobre los que el rendimiento **sí** es calculable.

    `tanqueos` viene ordenado por fecha y cada uno es
    `{'km': int, 'galones': Decimal, 'tanque': str}`.

    QUÉ AFIRMA cada ventana devuelta: que entre dos tanques llenos el vehículo
    recorrió `km` kilómetros consumiendo `galones` galones — y eso es cierto
    porque **de lleno a lleno, lo que entró al tanque es exactamente lo que se
    gastó**. Lo que quedaba adentro al empezar es lo mismo que quedaba al
    terminar: lleno.

    QUÉ NO AFIRMA: nada sobre los tramos que quedan fuera. Un `parcial` o un
    `sin_dato` en un extremo no produce una ventana peor — no produce ninguna, y
    esa ausencia es el punto entero del campo `tanque`.

    Los galones de la ventana son los cargados **después** del primer lleno y
    hasta el segundo inclusive, contando los parciales del medio: esos galones
    también se quemaron dentro del tramo. Lo que no puede pasar es que un extremo
    no sea lleno.

    Con menos de dos tanqueos llenos devuelve lista vacía. No es un error ni un
    cero: es que todavía no hay una sola ventana medible, y `[]` lo dice sin
    fingir que se midió algo.
    """
    llenos = [i for i, t in enumerate(tanqueos) if t['tanque'] == 'lleno']
    ventanas = []
    for a, b in zip(llenos, llenos[1:]):
        km = tanqueos[b]['km'] - tanqueos[a]['km']
        galones = sum((Decimal(tanqueos[i]['galones'])
                       for i in range(a + 1, b + 1)), Decimal('0'))
        if km <= 0 or galones <= 0:
            # Un tramo de cero kilómetros entre dos llenos no es rendimiento
            # infinito ni cero: es que las dos lecturas dicen lo mismo. Se
            # descarta la ventana en vez de publicar un número imposible.
            continue
        ventanas.append({
            'km_desde': tanqueos[a]['km'],
            'km_hasta': tanqueos[b]['km'],
            'km': km,
            'galones': galones,
            'km_por_galon': Decimal(km) / galones,
        })
    return ventanas


def rendimiento_km_galon(tanqueos: Sequence[dict]) -> Union[Decimal, str]:
    """Rendimiento del vehículo sobre todas sus ventanas lleno-a-lleno.

    QUÉ AFIRMA: kilómetros por galón, medido sobre los tramos donde la medición
    tiene sentido.

    QUÉ NO AFIRMA:

    · **No mide a quien maneja.** El plan es explícito: *«Scoring de conductores
      por km/galón — la respuesta de quien no quiere hacer el trabajo es tanquear
      a medias y declarar lleno»*. La firma no recibe conductor y no debe
      recibirlo nunca.
    · **No se compara entre vehículos.** Un motocarro y un NHR no rinden igual y
      la diferencia no dice nada.
    · **No explica una caída.** Un filtro tapado, una ruta con más montaña y un
      sifón producen el mismo número. El sistema dice cuánto rindió; el porqué lo
      averigua una persona.

    Se agrega sumando kilómetros y galones de todas las ventanas —no promediando
    los km/gal de cada una—: el promedio de razones le da el mismo peso a un
    tramo de 40 km que a uno de 600.

    Sin ninguna ventana devuelve `SIN_DATO`. Nunca cero: cero km/gal sería un
    camión que no se mueve gastando combustible.
    """
    ventanas = ventanas_lleno_a_lleno(tanqueos)
    if not ventanas:
        return SIN_DATO
    km = sum(v['km'] for v in ventanas)
    galones = sum((v['galones'] for v in ventanas), Decimal('0'))
    if galones <= 0:                        # pragma: no cover — filtrado arriba
        return SIN_DATO
    return Decimal(km) / galones


__all__ = [
    'CATEGORIAS_GASTO', 'CATEGORIAS_CON_PERIODO', 'ORIGENES_COSTO',
    'ESTADOS_TANQUE', 'MARCAS_TRAMO', 'exige_periodo', 'dias_del_periodo',
    'imputar_a_ventana', 'costo_por_kilometro', 'precio_por_galon',
    'excede_capacidad', 'ventanas_lleno_a_lleno', 'rendimiento_km_galon',
    'CalculoImposible', 'SIN_DATO',
]
