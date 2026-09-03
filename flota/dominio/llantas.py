"""
Llantas — vocabulario y cálculo. Sin I/O, sin framework.

La llanta es una **entidad con identidad propia**, no un renglón de factura de
repuesto. Rota de posición, pasa a otro camión, va a reencauche y vuelve: como
línea de una compra, *«¿cuánto duran?»* no se puede ni formular. Y el modo de
fallo caro no es que se gasten —eso pasa siempre— sino que se gasten **mal**:
desalineación, presión, un eje que come el flanco interno. Eso solo se ve
**por posición**, y por eso todo lo que se agrega acá se agrega por posición.

## `km_acumulado` NO se guarda, se calcula — y este módulo es dónde

Con 24 llantas, una columna `km_acumulado` es garantía de divergencia: el día
que alguien corrija un kilometraje de montaje, la columna queda apuntando al
número viejo y nadie se entera. Es la misma decisión que `flota_tanqueo` toma
con `precio_galon` y por el mismo motivo (regla 0 del WMS).

## Los tres estados de un tramo, y ninguno es cero

| Estado | Qué significa | Qué NO significa |
|---|---|---|
| un número | la llanta se montó y se desmontó, y la resta de odómetros es esa | — |
| `VIGENTE` | está montada **ahora**: su vida todavía no terminó | «0 km» ni «no se sabe» |
| `SIN_DATO` | los dos extremos del tramo son kilometrajes en duda | «0 km» ni «duró poco» |

`VIGENTE` es el tercer estado de la regla 4 del módulo, escrito con una palabra
y no con `None` ni con cero. Una llanta sin desmontar **no tiene 0 km
recorridos**: tiene un tramo abierto. Devolver 0 la haría aparecer como la
llanta que menos dura del parque, que es exactamente al revés.

## La confianza viaja, no se reimplementa

El kilometraje de una llanta es una resta entre dos lecturas de odómetro, y esas
lecturas tienen `confianza`. La marca del tramo la decide
`flota.dominio.odometro.confianza_del_tramo` — **una política, una función**
(regla 0). Escribir acá un segundo criterio sería la copia que diverge, y sería
la que decide si un número se publica.

## Regla 13 — ningún umbral inventado

**No hay una sola llanta medida.** Este módulo no dice a cuántos kilómetros se
cambia una llanta, ni marca ninguna como «gastada»: publica el hecho medido y
declara qué haría falta para poder fijar esa vida útil
(`MONTAJES_CERRADOS_PARA_VIDA_UTIL`). Un umbral escrito hoy sería a ojo, y un
detector que dispara sobre operación sana se apaga en una semana.
"""
from typing import List, Optional, Sequence, Tuple, Union

from flota.dominio.odometro import confianza_del_tramo
from flota.dominio.valores import (MAX_POSICIONES_LLANTA, SIN_DATO, Confianza,
                                   Lectura)


# ── El tercer estado (regla 4) ───────────────────────────────────────────────
#
# Mismas tres propiedades que `SIN_DATO`, elegidas por los mismos motivos:
#   1. es VERDADERO en contexto booleano — un `vigente` falsy invita a
#      `valor or 0`, que es el default optimista que la regla 1 prohíbe;
#   2. serializa a `"vigente"` en JSON sin conversión;
#   3. nunca es igual a 0, ni a '', ni a None.
#
# Y **no es `SIN_DATO`**: «todavía está rodando» y «no sé cuánto rodó» son dos
# cosas distintas, y la primera es información. Colapsarlas dejaría la pantalla
# sin poder decir la única frase útil sobre una llanta montada.
class _Vigente(str):
    __slots__ = ()

    def __repr__(self) -> str:                # pragma: no cover — solo depuración
        return 'VIGENTE'


VIGENTE = _Vigente('vigente')


