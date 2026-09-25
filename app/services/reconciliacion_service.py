import json
import logging
from datetime import datetime
from app.extensions import db

logger = logging.getLogger(__name__)

#: Cuántas tareas mira cada barrido. Rotan por `reconciliacion_intento_at`:
#: antes era `.limit(10)` sin orden y diez tareas que nunca se reconcilian
#: tapaban a la once para siempre.
LOTE_SWEEP = 10


class ReconciliacionService:
    """
    Detecta y corrige la inconsistencia donde Siesa ya facturó el despacho
    pero el WMS aún tiene siesa_triggered=False (la respuesta HTTP se perdió:
    restart, timeout).

    **Solo con evidencia documental**: la factura, con su consecutivo. Hasta
    el 2026-09-25 bastaba `get_estado_pedido == 9` (`_ESTADOS_CUMPLIDO`), y 9
    es ANULADO en el resto del repo (ver `estado_pedido_siesa`): un pedido
    anulado por comercial mientras la caja esperaba quedaba DESPACHADO y sus
    bultos subían al camión sin remisión ni factura.
    """

    @staticmethod
    def _factura_con_consecutivo(facturas):
        for f in facturas or []:
            if isinstance(f, dict) and str(f.get('f350_consec_docto') or '').strip():
                return f
        return None

    @staticmethod
    def reconciliar_despacho(tarea, tipo_docto: str, consec_docto: str) -> dict:
        """¿Siesa ya tiene la factura de este pedido? Tres respuestas:

          {'reconciliado': True, ...}               — la FE existe (con consecutivo)
          {'reconciliado': False, 'no_se': False}   — Siesa respondió: no hay FE
          {'reconciliado': False, 'no_se': True}    — no se pudo preguntar

        Además `anulado: True` si el pedido está anulado en Siesa (se marca
        `pedido_anulado_siesa`; **nunca** `siesa_triggered`).

        Con la FE: `siesa_triggered`, la factura en la tarea y, si la remisión
        se identifica, DESPACHADO. **Sin remisión identificada no queda
        DESPACHADO** (decisión del dueño, 2026-09-25): la caja sigue
        esperando, sin reenvío posible, y se registra la RM a mano.
        """
        from app.services import estado_pedido_siesa as _eps
        from app.services.connekta_gateway import RemisionNoDisponible, connekta

        if not tipo_docto or not consec_docto:
            return {'reconciliado': False, 'no_se': True,
                    'motivo': 'sin tipo/consecutivo del pedido'}

        try:
            factura = ReconciliacionService._factura_con_consecutivo(
                connekta.get_factura_desde_pedido(tipo_docto, consec_docto))
        except Exception as e:
            logger.warning('[RECONCILIACION] no se pudo consultar la FE de %s: %s',
                           tarea.numero_pedido_siesa, e)
            return {'reconciliado': False, 'no_se': True, 'motivo': str(e)[:300]}

        if factura is None:
            # Sin factura. El estado del pedido solo sirve para declarar un
            # anulado: no es evidencia de ningún documento.
            anulado = False
            try:
                estado = connekta.get_estado_pedido(tipo_docto, consec_docto)
            except Exception as e:  # noqa: BLE001 — informativo
                estado = None
                logger.warning('[RECONCILIACION] get_estado_pedido falló para %s: %s',
                               tarea.numero_pedido_siesa, e)
            if _eps.es_anulado(estado):
                anulado = True
                if not tarea.pedido_anulado_siesa:
                    tarea.pedido_anulado_siesa = True
                    tarea.pedido_estado_siesa_detectado = str(estado)
                    db.session.commit()
                    logger.warning('[RECONCILIACION] pedido %s ANULADO en Siesa (estado %s) '
                                   '— tarea %s marcada, sin tocar siesa_triggered',
                                   tarea.numero_pedido_siesa, estado, tarea.id)
            return {'reconciliado': False, 'no_se': False, 'anulado': anulado}

        # La factura existe. ¿Y la remisión?
        rm = None
        if tarea.rm_consec:
            rm = {'tipo': tarea.rm_tipo or 'RM', 'consec': tarea.rm_consec}
        else:
            try:
                rm = connekta.get_remision_desde_pedido(tipo_docto, consec_docto)
            except RemisionNoDisponible as e:
                logger.warning('[RECONCILIACION] FE de %s encontrada; la RM no se pudo '
                               'consultar: %s', tarea.numero_pedido_siesa, e)

        evidencia = {'via': 'factura',
                     'fe': f"{factura.get('f350_id_tipo_docto') or ''}-"
                           f"{factura.get('f350_consec_docto')}",
                     'rm': f"{rm['tipo']}-{rm['consec']}" if rm else None}
        logger.warning(
            f'[RECONCILIACION] Pedido {tarea.numero_pedido_siesa} (tarea {tarea.id}) '
            f'ya facturado en Siesa ({evidencia}) pero siesa_triggered=False — corrigiendo WMS'
        )
        try:
            from app.models.packing import EstadoPacking
            ahora = datetime.utcnow()
            tarea.siesa_triggered = True
            tarea.siesa_triggered_at = ahora
            if not tarea.fe_consec:
                tarea.fe_tipo = (str(factura.get('f350_id_tipo_docto') or '').strip()
                                 or connekta.tipo_docto_factura)[:10]
                tarea.fe_consec = str(factura.get('f350_consec_docto')).strip()[:30]
            tarea.fe_confirmada_at = tarea.fe_confirmada_at or ahora
            if rm and not tarea.rm_consec:
                tarea.rm_tipo, tarea.rm_consec = rm['tipo'], rm['consec']
            if tarea.rm_consec:
                tarea.estado = EstadoPacking.DESPACHADO
                tarea.fecha_despachado = tarea.fecha_despachado or ahora
            tarea.siesa_response = json.dumps({**evidencia, 'reconciliado': True})
            cerrados = ReconciliacionService._cerrar_jobs_fallidos(tarea, evidencia)
            db.session.commit()
            if cerrados:
                logger.info(f'[RECONCILIACION] Tarea {tarea.id}: {cerrados} job(s) '
                            f'DESPACHO_F470 FALLIDO cerrados — Siesa ya tiene el documento')
            return {'reconciliado': True, 'rm_identificada': bool(tarea.rm_consec),
                    'evidencia': evidencia}
        except Exception as e:
            db.session.rollback()
            logger.error(
                f'[RECONCILIACION] Fallo al guardar reconciliación '
                f'para tarea {tarea.id}: {e}'
            )
            return {'reconciliado': False, 'no_se': True, 'motivo': str(e)[:300]}

    @staticmethod
    def _cerrar_jobs_fallidos(tarea, evidencia) -> int:
        """Los DESPACHO_F470 FALLIDO de esta tarea pasan a COMPLETADO con
        `resultado.reconciliado`: Siesa **ya tiene** el documento, así que el
        job no está trabado y no puede seguir contando como tal en la salud,
        las fugas ni el KPI (`siesa_job_service.fallidos_vigentes`).

        No se tocan los jobs vivos (PENDIENTE/REINTENTANDO/PROCESANDO): la DLQ
        los resuelve sola por la guarda `siesa_triggered` que se acaba de
        poner. Sin commit: va en la transacción de la reconciliación."""
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        jobs = SiesaJob.query.filter(
            SiesaJob.tipo == 'DESPACHO_F470',
            SiesaJob.referencia_tipo == 'TareaPacking',
            SiesaJob.referencia_id == tarea.id,
            SiesaJob.estado == EstadoSiesaJob.FALLIDO,
        ).all()
        for job in jobs:
            job.marcar_completado({'reconciliado': True,
                                   'por': 'ReconciliacionService.reconciliar_despacho',
                                   'evidencia': evidencia,
                                   'error_que_tenia': (job.error_ultimo or '')[:500]})
        return len(jobs)

    @staticmethod
    def sweep_despachos_pendientes(app=None):
        """
        Sweep programado: busca tareas con siesa_triggered=False + bultos y las reconcilia.
        Cubre tanto jobs FALLIDO (que el DLQ no reintenta) como casos de timeout.
        """
        from flask import current_app
        _app = app or current_app._get_current_object()
        with _app.app_context():
            try:
                ReconciliacionService._ejecutar_sweep()
            except Exception as e:
                logger.error(f'[RECONCILIACION] Error en sweep: {e}', exc_info=True)

    @staticmethod
    def _ejecutar_sweep():
        from app.models.packing import TareaPacking
        from app.utils.lock import LOCK_RECONCILIACION_SWEEP, advisory_lock

        # Advisory lock: evita que 2 workers Gunicorn ejecuten sweep simultáneamente.
        #
        # El lock 2014 se tomaba y NO se liberaba nunca. Los advisory locks de
        # sesión viven en la conexión: volvía al pool tomada y el sweep siguiente
        # se encontraba el lock ocupado. Dejaba de correr en silencio — y este
        # sweep es el que detecta tareas de packing que Siesa YA proceso y el WMS
        # cree que no. Sin él, el inventario diverge del ERP sin que nada avise.
        with advisory_lock(LOCK_RECONCILIACION_SWEEP, 'reconciliacion_sweep') as tomado:
            if not tomado:
                logger.info('[RECONCILIACION] Lock no disponible — omitiendo sweep')
                return
            ReconciliacionService._sweep_con_lock(TareaPacking)

    @staticmethod
    def _sweep_con_lock(TareaPacking):
        """El trabajo del sweep. Separado para que el `with` del lock envuelva
        TODO el cuerpo sin reindentar cuarenta líneas — un diff de indentación
        masivo esconde el cambio real en la revisión."""
        # Tareas VERIFICADO o DESPACHADO con siesa_triggered=False.
        # No se exige bultos: tareas bloqueadas por guard anti-duplicado en cerrar_packing
        # nunca alcanzan a crear bultos pero Siesa ya procesó la factura.
        # Rota por el último intento: primero las nunca intentadas, después la
        # más vieja. Las de pedido anulado no se miran: no van a despacharse.
        orden_nulos = db.case((TareaPacking.reconciliacion_intento_at.is_(None), 0), else_=1)
        tareas = (
            TareaPacking.query
            .filter(
                TareaPacking.siesa_triggered == False,
                TareaPacking.estado.in_(['VERIFICADO', 'DESPACHADO']),
                TareaPacking.tipo_docto_pedido_siesa.isnot(None),
                TareaPacking.consec_docto_pedido_siesa.isnot(None),
                db.or_(TareaPacking.pedido_anulado_siesa.is_(None),
                       TareaPacking.pedido_anulado_siesa == False),
            )
            .order_by(orden_nulos, TareaPacking.reconciliacion_intento_at,
                      TareaPacking.id)
            .limit(LOTE_SWEEP)
            .all()
        )
        ahora = datetime.utcnow()
        for t in tareas:
            t.reconciliacion_intento_at = ahora
        db.session.commit()

        if not tareas:
            return

        logger.info(
            f'[RECONCILIACION] Sweep encontró {len(tareas)} tarea(s) candidatas '
            f'(ids={[t.id for t in tareas]})'
        )

        for tarea in tareas:
            try:
                ReconciliacionService.reconciliar_despacho(
                    tarea,
                    tipo_docto=tarea.tipo_docto_pedido_siesa,
                    consec_docto=tarea.consec_docto_pedido_siesa,
                )
            except Exception as e:
                logger.error(
                    f'[RECONCILIACION] Error reconciliando tarea {tarea.id} '
                    f'({tarea.numero_pedido_siesa}): {e}'
                )


def init_scheduler(app):
    """Sweep de reconciliación cada 5 minutos."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error('[RECONCILIACION] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido  # P1-11
    from app.services.ventana_siesa import solo_en_ventana_siesa  # P2
    scheduler.add_job(
        func=con_latido('reconciliacion_despachos', solo_en_ventana_siesa(ReconciliacionService.sweep_despachos_pendientes)),
        trigger=IntervalTrigger(minutes=5),
        kwargs={'app': app},
        id='reconciliacion_despachos',
        name='Reconciliación despachos Siesa (cada 5 min)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info('[RECONCILIACION] Scheduler iniciado — sweep cada 5 min')
    return scheduler
