"""
El mantenimiento que se hace ANTES de que se rompa. Sin base, sin framework.

`flota_ficha_tecnica.distribucion_km_cambio` está cargado en la base desde la
tanda 1 y **nadie lo lee**. Es la tabla diciendo a qué kilometraje toca cambiar
la correa, mientras la correa envejece. Una correa que revienta en motor de
interferencia no es una correa: es un motor.

Este módulo es la política que convierte ese número en una respuesta a *«¿toca
o no toca?»*. No sabe que existe SQLAlchemy y no sabe que existe WhatsApp.

## Las tres cosas que este módulo se niega a hacer

1. **No devuelve `False` cuando no sabe.** Una tarea sin intervalo declarado y
   una tarea que nunca se ejecutó no están al día ni vencidas: son dos estados
   propios con nombre propio (regla 4). `False` ahí significaría «se revisó y
   está bien», que es la evidencia falsa de seguridad de la regla 1.
2. **No suelta un número sin su procedencia** (regla 13 del módulo). Un
   intervalo de 60.000 km que salió de `estimado` y uno que salió de
   `manual_fabricante` no valen lo mismo, y el que los mira tiene que poder
   distinguirlos sin abrir la ficha.
3. **No inventa un umbral de kilómetros.** `vencida` no necesita ninguno: lo
   dice el fabricante. `por_vencer` sí necesita anticipación, y esa
   anticipación se expresa en DÍAS —ver `DIAS_AVISO_PREVENTIVO`— porque los
   días son lo que tarda conseguir el repuesto, y los kilómetros no.
"""
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Union

from flota.dominio.odometro import RitmoDeUso
from flota.dominio.valores import FUENTES, SIN_DATO

# ── Vocabulario ──────────────────────────────────────────────────────────────

#: Qué tareas puede proponer una ficha técnica. **Cerrado**: cada una existe
#: porque hay un campo de `flota_ficha_tecnica` que la dispara, y una tarea sin
#: campo que la siembre sería una fila que alguien tendría que escribir a mano
#: — que es exactamente lo que este módulo existe para no pedir.
#:
#: El orden es el de gravedad de la falla que previenen, y se usa para ordenar
#: la pantalla: una correa de distribución que revienta cuesta un motor; una
#: cadena sin lubricar cuesta una cadena.
TIPOS_TAREA = (
    'distribucion',
    'aceite_motor',
    'aceite_caja',
    'aceite_diferencial',
    'refrigerante',
    'lubricacion_cadena',
)

#: Cómo se lee cada tipo en pantalla. Sin `.get(tipo, tipo)`: un tipo
#: desconocido mostraría `aceite_diferencial` con guion bajo a quien decide si
#: manda un camión al taller (regla 5).
NOMBRE_TAREA = {
    'distribucion':        'correa o cadena de distribución',
    'aceite_motor':        'aceite de motor',
    'aceite_caja':         'aceite de caja',
    'aceite_diferencial':  'aceite de diferencial',
    'refrigerante':        'refrigerante',
    'lubricacion_cadena':  'lubricación de cadena',
}

#: De dónde puede salir el intervalo. **Es literalmente el vocabulario de
#: `flota_ficha_tecnica.distribucion_fuente`**, importado de `valores.py` y no
#: copiado: la procedencia que la tarea hereda tiene que poder decir
#: exactamente lo mismo que la ficha de la que salió. Dos tuplas con el mismo
#: contenido se separan el día que alguien agregue un valor a una sola, y ese
#: día el modelo acepta lo que la base rechaza.
#:
#: Se reexporta acá para que quien lea este módulo vea contra qué valida sin
#: tener que abrir otro archivo.

#: Las que **no son documentales**. No están prohibidas —un intervalo estimado
#: es mejor que ninguno— pero quien vea el número tiene que ver también que
#: nadie lo leyó de un manual.
#:
#: `taller` entra acá y no en las firmes: el mecánico de la esquina de Pitalito
#: sabe, y aun así lo que dijo es un recuerdo, no una página. La distinción no
#: es de calidad de la persona, es de si existe algo que se pueda volver a leer.
FUENTES_BLANDAS = ('taller', 'estimado', 'sin_dato')


