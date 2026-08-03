"""
Vocabulario del dominio de flota. Sin I/O, sin framework.

Esto NO es el esquema de persistencia: son las palabras con las que el dominio
razona. Los modelos SQLAlchemy llegan después y deben mapear contra esto, no al
revés.

Alcance deliberado: solo el vocabulario que tocan los siete invariantes de la
tanda 1 (`docs/flota/ESPECIFICACION_T1.md` §6). Los enums de la ficha técnica
—combustible, sistema de frenos, distribución— nacen con su modelo, no antes.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


# ── Regla 4: un estado que puede ser "no sé" se modela con palabras ──────────
#
# SIN_DATO es una cadena, no None y no un booleano. Tres propiedades que se
# eligieron a propósito:
#
#   1. Es VERDADERO en contexto booleano. Un `sin_dato` falsy invita a
#      `valor or 0`, que es exactamente el default optimista que la regla 1
#      prohíbe.
#   2. Serializa a "sin_dato" en JSON sin conversión: lo que ve el conductor en
#      pantalla es la misma palabra que está en la base.
#   3. Nunca es igual a 0 ni a '' ni a None. `SIN_DATO == 0` es False.
#
# Un vehículo sin lecturas de odómetro no tiene 0 km recorridos: no sabemos
# cuántos tiene. Son estados distintos y el tipo lo obliga.
class _SinDato(str):
    __slots__ = ()

    def __repr__(self) -> str:
        return 'SIN_DATO'


SIN_DATO = _SinDato('sin_dato')


class OrigenLectura(str, Enum):
    """De qué gesto operativo nació una lectura de odómetro."""

    ENTREGA        = 'entrega'
    PREOPERACIONAL = 'preoperacional'
    CIERRE_DIA     = 'cierre_dia'
    OT             = 'ot'
    TANQUEO        = 'tanqueo'
    CORRECCION     = 'correccion'


#: Orígenes que el endpoint de **lectura suelta** puede registrar hoy.
#:
#: El enum de arriba nombra los seis gestos que existen en el modelo. Este
#: subconjunto declara cuáles de esos gestos **son este endpoint**. Los otros
#: tres quedan fuera por razones distintas y ninguna es "todavía no lo hicimos":
#:
#:   · `entrega` nace del traspaso atómico, que además cierra la custodia
#:     anterior y abre la nueva. Una lectura suelta que se declare `entrega`
#:     dice que hubo un cambio de turno que nunca ocurrió — y queda
#:     indistinguible de las reales en el histórico que alimenta el CPK.
#:   · `preoperacional` viene de la inspección diaria (tanda 2).
#:   · `ot` viene de la orden de trabajo (tanda 3).
#:
#: Para los dos últimos el problema no es que la pantalla los ofrezca: es que
#: **la lectura apuntaría a un padre que no puede existir**. Un `ot` sin OT es
#: una referencia colgada, y el día que la tanda 3 exista habrá filas viejas que
#: no se pueden reconciliar con ninguna orden.
#:
#: Se habilitan agregándolos acá, no editando la pantalla: el selector se
#: valida contra esta tupla (`tests/flota/test_origen_lectura.py`).
ORIGENES_LECTURA_SUELTA = (
    OrigenLectura.TANQUEO,
    OrigenLectura.CIERRE_DIA,
    OrigenLectura.CORRECCION,
)

#: Por qué cada origen excluido no se puede registrar como lectura suelta.
#:
#: Es **total sobre el complemento** de la tupla de arriba, y un test lo obliga.
#: Total a propósito: la frontera lo indexa directo, sin `.get(x, '')` — un
#: default vacío ahí sería un mensaje de error que no dice nada justo cuando
#: alguien más lo necesita, que es la regla 5 aplicada a un texto.
MOTIVO_ORIGEN_NO_SUELTO = {
    OrigenLectura.ENTREGA:
        'Una entrega se registra en el recibo de turno, que además cierra la '
        'custodia anterior y abre la nueva.',
    OrigenLectura.PREOPERACIONAL:
        'La inspección diaria todavía no existe (tanda 2).',
    OrigenLectura.OT:
        'Las órdenes de trabajo todavía no existen (tanda 3): la lectura '
        'quedaría apuntando a una OT imposible.',
}


#: Máximo de posiciones de llanta que el vocabulario contempla.
#:
#: La flota de hoy son furgones (4) y camiones (6). 12 deja aire para un
#: doble-troque sin abrir la puerta a un valor arbitrario del cliente. Si algún
#: día entra una tractomula, esto se sube **acá** y el CHECK lo sigue: el
#: número está en un lugar con nombre y no repartido en tres archivos.
MAX_POSICIONES_LLANTA = 12

#: Ángulos fijos de la evidencia de estado, en orden. El orden fijo es lo que
#: hace comparable un turno con otro.
ANGULOS_FIJOS = (
    'frontal', 'trasera', 'lateral_izq', 'lateral_der',
    'cajon_abierto', 'interior_cabina', 'tablero',
)

#: Vocabulario completo de `flota_foto.angulo`.
#:
#: Hasta el 2026-08-03 las fotos se guardaban **sin ángulo**: ocho archivos
#: anónimos colgados de una custodia. El orden tampoco las identificaba, porque
#: el frontend filtra las faltantes antes de enviar — con `frontal` sin tomar,
#: la primera foto del arreglo es `trasera` y todo queda corrido un lugar.
#:
#: La consecuencia práctica: era imposible decir *"el flanco herido está en la
#: llanta trasera derecha"*. Una evidencia que no se puede referir a una parte
#: del vehículo no sirve para atribuir un daño, que es para lo que se toma.
ANGULOS_FOTO = ANGULOS_FIJOS + tuple(
    f'llanta_{i}' for i in range(1, MAX_POSICIONES_LLANTA + 1)
)


def angulos_de_custodia(posiciones_llanta: int) -> tuple:
    """Los ángulos que se le piden a un vehículo con N posiciones de llanta.

    Una sola foto llamada `llantas` no ubica nada: una tuerca floja está en una
    rueda concreta. Por eso el formulario se arma contra la ficha técnica y no
    contra una constante.
    """
    if not 1 <= posiciones_llanta <= MAX_POSICIONES_LLANTA:
        raise ValueError(
            f'posiciones_llanta fuera de rango: {posiciones_llanta} '
            f'(1..{MAX_POSICIONES_LLANTA})'
        )
    return ANGULOS_FIJOS + tuple(
        f'llanta_{i}' for i in range(1, posiciones_llanta + 1))


#: Posiciones de llanta cuando el vehículo todavía no tiene ficha técnica.
#:
#: Las claves son los valores reales de `Vehiculo.tipo`, que es texto libre —
#: no el vocabulario del dominio. Se normaliza a minúsculas sin tildes.
#:
#: **Es un supuesto, no un dato.** Por eso `posiciones_llanta()` devuelve además
#: de dónde salió el número, y la pantalla lo dice. Un default silencioso haría
#: que un camión de 6 posiciones pidiera 4 fotos y nadie notara las dos que
#: faltan — la regla 1 aplicada a la forma del formulario, no al valor de un ítem.
POSICIONES_LLANTA_POR_TIPO = {
    'nhr': 6, 'turbo': 6, 'camion': 6, 'sencillo': 6,
    'van': 4, 'furgon': 4, 'furgon liviano': 4, 'camioneta': 4,
    'moto': 2, 'motocarro': 3,
}
POSICIONES_LLANTA_FALLBACK = 4


def _normalizar(texto: str) -> str:
    tildes = str.maketrans('áéíóúü', 'aeiouu')
    return (texto or '').strip().lower().translate(tildes)


def posiciones_llanta(ficha_posiciones, tipo_vehiculo):
    """Cuántas fotos de llanta pedir, y **de dónde salió ese número**.

    Devuelve `(n, fuente)` con fuente en {'ficha', 'tipo', 'fallback'}. Los tres
    valen números distintos de confianza y la pantalla los muestra distinto:
    con ficha es un dato levantado en campo; por tipo es una inferencia
    razonable; el fallback es no saber.

    Nunca levanta: dejar al conductor sin formulario a las 5 a.m. porque el
    vehículo no tiene ficha sería peor que pedirle cuatro fotos y decírselo.
    """
    if ficha_posiciones:
        return int(ficha_posiciones), 'ficha'
    n = POSICIONES_LLANTA_POR_TIPO.get(_normalizar(tipo_vehiculo))
    if n:
        return n, 'tipo'
    return POSICIONES_LLANTA_FALLBACK, 'fallback'


class CustodioTipo(str, Enum):
    """Quién responde por el vehículo.

    `taller` llega en la tanda 3 con la tabla de talleres. En tanda 1 un
    vehículo en taller queda bajo custodia de la sede que lo envió.
    """

    CONDUCTOR = 'conductor'
    SEDE      = 'sede'


class CustodioEstado(str, Enum):
    """Si el custodio declarado se pudo representar contra el maestro.

    `pendiente_sede` no es un error ni un NULL disfrazado: es la sede que el
    WMS todavía no tiene como fila. `almacenes` cubre 5 de los 9 centros
    (medido 2026-08-01) y flota no crea maestros ajenos para tapar el hueco.

    Regla 4 aplicada a una relación: lo que no se puede representar se dice
    con una palabra, y el health lo cuenta para que no se acumule callado.
    """

    RESUELTO       = 'resuelto'
    PENDIENTE_SEDE = 'pendiente_sede'


class QuienPide(str, Enum):
    """Desde dónde se pide recibir un vehículo. Cambia la respuesta, no el dato.

    Un conductor no puede quitarle el turno a otro: la conversación la tienen
    ellos dos. Un admin de zona sí, porque es la única salida cuando alguien se
    fue sin cerrar y el camión tiene que salir a las 5 a.m.
    """

    CONDUCTOR  = 'conductor'
    ADMIN_ZONA = 'admin_zona'


class ClaseFoto(str, Enum):
    """Regla 7: hay dos clases de foto y no comparten parámetros.

    EVIDENCIA_ESTADO prueba cómo estaba algo. Se puede degradar.
    FOTO_DATO es la fuente de un número que alguien va a auditar. Degradarla
    es perder el respaldo del número.
    """

    EVIDENCIA_ESTADO = 'evidencia_estado'
    FOTO_DATO        = 'foto_dato'


class EntidadFoto(str, Enum):
    """A qué cuelga una foto. Regla 7: un archivo sin padre es un bug."""

    CUSTODIA_INICIO = 'custodia_inicio'
    CUSTODIA_FIN    = 'custodia_fin'
    ODOMETRO        = 'odometro'
    DOCUMENTO       = 'documento'
    HALLAZGO        = 'hallazgo'


# ── Estructuras de trabajo ───────────────────────────────────────────────────
#
# Mínimas a propósito: solo los campos que los siete invariantes necesitan
# para pronunciarse. No son la tabla.

@dataclass(frozen=True)
class Lectura:
    """Una lectura de odómetro. Append-only: no se edita, se corrige."""

    valor_km: int
    ts: datetime
    origen: OrigenLectura
    autor_usuario_id: int
    motivo_correccion: Optional[str] = None


@dataclass(frozen=True)
class Custodia:
    """Un tramo de responsabilidad sobre un vehículo. `fin_ts=None` = activa."""

    vehiculo_id: int
    custodio_tipo: CustodioTipo
    inicio_ts: datetime
    registrado_por_usuario_id: int
    km_inicio: int
    fin_ts: Optional[datetime] = None
    km_fin: Optional[int] = None
    custodio_conductor_id: Optional[int] = None
    custodio_sede_id: Optional[int] = None
    custodio_estado: CustodioEstado = CustodioEstado.RESUELTO
    linea_base: bool = False
    cierre_forzado: bool = False


@dataclass(frozen=True)
class Dimensiones:
    """Tamaño de una imagen en píxeles."""

    ancho: int
    alto: int

    @property
    def lado_largo(self) -> int:
        return max(self.ancho, self.alto)


@dataclass(frozen=True)
class Foto:
    """Referencia a un archivo. El binario nunca vive en la base (regla 7)."""

    clase: ClaseFoto
    entidad_tipo: EntidadFoto
    entidad_id: int
    storage_ref: str
    hash_sha256: str
    bytes: int
    dimensiones: Dimensiones
    autor_usuario_id: int
    simulado: bool = False
