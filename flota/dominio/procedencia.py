"""
Procedencia de una cifra — la regla 13 del módulo, hecha tipo.

> *«Ningún número entra sin decir contra qué base y en qué fecha.»*

La regla ya existía, escrita, y se cumplía a ojo. Este módulo la vuelve
estructural: **`Cifra` no se puede construir sin su base ni sin su ventana**, y
`Cifra.a_json()` es el único serializador. Un número que se salte el tipo no
tiene esas claves, y el trinquete lo ve.

## Por qué un tipo y no una convención

Porque la alternativa es un detector de texto sobre los adaptadores, y en este
repo los detectores de texto se atraparon en sus propios docstrings **nueve
veces**. Un tipo no se puede engañar con un comentario: o se instancia con base
y ventana, o levanta.

Es la misma decisión que `@invariante(detector_ciego=...)`: la afirmación tiene
que nombrar lo que la respalda, en un sitio que una máquina pueda leer.

## Puro, y por eso recibe el día en vez de calcularlo

`flota/dominio/` no puede importar `app.utils.fecha` —lo impide
`tests/flota/test_trinquetes_flota.py::TestTrinqueteFronteraDominio`— y eso es
correcto: el día operativo es una decisión de infraestructura (qué zona horaria
corre el proceso), no del dominio. El adaptador pasa `dia_operativo()`.

Consecuencia práctica: estas funciones se pueden probar barriendo las 24 horas
del día sin monkeypatchear un reloj, que es cómo se encontró que la ventana del
CPK corría cinco horas.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from flota.dominio.valores import SIN_DATO

#: Lo que `jsonify` sabe serializar sin ayuda.
#:
#: `Decimal` **no está**, y esa ausencia es deliberada: un `Decimal` que llega a
#: `jsonify` revienta el endpoint, y un `float(...)` puesto para que no reviente
#: pierde centavos en silencio. La conversión a texto la hace el adaptador, que
#: es quien sabe cuántos decimales lleva cada magnitud — pesos no se redondean
#: igual que galones.
_JSON_SEGURO = (str, int, float, bool, type(None))


def _es_sin_dato(valor) -> bool:
    """¿Este valor es el hueco? Por **igualdad**, y acá eso es lo correcto.

    El dominio compara `SIN_DATO` por identidad a propósito, y esa regla no se
    afloja. Pero `Cifra` vive en la frontera de presentación, y el valor que le
    llega ya pasó por un formateador: `numero_legible(SIN_DATO)` hace
    `str(valor)`, y `str()` sobre una subclase de `str` devuelve un **`str`
    plano** — la identidad se pierde ahí, no acá.

    Exigir identidad en este punto obligaría a cada adaptador a arrastrar dos
    valores en paralelo, el formateado y el crudo, para poder contestar «¿esto
    es un hueco?». Y el primero que se olvide de arrastrarlo publica un
    `sin_dato` sin motivo, que es justo lo que este tipo existe para impedir.

    `SIN_DATO == 'sin_dato'` es verdadero por ser subclase de `str`, así que la
    comparación por valor cubre los dos casos.
    """
    return valor == SIN_DATO


class ProcedenciaInvalida(ValueError):
    """La cifra no se puede publicar como está.

    `ValueError` y no `ErrorFlota`: no es un dato de negocio mal digitado que
    una frontera traduce a 400. Es código que intentó publicar un número sin su
    procedencia, y eso tiene que reventar ruidosamente en desarrollo.
    """


@dataclass(frozen=True)
class Ventana:
    """El tramo de tiempo sobre el que se calculó algo. En días de Bogotá.

    `etiqueta` no es decoración: es lo que la pantalla escribe al lado del
    número. «el mes en curso» y «la semana cerrada» se leen distinto, y un
    tablero que solo muestra dos fechas obliga a quien lo mira a deducir cuál
    de las dos preguntas contestó.
    """

    desde: date
    hasta: date
    etiqueta: str

    def __post_init__(self):
        if self.hasta < self.desde:
            raise ProcedenciaInvalida(
                f'ventana invertida: {self.hasta} < {self.desde}')
        if not (self.etiqueta or '').strip():
            raise ProcedenciaInvalida(
                'la ventana no tiene etiqueta. Dos fechas sueltas no dicen qué '
                'pregunta contestan.')

    @property
    def dias(self) -> int:
        """Días calendario que abarca, **con los dos extremos adentro**.

        Del 1 al 31 son 31 días, no 30. Es el mismo criterio de
        `costos.dias_del_periodo`, y por el mismo motivo: un período que se
        cuenta con `.days` pelado pierde un día por cada gasto imputado.
        """
        return (self.hasta - self.desde).days + 1


@dataclass(frozen=True)
class Cifra:
    """Un número **con todo lo que hace falta para desconfiar de él**.

    Cinco campos, y ninguno es opcional por comodidad:

    | | Qué contesta | Por qué es obligatorio |
    |---|---|---|
    | `valor` | el número, o `SIN_DATO` | — |
    | `base` | contra qué se calculó | «$4.780/km» sin base no se puede auditar |
    | `ventana` | de cuándo a cuándo | la regla 13 en su forma literal |
    | `n` | sobre cuántos elementos | **es el despromediado**: un promedio de 2 y uno de 200 no se leen igual |
    | `motivo` | por qué NO hay número | obligatorio si `valor` es `SIN_DATO` |

    `n` merece su renglón. Es el campo que impide que este tipo se vuelva
    decoración: una cifra que no sabe sobre cuántas cosas se calculó no se puede
    despromediar, y con seis vehículos el `n` suele ser la información entera —
    «el máximo de 6» y «el p90 de 6» son el mismo número con distinta autoridad.
    """

    valor: Any
    base: str
    ventana: Ventana
    n: int
    motivo: Optional[str] = None

    def __post_init__(self):
        if not (self.base or '').strip():
            raise ProcedenciaInvalida(
                f'cifra sin base: {self.valor!r}. Un número que no dice contra '
                f'qué se calculó no se puede auditar, y va a un tablero.')
        if not isinstance(self.n, int) or isinstance(self.n, bool):
            raise ProcedenciaInvalida(f'n no es un entero: {self.n!r}')
        if self.n < 0:
            raise ProcedenciaInvalida(f'n negativo: {self.n}')

        # El `sin_dato` sin motivo es el defecto que este módulo existe para
        # cerrar. Los tres caminos del CPK a `SIN_DATO` se corrigen llamando a
        # personas distintas, y sin motivo el tablero manda a mirar los tres.
        if _es_sin_dato(self.valor) and not (self.motivo or '').strip():
            raise ProcedenciaInvalida(
                'una cifra sin_dato tiene que decir POR QUÉ. Un hueco sin '
                'motivo no manda a nadie a hacer nada.')

        # Y la otra dirección: un motivo sobre una cifra que sí existe se
        # leería como una advertencia sobre un número sano.
        if not _es_sin_dato(self.valor) and self.motivo is not None:
            raise ProcedenciaInvalida(
                f'la cifra tiene valor ({self.valor!r}) y además un motivo '
                f'({self.motivo!r}). El motivo explica la ausencia; con el '
                f'número presente, contradice.')

        if not isinstance(self.valor, _JSON_SEGURO):
            raise ProcedenciaInvalida(
                f'valor de tipo {type(self.valor).__name__}, que `jsonify` no '
                f'sabe serializar. Convertilo en el adaptador, que es el que '
                f'sabe cuántos decimales lleva: los pesos y los galones no se '
                f'redondean igual.')

    def a_json(self, clave_valor: str = 'valor') -> dict:
        """**El único serializador.** Las seis claves de procedencia, siempre.

        Que sea el único es lo que hace exigible la regla 13: el trinquete
        recorre el payload y pide `base`, `desde`, `hasta` y `n`. Un número que
        no pasó por acá no las tiene, y sale en la lista.

        `clave_valor` existe para que un campo ya publicado pueda adoptar este
        tipo **sin renombrar su clave** — `cpk_mes` publica `cpk`, no `valor`, y
        romper esa clave para ganar uniformidad rompería la pantalla y los
        tests por una razón estética. Lo que no es negociable es el resto.
        """
        if clave_valor in ('base', 'desde', 'hasta', 'etiqueta', 'n', 'motivo'):
            raise ProcedenciaInvalida(
                f'{clave_valor!r} pisaría una clave de procedencia. El valor '
                f'tapando su propia base es peor que no tener base.')
        return {
            clave_valor: self.valor,
            'base': self.base,
            'desde': self.ventana.desde.isoformat(),
            'hasta': self.ventana.hasta.isoformat(),
            'etiqueta': self.ventana.etiqueta,
            'n': self.n,
            'motivo': self.motivo,
        }


# ── Ventanas con nombre ──────────────────────────────────────────────────────

def mes_en_curso_de(dia: date) -> Ventana:
    """Del primero del mes al día dado, los dos incluidos.

    Del **primero y no de hace 30 días**: el CPK se compara contra el mes
    anterior, y una ventana móvil no se puede comparar con nada.
    """
    return Ventana(desde=dia.replace(day=1), hasta=dia,
                   etiqueta='el mes en curso')



def semana_cerrada_antes_de(dia: date) -> Ventana:
    """El lunes-a-domingo **completo** anterior a `dia`.

    Los dos adjetivos del nombre son el defecto que evitan, y no son adorno:

    · **«cerrada»** — el reporte de los lunes corre el lunes. Una `semana_de(hoy)`
      devolvería la semana que empezó hace tres horas: un correo de ceros todos
      los lunes, que se lee como «no pasó nada» en vez de «pregunté por el día
      equivocado». Ese fallo no revienta y no deja rastro — es la forma exacta
      de la lectura temprana que dejó 28 requisiciones huérfanas en Siesa.
    · **«antes de»** — un miércoles también devuelve la semana pasada completa,
      no lo que va corrido. Una semana a medias parece un desplome, que es la
      misma razón por la que la ingesta del Vigía no escribe la semana en curso.

    Lunes a domingo y no domingo a sábado porque es como la operación cuenta la
    semana; `date.weekday()` ya usa el lunes como 0.

    Nació el 2026-09-04 **sin consumidor** y el trinquete de funciones sin
    caller la rechazó. Se borró y volvió con el reporte delante, que es la regla
    12 del módulo sin asterisco — declararla como deuda para no borrarla habría
    sido el atajo.
    """
    lunes_de_esta = dia - timedelta(days=dia.weekday())
    return Ventana(desde=lunes_de_esta - timedelta(days=7),
                   hasta=lunes_de_esta - timedelta(days=1),
                   etiqueta='la semana cerrada')


__all__ = ['Cifra', 'Ventana', 'ProcedenciaInvalida',
           'mes_en_curso_de', 'semana_cerrada_antes_de']
