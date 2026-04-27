"""
Monitor de latencia logística para traslados EN_TRANSITO.

Arquitectura:
  Un cron cada hora revisa la tabla solicitudes_traslado buscando registros
  que llevan demasiado tiempo en EN_TRANSITO (mercancía en el limbo).

  Umbrales:
    > 12h → alerta preventiva en Dashboard (flag visual)
    > 24h → alerta crítica (log ERROR — email/webhook en versión futura)

  El WMS asume el estrés logístico en la sombra y alerta solo cuando hay riesgo
  real de pérdida de inventario. Zero ruido a Siesa.

Cron: cada hora.
Endpoint: GET /api/traslados/alertas-transito (para dashboard).
"""
import logging
from datetime import datetime, timedelta
from app.extensions import db
from app.models.traslado import SolicitudTraslado

logger = logging.getLogger(__name__)

UMBRAL_ALERTA_H = 12    # horas → flag visual en dashboard
UMBRAL_CRITICO_H = 24   # horas → log crítico (futuro: webhook/email)


def _traslados_en_riesgo():
    """
    Retorna traslados EN_TRANSITO que superan los umbrales de latencia.
    Consulta eficiente — solo dos filtros sobre la tabla.
    """
    ahora = datetime.utcnow()
    limite_alerta = ahora - timedelta(hours=UMBRAL_ALERTA_H)
    limite_critico = ahora - timedelta(hours=UMBRAL_CRITICO_H)

    traslados = (
        SolicitudTraslado.query
        .filter_by(estado='EN_TRANSITO')
        .filter(SolicitudTraslado.fecha_despacho <= limite_alerta)
        .order_by(SolicitudTraslado.fecha_despacho.asc())
        .all()
    )

    resultado = {
        'alertas': [],      # 12h - 24h
        'criticos': [],     # > 24h
        'total_alerta': 0,
        'total_critico': 0,
    }

    for t in traslados:
        horas_transito = (ahora - t.fecha_despacho).total_seconds() / 3600
        entrada = {
            'id': t.id,
            'codigo': t.codigo,
            'nombre_punto_venta': t.nombre_punto_venta,
            'bodega_destino': t.bodega_destino_siesa,
            'fecha_despacho': t.fecha_despacho.isoformat(),
            'horas_en_transito': round(horas_transito, 1),
            'total_items': len(t.items),
        }
        if t.fecha_despacho <= limite_critico:
            resultado['criticos'].append(entrada)
        else:
            resultado['alertas'].append(entrada)

    resultado['total_alerta'] = len(resultado['alertas'])
    resultado['total_critico'] = len(resultado['criticos'])
    return resultado


def check_pending_transfers(app):
    """
    Job horario: detecta traslados estancados y emite logs de alerta.
    Los críticos (> 24h) loggean como ERROR para que Railway los capture.
    """
    with app.app_context():
        try:
            # [A13] Advisory lock — evita ejecuciones concurrentes (scheduler + API manual)
            _lock_acquired = db.session.execute(
                db.text('SELECT pg_try_advisory_lock(2010)')
            ).scalar()
            if not _lock_acquired:
                logger.info('[TRASLADO_MONITOR] Lock no disponible — omitiendo ejecución concurrente')
                return

            resumen = _traslados_en_riesgo()

            for t in resumen['criticos']:
                logger.error(
                    f'[TRASLADO_MONITOR] CRÍTICO — {t["codigo"]} lleva '
                    f'{t["horas_en_transito"]}h EN_TRANSITO hacia '
                    f'{t["nombre_punto_venta"]} ({t["bodega_destino"]}). '
                    f'Inventario en riesgo — verificar inmediatamente.'
                )

            for t in resumen['alertas']:
                logger.warning(
                    f'[TRASLADO_MONITOR] ALERTA — {t["codigo"]} lleva '
                    f'{t["horas_en_transito"]}h EN_TRANSITO hacia {t["nombre_punto_venta"]}.'
                )

            if resumen['total_critico'] + resumen['total_alerta'] > 0:
                logger.info(
                    f'[TRASLADO_MONITOR] Resumen: {resumen["total_critico"]} críticos '
                    f'(>24h), {resumen["total_alerta"]} alertas (>12h)'
                )
        except Exception as e:
            logger.error(f'[TRASLADO_MONITOR] Error en check: {e}')
        finally:
            try:
                db.session.execute(db.text('SELECT pg_advisory_unlock(2010)'))
                db.session.commit()
            except Exception:
                pass


def get_resumen_alertas():
    """
    Para el endpoint del dashboard — devuelve conteos sin levantar una nueva app_context.
    Se llama desde dentro de un request Flask (app_context ya activo).
    """
    try:
        return _traslados_en_riesgo()
    except Exception as e:
        logger.error(f'[TRASLADO_MONITOR] Error get_resumen_alertas: {e}')
        return {'alertas': [], 'criticos': [], 'total_alerta': 0, 'total_critico': 0}


def init_scheduler(app):
    """Cron horario — revisa traslados estancados."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error('[TRASLADO_MONITOR] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=check_pending_transfers,
        trigger=IntervalTrigger(hours=1),
        kwargs={'app': app},
        id='monitor_traslados_transito',
        name='Monitor latencia traslados EN_TRANSITO',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info('[TRASLADO_MONITOR] Scheduler iniciado — revisión cada hora')
    return scheduler
