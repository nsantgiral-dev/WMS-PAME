"""
El kardex se descarga y se reconstruye solo — si alguien lo enciende.

Hasta el 2026-09-24 nada actualizaba el kardex: se descargaba cuando alguien
pulsaba «Descargar» (`salud_kardex` lo declaraba: `actualizacion_automatica:
False`). Este módulo es el disparo programado. **No toca la descarga ni la
reconstrucción**: las llama, con las mismas guardas que la pantalla.

## Reglas

- **Nace apagado** (`KARDEX_AUTO=true` lo enciende).
- **Ventana**: `KARDEX_AUTO_VENTANA` (`HH:MM-HH:MM`, Bogotá; default
  `07:00-07:55`), recortada a la ventana de Siesa (7:00–19:30, Regla 14). La
  descarga son miles de peticiones contra el ERP que factura en los puntos de
  venta: la primera hora, antes de que abran las tiendas. **Cada corrida dura
  como mucho lo que queda de ventana** (y nunca más que `KARDEX_MAX_MINUTOS`).
- **Lock del registro** (`LOCK_KARDEX_AUTO`).
- **Respeta `estado_descarga`**: con una descarga en curso no arranca; una
  INTERRUMPIDA (deploy a mitad) se retoma desde la página 1 —lo guardado no se
  duplica—; una PARCIAL por tiempo se reanuda desde `reanudar_desde`; una
  COMPLETA de hoy no se repite.
- **Solo reconstruye sobre una descarga COMPLETA**, igual que la ruta
  (`/api/kardex/reconstruir` es deny-by-default). Nunca con `forzar`.
- Todo queda en `registros_sync` (tipo `kardex`), el mismo registro que lee la
  pantalla: una corrida del cron y una del botón son indistinguibles en su
  efecto y distinguibles en `origen`.

## Lo que NO arregla

- La consulta del kardex da **401 en Siesa QA** (permiso de la consulta en
  Siesa, no código). Encendido contra QA, cada corrida termina en error y lo
  deja escrito.
- La paginación de las consultas dinámicas **no es determinista**: si la del
  kardex tampoco lo es, ninguna descarga queda COMPLETA y el cron no
  reconstruye nunca. Eso es la verdad, no un falso negativo (ver CLAUDE.md,
  «Kardex: ¿funciona?»).
- Con ~17.000 páginas a ~3,4 s, la descarga completa son ~16 h: con una
  ventana de 55 min tarda ~18 días hábiles en completar una vuelta. Se declara
  en `estado()`; ampliar la ventana es decisión del dueño.
"""
import logging
import os
from datetime import datetime, time

from app.extensions import db

logger = logging.getLogger(__name__)

VENTANA_DEFAULT = (time(7, 0), time(7, 55))
DESDE_DEFAULT = '20240101'
#: Menos que esto de ventana no alcanza ni para una página con su commit.
MINUTOS_MINIMOS = 3


def encendido() -> bool:
    """`KARDEX_AUTO=true`. **Nace apagado.**"""
    return os.getenv('KARDEX_AUTO', 'false').strip().lower() == 'true'


def _hhmm(s):
    h, m = s.strip().split(':')
    return time(int(h), int(m))


def ventana() -> dict:
    """La ventana pedida y la efectiva (recortada a la de Siesa).

    Returns: {pedida: (ini, fin), efectiva: (ini, fin) | None, valida, problema}
    """
    from app.services.ventana_siesa import VENTANA as VENTANA_SIESA
    crudo = (os.getenv('KARDEX_AUTO_VENTANA') or '').strip()
    problema = None
    pedida = VENTANA_DEFAULT
    if crudo:
        try:
            a, b = crudo.split('-')
            pedida = (_hhmm(a), _hhmm(b))
            if pedida[0] >= pedida[1]:
                raise ValueError('el inicio no es anterior al fin')
        except (ValueError, TypeError) as e:
            problema = (f'KARDEX_AUTO_VENTANA={crudo!r} no se entiende ({e}): se '
                        f'usa la de siempre {VENTANA_DEFAULT[0]}–{VENTANA_DEFAULT[1]}')
            pedida = VENTANA_DEFAULT
    ini = max(pedida[0], VENTANA_SIESA[0])
    fin = min(pedida[1], VENTANA_SIESA[1])
    efectiva = (ini, fin) if ini < fin else None
    if efectiva is None:
        problema = (problema or '') + (' La ventana pedida cae fuera de la de Siesa '
                                       f'({VENTANA_SIESA[0]}–{VENTANA_SIESA[1]}): '
                                       'el cron no corre nunca.')
    elif efectiva != pedida:
        problema = (problema or '') + (f' Recortada a la ventana de Siesa: '
                                       f'{efectiva[0]}–{efectiva[1]}.')
    return {'pedida': pedida, 'efectiva': efectiva,
            'valida': problema is None, 'problema': (problema or '').strip() or None}