class EstadoTarea:
    """Los cinco estados, y **ninguno es un booleano** (regla 4).

    `al_dia` y `vencida` son las dos que todo el mundo espera. Las otras tres
    son las que hacen que el tablero no mienta:

        sin_intervalo    — la ficha dice QUÉ lleva el vehículo pero no CADA
                           CUÁNTOS KM. No hay contra qué comparar el odómetro.
                           Se arregla en un escritorio: una llamada al
                           concesionario y `PUT` sobre la tarea.
        sin_linea_base   — sí se sabe cada cuántos km, y **nunca se registró
                           una ejecución**. No hay desde dónde contar. No está
                           al día ni vencida, y el aviso no sale: no hay contra
                           qué comparar. Se arregla registrando la última vez
                           que se hizo, o haciéndolo.
        por_vencer       — al día todavía, y al ritmo medido de este vehículo
                           llega al punto de cambio dentro de la ventana de
                           anticipación. Es la única que depende de un umbral.

    `sin_intervalo` GANA sobre `sin_linea_base` cuando faltan los dos, y el
    orden no es arbitrario: sin intervalo, registrar la ejecución no destraba
    nada —seguirían sin poder compararse—, mientras que con el intervalo
    puesto la tarea ya empieza a decir algo el día que alguien la ejecute.
    Se reporta lo que hay que hacer PRIMERO.
    """

    SIN_INTERVALO  = 'sin_intervalo'
    SIN_LINEA_BASE = 'sin_linea_base'
    AL_DIA         = 'al_dia'
    POR_VENCER     = 'por_vencer'
    VENCIDA        = 'vencida'


ESTADOS_TAREA = (EstadoTarea.SIN_INTERVALO, EstadoTarea.SIN_LINEA_BASE,
                 EstadoTarea.AL_DIA, EstadoTarea.POR_VENCER,
                 EstadoTarea.VENCIDA)

#: Los dos que piden acción sobre el vehículo. Separados de los otros tres
#: porque **los otros tres piden acción sobre el DATO**, y eso lo hace otra
#: persona en otro lugar. Un solo total mezclaría «hay que llevar el camión al
#: taller» con «hay que llamar al concesionario», y el tablero dejaría de decir
#: a quién llamar.
ESTADOS_QUE_PIDEN_TALLER = (EstadoTarea.VENCIDA, EstadoTarea.POR_VENCER)


class PlanInvalido(ValueError):
    """La pregunta que se le hizo al plan no se puede contestar."""


# ── EL umbral, y es uno solo ─────────────────────────────────────────────────

#: Cuántos días antes de llegar al punto de cambio una tarea pasa a
#: `por_vencer`.
#:
#: **ESTO ES UN UMBRAL Y ES EL ÚNICO DE ESTA FASE.** Se declara acá, con nombre
#: propio y en un solo lugar, por lo mismo que `FACTOR_SALTO_SOSPECHOSO`: un
#: número que decide se esconde adentro de un `if` y nadie lo vuelve a discutir.
#:
#: ## Por qué en DÍAS y no en kilómetros
#:
#: «Avisar 2.000 km antes» exigiría un número distinto por tarea —2.000 km es
#: un mes de un NHR y medio año de un motocarro— y ninguno de esos números
#: existe medido. Los días, en cambio, son lo que de verdad limita: **lo que
#: tarda conseguir el repuesto y un turno de taller en Neiva**. Es el mismo
#: razonamiento con el que `DIAS_AVISO_DOCUMENTO = 15` se fijó en la cita de
#: tecnomecánica, y por eso el número coincide sin ser el mismo objeto: son dos
#: plazos de dos trámites distintos y cambiar uno no debe cambiar el otro.
#:
#: ## Qué NO afirma este número
#:
#: No afirma que a los 14 días haya que correr y a los 16 no. Afirma que la
#: conversión de «te faltan 500 km» a «unos 6 días» se hace con el ritmo MEDIDO
#: de ese vehículo, y que dentro de esta ventana la tarea aparece en el tablero
#: en vez de aparecer el día que ya se pasó.
#:
#: ## La condición de disparo para cambiarlo (regla 13)
#:
#: Hoy **no hay una sola medición de km/día en producción**: `km_por_dia`
#: devolvía `SIN_DATO` para los seis vehículos el 2026-09-02, porque ninguno
#: tiene dos lecturas vigentes con confianza suficiente. Por eso el health
#: publica `km_dia_por_vehiculo` como HECHO, con `n` y `dias` al lado.
#:
#: Se revisa cuando **los seis vehículos tengan un km/día publicable durante un
#: mes**. Con eso se puede contestar la pregunta que hoy no se puede: cuántos
#: kilómetros son quince días en cada camión, y si la anticipación alcanza para
#: pedir una correa a Neiva. Antes de eso, mover este número sería cambiar un
#: número a ojo por otro número a ojo.
DIAS_AVISO_PREVENTIVO = 15


