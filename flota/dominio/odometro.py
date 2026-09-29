"""
Políticas del odómetro. Invariantes 1 (monotonía) y 7 (borde degenerado).

La base impone lo mismo por trigger y por CHECK
(`flota/adaptadores/modelos.py`). Está dicho dos veces a propósito: acá para dar
un error legible a quien lo escribe, allá para que no exista camino que lo
esquive. Lo que no se vale es que digan cosas distintas — por eso las dos mitades
se prueban contra los mismos casos.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple, Union

from flota.dominio.errores import LecturaRechazada
from flota.dominio.valores import SIN_DATO, Confianza, Lectura, OrigenLectura


#: El lugar de una lectura en la serie del odómetro (2026-09-27). Palabras y
#: no un booleano (regla 4). `None` en la base = anterior a la marca: cuenta.
SERIE_CUENTA = 'cuenta'
#: Un salto que no se explica (×10 o km en cero tiempo): NO sube el tope ni
#: mueve el odómetro hasta que una persona lo verifique.
SERIE_SALTO = 'salto'
#: Llegó DESPUÉS de lecturas con más km (un tanqueo sincronizado tarde): se
#: guarda para no perder el gasto, en duda, y nunca mueve el odómetro.
SERIE_TARDIA = 'tardia'
#: Menor que la última lectura que cuenta, hecho AHORA (no llegó tarde): o
#: esta está mal o la anterior (2026-09-29, VAL-COLA-1). Solo desde la cola del
#: conductor: en el patio a las 5 a. m. no se lo bloquea por un número que
#: tecleó otro. En duda, no sube ni baja el tope, y va a verificación; control
#: de flota anula la equivocada. Verificada (con la serie ya coherente), cuenta.
SERIE_CONTRADICE = 'contradice'
SERIES = (SERIE_CUENTA, SERIE_SALTO, SERIE_TARDIA, SERIE_CONTRADICE)
#: Las series con que puede entrar un km MENOR que el tope, si se pide.
SERIES_KM_MENOR = (SERIE_TARDIA, SERIE_CONTRADICE)


def validar_lectura(previas: Sequence[Lectura], nueva: Lectura) -> None:
    """Acepta o rechaza una lectura nueva contra el historial del vehículo.

    QUÉ AFIRMA: que `nueva` puede persistirse sin romper la monotonía del
    odómetro de ese vehículo.

    QUÉ NO AFIRMA: nada sobre si el valor es *cierto*. La verificación contra la
    foto del tablero es otra cosa y vive en otra parte. Esto solo dice que la
    serie sigue siendo una serie.

    Regla: el odómetro nunca decrece **respecto de las lecturas que cuentan**
    (`lecturas_que_cuentan`: sin las anuladas, sin los saltos que nadie
    verificó, sin las tardías). Un salto de ×10 tecleado por error ya no sube
    el tope para las siguientes (2026-09-27): antes, 125.000 donde iba 12.500
    dejaba el camión trabado — todo lo que viniera después daba 409.

    **La corrección anula UNA lectura** (`anula_lectura_id`), con motivo
    escrito y autor, y su propio valor es el odómetro de ahora: tiene que
    cuadrar con el resto de la serie, sin la anulada. Hasta el 2026-09-27 la
    corrección no apuntaba a nada, no se validaba, y dejaba atrás TODA la
    historia anterior (el CPK del mes salía +61%).

    Levanta `LecturaRechazada` si no se puede aceptar. No devuelve un valor
    corregido: en la frontera no se degrada hacia el éxito (regla 5).
    """
    if nueva.origen == OrigenLectura.CORRECCION:
        motivo = (nueva.motivo_correccion or '').strip()
        if not motivo:
            raise LecturaRechazada(
                'una corrección exige motivo escrito: sin él es indistinguible '
                'de un error de digitación'
            )
        if not nueva.autor_usuario_id:
            raise LecturaRechazada('una corrección exige autor: el punto es dejar rastro')
        if nueva.anula_lectura_id is None:
            raise LecturaRechazada(
                'una corrección anula UNA lectura: diga cuál (anula_lectura_id). '
                'Sin eso borraría la historia entera, no el error')
        anulada = next((l for l in previas if l.id == nueva.anula_lectura_id), None)
        if anulada is None:
            raise LecturaRechazada(
                f'la lectura {nueva.anula_lectura_id} no es de este vehículo')
        if nueva.anula_lectura_id in anuladas(previas):
            raise LecturaRechazada(
                f'la lectura {nueva.anula_lectura_id} ya se anuló: una lectura '
                f'se anula una sola vez')
        resto = [l for l in previas if l.id != nueva.anula_lectura_id]
        _exigir_que_no_decrezca(resto, nueva)
        return

    if not previas:
        return
    _exigir_que_no_decrezca(previas, nueva)


def _exigir_que_no_decrezca(previas: Sequence[Lectura], nueva: Lectura) -> None:
    """El tope es el MÁXIMO de las lecturas que cuentan, no la última: si ya se
    registró 100.450, una lectura de 100.000 decrece aunque la anterior fuera
    menor. Empates de `ts` cuentan hacia adentro (el lado estricto)."""
    vigentes = lecturas_que_cuentan(previas)
    if not vigentes:
        return
    tope = max(l.valor_km for l in vigentes)
    if nueva.valor_km < tope:
        _vig, desde = vigentes_tras_la_ultima_correccion(previas)
        _cuando = (f' (desde la corrección del {desde:%Y-%m-%d %H:%M})'
                   if desde is not None else '')
        raise LecturaRechazada(
            f'el odómetro no puede decrecer: {nueva.valor_km} < {tope}'
            f'{_cuando}. Si el número del tablero es ese, control de flota '
            f'anula la lectura equivocada.'
        )


def anuladas(lecturas: Sequence[Lectura]) -> set:
    """Los ids de las lecturas que una corrección anuló."""
    return {l.anula_lectura_id for l in lecturas if l.anula_lectura_id is not None}


def cuenta_en_la_serie(l: Lectura, ids_anulados) -> bool:
    """¿Esta lectura dice cuánto marca el odómetro? La pregunta, UNA vez.

    No cuenta: la anulada; el salto que nadie verificó; la tardía (llegó
    después de lecturas con más km: es del pasado, nunca mueve el odómetro).
    """
    if l.id is not None and l.id in ids_anulados:
        return False
    if l.serie == SERIE_TARDIA:
        return False
    if (l.serie in (SERIE_SALTO, SERIE_CONTRADICE)
            and l.confianza != Confianza.VERIFICADA):
        return False
    return True


def lecturas_que_cuentan(previas: Sequence[Lectura]) -> List[Lectura]:
    """Las lecturas con las que se decide: el tope, el odómetro vigente, el
    ritmo y el preventivo. Las que una corrección vieja (sin
    `anula_lectura_id`) dejó atrás tampoco — esa ventana se conserva para las
    correcciones de antes del 2026-09-27."""
    vigentes, _desde = vigentes_tras_la_ultima_correccion(previas)
    ids = anuladas(previas)
    return [l for l in vigentes if cuenta_en_la_serie(l, ids)]


def fuera_de_la_serie(previas: Sequence[Lectura]) -> dict:
    """Cuántas lecturas NO cuentan, y por qué — para declararlo, no esconderlo."""
    ids = anuladas(previas)
    return {
        'anuladas': sum(1 for l in previas if l.id is not None and l.id in ids),
        'saltos_sin_verificar': sum(
            1 for l in previas if l.serie == SERIE_SALTO
            and l.confianza != Confianza.VERIFICADA
            and not (l.id is not None and l.id in ids)),
        'tardias': sum(1 for l in previas if l.serie == SERIE_TARDIA
                       and not (l.id is not None and l.id in ids)),
        'contradicen': sum(1 for l in previas if l.serie == SERIE_CONTRADICE
                           and l.confianza != Confianza.VERIFICADA
                           and not (l.id is not None and l.id in ids)),
    }


def vigentes_tras_la_ultima_correccion(
        previas: Sequence[Lectura]) -> Tuple[List[Lectura], Optional[datetime]]:
    """Las lecturas que una corrección posterior NO dejó atrás, y desde cuándo.

    QUÉ AFIRMA: que las devueltas son las que siguen contando para ese vehículo
    — una corrección declara «de acá en adelante, esto es lo cierto», y lo
    anterior deja de ser el piso.

    QUÉ NO AFIRMA: que las que quedaron afuera estén mal, ni que hayan
    desaparecido. La tabla es append-only y siguen ahí; lo que dejan de hacer es
    decidir.

    Devuelve `(vigentes, desde)`, con `desde = None` cuando el vehículo nunca
    tuvo una corrección — y `None` acá significa «no hubo», no «no sé»: sale de
    mirar la lista entera.

    **Extraída el 2026-09-02 porque tiene un segundo consumidor**: la cola de
    verificación (`flota/adaptadores/verificacion.py`) necesita exactamente esta
    ventana para no pedirle a nadie que confirme un número que una corrección ya
    reemplazó. Escrita dos veces, la copia de la cola habría empezado a mostrar
    filas que la validación ya ignora — y quien las mirara estaría trabajando
    sobre lo que el sistema ya descartó.

    Empates de `ts` cuentan hacia ADENTRO de la ventana: en producción hay diez
    lecturas con el mismo segundo, y ante el empate conviene incluir de más.
    """
    ids = anuladas(previas)
    sin_anuladas = [l for l in previas if not (l.id is not None and l.id in ids)]
    # Solo las correcciones VIEJAS —sin `anula_lectura_id`, de antes del
    # 2026-09-27— abren ventana. La nueva anula una sola lectura y la historia
    # anterior sigue contando.
    correcciones = [l.ts for l in previas
                    if l.origen == OrigenLectura.CORRECCION
                    and l.anula_lectura_id is None]
    if not correcciones:
        return sin_anuladas, None
    desde = max(correcciones)
    return [l for l in sin_anuladas if l.ts >= desde], desde


def odometro_actual(lecturas: Sequence[Lectura]) -> Union[int, str]:
    """Kilometraje vigente de un vehículo, o SIN_DATO si nunca se leyó.

    QUÉ AFIRMA: el último kilometraje conocido, con su procedencia implícita en
    la lectura de la que sale.

    QUÉ NO AFIRMA: que el vehículo tenga ese recorrido hoy. Afirma lo último que
    alguien registró.

    Un vehículo sin lecturas devuelve SIN_DATO, jamás 0. Son estados distintos:
    0 km es "no ha rodado", SIN_DATO es "no sabemos". Devolver 0 aquí convierte
    todo CPK y todo preventivo por kilometraje aguas abajo en un número
    inventado con cara de medición.
    """
    cuentan = lecturas_que_cuentan(lecturas)
    if not cuentan:
        return SIN_DATO
    # La más reciente por marca de tiempo, no la de mayor kilometraje: una
    # corrección posterior debe ganarle a la lectura que corrige. Solo entre
    # las que cuentan (2026-09-27): un salto sin verificar, una anulada o una
    # tardía no son «el odómetro de ahora».
    return max(cuentan, key=lambda l: (l.ts, l.valor_km)).valor_km


# ═══════════════════════════════════════════════════════════════════════════
# El tercer estado — `confianza`
# ═══════════════════════════════════════════════════════════════════════════
#
# `validar_lectura` contesta «¿entra o no entra?». Es una pregunta binaria y por
# eso deja pasar el caso que dio origen a todo esto: **los 16.697.948 km del
# THP696 entraron**, porque crecer es lo único que la monotonía exige. Una vez
# adentro, ese número es indistinguible de uno bueno para cualquier cálculo
# aguas abajo.
#
# `confianza` es la segunda pregunta: «¿me puedo apoyar en este número?». Se
# contesta AL NACER la lectura, con lo que se sabe en ese momento, y se escribe
# en la fila. Nunca se convierte en un rechazo.
#
# ── POR QUÉ NO SE RECHAZA ──────────────────────────────────────────────────
# Bloquear al conductor a las 5 a.m. porque el número parece raro es cómo la
# operación desmonta el sistema en 48 horas — y desmontado no mide nada. Se
# marca, se saca del cálculo, y alguien la mira DESPUÉS con la foto al lado.
# Es la misma decisión que ya está tomada en `traspaso.traspasar` (una custodia
# con fotos faltantes se abre igual y el health la cuenta) y en
# `guardar_foto` (una dimensión declarada falsa se corrige y se cuenta, no
# rechaza el turno).

#: Cuántas veces la lectura previa tiene que caber en la nueva para marcarla.
#:
#: **ESTO ES UN UMBRAL, y es el único de la fase 0.** Todo lo demás que marca
#: `dudosa` son hechos —no hay foto; el reloj no avanzó y el odómetro sí— y un
#: hecho no necesita que nadie elija un número. Éste sí, así que se declara como
#: lo que es, con nombre propio y en un solo lugar.
#:
#: **Por qué ×10 no necesita medición previa**, que es la excepción a la regla
#: 13 del módulo y por eso hay que justificarla y no invocarla:
#:
#:   · Un orden de magnitud NO es un error de digitación de un dígito. Tecleando
#:     55.400 donde iba 55.000 se yerra en cientos; para multiplicar por diez hay
#:     que agregar un dígito entero, que es un gesto distinto. Marcar ×10 no
#:     depende de saber cuántos kilómetros hace un camión por día.
#:   · Un umbral de km/día SÍ dependería de eso, y por eso NO se fija hoy: el
#:     health publica `salto_km_maximo_30d` como hecho para poder fijar
#:     `km_dia_plausible_max` **por vehículo** dentro de un mes, con procedencia.
#:     Los dos números resuelven cosas distintas y sólo uno se puede escribir sin
#:     datos.
#:   · El caso real que lo motiva no es marginal: 55.349 → 16.697.948 es ×301.
#:     Cualquier factor entre 2 y 100 lo habría atrapado; se elige 10 porque un
#:     factor bajo empezaría a marcar operación sana (un vehículo con 800 km que
#:     hace un viaje largo puede llegar a 1.700, que es ×2).
#:
#: Lo que este umbral NO afirma: que ×9,9 esté bien. Afirma que ×10 no se puede
#: explicar por un dedo torcido y merece que alguien lo mire.
FACTOR_SALTO_SOSPECHOSO = 10


def _salto_en_cero_tiempo(valor_km, ts, previa_valor_km, previa_ts) -> Optional[str]:
    """Regla 2 · Δt = 0 con Δkm > 0: velocidad infinita (un hecho, no un umbral)."""
    if previa_valor_km is None or previa_ts is None:
        return None
    delta_km = valor_km - previa_valor_km
    if ts == previa_ts and delta_km > 0:
        return (f'{delta_km} km recorridos en cero tiempo: la lectura anterior '
                f'tiene exactamente la misma marca de tiempo')
    return None


def _salto_de_orden_de_magnitud(valor_km, previa_valor_km) -> Optional[str]:
    """Regla 3 · salto de ×10 (EL umbral, ver `FACTOR_SALTO_SOSPECHOSO`)."""
    if previa_valor_km is None:
        return None
    if previa_valor_km > 0 and valor_km >= previa_valor_km * FACTOR_SALTO_SOSPECHOSO:
        veces = valor_km // previa_valor_km
        return (f'salto de ×{veces} respecto de la lectura anterior '
                f'({previa_valor_km} → {valor_km}): un orden de magnitud no se '
                f'explica por un dígito mal tecleado')
    return None


#: El motivo escrito de una lectura tardía.
MOTIVO_TARDIA = ('llegó después de lecturas con más kilómetros (se registró sin '
                 'señal y se mandó tarde): se guarda para no perder el gasto, '
                 'queda para revisión y no mueve el odómetro')


#: El motivo escrito de una lectura que contradice a la anterior.
MOTIVO_CONTRADICE = ('es menor que la última lectura que cuenta: o esta está '
                     'mal o la anterior. No mueve el odómetro; control de flota '
                     'revisa las dos y anula la equivocada')


def serie_si_retrocede(*, pedida: str, valor_km: int,
                       ts_operacion: Optional[datetime],
                       previas: Sequence[Lectura]) -> str:
    """Con qué serie entra un km MENOR que el tope, cuando quien escribe lo
    permite (`pedida` ∈ `SERIES_KM_MENOR`).

    Si la operación se HIZO antes que la lectura que la supera (su hora del
    teléfono es creíble y anterior), no contradice a nadie: llegó tarde. Si no,
    es lo que se pidió. Un gasto siempre pide `tardia`; el recibo, la entrega,
    el daño y la inspección que llegan por la cola piden `contradice`.
    """
    if ts_operacion is not None:
        mayores = [l.ts for l in lecturas_que_cuentan(previas)
                   if l.valor_km > valor_km]
        if mayores and ts_operacion < max(mayores):
            return SERIE_TARDIA
    return pedida


def serie_al_nacer(*, valor_km: int, ts: datetime, previa_valor_km: Optional[int],
                   previa_ts: Optional[datetime], tardia: bool,
                   menor: Optional[str] = None) -> str:
    """Dónde entra una lectura nueva en la serie. Mismas reglas de salto que
    `confianza_al_nacer` (las mismas funciones): un salto nace dudoso Y fuera
    del tope, nunca una cosa sin la otra. `previa` es la última que CUENTA."""
    if menor in SERIES_KM_MENOR:
        return menor
    if tardia:
        return SERIE_TARDIA
    if (_salto_en_cero_tiempo(valor_km, ts, previa_valor_km, previa_ts)
            or _salto_de_orden_de_magnitud(valor_km, previa_valor_km)):
        return SERIE_SALTO
    return SERIE_CUENTA


def confianza_al_nacer(
    *,
    valor_km: int,
    ts: datetime,
    tiene_foto: bool,
    previa_valor_km: Optional[int],
    previa_ts: Optional[datetime],
    tardia: bool = False,
    menor: Optional[str] = None,
) -> Tuple[Confianza, Optional[str]]:
    """Con qué confianza nace una lectura, y **por qué**.

    QUÉ AFIRMA: que se aplicaron las tres reglas de marcado sobre los datos que
    existen en el instante de escribir la fila, y que el motivo devuelto los
    enumera todos.

    QUÉ NO AFIRMA:
      · que una `declarada` sea cierta. Significa «nada evidente la contradice»,
        no «se comprobó». Comprobar es lo que hace un humano por la cola, y eso
        produce `verificada`.
      · que una `dudosa` esté mal. Significa «no la uses para dividir hasta que
        alguien la mire». Las 26 lecturas sin foto de producción son casi con
        seguridad correctas; lo que no tienen es con qué demostrarlo.
      · nada sobre monotonía. Eso ya lo juzgó `validar_lectura`, y una lectura
        que retrocede no llega hasta acá: no existe.

    Nunca devuelve `VERIFICADA`. Ese valor sólo lo escribe una persona por la
    cola de verificación — un automatismo que pudiera escribirlo volvería
    decorativa la única marca que afirma que alguien miró.

    Los motivos se **acumulan**, no gana el primero: una lectura sin foto Y con
    salto de ×10 tiene dos problemas, y quien la revise tiene que verlos los dos.
    Quedarse con el primero es cómo un canal de avisos se vuelve ilegible.

    Argumentos por nombre a propósito: `previa_valor_km` y `previa_ts` son la
    lectura anterior del MISMO vehículo, o `(None, None)` si es la primera. Se
    reciben sueltos y no como `Lectura` porque quien llama a esta función está
    dentro de un `before_insert` y tiene columnas, no objetos de dominio.
    """
    motivos: List[str] = []

    # ── Regla 1 · sin foto (un hecho) ────────────────────────────────────────
    # Un kilometraje sin foto del tablero es una declaración sin respaldo: no se
    # puede cotejar contra nada, ni hoy ni en la discusión de dentro de un año.
    # En producción son 26 de 26 (medido el 2026-09-01), así que esta regla sola
    # ya llena la cola desde el día uno.
    if not tiene_foto:
        motivos.append('sin foto del tablero: el número no se puede cotejar '
                       'contra el vehículo')

    # ── Regla 2 · Δt = 0 con Δkm > 0 (un hecho, no un umbral) ────────────
    # Velocidad infinita. La comparación es de igualdad exacta y no «menos de
    # N segundos» justamente para que siga siendo un hecho (regla 13). En
    # producción hay diez lecturas con el mismo segundo (reintento del
    # 2026-08-03) con Δkm = 0: el mismo km dos veces es un duplicado, no un
    # viaje imposible, y no se marca.
    #
    # ── Regla 3 · salto de ×10 (EL umbral, ver FACTOR_SALTO_SOSPECHOSO) ──
    # `previa_valor_km > 0` no es defensiva: con un odómetro anterior en 0 el
    # factor no está definido. 0 → 100 son 100 km, lo más normal del mundo.
    #
    # Las dos viven en funciones que `serie_al_nacer` también usa: lo que nace
    # dudoso por salto nace fuera del tope, con la misma definición.
    for regla in (_salto_en_cero_tiempo(valor_km, ts, previa_valor_km, previa_ts),
                  _salto_de_orden_de_magnitud(valor_km, previa_valor_km)):
        if regla:
            motivos.append(regla)

    # ── Regla 4 · tardía (2026-09-27): un hecho que el adaptador comprobó ──
    if menor == SERIE_CONTRADICE:
        motivos.append(MOTIVO_CONTRADICE)
    elif tardia or menor == SERIE_TARDIA:
        motivos.append(MOTIVO_TARDIA)

    if motivos:
        return Confianza.DUDOSA, ' · '.join(motivos)
    return Confianza.DECLARADA, None


#: De más confiable a menos. El tramo hereda **el peor de sus dos extremos**.
_ORDEN = (Confianza.VERIFICADA, Confianza.DECLARADA, Confianza.DUDOSA)


def confianza_del_tramo(lectura_a: Lectura, lectura_b: Lectura
                        ) -> Union[Confianza, str]:
    """Con qué confianza se puede publicar un número calculado entre dos lecturas.

    Es la función que la fase 1 va a consumir para el CPK, y vive acá —pura, sin
    I/O y con sus tests— para que no nazca escrita dentro de la consulta del
    tablero. Una regla escrita dentro de un `SELECT` es una regla que se copia a
    la segunda pantalla y diverge; es el corolario de la regla 0 y en este repo
    ya costó 25× de sobrecompra.

    QUÉ AFIRMA:

        dudosa + dudosa   → SIN_DATO. **Los dos extremos sin respaldo no son un
                            número malo: no son un número.** Un CPK con los dos
                            kilometrajes en duda no tiene numerador ni
                            denominador confiables, y publicarlo «con asterisco»
                            garantiza que alguien lo va a promediar con los
                            buenos.
        una dudosa        → el peor extremo, o sea `dudosa`. El número SÍ se
                            devuelve —hay un extremo sólido y una resta real—
                            pero va marcado. Quien lo lea decide; quien lo
                            agregue puede excluirlo.
        ninguna dudosa    → el peor de los dos: `declarada` si alguno lo es,
                            `verificada` sólo si los dos lo son.

    QUÉ NO AFIRMA: nada sobre el valor del tramo. No lo calcula, no lo corrige y
    no lo rellena. **Nunca existe un promedio que tape el hueco** — ése era el
    camino fácil y es el que convierte un tablero en decoración.

    Devuelve `SIN_DATO` (la palabra) y no `None`: `None` invita a `valor or 0`,
    que es el default optimista que la regla 4 del módulo prohíbe.

    El orden es total y por eso no hay `.get(x, default)` en ninguna parte: una
    confianza fuera del vocabulario revienta con `ValueError` al indexar, que es
    lo que tiene que pasar (regla 5).
    """
    a, b = lectura_a.confianza, lectura_b.confianza
    if a == Confianza.DUDOSA and b == Confianza.DUDOSA:
        return SIN_DATO
    # `.index` y no un dict con default: si mañana aparece un cuarto valor de
    # confianza, esto tiene que reventar ruidosamente en vez de clasificarlo
    # como lo más confiable por omisión.
    return _ORDEN[max(_ORDEN.index(a), _ORDEN.index(b))]


# ═══════════════════════════════════════════════════════════════════════════
# El ritmo de uso — km/día
# ═══════════════════════════════════════════════════════════════════════════
#
# Vive acá y no en `flota/dominio/preventivo.py` por la misma razón por la que
# `confianza_del_tramo` no vive dentro del CPK: **es una propiedad de la serie
# de odómetro, no del plan de mantenimiento.** El preventivo es su primer
# consumidor y no va a ser el último — «cuánto rueda este camión por día» es la
# misma pregunta que contesta cuándo se acaba una llanta y cuándo toca la
# próxima tecnomecánica. Escrita adentro del preventivo, la segunda copia
# nacería en la pantalla de llantas y sería la que diverja.
#
# **Ninguna de estas líneas fija un umbral** (regla 13). Devuelve un hecho
# medido y su marca; quién decida que 6 días es «pronto» es otra función, y esa
# sí lleva su umbral declarado con nombre propio.


@dataclass(frozen=True)
class RitmoDeUso:
    """Cuánto rueda un vehículo por día, **con todo lo que hace falta para dudar**.

    `km_dia` suelto no se puede auditar: sobre dos lecturas de un mismo día dice
    lo mismo que sobre cuarenta de tres meses, y son dos números distintos. Por
    eso `n` y `dias` viajan al lado, igual que `pesos` y `km` viajan con el CPK.

    `motivo` sólo se llena cuando `km_dia` es `SIN_DATO`, y enumera por qué no
    se pudo medir. Un `SIN_DATO` sin motivo obliga a quien lo lea a adivinar si
    faltan lecturas, si el vehículo está quieto o si el tramo es dudoso — y las
    tres se corrigen distinto.
    """

    km_dia: Union[Decimal, str]
    marca: Union[Confianza, str]
    n: int
    dias: Union[Decimal, str]
    motivo: Optional[str] = None


def km_por_dia(lecturas: Sequence[Lectura]) -> RitmoDeUso:
    """El ritmo de uso del vehículo sobre sus lecturas vigentes.

    QUÉ AFIRMA: cuántos kilómetros por día separan la primera y la última
    lectura **vigente** de este vehículo, y con qué confianza se puede publicar
    ese cociente.

    QUÉ NO AFIRMA:

    · **Que el vehículo vaya a seguir rodando así.** Es el ritmo que tuvo, no
      el que va a tener. Todo lo que se derive de acá —«te faltan unos 6 días»—
      es una proyección y tiene que decirlo con esa palabra.
    · **Nada sobre quién maneja** (regla 2). No recibe conductor y no debe
      recibirlo nunca: el mismo camión con dos custodios da un solo número.
    · **Que sea comparable entre vehículos.** Un motocarro de reparto urbano y
      un NHR de ruta intermunicipal no se comparan por km/día más de lo que se
      comparan por CPK.

    ## Sin ventana de tiempo, a propósito

    Se mide sobre **todas** las lecturas vigentes y no sobre los últimos 30 ó
    90 días. Cualquier ventana sería un umbral inventado (regla 13) y en este
    módulo hay exactamente un umbral justificado, que es
    `FACTOR_SALTO_SOSPECHOSO`. Con `n` y `dias` publicados al lado, quien lea el
    número sabe sobre cuánta historia está parado y puede desconfiar solo.

    ## Vigentes, no todas — `vigentes_tras_la_ultima_correccion`

    Es el tercer consumidor de esa función y por eso está extraída. Sin ella,
    los 16.697.948 km del THP696 harían que su km/día fuera de seis cifras para
    siempre, y **la corrección que lo arregló no lo arreglaría**: el tramo
    seguiría arrancando en la lectura envenenada.

    ## Los dos ceros, otra vez

    Un vehículo quieto devuelve `Decimal('0')`, que es un cero **medido**: hubo
    dos lecturas, pasaron días y el odómetro no se movió. Un vehículo con una
    sola lectura devuelve `SIN_DATO`. Confundirlos convertiría «no sabemos» en
    «está parado», y de ahí en «nunca va a llegar al cambio de correa».
    """
    vigentes = lecturas_que_cuentan(lecturas)
    n = len(vigentes)

    if n < 2:
        return RitmoDeUso(
            km_dia=SIN_DATO, marca=SIN_DATO, n=n, dias=SIN_DATO,
            motivo='menos de dos lecturas vigentes: no hay tramo que medir')

    # Desempate por `valor_km` porque el dominio no tiene `id`: en producción
    # hay diez lecturas con el mismo segundo (`lecturas_ts_duplicado`), y sin
    # desempate estable el mismo vehículo podría publicar dos ritmos distintos
    # en dos consultas seguidas.
    primera = min(vigentes, key=lambda l: (l.ts, l.valor_km))
    ultima = max(vigentes, key=lambda l: (l.ts, l.valor_km))

    dias = Decimal((ultima.ts - primera.ts).total_seconds()) / Decimal(86400)
    if dias <= 0:
        return RitmoDeUso(
            km_dia=SIN_DATO, marca=SIN_DATO, n=n, dias=SIN_DATO,
            motivo='todas las lecturas vigentes tienen la misma marca de '
                   'tiempo: no hay días entre las que dividir')

    delta_km = ultima.valor_km - primera.valor_km
    if delta_km < 0:
        # No lo produce la operación —`validar_lectura` no deja decrecer dentro
        # de la ventana vigente— pero sí lo produce un `INSERT` a mano o una
        # corrección mal cargada. **Un km/día negativo es peor que ninguno**:
        # se propaga a «faltan −4 días» y eso se lee como vencido.
        return RitmoDeUso(
            km_dia=SIN_DATO, marca=SIN_DATO, n=n, dias=dias,
            motivo=f'el tramo vigente decrece ({primera.valor_km} → '
                   f'{ultima.valor_km}): un ritmo negativo no existe')

    marca = confianza_del_tramo(primera, ultima)
    if marca == SIN_DATO:
        # Los dos extremos dudosos. Es la misma decisión que el CPK: no es un
        # ritmo bajo ni uno alto, **no es un ritmo**. Publicarlo «con asterisco»
        # garantiza que alguien lo promedie con los buenos.
        return RitmoDeUso(
            km_dia=SIN_DATO, marca=SIN_DATO, n=n, dias=dias,
            motivo='los dos extremos del tramo son lecturas dudosas: el '
                   'cociente no tendría numerador ni denominador confiables')

    return RitmoDeUso(km_dia=Decimal(delta_km) / dias, marca=marca,
                      n=n, dias=dias)


# ═══════════════════════════════════════════════════════════════════════════
# ¿Es creíble este kilometraje? — la pregunta del teléfono (2026-09-27)
# ═══════════════════════════════════════════════════════════════════════════
#
# **No es una regla de confianza ni un rechazo.** Es el techo a partir del cual
# la PANTALLA del conductor le pregunta «¿seguro?» antes de mandar: 125.000
# donde iba 12.500 se ataja en el patio, no en la cola de verificación tres
# días después. El servidor no lo usa para nada: su única marca por salto
# sigue siendo el ×10 (regla 13: un umbral de km/día no se fija sin
# medición). Por eso los números van con nombre, son provisionales, y viajan
# en la respuesta para que se vea de dónde salió la pregunta.

#: Cuántas veces el ritmo medido de ESTE vehículo se tolera antes de preguntar.
FACTOR_RITMO_PLAUSIBLE = 3
#: Piso del techo con ritmo medido: un vehículo que casi no rueda también
#: puede hacer un viaje largo.
#: 600 y no 400 (2026-09-29, validación): Neiva–Florencia ida y vuelta son
#: ~500 km, y un camión intermunicipal que promedia 120 km por día calendario
#: preguntaba cada vez que iba. Es para todos los vehículos: el WMS no sabe
#: cuáles son intermunicipales, y un piso más alto solo quita preguntas.
KM_DIA_PLAUSIBLE_MINIMO = 600
#: Techo sin ritmo medido (menos de dos lecturas que cuenten, o tramo dudoso).
KM_DIA_PLAUSIBLE_SIN_HISTORIA = 800


def techo_km_por_dia(ritmo: 'RitmoDeUso') -> dict:
    """El techo de km por día a partir del cual el teléfono pregunta, y de
    dónde sale. Provisional y declarado (ver el encabezado de la sección)."""
    if isinstance(ritmo.km_dia, Decimal) and ritmo.marca != SIN_DATO:
        techo = max(KM_DIA_PLAUSIBLE_MINIMO,
                    int(ritmo.km_dia * FACTOR_RITMO_PLAUSIBLE))
        return {'km_dia': techo, 'base': (
            f'{FACTOR_RITMO_PLAUSIBLE} veces lo que suele rodar este vehículo '
            f'(~{int(ritmo.km_dia)} km por día sobre {ritmo.n} lecturas), '
            f'mínimo {KM_DIA_PLAUSIBLE_MINIMO}')}
    return {'km_dia': KM_DIA_PLAUSIBLE_SIN_HISTORIA, 'base': (
        f'sin ritmo medido de este vehículo: {KM_DIA_PLAUSIBLE_SIN_HISTORIA} '
        f'km por día, provisional')}


__all__ = ['validar_lectura', 'odometro_actual', 'confianza_al_nacer',
           'serie_al_nacer', 'lecturas_que_cuentan', 'cuenta_en_la_serie',
           'fuera_de_la_serie', 'anuladas', 'techo_km_por_dia',
           'SERIE_CUENTA', 'SERIE_SALTO', 'SERIE_TARDIA', 'SERIE_CONTRADICE',
           'SERIES', 'SERIES_KM_MENOR', 'MOTIVO_TARDIA', 'MOTIVO_CONTRADICE',
           'serie_si_retrocede',
           'confianza_del_tramo', 'vigentes_tras_la_ultima_correccion',
           'km_por_dia', 'RitmoDeUso',
           'FACTOR_SALTO_SOSPECHOSO', 'SIN_DATO']
