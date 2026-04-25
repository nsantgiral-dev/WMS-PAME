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

    # [46] Envolver todo el cuerpo en try/except para que un fallo de DB no paralice la DLQ
    try:
        _procesar_jobs_pendientes_interno(_app)
    except Exception as e:
        logger.error(f'[DLQ] Error inesperado en procesar_jobs_pendientes: {e}', exc_info=True)


def _procesar_jobs_pendientes_interno(_app):
    """Lógica interna de procesamiento — separada para permitir captura de errores DB externos."""
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

                # Actualizar referencia si aplica — aislado para que un fallo aquí
                # no marque el job como FALLIDO (Siesa ya procesó el trabajo)
                try:
                    _post_completado(job)
                except Exception as post_err:
                    logger.warning(
                        f'[DLQ] Job {job.id} ({job.tipo}) completado en Siesa pero '
                        f'_post_completado falló: {post_err} — revisar referencia manualmente'
                    )

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
        # [57] TODO: riesgo de duplicado — Siesa puede haber procesado el primer intento
        # sin retornar éxito (timeout de red). Verificar en Siesa si el traslado ya existe
        # antes de reenviar para evitar doble movimiento de inventario.
        if job.intentos > 0:
            logger.warning(
                f'[DLQ] REINTENTO TRANSFERENCIA_UBICACIONES job={job.id} intento={job.intentos + 1} '
                f'— riesgo de duplicado si el primer intento llegó a Siesa sin confirmación. '
                f'Verificar manualmente en Siesa si el traslado ya fue registrado.'
            )
        return connekta.transferir_entre_ubicaciones(
            bodega_id=payload['bodega_id'],
            ubicacion_origen=payload['ubicacion_origen'],
            ubicacion_destino=payload['ubicacion_destino'],
            referencia_item=payload['referencia_item'],
            cantidad=payload['cantidad'],
            nota=payload.get('nota', ''),
        )

    if job.tipo == 'DESPACHO_F470':
        # Idempotencia: si un intento anterior llegó a Siesa (siesa_triggered=True),
        # no volver a llamar — evita crear remisión duplicada.
        from app.models.packing import TareaPacking
        import json as _json
        tarea = TareaPacking.query.get(payload.get('tarea_id'))
        if tarea and tarea.siesa_triggered:
            logger.info(
                f'[DLQ] DESPACHO_F470 job={job.id}: tarea {tarea.id} ya tiene '
                f'siesa_triggered=True — omitiendo llamada a Siesa (idempotencia)'
            )
            return {'idempotente': True, 'tarea_id': tarea.id}
        resultado = connekta.trigger_factura(
            tipo_docto_pedido=payload['tipo_docto_pedido'],
            consec_docto_pedido=payload['consec_docto_pedido'],
            items=payload.get('items', []),
        )
        # Marcar la tarea como despachada si el reintento funcionó
        if tarea and not tarea.siesa_triggered:
            from datetime import datetime as _dt
            tarea.siesa_triggered = True
            tarea.siesa_response = _json.dumps(resultado)
            tarea.siesa_triggered_at = _dt.utcnow()
            tarea.estado = 'DESPACHADO'
            tarea.fecha_despachado = _dt.utcnow()
            from app.extensions import db as _db
            _db.session.commit()
        return resultado

    if job.tipo == 'ENTRADA_OC':
        # Idempotencia: si un intento anterior llegó a Siesa (siesa_triggered=True),
        # no volver a llamar — evita crear entrada contable duplicada.
        from app.models.recepcion import RecepcionMercancia
        import json as _json
        rec = RecepcionMercancia.query.get(payload.get('recepcion_id'))
        if rec and rec.siesa_triggered:
            logger.info(
                f'[DLQ] ENTRADA_OC job={job.id}: recepción {rec.id} ya tiene '
                f'siesa_triggered=True — omitiendo llamada a Siesa (idempotencia)'
            )
            return {'idempotente': True, 'recepcion_id': rec.id}
        resultado = connekta.confirmar_entrada_compras(
            id_co_oc=payload.get('id_co_oc', connekta.centro_op),
            tipo_docto_oc=payload.get('tipo_docto_oc', ''),
            consec_docto_oc=payload.get('consec_docto_oc', ''),
            items=payload.get('items', []),
            es_parcial=payload.get('es_parcial', False),
            proveedor_id=payload.get('proveedor_id', ''),
            sucursal_prov=payload.get('sucursal_prov', ''),
            tercero_comprador=payload.get('tercero_comprador'),
            sucursal_comprador=payload.get('sucursal_comprador'),
            moneda_docto=payload.get('moneda_docto'),
            moneda_conv=payload.get('moneda_conv'),
            moneda_local=payload.get('moneda_local'),
            tasa_conv=payload.get('tasa_conv', 0.0),
            tasa_local=payload.get('tasa_local', 0.0),
            num_docto_referencia=payload.get('num_docto_referencia'),
            cond_pago=payload.get('cond_pago', ''),
        )
        # Marcar la recepción como siesa_triggered si el reintento funcionó
        if rec and not rec.siesa_triggered:
            from datetime import datetime as _dt
            rec.siesa_triggered = True
            rec.siesa_response = _json.dumps(resultado)
            rec.siesa_triggered_at = _dt.utcnow()
            from app.extensions import db as _db
            _db.session.commit()
        return resultado

    if job.tipo == 'TRASLADO_AVERIAS':
        # Idempotencia parcial: verificar si la TareaDevolucion ya está en estado
        # que indica que el traslado fue procesado exitosamente en una corrida anterior.
        if job.intentos > 0:
            from app.models.devolucion import TareaDevolucion as _TareaDev
            tarea_dev = _TareaDev.query.get(payload.get('tarea_id'))
            if tarea_dev and tarea_dev.estado == 'COMPLETADO':
                logger.warning(
                    f'[DLQ] TRASLADO_AVERIAS job={job.id} intento={job.intentos + 1}: '
                    f'TareaDevolucion {tarea_dev.id} ya está COMPLETADO — '
                    f'verificar manualmente en Siesa si el traslado NB1→AV1 fue duplicado.'
                )
        return connekta.transferir_a_averias(
            item_codigo=payload['item_codigo'],
            cantidad=payload['cantidad'],
            referencia=payload.get('referencia', ''),
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
    Log crítico + email inmediato cuando un job alcanza estado=FALLIDO.
    """
    logger.critical(
        f'[ALERTA ADMIN] Job Siesa FALLIDO — id={job.id} tipo={job.tipo} '
        f'ref={job.referencia_tipo}:{job.referencia_id} '
        f'error="{job.error_ultimo}" — Verificar periodo contable en Siesa.'
    )
    try:
        from app.services.alertas_service import alertar_job_fallido
        alertar_job_fallido(job)
    except Exception as e:
        logger.error(f'[ALERTAS] No se pudo enviar email de job fallido: {e}')


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
