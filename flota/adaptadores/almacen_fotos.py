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
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

from flota.dominio.fotos import validar_formato
from flota.dominio.valores import ClaseFoto

logger = logging.getLogger(__name__)


def _medir_dimensiones(contenido: bytes):
    """Ancho y alto REALES del archivo, o `None` si no se pueden medir.

    `None` es un tercer estado y no un cero: significa «esto no es una
    imagen que yo sepa abrir» —un PDF adjunto, un archivo truncado—, no
    «mide cero». El llamador conserva lo declarado y sigue; rechazar acá
    trabaría un traspaso de custodia por un formato que la clase sí
    permite.

    Pillow está en `requirements.txt:30`. El import va adentro para que
    un entorno sin la dependencia degrade a «no medí» en vez de tumbar
    el módulo entero al importarlo — que es exactamente lo que le
    pasaba a `AlmacenLocal.dimensiones()`, la función gemela que nadie
    llamaba y que habría reventado el día que alguien la usara.
    """
    try:
        from io import BytesIO

        from PIL import Image
        with Image.open(BytesIO(contenido)) as img:
            return {'ancho': img.width, 'alto': img.height}
    except Exception:
        return None

#: Extensión en disco por tipo de archivo. **No es la política de qué se
#: acepta** — eso lo decide `flota.dominio.fotos.validar_formato` según la
#: clase, y este mapa solo sabe cómo nombrar el archivo. Separado a propósito:
#: si el almacén decidiera qué es aceptable, la regla viviría en dos sitios.
_MIMES = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
    'application/pdf': '.pdf',
}


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
    clase = ClaseFoto(datos['clase'])
    # Un adjunto puede no tener dimensiones (PDF) o tenerlas (una foto del
    # papel). Las demás clases las exigen: `validar_formato` lo comprueba abajo,
    # contra el mime ya resuelto, no contra el que el cliente dice traer.
    campos = {
        'clase': datos['clase'],
        'bytes': datos['bytes'] if 'bytes' in datos else 0,
        'ancho': datos['ancho'] if 'ancho' in datos else None,
        'alto': datos['alto'] if 'alto' in datos else None,
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

    # ── Las dimensiones se MIDEN, no se creen (2026-09-01) ───────────────────
    # Antes salían de `datos['ancho']`/`datos['alto']`, o sea del JSON que manda
    # el cliente, y el CHECK de la base que exige 1600 px de lado largo para una
    # `foto_dato` evaluaba **ese número autorreportado**. La foto del tablero es
    # la única evidencia del kilometraje, y su respaldo se apoyaba en una
    # promesa: cualquiera que arme el POST a mano declara `{ancho: 4000}` sobre
    # una imagen de 100 px y pasa.
    #
    # `AlmacenLocal.dimensiones()` medía de verdad con PIL desde la tanda 1 y
    # **no tenía un solo caller** — la pieza estaba construida y desconectada.
    # No se usa ésa porque toma un `storage_ref` y acá todavía no se guardó
    # nada: se mide sobre los bytes ya decodificados, que además es más honesto
    # (valida lo mismo que se va a escribir).
    #
    # NO rechaza cuando no puede medir. Un PDF adjunto no tiene píxeles, y un
    # traspaso de custodia no se puede trabar a las 5 a.m. porque PIL no supo
    # abrir un archivo: ante «no sé», se conserva lo declarado y sigue el
    # camino de validación de siempre.
    #
    # Para el PWA de hoy esto es no-op: `flotaComprimir` dibuja en un canvas de
    # w×h y declara esos mismos w×h, así que declarado == medido (verificado
    # generando JPEG de 1600×1200, 800×600, 1600×900 y 1200×1600).
    medido = _medir_dimensiones(contenido)
    if medido is not None:
        if (campos['ancho'], campos['alto']) != (medido['ancho'], medido['alto']):
            logger.warning(
                '[FLOTA] foto %s: el cliente declaró %sx%s y el archivo mide '
                '%sx%s — se guarda lo medido',
                datos.get('angulo') or clase.value,
                campos['ancho'], campos['alto'], medido['ancho'], medido['alto'])
        campos.update(medido)
    # El mime que manda a la validación es el del contenido, no el declarado en
    # el JSON: si el cliente dijera 'image/jpeg' y subiera otra cosa, la fila
    # afirmaría un formato que el archivo no tiene.
    validar_formato(clase, mime, campos['ancho'], campos['alto'])
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


def colgar_fotos(fotos, entidad_tipo, entidad_id, autor_id, ahora):
    """Ata cada foto a su padre y GUARDA EL ARCHIVO. Devuelve las filas creadas.

    Un archivo sin padre es un bug (regla 7); un padre sin archivo es evidencia
    falsa, que es peor. El binario se escribe en el almacén y la fila queda con
    la referencia y el hash REALES. Si el almacén falla, `guardar_foto` devuelve
    la fila marcada `pendiente_evidencia` — nunca un hash de ceros.

    **Vive acá y no en cada adaptador.** Nació en `traspaso.py` y el 2026-09-01
    se copió a `hallazgos.py` con el cuerpo idéntico. Regla 0 del WMS, corolario:
    el mismo concepto implementado dos veces divergió en tres horas la vez que
    pasó de verdad. Acá lo que divergiría es qué se considera evidencia guardada
    — y la copia que se quede atrás es la que va a decir «foto ok» sobre un
    archivo que no se escribió.

    No se importa desde `traspaso` para no crear una dependencia entre dos
    adaptadores hermanos: la política de fotos es del almacén, no del traspaso.
    """
    from app.extensions import db
    from flota.adaptadores.modelos import Foto

    creadas = []
    for f in fotos or []:
        campos = guardar_foto(f)
        fila = Foto(
            entidad_tipo=entidad_tipo, entidad_id=entidad_id,
            ts_captura=f['ts_captura'] if 'ts_captura' in f else ahora,
            gps_lat=f['gps_lat'] if 'gps_lat' in f else None,
            gps_lon=f['gps_lon'] if 'gps_lon' in f else None,
            autor_usuario_id=autor_id,
            **campos,
        )
        db.session.add(fila)
        creadas.append(fila)
    return creadas


__all__ = ['AlmacenLocal', 'ErrorAlmacen', 'desde_data_url', 'guardar_foto',
           'colgar_fotos']
