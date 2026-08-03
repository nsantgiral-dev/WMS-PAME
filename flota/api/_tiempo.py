"""
Cómo sale una hora de flota hacia la pantalla.

El módulo guarda `datetime.utcnow()`: naive, en UTC. `.isoformat()` sobre eso
produce `2026-08-03T20:14:41` — una hora **que no dice de qué zona es**, y
JavaScript la interpreta como hora local del teléfono. En Colombia eso corre el
reloj cinco horas: un recibo de turno hecho a las 15:00 se mostraba como 20:00
(reportado 2026-08-03, con el recibo del TGZ653 a la vista).

No es un problema de formato. Es la regla 4 aplicada al tiempo: un dato que
puede malinterpretarse tiene que decir lo que es. Una hora sin zona es un número
que parece correcto y está mal, que es la peor clase de dato — y acá el punto
entero del registro es que la hora sea confiable frente a un tercero.

La conversión a hora de Colombia se hace **en la pantalla**, no acá: la base y la
API hablan UTC (una sola verdad, comparable entre zonas), y quien muestra
traduce. Lo que cambia es que ahora la API lo declara.
"""
from datetime import datetime, timezone
from typing import Optional


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serializa un datetime naive-en-UTC declarando su zona.

    `None` sigue siendo `None`: una custodia sin cerrar no tiene `fin_ts`, y eso
    es un hecho, no un dato faltante que haya que rellenar.

    Un datetime que ya trae `tzinfo` se respeta y se normaliza a UTC — así el
    día que algo empiece a guardar con zona, esto no la pisa.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


__all__ = ['iso_utc']
