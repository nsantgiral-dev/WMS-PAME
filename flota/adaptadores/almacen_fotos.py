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

from flota.dominio.fotos import LADO_LARGO_MINIMO_FOTO_DATO, validar_formato
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


class AlmacenNoConfigurado(ErrorAlmacen):
    """`FLOTA_FOTOS_DIR` falta o no es una ruta absoluta en ESTE proceso. No es
    un problema de la foto: es de la configuración del servicio."""


class ArchivoAusente(ErrorAlmacen):
    """La fila dice que hay foto y el archivo no está (o no es el que se
    guardó) bajo la raíz de este proceso."""


def _raiz() -> Path:
    """Carpeta raíz del almacén. Sin default silencioso.

    Si `FLOTA_FOTOS_DIR` no está, esto **levanta**. Un default a `/tmp` haría que
    las fotos se guarden y desaparezcan en el próximo deploy, que es peor que no
    guardarlas: en vez de un hueco visible da una evidencia que se evapora.

    En Railway apunta al volumen montado. En local, a cualquier carpeta.
    """
    d = os.getenv('FLOTA_FOTOS_DIR')
    if d is None or not d.strip():
        raise AlmacenNoConfigurado(
            'FLOTA_FOTOS_DIR no está configurada. Sin almacén, una foto '
            'guardada es una evidencia que no existe — el registro queda en '
            'pendiente_evidencia y el health lo cuenta.'
        )
    raiz = Path(d.strip())
    # Relativa, se resuelve contra el directorio de trabajo de CADA proceso:
    # el que escribe y el que sirve pueden mirar dos carpetas distintas y la
    # foto «guardada» no aparece nunca (2026-09-25).
    if not raiz.is_absolute():
        raise AlmacenNoConfigurado(
            f'FLOTA_FOTOS_DIR={d.strip()!r} no es una ruta absoluta: cada proceso '
            f'la resolvería contra su propio directorio de trabajo.')
    return raiz


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
            # Direccionado por contenido: si ya está Y es este contenido, no se
            # reescribe. Uno que está con otro contenido (truncado, corrupto) se
            # reemplaza.
            if not (destino.is_file() and _sha_de_archivo(destino) == digest):
                with open(destino, 'wb') as fh:
                    fh.write(contenido)
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError as e:
            raise ErrorAlmacen(f'no se pudo escribir {relativa}: {e}') from e
        # **«ok» solo con el archivo releído** (2026-09-25). La fila decía `ok`
        # apenas `write_bytes` volvía; si el archivo no quedó donde el que
        # sirve lo va a buscar, la evidencia no existe y nadie se entera hasta
        # el 410. Releer y comparar el hash es lo mínimo que se puede afirmar
        # desde este proceso.
        if _sha_de_archivo(destino) != digest:
            raise ErrorAlmacen(f'{relativa} se escribió y al releerlo no es la misma foto')
        return str(relativa)

    def existe(self, storage_ref: str, bytes_esperados=None) -> bool:
        """¿El archivo está bajo la raíz de este proceso (y con su tamaño)?
        Levanta `AlmacenNoConfigurado` si este proceso no tiene almacén."""
        destino = _raiz() / str(storage_ref or '')
        if not storage_ref or not destino.is_file():
            return False
        return bytes_esperados in (None, 0) or destino.stat().st_size == bytes_esperados

    def leer(self, storage_ref: str) -> bytes:
        """Devuelve el archivo. Levanta si no está — no un placeholder."""
        destino = _raiz() / storage_ref
        if not destino.is_file():
            raise ArchivoAusente(f'la foto {storage_ref} no está en el almacén')
        return destino.read_bytes()

    def dimensiones(self, storage_ref: str) -> Dict[str, int]:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(self.leer(storage_ref))) as img:
            return {'ancho': img.width, 'alto': img.height}


def _sha_de_archivo(ruta: Path):
    try:
        return hashlib.sha256(ruta.read_bytes()).hexdigest()
    except OSError:
        return None


#: Lo que dice `estado_verificable` además de los dos estados de la fila.
SIN_ARCHIVO = 'sin_archivo'
ALMACEN_SIN_CONFIGURAR = 'almacen_sin_configurar'


def estado_verificable(foto) -> str:
    """El estado de una foto **como se puede afirmar hoy**: `ok` solo si la
    fila dice `ok` Y el archivo está bajo la raíz de este proceso con su
    tamaño. Si no, `sin_archivo` (la fila miente) o `almacen_sin_configurar`
    (este proceso no puede mirar). Una política para toda lista de fotos."""
    if foto.estado != 'ok':
        return foto.estado
    try:
        return 'ok' if AlmacenLocal().existe(foto.storage_ref, foto.bytes) else SIN_ARCHIVO
    except AlmacenNoConfigurado:
        return ALMACEN_SIN_CONFIGURAR