#: Por qué salió la llanta. **Es el eje del análisis, no un adorno.**
#:
#: El plan lo dice: el modo de fallo caro no es que se gasten, es que se gasten
#: MAL. `desgaste_irregular` es la palabra que hace visible una desalineación o
#: una presión mal llevada; sin ella, esa llanta y una que cumplió su vida
#: quedan indistinguibles en el histórico y el eje que come flancos no aparece
#: nunca.
#:
#: `rotacion` existe porque una llanta que sale para cambiar de posición **no
#: falló**: contarla como desmontaje por desgaste hundiría la vida útil medida
#: de esa posición justamente en los vehículos mejor mantenidos.
#:
#: `sin_dato` es una respuesta legítima (regla 1) y **no es el default**: hay que
#: escribirla. Sin ella, quien no sabe por qué salió la llanta elige la opción
#: cómoda, y la opción cómoda es siempre `desgaste_normal`.
MOTIVOS_DESMONTAJE = (
    'desgaste_normal',
    'desgaste_irregular',
    'pinchazo',
    'corte_flanco',
    'reencauche',
    'rotacion',
    'sin_dato',
)

#: Motivos que hablan de la llanta y no del eje. Se declara acá y no dentro de
#: una consulta por el motivo de siempre: la copia que vive en un `SELECT` es la
#: que nadie encuentra cuando la regla cambia.
MOTIVOS_DE_FALLA_DE_MONTAJE = ('desgaste_irregular', 'corte_flanco')


#: Cuántos montajes CERRADOS de la misma posición hacen falta para publicar una
#: vida útil de esa posición.
#:
#: **No es un umbral sobre la operación**: no marca ninguna llanta, no dispara
#: ningún aviso y no bloquea nada. Es la condición de disparo de la regla 13 —
#: cuándo se puede empezar a decir «esta posición rinde N km» sin inventarlo.
#: Hasta entonces el hecho se publica igual (cuántos hay y cuáles son) y la
#: mediana sale `SIN_DATO`.
#:
#: Por qué 6 y no 2: con dos vidas medidas, la mediana es el promedio de dos
#: números y una sola llanta pinchada a los 3.000 km la parte a la mitad. Con
#: seis hay dos mitades comparables. Es el mismo criterio que la condición de
#: disparo del detector de rendimiento (`docs/flota/ESTADO.md`), y se escribe
#: igual para que las dos se puedan revisar juntas.
MONTAJES_CERRADOS_PARA_VIDA_UTIL = 6


def posicion_valida(posicion, posiciones_del_vehiculo: Optional[int]) -> bool:
    """¿Existe esa posición en ese vehículo?

    QUÉ AFIRMA: que la posición está entre 1 y las que el vehículo declara tener
    en su ficha técnica. Una llanta en la posición 6 de un furgón de 4 es un dato
    imposible, no un dato raro.

    QUÉ NO AFIRMA: que la llanta quepa ahí. La medida es otra pregunta y vive en
    `flota_llanta.medida` contra `flota_ficha_tecnica.medida_llanta`; esta
    función solo mira el número.

    **Sin ficha devuelve `False`, jamás `True`.** No es una guarda defensiva: es
    el lado conservador (regla 0). `posiciones_llanta` es NOT NULL en la ficha,
    así que la única forma de no saber cuántas hay es que el vehículo no tenga
    ficha — y ahí no hay contra qué validar. Aceptar por omisión dejaría entrar
    la posición 9 de un motocarro sin que nada fallara.
    """
    if posiciones_del_vehiculo is None:
        return False
    if not isinstance(posicion, int) or isinstance(posicion, bool):
        return False
    return 1 <= posicion <= min(int(posiciones_del_vehiculo),
                                MAX_POSICIONES_LLANTA)


