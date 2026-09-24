"""
Políticas de custodia. Invariantes 2 (cardinalidad), 3 (cobertura temporal)
y 4 (arco exclusivo).

La base impone 2 y 4 por índice único parcial y CHECK; el 3 solo
parcialmente — ver la nota de `flota/adaptadores/modelos.py`.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

from flota.dominio.errores import CustodiaInvalida
from flota.dominio.valores import (
    Custodia,
    CustodioEstado,
    CustodioTipo,
    QuienPide,
)


@dataclass(frozen=True)
class Veredicto:
    """Si se puede recibir el vehículo, y qué hace falta.

    `mensaje` no es un detalle de presentación: es lo que convierte una
    restricción de base de datos en una conversación entre dos personas. Un 409
    crudo deja al conductor mirando el celular en el patio; "el WHX245 lo tiene
    Víctor desde ayer" le dice a quién llamar.

    Por eso vive en el dominio y no en la pantalla — un mensaje en la vista se
    pierde en el próximo rediseño.
    """

    puede: bool
    requiere_forzado: bool
    mensaje: str
    #: Si las fotos de cierre NO eximen del forzado. `False` para el admin de
    #: zona (un cierre con las fotos del turno es un cierre normal) y `True`
    #: para control de flota: es el rol al que se mide por los cierres
    #: forzados, y un atajo que no los cuente sería el botón que baja su propio
    #: contador (regla 11).
    fotos_no_eximen: bool = False


@dataclass(frozen=True)
class Hueco:
    """Tramo en el que un vehículo activo no tuvo custodio. No debería existir."""

    desde: datetime
    hasta: datetime


def puede_recibir(custodia_vigente, quien_pide: QuienPide,
                  nombre_custodio_actual: str = '', placa: str = '',
                  desde: str = '', es_el_custodio_actual: bool = False) -> Veredicto:
    """Si quien pide puede abrir custodia sobre este vehículo.

    QUÉ AFIRMA: si la operación está permitida y si necesita un forzado
    declarado.

    QUÉ NO AFIRMA: que sea buena idea. Un admin puede forzar; que lo haga ocho
    veces en un mes significa que los conductores no están cerrando turno, y eso
    lo dice el contador del health, no esta función.

    Seis casos:

    · **Quien pide YA es el custodio** — se puede, siempre y sin forzado. Está
      cerrando su propio turno; a quién se lo entregue es otra decisión.

    · **Sin custodia vigente** — se puede, sin más.
    · **Vigente del mismo conductor** — se puede: recibir lo que ya se tiene es
      un no-op, no un conflicto.
    · **Vigente de una SEDE** — se puede, sin forzado, para cualquiera que
      pida. Una sede no es una persona que deba "firmar" el cierre: si el
      vehículo está ahí es porque alguien YA lo entregó bien (con sus fotos
      de cierre) — exigir forzado acá le pone la fricción del caso anómalo al
      caso normal de todos los días (recibirlo en el patio a las 5 a.m.).
    · **Vigente de OTRO conductor** — depende de quién pide. El conductor
      **no puede**: la conversación es entre él y quien tiene el vehículo. El
      admin de zona **sí**, y queda marcado como forzado — es la única salida
      cuando alguien se fue sin cerrar y el camión tiene que salir.
    · **Vigente de OTRO conductor, y pide control de flota** — sí, como el
      admin de zona, pero siempre con motivo y siempre marcado aunque traiga
      fotos (`fotos_no_eximen`). Decidido el 2026-09-24: el patio lo opera él.

    El mensaje del rechazo dice CÓMO se destraba, no solo que está trabado. El
    2026-08-05 decía «tiene que cerrar su turno primero» y Yesid preguntó «¿cómo
    se hace?»; después apretó el único botón que decía *Cerrar* —el del modal— y
    perdió todo lo que había cargado. Un mensaje que nombra un gesto sin decir
    dónde está manda a la gente a buscar la palabra en la pantalla.
    """
    if custodia_vigente is None:
        return Veredicto(True, False, '')

    # CERRAR EL TURNO PROPIO SIEMPRE SE PUEDE.
    #
    # Esta rama faltaba y bloqueaba el gesto más común del día. El adaptador
    # decidía por comparación de custodios —`vigente.custodio_conductor_id ==
    # custodio_conductor_id`—, y al ENTREGAR a una sede el custodio entrante no
    # es un conductor: la comparación daba falso y caía en el rechazo de abajo.
    #
    # Resultado, el 2026-08-05: Víctor no podía entregar el UPQ606 que él mismo
    # tenía, y el mensaje que recibía hablaba de RECIBIRLO — «si lo vas a
    # recibir vos, Víctor tiene que cerrar su turno primero», dicho al propio
    # Víctor. El guard que existe para que nadie le quite el vehículo a otro
    # estaba impidiendo que su dueño lo soltara.
    #
    # La pregunta correcta no es «¿a quién va?» sino «¿quién lo tiene ahora?».
    if es_el_custodio_actual:
        return Veredicto(True, False, '')

    # UNA SEDE NO TIENE TURNO QUE CERRAR.
    #
    # El caso más común del módulo —el vehículo durmió en el patio, el
    # conductor lo recibe a las 5 a.m.— quedaba tratado igual que "otro
    # conductor no cerró": bloqueado para cualquier conductor, exigiendo un
    # admin de zona con motivo escrito cada mañana. Pero la sede no es un
    # custodio que se fue sin firmar: es el estado en el que un traspaso bien
    # hecho DEJA el vehículo (`flotaCondAbrirEntrega` → "En la sede"), con
    # las fotos de cierre de quien lo entregó ya guardadas en esa custodia
    # anterior. No hay nada que un forzado esté protegiendo acá.
    if custodia_vigente.custodio_tipo == CustodioTipo.SEDE.value:
        return Veredicto(True, False, '')

    if quien_pide == QuienPide.CONTROL_FLOTA:
        return Veredicto(
            True, True,
            f'{nombre_custodio_actual or "El custodio anterior"} no cerró su '
            f'turno. Lo cerrás vos a la fuerza: pide motivo escrito siempre, '
            f'queda registrado con tu nombre y cuenta en «cierres forzados». '
            f'Avisale hoy a quien lo tenía.',
            fotos_no_eximen=True,
        )

    if quien_pide == QuienPide.CONDUCTOR:
        quien = nombre_custodio_actual or 'otro conductor'
        cuando = f' desde {desde}' if desde else ''
        return Veredicto(
            False, False,
            f'El {placa or "vehículo"} lo tiene {quien}{cuando}. '
            f'Para recibirlo vos, {quien} tiene que entrar con SU usuario y '
            f'apretar «Entregar turno». Si no está disponible, un admin de zona '
            f'puede forzar el cierre y queda registrado quién lo autorizó.'
        )

    return Veredicto(
        True, True,
        f'{nombre_custodio_actual or "El custodio anterior"} no cerró su turno. '
        f'Al recibirlo se cierra a la fuerza: queda registrado quién lo autorizó '
        f'y por qué, y sin fotos de cierre no hay con qué comparar el estado del '
        f'vehículo en el turno siguiente.'
    )


def custodio_que_puede_nombrar(quien_pide: QuienPide, *,
                               custodio_tipo: CustodioTipo,
                               custodio_conductor_id,
                               conductor_del_que_pide_id,
                               es_el_custodio_actual: bool) -> Veredicto:
    """A nombre de quién puede dejar la custodia quien pide.

    QUÉ AFIRMA: si quien pide puede nombrar a ESE custodio entrante.

    QUÉ NO AFIRMA: que pueda cerrar el turno vigente — eso lo dice
    `puede_recibir`. Son dos preguntas: «¿de quién lo sacás?» y «¿a quién se lo
    das?». Hasta el 2026-09-24 solo existía la primera, y la segunda la
    contestaba el cuerpo del request: un conductor mandaba
    `custodio_conductor_id` de un compañero sobre un camión que dormía en la
    sede —`puede_recibir` lo deja pasar, una sede no firma—, y el camión quedaba
    a nombre de otro, que respondía por lo que no recibió.

    · **Gestión y control de flota** — a nombre de cualquiera. Es el recibo de
      escritorio: el encargado asigna el camión a quien lo va a manejar.
    · **Conductor** — el custodio entrante solo puede ser **él mismo**. Dejarlo
      en la sede, solo si lo está entregando él (es el custodio actual): un
      conductor no mueve a la sede un camión que no tiene.
    """
    if quien_pide != QuienPide.CONDUCTOR:
        return Veredicto(True, False, '')

    if custodio_tipo == CustodioTipo.CONDUCTOR:
        if conductor_del_que_pide_id is None:
            return Veredicto(False, False,
                             'Tu usuario no está vinculado a un conductor: sin '
                             'eso no se puede dejar un turno a tu nombre. '
                             'Pedile a administración que te vincule la ficha.')
        if custodio_conductor_id != conductor_del_que_pide_id:
            return Veredicto(False, False,
                             'Solo podés recibir el turno a tu nombre. Si lo va '
                             'a manejar otro conductor, que lo reciba él con su '
                             'usuario, o que el encargado de flota lo asigne '
                             'desde el escritorio.')
        return Veredicto(True, False, '')

    if es_el_custodio_actual:
        return Veredicto(True, False, '')
    return Veredicto(False, False,
                     'Solo podés dejar en la sede el vehículo de tu turno. '
                     'Este no lo tenés vos.')


def custodias_activas(custodias: Sequence[Custodia]) -> List[Custodia]:
    """Las custodias sin `fin_ts` de un mismo vehículo.

    QUÉ AFIRMA: cuáles tramos están abiertos ahora mismo según los datos dados.

    QUÉ NO AFIRMA: que sea legal que haya más de una. Eso lo dice
    `validar_cardinalidad`. Esta función cuenta; la otra juzga.
    """
    return [c for c in custodias if c.fin_ts is None]


def validar_cardinalidad(custodias: Sequence[Custodia]) -> None:
    """Un vehículo tiene exactamente 0 o 1 custodia activa. Nunca dos.

    Dos custodias abiertas a la vez significan que dos personas responden por el
    mismo camión, que en la práctica es que ninguna responde. Es el estado que
    el `UNIQUE` parcial `(vehiculo_id) WHERE fin_ts IS NULL` impide en la base;
    esta función es la misma política del lado del dominio, para que el traspaso
    no dependa de que la base lo atrape.

    Levanta `CustodiaInvalida`.
    """
    activas = custodias_activas(custodias)
    if len(activas) > 1:
        raise CustodiaInvalida(
            f'{len(activas)} custodias activas a la vez para el mismo vehículo. '
            f'Dos responsables del mismo camión es, en la práctica, ninguno.'
        )


def validar_un_vehiculo_por_conductor(otras_custodias_abiertas: Sequence[tuple]) -> None:
    """Un conductor no puede tener más de un vehículo bajo custodia a la vez.

    `otras_custodias_abiertas` es `[(placa, "dd/mm a las HH:MM"), ...]` —
    las custodias abiertas de ESTE conductor sobre vehículos DISTINTOS al que
    está por recibir. Vacía = puede recibir.

    QUÉ AFIRMA: que recibir un vehículo nuevo exige haber entregado los demás
    primero.

    QUÉ NO AFIRMA: cuál de los vehículos es el que el conductor tiene
    realmente en el patio — eso lo decide una persona, no esta función.

    Motivo: `validar_cardinalidad` impone 0-o-1 custodia activa **por
    vehículo**, pero nada imponía 0-o-1 **por conductor** — cada apertura
    solo mira su propio vehículo. El 2026-08-13 esto dejó a un conductor con
    tres custodias abiertas a la vez (recibió TGZ653, después TGZ655, después
    UPQ606, sin entregar ninguna) y tumbó `/flota/conductor/mi-turno`
    (`Custodia.query...one_or_none()` con tres filas — `MultipleResultsFound`)
    — su pantalla quedó en blanco, sin poder ver ni recibir ningún vehículo.
    Un conductor manejando dos camiones a la vez tampoco tiene sentido en la
    operación real: si algo le pasa a uno, no queda claro cuál estaba
    conduciendo.

    Levanta `CustodiaInvalida`.
    """
    if not otras_custodias_abiertas:
        return
    detalle = '; '.join(f'{placa} desde {desde}' for placa, desde in otras_custodias_abiertas)
    plural = len(otras_custodias_abiertas) > 1
    raise CustodiaInvalida(
        f'Ya tenés {"otros vehículos" if plural else "otro vehículo"} bajo '
        f'custodia sin entregar: {detalle}. Entregalo primero (botón «Entregar '
        f'turno») antes de recibir uno nuevo — no podés responder por dos '
        f'camiones a la vez.'
    )


def huecos_de_cobertura(custodias: Sequence[Custodia], ahora: datetime) -> List[Hueco]:
    """Tramos sin custodio entre la primera custodia y `ahora`.

    QUÉ AFIRMA: que entre la apertura de la primera custodia y el instante dado
    hubo momentos sin nadie responsable, y cuáles.

    QUÉ NO AFIRMA: nada sobre el período anterior a la primera custodia. Antes
    del arranque en frío el sistema no sabe y no pretende saber — por eso la
    primera custodia de cada vehículo se marca `linea_base` y sus daños nacen
    preexistentes, sin responsable.

    Lista vacía = cobertura completa. El traspaso es atómico justamente para que
    esta lista no pueda crecer: cierra la anterior y abre la nueva en una sola
    transacción, sin instante intermedio.
    """
    if not custodias:
        return []

    ordenadas = sorted(custodias, key=lambda c: c.inicio_ts)
    huecos = []
    # El reloj arranca en la primera custodia, nunca antes: sobre el período
    # previo al arranque en frío el sistema no sabe y no pretende saber.
    cursor = ordenadas[0].inicio_ts

    for c in ordenadas:
        if c.inicio_ts > cursor:
            huecos.append(Hueco(desde=cursor, hasta=c.inicio_ts))
        fin = ahora if c.fin_ts is None else c.fin_ts
        if fin > cursor:
            cursor = fin

    if cursor < ahora:
        huecos.append(Hueco(desde=cursor, hasta=ahora))
    return huecos


def validar_arco_exclusivo(custodia: Custodia) -> None:
    """Exactamente un `custodio_*_id` no nulo, y corresponde a `custodio_tipo`.

    Un `custodio_tipo = conductor` con `custodio_sede_id` lleno —o con los dos
    llenos, o con ninguno— es un registro que no dice de quién es la
    responsabilidad. En un acta eso no vale nada.

    Levanta `CustodiaInvalida`.
    """
    if custodia.custodio_estado == CustodioEstado.PENDIENTE_SEDE:
        # No afloja el invariante: lo hace condicional a un estado que a su vez
        # está constreñido. Una custodia `pendiente_sede` tiene que ser de tipo
        # sede y no puede traer NINGÚN custodio puesto — si trajera uno, sería
        # una sede resuelta que finge no serlo.
        if custodia.custodio_tipo != CustodioTipo.SEDE:
            raise CustodiaInvalida(
                'pendiente_sede solo aplica a custodia de sede: un conductor '
                'siempre tiene fila, y es la cédula lo que hace válida el acta.'
            )
        if (custodia.custodio_conductor_id is not None
                or custodia.custodio_sede_id is not None):
            raise CustodiaInvalida(
                'pendiente_sede con un custodio puesto: o la sede se pudo '
                'representar, o no. No las dos cosas.'
            )
        return

    llenos = [
        custodia.custodio_conductor_id is not None,
        custodia.custodio_sede_id is not None,
    ]
    if sum(llenos) != 1:
        raise CustodiaInvalida(
            'una custodia lleva exactamente un custodio: '
            f'conductor={custodia.custodio_conductor_id}, sede={custodia.custodio_sede_id}. '
            'Un registro con los dos, o con ninguno, no dice de quién es la responsabilidad.'
        )

    esperado = ('custodio_conductor_id' if custodia.custodio_tipo == CustodioTipo.CONDUCTOR
                else 'custodio_sede_id')
    if getattr(custodia, esperado) is None:
        raise CustodiaInvalida(
            f'custodio_tipo={custodia.custodio_tipo.value} exige {esperado}, '
            f'y el que viene lleno es el otro.'
        )


__all__ = [
    'Hueco',
    'Veredicto',
    'puede_recibir',
    'custodio_que_puede_nombrar',
    'custodias_activas',
    'validar_cardinalidad',
    'validar_un_vehiculo_por_conductor',
    'huecos_de_cobertura',
    'validar_arco_exclusivo',
]