# ── La tarea, y su diagnóstico ───────────────────────────────────────────────

@dataclass(frozen=True)
class Tarea:
    """Lo mínimo que hace falta para pronunciarse sobre una tarea del plan.

    `intervalo_km = None` significa **«la ficha no dice cada cuántos km»**, y
    `ultima_ejecucion_km = None` significa **«nunca se registró una
    ejecución»**. Ninguno de los dos significa «no sé» en el sentido de la
    regla 4: los dos salen de mirar la base entera y son ausencias
    comprobadas, igual que el `desde=None` de
    `vigentes_tras_la_ultima_correccion`.

    Lo que la regla 4 prohíbe es que esas ausencias salgan de acá como `None` o
    como un booleano hacia quien decide: por eso `diagnosticar` las traduce a
    `sin_intervalo` y `sin_linea_base`, que son palabras, antes de que nadie
    las vea.

    `fuente` es obligatoria y **no tiene default**. Un intervalo sin
    procedencia se lee como si alguien lo hubiera verificado; ponerle
    `'sin_dato'` por omisión haría que el caso más común —el que hay que
    corregir— fuera el invisible.
    """

    tipo: str
    intervalo_km: Optional[int]
    fuente: str
    ultima_ejecucion_km: Optional[int] = None


@dataclass(frozen=True)
class Diagnostico:
    """Qué le pasa a una tarea, **con los insumos que lo producen al lado**.

    Un `estado` suelto no se puede auditar ni explicar: quien vea `vencida`
    tiene que poder ver a qué kilometraje tocaba, cuánto marca el odómetro y de
    dónde salió el intervalo. Es el mismo criterio con el que el CPK viaja con
    `pesos` y `km`.

    `fuente` y `fuente_blanda` viajan SIEMPRE, incluso cuando el estado es
    `sin_intervalo` y no hay número que respaldar. Que la procedencia sea un
    campo del resultado y no un dato que el consumidor tenga que ir a buscar es
    lo que impide que una pantalla la deje de pintar sin que nadie lo note.
    """

    estado: str
    tipo: str
    fuente: str
    fuente_blanda: bool
    intervalo_km: Union[int, str]
    proximo_km: Union[int, str]
    km_restante: Union[int, str]
    dias_estimados: Union[int, str]
    marca_ritmo: Union[str, object]


def validar_tipo(tipo: str) -> str:
    """El tipo, o levanta. Sin `.get`, sin normalizar, sin adivinar.

    Un tipo desconocido que pasara silenciosamente produciría una fila de plan
    que nadie siembra, nadie diagnostica y nadie ve — y el vehículo se quedaría
    sin la tarea que sí necesita.
    """
    if tipo not in TIPOS_TAREA:
        raise PlanInvalido(
            f'tipo de tarea desconocido: {tipo!r}. Los que una ficha puede '
            f'sembrar son: {", ".join(TIPOS_TAREA)}.')
    return tipo


def validar_fuente(fuente: str) -> str:
    """La procedencia, o levanta.

    Es el guard que hace cierta la decisión 2 del plan: si una fuente
    desconocida se aceptara, el camino barato para saltarse la procedencia
    sería escribir cualquier cosa, y `fuente_blanda` clasificaría como firme
    algo que nadie sabe qué es.
    """
    if fuente not in FUENTES:
        raise PlanInvalido(
            f'fuente desconocida: {fuente!r}. Las de la ficha técnica son: '
            f'{", ".join(FUENTES)}.')
    return fuente


