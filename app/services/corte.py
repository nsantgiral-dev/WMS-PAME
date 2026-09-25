"""
¿Desde cuándo mira la analítica? — `FECHA_INICIO_AUDITORIA`. **Una variable,
una función.**

## El problema

La analítica, la auditoría de invariantes, la salud del dato y el tablero
mostraban como «errores» cosas que no son de hoy: jobs FALLIDO del ensayo de
abril, conteos abiertos de una prueba, rutas de QA que nadie iba a liquidar.
Un canal donde la mitad de lo que grita es viejo entrena a no mirarlo — la
lección de los 639 avisos conocidos.

Borrarlo no es la salida: la bitácora, el KPI diario y los eventos de agotado
son tablas protegidas del acta de corte, y lo operativo viejo es evidencia.

## La regla

`FECHA_INICIO_AUDITORIA` (ISO, hora de **Bogotá**) marca desde cuándo lo que
se registra es operación real:

    2026-10-01            → 2026-10-01 00:00 Bogotá (05:00 UTC)
    2026-10-01T06:00      → 06:00 Bogotá
    2026-10-01T11:00Z     → con zona explícita, se respeta

Lo anterior **no se borra y no desaparece**: cada lector lo cuenta aparte
(`antes_del_corte`) y **no sube el nivel ni el veredicto**. Sin variable no
hay corte, y eso también se declara (`estado()` va en el `meta` de cada vista:
«sin corte» no puede leerse igual que «corte en tal fecha»).

Una variable inválida (texto, fecha imposible) **no corta nada** y se declara
inválida: cortar con una fecha adivinada escondería errores reales (Regla 0 —
el lado conservador es mostrar de más).

Un registro **sin fecha** cuenta como vigente, no como anterior: no saber
cuándo pasó no lo vuelve viejo.

Trinquete: `tests/test_analitica_solo_lo_actual.py` — nadie más lee la
variable, todo invariante declara la fecha de su entidad, y cada lector de la
lista `LECTORES_CON_CORTE` pasa por acá.
"""
import logging
import os
from datetime import date, datetime, timedelta, timezone

from app.utils.fecha import TZ_BOGOTA, dia_operativo_de

logger = logging.getLogger(__name__)

VARIABLE = 'FECHA_INICIO_AUDITORIA'


def _parsear(crudo: str):
    """`(utc_naive, motivo)`. `utc_naive=None` con motivo si no se puede leer."""
    texto = (crudo or '').strip()
    if not texto:
        return None, None
    try:
        if len(texto) == 10:
            local = datetime.combine(date.fromisoformat(texto), datetime.min.time())
        else:
            local = datetime.fromisoformat(texto.replace('Z', '+00:00'))
    except ValueError:
        return None, f'«{texto}» no es una fecha ISO (AAAA-MM-DD o AAAA-MM-DDTHH:MM)'
    if local.tzinfo is None:
        local = local.replace(tzinfo=TZ_BOGOTA)
    return local.astimezone(timezone.utc).replace(tzinfo=None), None


def _leer() -> dict:
    crudo = os.environ.get(VARIABLE, '')
    utc, motivo = _parsear(crudo)
    return {'crudo': crudo.strip() or None, 'utc': utc, 'motivo': motivo}


def inicio_auditoria():
    """El corte en UTC naive (el marco de las columnas), o `None`: sin
    variable, o inválida (declarada en `estado()`)."""
    return _leer()['utc']


def dia_de_corte():
    """El día Bogotá del corte. Para lectores por día: el día del corte cuenta
    entero como vigente (mostrar de más, no de menos)."""
    corte = inicio_auditoria()
    return dia_operativo_de(corte) if corte is not None else None


def es_anterior(fecha, corte=None) -> bool:
    """¿`fecha` es anterior al corte?

    · `datetime` → UTC naive (como las columnas); con zona, se convierte.
    · `date` → día Bogotá: anterior si es anterior al día del corte.
    · `None` → **False**: sin fecha no se sabe que sea vieja (Regla 0).

    `corte` permite comparar contra otra frontera con la misma regla (lo usa
    `vigente_desde` de los invariantes)."""
    if corte is None:
        corte = inicio_auditoria()
    if corte is None or fecha is None:
        return False
    if isinstance(fecha, datetime):
        if fecha.tzinfo is not None:
            fecha = fecha.astimezone(timezone.utc).replace(tzinfo=None)
        return fecha < corte
    if isinstance(fecha, date):
        return fecha < dia_operativo_de(corte)
    return False


def recortar_rango(desde, hasta):
    """Un rango de días Bogotá llevado al corte.

    Devuelve `(desde_efectivo, hasta, info)`. `desde_efectivo` puede quedar
    después de `hasta` (todo el rango es anterior): el lector devuelve vacío y
    `info` dice por qué. `info['antes']` es el tramo recortado
    `(desde, dia_corte − 1)` o `None`, para quien quiera contarlo aparte.
    """
    dia = dia_de_corte()
    info = {'aplicado': False, 'dia_corte': dia.isoformat() if dia else None,
            'antes': None}
    if dia is None or desde is None or desde >= dia:
        return desde, hasta, info
    fin_antes = min(hasta, dia - timedelta(days=1)) if hasta is not None else dia - timedelta(days=1)
    info.update(aplicado=True, antes=(desde, fin_antes),
                dias_antes_del_corte=(fin_antes - desde).days + 1)
    return dia, hasta, info


def separar(filas, fecha_de):
    """`(vigentes, anteriores)` según `fecha_de(fila)`. Para los lectores que
    ya trajeron las filas y tienen que contar aparte lo viejo."""
    corte = inicio_auditoria()
    vigentes, anteriores = [], []
    for f in filas:
        (anteriores if es_anterior(fecha_de(f), corte) else vigentes).append(f)
    return vigentes, anteriores


def estado() -> dict:
    """Lo que va en el `meta` de cada vista. Sin variable → `configurado:
    False`, y el texto lo dice: «sin corte» es un dato, no un silencio."""
    leido = _leer()
    corte = leido['utc']
    base = {'variable': VARIABLE, 'crudo': leido['crudo'],
            'configurado': leido['crudo'] is not None,
            'valido': corte is not None,
            'fecha_inicio_utc': corte.isoformat() if corte else None,
            'dia_bogota': dia_operativo_de(corte).isoformat() if corte else None,
            'futuro': bool(corte and corte > datetime.utcnow())}
    if leido['motivo']:
        base['texto'] = (f'{VARIABLE} inválida ({leido["motivo"]}): no se aplica ningún '
                         'corte — se muestra todo, también lo del ensayo.')
    elif corte is None:
        base['texto'] = (f'Sin {VARIABLE}: se muestra todo lo registrado, también lo '
                         'anterior a la operación real.')
    elif base['futuro']:
        base['texto'] = (f'El corte ({base["dia_bogota"]}) todavía no llegó: todo lo de '
                         'hoy cuenta como anterior al corte.')
    else:
        base['texto'] = (f'Solo lo registrado desde el {base["dia_bogota"]} (Bogotá) sube el '
                         'nivel; lo anterior se cuenta aparte.')
    return base
