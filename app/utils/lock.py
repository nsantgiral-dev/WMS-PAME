"""
Advisory locks de PostgreSQL, tomados y **siempre** liberados.

Los locks de sesión de PostgreSQL están atados a la CONEXIÓN, no a la
transacción. Con un pool de conexiones eso significa que un lock que no se
libera **vuelve al pool tomado**: la próxima corrida del job pide el lock, se lo
niegan, y el job deja de ejecutarse.

Y no da error. `pg_try_advisory_lock` devuelve `false` y el código hace
`return` — que es exactamente lo que tiene que hacer cuando otro worker lo tiene.
El job simplemente no vuelve a correr nunca, en silencio. **Un invariante
ausente no falla: deja pasar.**

Auditoría del 2026-08-04: 16 sitios toman advisory locks, 14 los liberan en un
`finally` y 2 no los liberaban nunca:

  · `abc_service._liberar_zombis` (2015) — la liberación de tareas zombi. Sin
    ella, las tareas quedan EN_PROCESO para siempre y el operario no puede
    retomarlas.
  · `reconciliacion_service._ejecutar_sweep` (2014) — el barrido que detecta
    tareas de packing que Siesa YA procesó y el WMS cree que no. Sin él, esas
    tareas quedan sin reconciliar y el inventario diverge del ERP.

Los 14 correctos lo hacen a mano, cada uno con su propio `try/finally`. Este
módulo existe para que el 17.º no dependa de que alguien se acuerde.

    with advisory_lock(2015) as tomado:
        if not tomado:
            return          # otro worker lo tiene: no es un error
        ...trabajo...
"""
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def advisory_lock(clave: int, etiqueta: str = ''):
    """Toma un advisory lock de sesión y lo libera pase lo que pase.

    Rinde `True` si lo consiguió, `False` si otro proceso lo tiene. **No levanta
    cuando no lo consigue**: que otro worker esté corriendo el mismo job es el
    caso normal en un despliegue con varios workers de Gunicorn, no una falla.

    La liberación va en `finally` y se traga su propio error a propósito: si el
    trabajo levantó, el error que importa es el del trabajo, no el del unlock.
    Pero se registra — un unlock que falla deja el pool envenenado y alguien
    tiene que poder verlo.
    """
    from sqlalchemy import text

    from app.extensions import db

    nombre = etiqueta or str(clave)
    tomado = db.session.execute(
        text('SELECT pg_try_advisory_lock(:k)'), {'k': clave}).scalar()
    try:
        yield bool(tomado)
    finally:
        if tomado:
            try:
                db.session.execute(
                    text('SELECT pg_advisory_unlock(:k)'), {'k': clave})
                db.session.commit()
            except Exception as e:
                # Ruidoso: la conexión vuelve al pool con el lock puesto y el
                # job no va a volver a correr. Sin este log, el sintoma es
                # "hace días que no pasa nada" y nadie sabe por qué.
                logger.error(
                    '[LOCK] no se pudo liberar el advisory lock %s: %s', nombre, e)


__all__ = ['advisory_lock']
