"""**¿De qué bodega es esta persona?** — una sola respuesta.

## Por qué existe

La pertenencia de un usuario a una sede se expresa hoy de **dos formas que
nadie concilia**:

  · los usuarios de los puntos de venta llevan `bodega_siesa_id` y tienen
    `almacen_id` en NULL — la pantalla de alta ni siquiera envía ese campo;
  · los del CD llevan `almacen_id`, y su bodega vive en la fila del almacén.

Consecuencia medida (2026-09-17, producción): de 27 usuarios, los 13 de puntos
tienen `almacen_id` NULL y los 5 de NB1 tienen `bodega_siesa_id` NULL.
**Cualquier guard que mire un solo campo no ve a la mitad de la gente.** Y los
guards que existen están escritos como `if u.almacen_id and ...`: con el campo
vacío no se evalúan y **dejan pasar**.

## El contrato, y en qué se aparta del precedente

`tienda_oc._validar_recepcion_tienda` es el único acotamiento de escritura que
el repo tenía, y **falla ABIERTO**: `if almacen and almacen.bodega != ...`, o
sea que un recurso sin almacén pasa. Acá es al revés y a propósito:

    sin bodega resoluble → NO pertenece.

Un permiso que no se puede comprobar no se concede. Es la Regla 0 aplicada a
autorización: ante dato ausente, fallar hacia el lado conservador.

## Lo que este módulo NO hace

No cambia el alcance de nada que ya funcione. Es una función nueva que los
guards nuevos usan; los 333 endpoints existentes siguen exactamente igual.
Acotar el sistema entero es otro trabajo y no se hace de polizón en este.
"""
import logging

logger = logging.getLogger(__name__)


def bodega_del_usuario(usuario) -> str | None:
    """La bodega Siesa a la que pertenece `usuario`, o `None` si no se sabe.

    Resuelve los dos mecanismos: el campo directo del usuario y, si está
    vacío, la bodega del almacén al que pertenece. `None` significa
    literalmente «no se puede saber», no «cualquiera».
    """
    if usuario is None:
        return None

    directa = (getattr(usuario, 'bodega_siesa_id', None) or '').strip()
    if directa:
        return directa

    almacen = getattr(usuario, 'almacen', None)
    if almacen is None and getattr(usuario, 'almacen_id', None):
        from app.models.almacen import Almacen
        from app.extensions import db
        almacen = db.session.get(Almacen, usuario.almacen_id)

    por_almacen = (getattr(almacen, 'bodega_siesa_id', None) or '').strip()
    return por_almacen or None


def usuario_es_de_la_bodega(usuario, bodega: str) -> bool:
    """¿`usuario` pertenece a `bodega`? **Falla cerrado.**

    Sin bodega resoluble en el usuario, o sin bodega en el recurso, la
    respuesta es NO. Un permiso que no se puede comprobar no se concede.
    """
    objetivo = (bodega or '').strip()
    if not objetivo:
        return False
    propia = bodega_del_usuario(usuario)
    return bool(propia) and propia == objetivo


def bodega_de_la_recepcion(recepcion) -> str | None:
    """La bodega Siesa de una recepción, vía su almacén.

    Separada a propósito: la recepción guarda `almacen_id`, no la bodega, así
    que la traducción vive en un solo sitio en vez de repetirse en cada guard.
    """
    if recepcion is None:
        return None
    almacen = getattr(recepcion, 'almacen', None)
    if almacen is None and getattr(recepcion, 'almacen_id', None):
        from app.models.almacen import Almacen
        from app.extensions import db
        almacen = db.session.get(Almacen, recepcion.almacen_id)
    return (getattr(almacen, 'bodega_siesa_id', None) or '').strip() or None