def diagnostico_almacen(muestra: int = 200) -> dict:
    """¿Dónde guarda este proceso las fotos y están ahí las que la base dice?

    Contesta lo que el 410 de QA preguntaba sin poder mirar Railway:
    · `raiz`/`absoluta`/`existe`/`escribible`: la carpeta de ESTE proceso;
    · `mismo_disco_que_el_contenedor`: si la carpeta está en el mismo
      dispositivo que `/`, **no es un volumen montado** y lo que se escriba se
      pierde en el próximo deploy (así se ve «ok» en la base y 410 después);
    · `muestra`: las últimas N fotos `ok` de la base, y cuántas no tienen el
      archivo acá.
    Lo que no se puede escribir lo dice; lo que revienta, levanta."""
    out = {'raiz': os.getenv('FLOTA_FOTOS_DIR'), 'configurada': False, 'absoluta': None,
           'existe': None, 'escribible': None, 'mismo_disco_que_el_contenedor': None,
           'fotos_ok_revisadas': None, 'fotos_ok_sin_archivo': None, 'ejemplos_sin_archivo': []}
    try:
        raiz = _raiz()
    except AlmacenNoConfigurado as e:
        out['problema'] = str(e)
        return out
    out.update(configurada=True, absoluta=True, existe=raiz.is_dir())
    if out['existe']:
        try:
            prueba = raiz / f'.prueba-{os.getpid()}'
            prueba.write_bytes(b'x')
            prueba.unlink()
            out['escribible'] = True
        except OSError as e:
            out['escribible'] = False
            out['problema'] = f'no se puede escribir en {raiz}: {e}'
        out['mismo_disco_que_el_contenedor'] = os.stat(raiz).st_dev == os.stat('/').st_dev
    # Sin try: si la muestra revienta, el health devuelve error — un health
    # que responde ceros porque algo falló adentro es evidencia falsa.
    from flota.adaptadores.modelos import Foto
    filas = (Foto.query.filter(Foto.estado == 'ok')
             .order_by(Foto.id.desc()).limit(muestra).all())
    faltan = [f.id for f in filas if estado_verificable(f) == SIN_ARCHIVO]
    out.update(fotos_ok_revisadas=len(filas), fotos_ok_sin_archivo=len(faltan),
               ejemplos_sin_archivo=faltan[:10])
    return out


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
    # Una `foto_dato` por debajo del mínimo SE GUARDA, pero declarada rota.
    #
    # La pantalla lo promete desde la tanda 1 —«por debajo de 1600 px: queda
    # como pendiente_evidencia»— y nadie lo escribía: la fila salía `ok`, el
    # CHECK `ck_flota_foto_dato_resolucion` la rechazaba al hacer commit, y el
    # recibo entero terminaba en un 500. Un tablero fotografiado con la cámara
    # en baja resolución dejaba el camión sin turno. Se declara acá, sobre lo
    # MEDIDO, que es lo que el CHECK juzga.
    if (clase == ClaseFoto.FOTO_DATO and campos['estado'] == 'ok'
            and campos['ancho'] and campos['alto']
            and max(campos['ancho'], campos['alto']) < LADO_LARGO_MINIMO_FOTO_DATO):
        campos['estado'] = 'pendiente_evidencia'
    return campos


def validar_fotos(fotos) -> None:
    """Rechaza ANTES de escribir nada una foto que la base no va a aceptar.

    La clase del defecto (QA e2e 2026-09-24): *un valor que un CHECK de la base
    rechaza llega al commit sin validarse antes*. `angulo: 'lateral_izquierda'`
    explotaba en `ck_flota_angulo` → 500, y la cola del conductor reintenta todo
    5xx: el ítem no salía nunca y trababa los que venían detrás. Acá se valida
    contra el MISMO vocabulario del CHECK (`ANGULO_FOTO`, `ClaseFoto`) y se
    levanta `FotoInvalida`, que la frontera traduce a 400.
    """
    from flota.adaptadores.modelos import ANGULO_FOTO
    from flota.dominio.errores import FotoInvalida
    from flota.dominio.valores import ClaseFoto

    if fotos is None:
        return
    if not isinstance(fotos, (list, tuple)):
        raise FotoInvalida('las fotos van en una lista')
    clases = {c.value for c in ClaseFoto}
    for i, f in enumerate(fotos, 1):
        if not isinstance(f, dict):
            raise FotoInvalida(f'la foto {i} no es un objeto')
        clase = f['clase'] if 'clase' in f else None
        if clase not in clases:
            raise FotoInvalida(f'la foto {i} tiene una clase desconocida: {clase!r}')
        angulo = f['angulo'] if 'angulo' in f else None
        if angulo is not None and angulo not in ANGULO_FOTO:
            raise FotoInvalida(
                f'la foto {i} tiene un ángulo desconocido: {angulo!r}. '
                f'Ángulos válidos: {", ".join(ANGULO_FOTO)}')


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

    # Todas antes que ninguna: una inválida en el medio no deja archivos
    # escritos de las anteriores.
    validar_fotos(fotos)
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


__all__ = ['AlmacenLocal', 'ErrorAlmacen', 'AlmacenNoConfigurado', 'ArchivoAusente',
           'desde_data_url', 'guardar_foto', 'colgar_fotos', 'validar_fotos',
           'estado_verificable', 'diagnostico_almacen', 'SIN_ARCHIVO', 'ALMACEN_SIN_CONFIGURAR']