def fecha_desde() -> str:
    """`KARDEX_AUTO_DESDE` (AAAAMMDD) o el default de la ruta (20240101)."""
    v = (os.getenv('KARDEX_AUTO_DESDE') or '').strip()
    try:
        datetime.strptime(v, '%Y%m%d')
        return v
    except ValueError:
        return DESDE_DEFAULT


def descargar_con_registro(fecha_desde_: str, fecha_hasta: str = None,
                           pagina_inicial: int = 1, max_minutos: int = None,
                           origen: str = 'manual') -> dict:
    """Una descarga con su fila en `registros_sync`. **La única que llama a
    `descargar_kardex`** (la usan el botón y el cron): abrir y cerrar el
    registro en un solo sitio es lo que deja que `estado_descarga` distinga una
    corrida viva de una muerta."""
    from app.services import registro_sync_service as _reg
    from app.services.kardex_service import KardexService

    registro_id = _reg.abrir('kardex')
    try:
        resultado = KardexService.descargar_kardex(
            fecha_desde_, fecha_hasta, pagina_inicial=pagina_inicial,
            max_minutos=max_minutos)
        resultado = dict(resultado or {})
        resultado['origen'] = origen
        # `cerrar_ok` dice «el proceso terminó»; el veredicto de negocio
        # (COMPLETA/PARCIAL) va en `resultado.ok`, que es lo que lee la salud.
        _reg.cerrar_ok(registro_id, resultado)
        return resultado
    except Exception as e:
        db.session.rollback()
        _reg.cerrar_error(registro_id, str(e))
        logger.error('[KARDEX_AUTO] descarga (%s) falló: %s', origen, e)
        return {'ok': False, 'error': str(e)[:500], 'origen': origen}


def _pagina_de_reanudacion(est: dict) -> int:
    """Desde qué página seguir, según cómo terminó la última."""
    res = est.get('resultado') or {}
    if est.get('interrumpida') or res.get('ok') is True:
        return 1
    r = res.get('reanudar_desde')
    try:
        return max(1, int(r)) if r else 1
    except (TypeError, ValueError):
        return 1


def _completa_hoy(est: dict, hoy) -> bool:
    from app.utils.fecha import dia_operativo_de
    res = est.get('resultado') or {}
    fin = (est.get('registro') or {}).get('fin')
    if res.get('ok') is not True or not fin:
        return False
    try:
        return dia_operativo_de(datetime.fromisoformat(fin)) == hoy
    except (TypeError, ValueError):
        return False


def _reconstruir() -> dict:
    from app.services.kardex_service import KardexService
    try:
        r = KardexService.reconstruir_stock_diario(None) or {}
        return {'ok': True,
                'dias_generados': r.get('dias_generados'),
                'referencias_procesadas': r.get('referencias_procesadas'),
                'refs_sin_ancla': (r.get('refs_sin_ancla') or {}).get('cantidad')}
    except Exception as e:
        db.session.rollback()
        logger.error('[KARDEX_AUTO] reconstrucción falló: %s', e, exc_info=True)
        return {'ok': False, 'error': str(e)[:300]}


