"""
Políticas de fotos. Invariantes 5 (paternidad) y 6 (integridad de clase).

La paternidad es la única de los siete invariantes que la base NO puede
imponer sola: `entidad_tipo` + `entidad_id` es polimórfico y no admite FK.
Queda dicho en vez de disimulado.
"""
from typing import Callable

from flota.dominio.errores import FotoInvalida
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

# Qué formatos acepta cada clase.
#
# Las fotos que toma la app son imágenes y punto. Un adjunto de documento
# también puede ser el PDF que mandó la aseguradora — obligar a fotografiar la
# pantalla donde se abre ese PDF degrada el original y no agrega nada.
MIMES_IMAGEN = ('image/jpeg', 'image/png', 'image/webp')
MIMES_DOCUMENTO = MIMES_IMAGEN + ('application/pdf',)


def mimes_permitidos(clase: ClaseFoto) -> tuple:
    return MIMES_DOCUMENTO if clase == ClaseFoto.DOCUMENTO_ADJUNTO else MIMES_IMAGEN


def exige_dimensiones(clase: ClaseFoto) -> bool:
    """¿Esta clase obliga a tener ancho y alto?

    Un PDF no tiene píxeles. Guardarle `0×0` para satisfacer una columna NOT
    NULL sería un número que miente sobre un archivo que sí existe; guardarle
    `1×1` es peor. La ausencia se modela con ausencia (regla 4), y el CHECK de
    la tabla dice exactamente esto mismo para que la base no dependa de que
    alguien pase por acá.
    """
    return clase != ClaseFoto.DOCUMENTO_ADJUNTO


def validar_formato(clase: ClaseFoto, mime: str,
                    ancho: int | None, alto: int | None) -> None:
    """El archivo es de un tipo que esa clase acepta, y trae lo que debe traer.

    Levanta `FotoInvalida`. No normaliza, no rellena, no elige por el llamador:
    un mime desconocido guardado "por si acaso" es un archivo que después nadie
    puede abrir y que la fila afirma que es evidencia.
    """
    permitidos = mimes_permitidos(clase)
    if mime not in permitidos:
        raise FotoInvalida(
            f'{mime!r} no se acepta para {clase.value}. '
            f'Permitidos: {", ".join(permitidos)}.'
        )
    if exige_dimensiones(clase):
        if not ancho or not alto:
            raise FotoInvalida(
                f'{clase.value} exige ancho y alto: son la evidencia de que la '
                f'imagen sirve para lo que se tomó (recibido {ancho}x{alto}).'
            )
    elif (ancho is None) != (alto is None):
        raise FotoInvalida(
            f'dimensiones a medias ({ancho}x{alto}): o están las dos o no está '
            'ninguna. Una sola no describe nada.'
        )


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
    if not resolver(foto.entidad_tipo, foto.entidad_id):
        raise FotoInvalida(
            f'foto sin padre resoluble: {foto.entidad_tipo.value}#{foto.entidad_id} '
            f'no existe. Una foto huérfana es evidencia que nadie va a encontrar '
            f'el día que la necesite.'
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
    if clase != ClaseFoto.FOTO_DATO:
        return
    if servida.ancho < capturada.ancho or servida.alto < capturada.alto:
        raise FotoInvalida(
            f'foto_dato degradada: se capturó {capturada.ancho}x{capturada.alto} '
            f'y se sirve {servida.ancho}x{servida.alto}. Un odómetro que no se '
            f'puede verificar contra su foto es una declaración sin respaldo.'
        )


__all__ = [
    'ResolvedorDePadre',
    'LADO_LARGO_MINIMO_FOTO_DATO',
    'CALIDAD_MINIMA_FOTO_DATO',
    'MIMES_IMAGEN',
    'MIMES_DOCUMENTO',
    'mimes_permitidos',
    'exige_dimensiones',
    'validar_formato',
    'validar_paternidad',
    'validar_integridad_de_clase',
]