def fuente_es_blanda(fuente: str) -> bool:
    """Si el intervalo salió del juicio de alguien y no de un documento.

    QUÉ AFIRMA: que no hay una página que se pueda volver a leer.

    QUÉ NO AFIRMA: que el número esté mal. Un intervalo dicho por el taller
    suele ser el correcto; lo que no tiene es con qué demostrarlo, y eso es
    exactamente lo que hay que poder ver al lado del número.
    """
    return validar_fuente(fuente) in FUENTES_BLANDAS


def nombre_tarea(tipo: str) -> str:
    """Cómo se nombra la tarea para una persona."""
    return NOMBRE_TAREA[validar_tipo(tipo)]


def dias_hasta(km_restante: int, ritmo: RitmoDeUso) -> Union[int, str]:
    """«Te faltan 500 km» → «unos 6 días», con el ritmo medido del vehículo.

    QUÉ AFIRMA: cuántos días tardaría este vehículo en recorrer `km_restante`
    **si siguiera rodando como venía rodando**. Es una proyección y por eso el
    resto del sistema la nombra «unos N días» y nunca «vence el día X».

    QUÉ NO AFIRMA: nada sobre la confianza del número. Esa viaja aparte, en
    `ritmo.marca`, y quien lo pinte tiene que pintarla — un «faltan 6 días»
    calculado sobre dos lecturas dudosas se lee igual de firme que uno bueno,
    y ése es justo el modo de falla que `confianza` existe para cerrar.

    Devuelve `SIN_DATO` —la palabra, nunca `None` ni un número grande— en dos
    casos que se ven distintos y se corrigen distinto:

    · **el ritmo no se pudo medir** (`ritmo.km_dia` es `SIN_DATO`): el motivo
      está en `ritmo.motivo` y va desde «hay una sola lectura» hasta «los dos
      extremos son dudosos».
    · **el vehículo está quieto** (`km_dia == 0`, un cero medido): a ritmo cero
      no llega nunca, y «nunca» dividido no da un número de días. Devolver un
      entero enorme lo ordenaría al final de la lista como si fuera lo menos
      urgente, que es una afirmación que nadie midió.

    Se redondea hacia ARRIBA. Con 0,4 días restantes la respuesta honesta es
    «un día», no «cero días»: cero se lee como «ya», y ya no es.
    """
    if ritmo.km_dia is SIN_DATO or not isinstance(ritmo.km_dia, Decimal):
        return SIN_DATO
    if ritmo.km_dia <= 0:
        return SIN_DATO
    return int(math.ceil(Decimal(km_restante) / ritmo.km_dia))


