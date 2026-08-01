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


class CustodioTipo(str, Enum):
    """Quién responde por el vehículo.

    `taller` llega en la tanda 3 con la tabla de talleres. En tanda 1 un
    vehículo en taller queda bajo custodia de la sede que lo envió.
    """

    CONDUCTOR = 'conductor'
    SEDE      = 'sede'


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
    linea_base: bool = False


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