def ciclo(reloj=None) -> dict:
    """Una corrida del cron. `reloj()` devuelve la hora de Bogotá (tests)."""
    from app.services.kardex_service import estado_descarga, salud_kardex, tope_minutos
    from app.utils.fecha import ahora_bogota

    if not encendido():
        return {'omitido': 'KARDEX_AUTO no está en true — nace apagado'}
    v = ventana()
    if v['efectiva'] is None:
        return {'omitido': v['problema']}
    ahora = reloj() if reloj else ahora_bogota()
    ini, fin = v['efectiva']
    if not (ini <= ahora.time() < fin):
        return {'omitido': f'fuera de la ventana del kardex ({ini}–{fin} Bogotá)'}
    restantes = (datetime.combine(ahora.date(), fin)
                 - datetime.combine(ahora.date(), ahora.time())).total_seconds() / 60
    if restantes < MINUTOS_MINIMOS:
        return {'omitido': f'quedan {restantes:.0f} min de ventana: no alcanza'}

    est = estado_descarga()
    if est.get('error_lectura'):
        return {'omitido': 'no se pudo leer el registro de descargas: '
                           + str(est['error_lectura'])}
    if est.get('en_curso'):
        return {'omitido': 'hay una descarga en curso'}

    salida = {}
    if _completa_hoy(est, ahora.date()):
        salida['descarga'] = {'omitida': 'ya hay una descarga COMPLETA hoy'}
        completa = True
    else:
        pagina = _pagina_de_reanudacion(est)
        tope = max(1, min(tope_minutos(), int(restantes)))
        res = descargar_con_registro(fecha_desde(), None, pagina_inicial=pagina,
                                     max_minutos=tope, origen='cron')
        salida['descarga'] = {k: res.get(k) for k in (
            'ok', 'estado', 'error', 'total_descargados', 'reanudar_desde')}
        salida['descarga'].update({'desde_pagina': pagina, 'tope_minutos': tope})
        completa = res.get('ok') is True

    if completa:
        salud = salud_kardex()
        codigos = {p['codigo'] for p in salud.get('problemas', [])}
        if 'STOCK_DIARIO_ATRASADO' in codigos or 'omitida' not in salida['descarga']:
            salida['reconstruccion'] = _reconstruir()
        else:
            salida['reconstruccion'] = {'omitida': 'el stock diario ya cubre la descarga'}
    else:
        salida['reconstruccion'] = {
            'omitida': 'la descarga no quedó COMPLETA: reconstruir sobre un kardex '
                       'truncado inventa días sin movimiento'}
    return salida


def init_scheduler(app):
    """Cada 30 min (:02 y :32) de 7 a 19 h Bogotá; `ciclo` decide si está en su
    ventana. Nace apagado: sin `KARDEX_AUTO=true` no lee ni escribe nada."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[KARDEX_AUTO] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            from app.utils.lock import LOCK_KARDEX_AUTO, advisory_lock
            with advisory_lock(LOCK_KARDEX_AUTO, 'kardex_auto') as tomado:
                if not tomado:
                    logger.info('[KARDEX_AUTO] otro worker ya corre el kardex')
                    return
                try:
                    logger.info('[KARDEX_AUTO] %s', ciclo())
                except Exception as e:
                    db.session.rollback()
                    logger.error('[KARDEX_AUTO] falló: %s', e, exc_info=True)

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido  # P1-11
    from app.services.ventana_siesa import solo_en_ventana_siesa  # P2
    scheduler.add_job(func=con_latido('kardex_auto', solo_en_ventana_siesa(_job)),
                      trigger=CronTrigger(hour='7-19', minute='2,32',
                                          timezone='America/Bogota'),
                      id='kardex_auto', replace_existing=True,
                      max_instances=1, misfire_grace_time=600)
    scheduler.start()
    logger.info('[KARDEX_AUTO] Scheduler (encendido=%s, ventana=%s)',
                encendido(), ventana()['efectiva'])
    return scheduler


def estado() -> dict:
    """Para `/api/health/siesa`. Nunca levanta."""
    try:
        from app.services.kardex_service import estado_descarga, tope_minutos
        v = ventana()
        est = estado_descarga()
        res = est.get('resultado') or {}
        minutos = 0
        if v['efectiva']:
            a, b = v['efectiva']
            minutos = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
        return {
            'encendido': encendido(),
            'ventana_pedida': [str(t) for t in v['pedida']],
            'ventana_efectiva': [str(t) for t in v['efectiva']] if v['efectiva'] else None,
            'problema_ventana': v['problema'],
            'minutos_por_dia': minutos,
            'tope_por_corrida_min': tope_minutos(),
            'desde': fecha_desde(),
            'ultima_descarga': {
                'en_curso': est.get('en_curso'),
                'interrumpida': est.get('interrumpida'),
                'ok': res.get('ok'),
                'estado': res.get('estado') or ('ERROR' if res.get('error') else None),
                'origen': res.get('origen'),
                'reanudar_desde': res.get('reanudar_desde'),
                'inicio_utc': (est.get('registro') or {}).get('inicio'),
            },
            'nota': ('Una vuelta completa del kardex son ~17.000 páginas (~16 h a '
                     '3,4 s/página). Con la ventana diaria de arriba tarda varios '
                     'días hábiles; cada corrida retoma donde quedó la anterior.'),
        }
    except Exception as e:
        return {'encendido': encendido(), 'error': str(e)[:200]}