def km_del_montaje(
    *,
    lectura_inicio: Lectura,
    lectura_fin: Optional[Lectura],
) -> Tuple[Union[int, str], str]:
    """Los kilómetros que rodó una llanta en ESTE montaje, **con su marca**.

    Devuelve `(valor, marca)`. La marca viaja siempre, incluso cuando el valor
    no es un número: quien lo pinte tiene que poder decir por qué.

    QUÉ AFIRMA: que entre las dos lecturas de odómetro del vehículo hubo esa
    diferencia de kilómetros, y con qué confianza se puede sostener.

    QUÉ NO AFIRMA:

    · **Que la llanta esté gastada.** Un número de kilómetros no es un juicio
      sobre el estado del caucho. Ningún umbral vive acá (regla 13).
    · **Que sean los kilómetros de la llanta y no del vehículo.** Es la resta del
      odómetro del camión mientras la llanta estuvo puesta. Es lo mismo salvo que
      alguien la desmonte sin registrarlo, y eso lo dice el histórico, no esto.
    · **Nada sobre quien maneja** (regla 2). La firma no recibe conductor.

    Los tres resultados posibles, y **ninguno es cero**:

        (VIGENTE, VIGENTE)   `lectura_fin is None` — la llanta sigue montada. Su
                             vida no terminó: no es que haya recorrido 0 km.
        (SIN_DATO, SIN_DATO) los dos extremos son kilometrajes `dudosa`. Los dos
                             sin respaldo no son un número malo: no son un
                             número. Es el mismo contrato que `cpk_de`.
        (int, marca)         la resta, marcada con el PEOR de los dos extremos.
                             Con un extremo dudoso el número **sí se publica**,
                             marcado — hay un extremo sólido y una resta real.

    La marca la decide `confianza_del_tramo` y no se reimplementa acá (regla 0):
    una segunda copia de esa política sería la que decide si un número se publica.
    """
    if lectura_fin is None:
        return VIGENTE, VIGENTE

    marca = confianza_del_tramo(lectura_inicio, lectura_fin)
    if marca == SIN_DATO:
        return SIN_DATO, SIN_DATO

    km = lectura_fin.valor_km - lectura_inicio.valor_km
    # Un montaje cuyo cierre tiene MENOS kilómetros que su apertura no es un
    # tramo negativo: es que una de las dos lecturas está mal. Se dice, no se
    # devuelve un número imposible ni se toma el valor absoluto — el absoluto
    # convertiría un error en un dato plausible.
    if km < 0:                                # pragma: no cover — el CHECK lo impide
        return SIN_DATO, SIN_DATO
    return km, marca.value if isinstance(marca, Confianza) else str(marca)


def km_acumulado(tramos: Sequence[Tuple[Union[int, str], str]]
                 ) -> Tuple[Union[int, str], str]:
    """Lo que lleva rodado una llanta sumando **sus montajes ya cerrados**.

    `tramos` son los `(valor, marca)` que devuelve `km_del_montaje`, uno por
    montaje de esa llanta.

    QUÉ AFIRMA: la suma de los tramos que ya terminaron, con la peor marca de
    todos ellos — un total no puede ser más confiable que su peor sumando.

    QUÉ NO AFIRMA:

    · **Que la llanta haya rodado solo eso.** Un montaje vigente todavía no
      completó su tramo y **no entra**: sumarle un parcial mezclaría una vida
      terminada con una a medias, y el resultado no sería ni una cosa ni la otra.
    · **Que le quede o no vida.** No hay umbral (regla 13).

    Los casos que no dan un número, y por qué ninguno da cero:

        (VIGENTE, VIGENTE)   la llanta está montada y no ha cerrado ni un tramo.
                             Es su primer montaje y sigue en curso.
        (SIN_DATO, SIN_DATO) o no se montó nunca, o **alguno** de sus tramos
                             cerrados salió `SIN_DATO`. Un sumando desconocido
                             hace desconocida la suma: publicar el resto sería un
                             total que se lee como completo y no lo es.
    """
    cerrados = [(v, m) for v, m in tramos if v != VIGENTE]
    hay_vigente = len(cerrados) != len(tramos)

    if not cerrados:
        return (VIGENTE, VIGENTE) if hay_vigente else (SIN_DATO, SIN_DATO)
    if any(v == SIN_DATO for v, _ in cerrados):
        return SIN_DATO, SIN_DATO

    # `.index` sobre el orden del dominio y no un dict con default: una marca
    # fuera del vocabulario tiene que reventar acá en vez de clasificarse como
    # la más confiable por omisión (regla 5).
    orden = [c.value for c in (Confianza.VERIFICADA, Confianza.DECLARADA,
                               Confianza.DUDOSA)]
    peor = orden[max(orden.index(str(m)) for _, m in cerrados)]
    return sum(int(v) for v, _ in cerrados), peor


