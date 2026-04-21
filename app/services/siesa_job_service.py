"""
Dead Letter Queue — procesador de jobs asíncronos hacia Siesa.

El scheduler llama a procesar_jobs_pendientes() cada 5 minutos.
Si Connekta rechaza (periodo cerrado, ítem bloqueado, timeout):
  - Reintento 1 → espera 5 min
  - Reintento 2 → espera 15 min
  - Reintento 3 → espera 45 min
  - Tras 3 fallos → estado=FALLIDO → alerta roja en dashboard admin

Tipos de job implementados:
  TRANSFERENCIA_UBICACIONES → conector 173076 (RESERVA → PICKING)
  (extensible: agregar nuevo tipo + handler en _ejecutar_job)
"""

import json
import logging
from datetime import datetime
from app.extensions import db
from app.models.siesa_job import SiesaJob

logger = logging.getLogger(__name__)


def encolar_transferencia_ubicaciones(
    bodega_id: str,
    ubicacion_origen: str,
    ubicacion_destino: str,
    referencia_item: str,
    cantidad: int,
    nota: str = '',
    referencia_tipo: str = None,
    referencia_id: int = None,
) -> SiesaJob:
    """
    Encola una transferencia entre ubicaciones (conector 173076).
    El caller hace commit.
    """
    return SiesaJob.encolar(
        tipo='TRANSFERENCIA_UBICACIONES',
        payload={
            'bodega_id': bodega_id,
            'ubicacion_origen': ubicacion_origen,
            'ubicacion_destino': ubicacion_destino,
            'referencia_item': referencia_item,
            'cantidad': cantidad,
            'nota': nota,
        },
        referencia_tipo=referencia_tipo,
        referencia_id=referencia_id,
    )


def procesar_jobs_pendientes(app=None):
    """
    Procesa todos los jobs PENDIENTE cuyo proximo_intento <= ahora.
    El scheduler lo llama cada 5 minutos.
    """
    from flask import current_app
    _app = app or current_app._get_current_object()

    with _app.app_context():
        ahora = datetime.utcnow()

        q = SiesaJob.query.filter(
            SiesaJob.estado == 'PENDIENTE',
            db.or_(
                SiesaJob.proximo_intento.is_(None),
                SiesaJob.proximo_intento <= ahora,
            )
        ).limit(20)
        # skip_locked solo disponible en PostgreSQL — en SQLite lo ignoramos
        try:
            jobs = q.with_for_update(skip_locked=True).all()
        except Exception:
            jobs = q.all()

        if not jobs:
            return 0

        procesados = 0
        for job in jobs:
            job.estado = 'PROCESANDO'
            db.session.commit()  # lock en el registro

            try:
                resultado = _ejecutar_job(job)
                job.marcar_completado(resultado)
                db.session.commit()
                logger.info(f'[DLQ] Job {job.id} ({job.tipo}) completado — intento {job.intentos + 1}')
                procesados += 1

                # Actualizar referencia si aplica
                _post_completado(job)

            except Exception as e:
                error_msg = str(e)
                job.marcar_fallo(error_msg)
                db.session.commit()

                if job.estado == 'FALLIDO':
                    logger.error(
                        f'[DLQ] Job {job.id} ({job.tipo}) FALLIDO tras {job.intentos} intentos: {error_msg}'
                    )
                    _crear_alerta_admin(job)
                else:
                    logger.warning(
                        f'[DLQ] Job {job.id} ({job.tipo}) falló (intento {job.intentos}/'
                        f'{job.max_intentos}) — reintento en '
                        f'{_BACKOFF_LABELS[min(job.intentos - 1, 2)]}: {error_msg}'
                    )

        return procesados


_BACKOFF_LABELS = ['5 min', '15 min', '45 min']


def _ejecutar_job(job: SiesaJob) -> dict:
    """Despacha el job al handler correcto según su tipo."""
    from app.services.connekta_gateway import connekta
    payload = job.get_payload()

    if job.tipo == 'TRANSFERENCIA_UBICACIONES':
        return connekta.transferir_entre_ubicaciones(
            bodega_id=payload['bodega_id'],
            ubicacion_origen=payload['ubicacion_origen'],
            ubicacion_destino=payload['ubicacion_destino'],
            referencia_item=payload['referencia_item'],
            cantidad=payload['cantidad'],
            nota=payload.get('nota', ''),
        )

    raise ValueError(f'Tipo de job no reconocido: {job.tipo}')


def _post_completado(job: SiesaJob):
    """Actualiza la entidad referenciada al completarse el job."""
    if not job.referencia_tipo or not job.referencia_id:
        return
    if job.referencia_tipo == 'TareaReposicion':
        from app.models.tarea_reposicion import TareaReposicion
        tarea = TareaReposicion.query.get(job.referencia_id)
        if tarea:
            resultado = json.loads(job.resultado or '{}')
            tarea.siesa_enviado = True
            tarea.siesa_job_id = str(resultado.get('consecutivo') or resultado.get('id') or job.id)
            db.session.commit()


def _crear_alerta_admin(job: SiesaJob):
    """
    Registra el job fallido en el log de alertas críticas.
    El dashboard del admin consulta SiesaJob con estado=FALLIDO.
    No se envía email ni SMS aquí — el admin lo ve en /api/siesa-jobs/fallidos.
    """
    logger.critical(
        f'[ALERTA ADMIN] Job Siesa FALLIDO — id={job.id} tipo={job.tipo} '
        f'ref={job.referencia_tipo}:{job.referencia_id} '
        f'error="{job.error_ultimo}" — Verificar periodo contable en Siesa.'
    )


def get_jobs_fallidos():
    """Para el dashboard del admin."""
    return SiesaJob.query.filter_by(estado='FALLIDO').order_by(SiesaJob.fecha_creacion.desc()).all()


def reintentar_job(job_id: int) -> dict:
    """Admin fuerza un reintento de un job FALLIDO."""
    job = SiesaJob.query.get(job_id)
    if not job:
        raise ValueError(f'Job {job_id} no encontrado')
    if job.estado != 'FALLIDO':
        raise ValueError(f'Job {job_id} no está en estado FALLIDO — está {job.estado}')
    job.estado = 'PENDIENTE'
    job.intentos = 0
    job.proximo_intento = None
    job.error_ultimo = None
    db.session.commit()
    return job.to_dict()


def init_scheduler(app):
    """Cron cada 5 minutos — procesa la cola de jobs Siesa pendientes."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error('[DLQ] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=procesar_jobs_pendientes,
        trigger=IntervalTrigger(minutes=5),
        kwargs={'app': app},
        id='dlq_siesa_jobs',
        name='DLQ — procesar jobs Siesa pendientes (cada 5 min)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info('[DLQ] Scheduler iniciado — DLQ cada 5 min')
    return scheduler