def diagnosticar(tarea: Tarea, odometro_actual: Union[int, str],
                 ritmo: RitmoDeUso) -> Diagnostico:
    """Qué le pasa a esta tarea hoy. **La única función que emite el estado.**

    QUÉ AFIRMA: el estado de la tarea contra el kilometraje que el odómetro
    dice hoy, con el intervalo que la ficha declara y la última ejecución que
    alguien registró.

    QUÉ NO AFIRMA:

    · **Que el vehículo esté sano.** `al_dia` significa «todavía no llegó al
      kilometraje del cambio», no «se revisó y está bien». Una correa puede
      reventar a los 40.000 de un intervalo de 60.000.
    · **Que la tarea aplique.** Que una ficha declare `aceite_caja_spec` no
      dice que ese vehículo tenga caja que mantener; dice que alguien escribió
      una especificación. Retirar una tarea que no aplica es una decisión de
      persona y se hace desactivándola, no borrándola.
    · **Que el intervalo sea correcto.** Se hereda de la ficha con su
      procedencia y se publica con ella; este módulo no juzga si 60.000 km es
      el número del fabricante.

    ## El orden de las preguntas, que es la política entera

    1. **¿Hay intervalo?** Sin él no hay contra qué comparar el odómetro. Gana
       sobre todo lo demás porque es lo que se arregla primero (ver
       `EstadoTarea`).
    2. **¿Hay línea base?** Sin una ejecución registrada no hay desde dónde
       contar. **No está al día ni vencida**, y el aviso no sale: no hay contra
       qué comparar. Es la decisión que hace que un preventivo recién sembrado
       no dispare cuarenta avisos el día uno.
    3. **¿Ya se pasó?** `km_restante <= 0` es `vencida`, y no necesita ningún
       umbral: lo dice el fabricante, no nosotros.
    4. **¿Llega pronto?** Sólo acá interviene `DIAS_AVISO_PREVENTIVO`, y sólo
       si el ritmo se pudo medir. **Sin ritmo NO se degrada a `por_vencer` ni
       se degrada a alarma**: se queda en `al_dia`, que es lo que el odómetro
       afirma, con `dias_estimados = sin_dato` al lado para que se vea que el
       «cuándo» no se sabe.

    Levanta `PlanInvalido` si hay línea base y el odómetro es `SIN_DATO`. No es
    una defensa decorativa: una ejecución cuelga de una lectura por regla 3 y
    con FK NOT NULL, así que un vehículo con ejecución y sin lecturas es una
    base rota. Devolver un estado ahí lo taparía; devolver `sin_dato` lo haría
    indistinguible de una tarea sin línea base, que es un problema muy distinto.
    """
    tipo = validar_tipo(tarea.tipo)
    fuente = validar_fuente(tarea.fuente)
    blanda = fuente in FUENTES_BLANDAS

    def _sin(estado, **campos):
        base = dict(estado=estado, tipo=tipo, fuente=fuente,
                    fuente_blanda=blanda, intervalo_km=SIN_DATO,
                    proximo_km=SIN_DATO, km_restante=SIN_DATO,
                    dias_estimados=SIN_DATO, marca_ritmo=ritmo.marca)
        base.update(campos)
        return Diagnostico(**base)

    # 1 · Sin intervalo no hay contra qué comparar. Gana sobre la línea base.
    if tarea.intervalo_km is None:
        return _sin(EstadoTarea.SIN_INTERVALO)

    if tarea.intervalo_km <= 0:
        raise PlanInvalido(
            f'intervalo de {tarea.intervalo_km} km en la tarea {tipo!r}: un '
            f'intervalo de cero o negativo dejaría la tarea vencida para '
            f'siempre, cada día, sobre cualquier odómetro.')

    # 2 · Sin línea base no hay desde dónde contar. **El aviso no sale.**
    if tarea.ultima_ejecucion_km is None:
        return _sin(EstadoTarea.SIN_LINEA_BASE,
                    intervalo_km=tarea.intervalo_km)

    if odometro_actual is SIN_DATO or not isinstance(odometro_actual, int):
        raise PlanInvalido(
            f'la tarea {tipo!r} tiene una ejecución registrada y el vehículo '
            f'no tiene kilometraje: una ejecución cuelga de una lectura '
            f'(regla 3, FK NOT NULL), así que esto no puede pasar sin que la '
            f'base esté rota. No se devuelve un estado para no taparlo.')

    proximo = tarea.ultima_ejecucion_km + tarea.intervalo_km
    restante = proximo - odometro_actual

    comun = dict(intervalo_km=tarea.intervalo_km, proximo_km=proximo,
                 km_restante=restante)

    # 3 · Ya se pasó. Ningún umbral: el kilometraje lo dijo el fabricante.
    if restante <= 0:
        return _sin(EstadoTarea.VENCIDA, dias_estimados=SIN_DATO, **comun)

    dias = dias_hasta(restante, ritmo)

    # 4 · El único punto donde interviene un umbral, y sólo con ritmo medido.
    if dias is not SIN_DATO and dias <= DIAS_AVISO_PREVENTIVO:
        return _sin(EstadoTarea.POR_VENCER, dias_estimados=dias, **comun)

    return _sin(EstadoTarea.AL_DIA, dias_estimados=dias, **comun)


__all__ = [
    'TIPOS_TAREA', 'NOMBRE_TAREA', 'FUENTES', 'FUENTES_BLANDAS',
    'EstadoTarea', 'ESTADOS_TAREA', 'ESTADOS_QUE_PIDEN_TALLER',
    'DIAS_AVISO_PREVENTIVO', 'PlanInvalido', 'Tarea', 'Diagnostico',
    'validar_tipo', 'validar_fuente', 'fuente_es_blanda', 'nombre_tarea',
    'dias_hasta', 'diagnosticar', 'SIN_DATO',
]
