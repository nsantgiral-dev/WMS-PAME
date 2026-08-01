"""
Políticas de fotos. Invariantes 5 (paternidad) y 6 (integridad de clase).

Sin implementar. Ver la nota de `odometro.py` sobre por qué existen las firmas.
"""
from typing import Callable

from flota.dominio.valores import ClaseFoto, Dimensiones, EntidadFoto, Foto

# Resuelve (entidad_tipo, entidad_id) → ¿existe esa fila?
# Es una función, no un repositorio, para que el dominio no conozca la base.
ResolvedorDePadre = Callable[[EntidadFoto, int], bool]

# Regla 7 — parámetros mínimos por clase.
# `evidencia_estado` prueba cómo estaba algo: se puede degradar.
# `foto_dato` es la fuente de un número que alguien va a auditar contra la
# imagen: degradarla es perder el respaldo del número. El odómetro es seis
# dígitos fotografiados a las 5 a.m. en patio.
LADO_LARGO_MINIMO_FOTO_DATO = 1600
CALIDAD_MINIMA_FOTO_DATO = 75


def validar_paternidad(foto: Foto, resolver: ResolvedorDePadre) -> None:
    """Toda foto cuelga de una fila que existe. Un archivo sin padre es un bug.

    QUÉ AFIRMA: que `(entidad_tipo, entidad_id)` resuelve a una fila real en el
    momento de la validación.

    QUÉ NO AFIRMA: que el archivo exista en el object storage. Eso es otra
    verificación, contra `storage_ref` y `hash_sha256`.

    Levanta `FotoInvalida`. No crea el padre, no inventa uno, no la guarda
    "suelta para después": una foto huérfana es evidencia que nadie va a
    encontrar el día que la necesite.
    """
    raise NotImplementedError(
        'flota.dominio.fotos.validar_paternidad — invariante 5 (paternidad) sin implementar'
    )


def validar_integridad_de_clase(
    clase: ClaseFoto,
    capturada: Dimensiones,
    servida: Dimensiones,
) -> None:
    """Ninguna `foto_dato` sale del servidor más pequeña de lo que se capturó.

    QUÉ AFIRMA: que la imagen servida conserva, para su clase, la información
    que hace auditable el número que la foto respalda.

    QUÉ NO AFIRMA: que el número sea legible. Eso depende del encuadre y de la
    luz, y ninguna validación automática lo puede decidir.

    Para `evidencia_estado` la recompresión del pipeline existente es válida.
    Para `foto_dato` no hay recompresión en servidor: 800×600 a calidad 0.65
    recomprimido a calidad 40 no deja leer un odómetro, y un odómetro que no se
    puede verificar contra su foto es una declaración sin respaldo.

    Levanta `FotoInvalida`.
    """
    raise NotImplementedError(
        'flota.dominio.fotos.validar_integridad_de_clase — invariante 6 (integridad de clase) sin implementar'
    )


__all__ = [
    'ResolvedorDePadre',
    'LADO_LARGO_MINIMO_FOTO_DATO',
    'CALIDAD_MINIMA_FOTO_DATO',
    'validar_paternidad',
    'validar_integridad_de_clase',
]
