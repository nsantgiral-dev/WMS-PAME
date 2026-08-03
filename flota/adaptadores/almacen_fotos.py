"""
Dónde viven los archivos de las fotos. Implementa `puertos.AlmacenDeFotos`.

Detrás del `Protocol` a propósito: hoy es un volumen de Railway porque es lo que
existe el lunes sin cuenta nueva ni credenciales. El día que haga falta S3 o R2
se cambia esta clase y **el dominio no se entera** — para eso está declarado el
puerto.

**Direccionado por contenido:** el nombre del archivo es su propio SHA-256. Dos
consecuencias que se eligieron, no que salieron:

  · La misma foto subida dos veces ocupa un archivo. Ocho ángulos por turno,
    cinco vehículos, todos los días: la deduplicación no es un lujo.
  · **El hash no se puede falsear.** Si el archivo se corrompe o se cambia, deja
    de coincidir con su nombre. Un `hash_sha256` que se calcula aparte del
    contenido es un número que dice la verdad hasta que alguien lo edita.

Lo que ESTE archivo reemplaza: hasta el 2026-08-03 el frontend mandaba
`storage_ref: 'inline://pendiente-subida'` y `hash_sha256: '0'*64`. La imagen se
comprimía en el navegador y se descartaba. La fila decía que había una foto del
tablero, con una referencia que no apuntaba a nada y un hash de ceros —
evidencia falsa, que es exactamente lo que este módulo existe para impedir.
"""
import base64
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

_MIMES = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}


class ErrorAlmacen(Exception):
    """No se pudo guardar. Se propaga: el llamador marca `pendiente_evidencia`."""


def _raiz() -> Path:
    """Carpeta raíz del almacén. Sin default silencioso.

    Si `FLOTA_FOTOS_DIR` no está, esto **levanta**. Un default a `/tmp` haría que
    las fotos se guarden y desaparezcan en el próximo deploy, que es peor que no
    guardarlas: en vez de un hueco visible da una evidencia que se evapora.

    En Railway apunta al volumen montado. En local, a cualquier carpeta.
    """
    d = os.getenv('FLOTA_FOTOS_DIR')
    if d is None or not d.strip():
        raise ErrorAlmacen(
            'FLOTA_FOTOS_DIR no está configurada. Sin almacén, una foto '
            'guardada es una evidencia que no existe — el registro queda en '
            'pendiente_evidencia y el health lo cuenta.'
        )
    return Path(d.strip())


class AlmacenLocal:
    """Archivos en disco. Volumen de Railway en producción."""

    def guardar(self, contenido: bytes, mime: str) -> str:
        """Escribe el archivo y devuelve su `storage_ref`.

        Levanta `ErrorAlmacen` si no puede. **No devuelve una referencia
        inventada ante el fallo** — eso es la regla 5: o funciona, o falla
        ruidosamente. Una ref que no apunta a nada se descubre el día que
        alguien busca la foto, que es siempre el peor día.
        """
        if not contenido:
            raise ErrorAlmacen('contenido vacío')
        if mime not in _MIMES:
            raise ErrorAlmacen(f'mime no soportado: {mime!r}')

        digest = hashlib.sha256(contenido).hexdigest()
        hoy = datetime.utcnow()
        relativa = Path(f'{hoy:%Y/%m}') / f'{digest}{_MIMES[mime]}'
        destino = _raiz() / relativa
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            if not destino.exists():           # direccionado por contenido: ya está
                destino.write_bytes(contenido)
        except OSError as e:
            raise ErrorAlmacen(f'no se pudo escribir {relativa}: {e}') from e
        return str(relativa)

    def leer(self, storage_ref: str) -> bytes:
        """Devuelve el archivo. Levanta si no está — no un placeholder."""
        destino = _raiz() / storage_ref
        if not destino.is_file():
            raise ErrorAlmacen(f'la foto {storage_ref} no está en el almacén')
        return destino.read_bytes()

    def dimensiones(self, storage_ref: str) -> Dict[str, int]:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(self.leer(storage_ref))) as img:
            return {'ancho': img.width, 'alto': img.height}


def desde_data_url(data_url: str) -> Tuple[bytes, str]:
    """`data:image/jpeg;base64,...` → (bytes, mime).

    El base64 viaja por la red y **no toca la base**: se decodifica acá y lo que
    se guarda es el archivo. La regla 7 prohíbe el binario en una columna, no en
    un request.
    """
    if not data_url or not data_url.startswith('data:'):
        raise ErrorAlmacen('no es un data URL')
    try:
        cabecera, datos = data_url.split(',', 1)
        mime = cabecera.split(';')[0][len('data:'):]
        return base64.b64decode(datos), mime
    except (ValueError, TypeError) as e:
        raise ErrorAlmacen(f'data URL ilegible: {e}') from e


def guardar_foto(datos: dict) -> dict:
    """Toma el payload del frontend y devuelve los campos reales de la fila.

    Si el guardado falla, **no inventa**: devuelve el registro marcado
    `pendiente_evidencia`, con la referencia del fallo y sin hash. El health lo
    cuenta y alguien puede ir a buscar la foto que no quedó.
    """
    campos = {
        'clase': datos['clase'],
        'bytes': datos['bytes'] if 'bytes' in datos else 0,
        'ancho': datos['ancho'], 'alto': datos['alto'],
        'mime': datos['mime'] if 'mime' in datos else 'image/jpeg',
        'simulado': datos['simulado'] if 'simulado' in datos else False,
        # Qué parte del vehículo muestra. Si no viene, queda NULL y el health lo
        # cuenta: una foto anónima no se puede referir a una rueda ni a un
        # costado, que es justamente para lo que se toma.
        'angulo': datos['angulo'] if 'angulo' in datos else None,
    }
    # Decodificar y escribir son dos fallos distintos y no se mezclan:
    #
    #   · Un data URL ilegible es un bug del cliente. Levanta, y el endpoint
    #     responde 400 — no se guarda media fila con datos inventados.
    #   · Un almacén caído es del servidor. La foto SÍ existe y se conoce su
    #     tamaño; lo que falta es dónde quedó. Eso es `pendiente_evidencia`.
    #
    # Mezclarlos daba una fila con `bytes = 0` que el CHECK rechazaba — una
    # foto de cero bytes no es una foto.
    contenido, mime = desde_data_url(
        datos['data_url'] if 'data_url' in datos else '')
    campos.update({'bytes': len(contenido), 'mime': mime})
    try:
        ref = AlmacenLocal().guardar(contenido, mime)
        campos.update({
            'storage_ref': ref,
            # El hash sale del contenido real. Nunca de ceros: un hash inventado
            # es una firma que dice que la foto es íntegra sin haberla mirado.
            'hash_sha256': hashlib.sha256(contenido).hexdigest(),
            'estado': 'ok',
        })
    except ErrorAlmacen as e:
        campos.update({
            'storage_ref': f'sin-guardar: {str(e)[:180]}',
            'hash_sha256': '',
            'estado': 'pendiente_evidencia',
        })
    return campos


__all__ = ['AlmacenLocal', 'ErrorAlmacen', 'desde_data_url', 'guardar_foto']