def vida_util_por_posicion(tramos_por_posicion: dict) -> List[dict]:
    """El hecho medido por posición, **y qué falta para poder fijar la vida útil**.

    `tramos_por_posicion` es `{posicion: [km, km, ...]}` con los kilómetros de los
    montajes **ya cerrados** de esa posición. Los vigentes no entran: una vida a
    medias no es una vida.

    Devuelve, ordenado por posición:

        {'posicion', 'n', 'km': [...], 'mediana_km', 'faltan'}

    QUÉ AFIRMA: cuántas vidas completas se midieron en esa posición y cuáles
    fueron. Es un hecho, del mismo tipo que `salto_km_maximo_30d` y
    `segundos_llenado_30d`.

    QUÉ NO AFIRMA — y es la regla 13 escrita en el valor devuelto:

    · **No dice a los cuántos kilómetros se cambia una llanta.** `mediana_km`
      sale `SIN_DATO` hasta que la posición junte
      `MONTAJES_CERRADOS_PARA_VIDA_UTIL` vidas cerradas, y `faltan` dice cuántas
      quedan. Con dos vidas, la mediana la mueve un pinchazo.
    · **No compara posiciones entre sí ni vehículos entre sí.** Una direccional y
      una de tracción no duran lo mismo, y la diferencia no es un defecto.
    · **No marca ninguna llanta.** No hay alarma, no hay color, no hay aviso.

    Sin ninguna posición medida devuelve `[]`. **No es un cero**: es que todavía
    no se desmontó una sola llanta, y `[]` lo dice sin fingir que se midió algo.
    """
    salida = []
    for posicion in sorted(tramos_por_posicion):
        km = sorted(int(k) for k in tramos_por_posicion[posicion])
        n = len(km)
        if n >= MONTAJES_CERRADOS_PARA_VIDA_UTIL:
            medio = n // 2
            mediana = km[medio] if n % 2 else (km[medio - 1] + km[medio]) // 2
        else:
            mediana = SIN_DATO
        salida.append({
            'posicion': posicion,
            'n': n,
            'km': km,
            'mediana_km': mediana,
            'faltan': max(0, MONTAJES_CERRADOS_PARA_VIDA_UTIL - n),
        })
    return salida


def posiciones_sin_llanta(posiciones_del_vehiculo: Optional[int],
                          ocupadas: Sequence[int]) -> Union[List[int], str]:
    """Las posiciones que el vehículo declara y que **hoy no tienen llanta**.

    QUÉ AFIRMA: que en esas posiciones no hay ningún montaje vigente registrado.

    QUÉ NO AFIRMA: **que el camión ande sin llanta ahí**. Casi siempre significa
    lo contrario — que la llanta está puesta y nadie la registró. Es la medida de
    cuánto le falta al inventario para describir el vehículo real, no una alarma
    mecánica, y por eso el renglón del tablero lo dice con esas palabras.

    Sin ficha devuelve `SIN_DATO`, jamás `[]`. `[]` significaría «se revisó y
    están todas cubiertas», y lo que pasa es que no se sabe cuántas posiciones
    tiene el vehículo. Es el mismo corazón que `excede_capacidad`: un vehículo
    sin ficha saldría limpio para siempre, que es cómo un detector se apaga sin
    que nadie lo note.
    """
    if posiciones_del_vehiculo is None:
        return SIN_DATO
    tomadas = set(int(p) for p in ocupadas)
    return [p for p in range(1, int(posiciones_del_vehiculo) + 1)
            if p not in tomadas]


__all__ = [
    'VIGENTE', 'SIN_DATO', 'MOTIVOS_DESMONTAJE', 'MOTIVOS_DE_FALLA_DE_MONTAJE',
    'MONTAJES_CERRADOS_PARA_VIDA_UTIL', 'posicion_valida', 'km_del_montaje',
    'km_acumulado', 'vida_util_por_posicion', 'posiciones_sin_llanta',
]
