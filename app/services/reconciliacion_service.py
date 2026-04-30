import json
import logging
from datetime import datetime
from app.extensions import db

logger = logging.getLogger(__name__)

# Estados del pedido en Siesa que confirman que ya fue facturado/procesado
# 4 = Cumplido (facturado), 9 = Anulado/ya procesado
_ESTADOS_CUMPLIDO = {'4', '9'}


class ReconciliacionService:
    """
    Detecta y corrige la inconsistencia donde Siesa ya procesó el despacho
    pero WMS aún tiene siesa_triggered=False.

    Ocurre cuando la respuesta HTTP de Siesa se pierde (restart del servidor,
    timeout de red) después de que Siesa ya procesó el despacho.
    """

    @staticmethod
    def reconciliar_despacho(tarea, tipo_docto: str, consec_docto: str) -> dict:
        """
        Verifica si la factura ya existe en Siesa para el pedido dado.
        Usa dos señales:
          1. get_factura_desde_pedido() — factura directa
          2. get_estado_pedido() == 9  — pedido marcado cumplido en Siesa

        Retorna:
          {'reconciliado': True}   — si se corrigió
          {'reconciliado': False}  — si Siesa aún no tiene la factura
        """
        from app.services.connekta_gateway import connekta

        if not tipo_docto or not consec_docto:
            return {'reconciliado': False}

        # Señal 1: factura directa.
        # Si la consulta falla, advertir y continuar a Señal 2 — trigger_factura tiene
        # su propio guard (get_estado_pedido == 4) que evita FE duplicada igualmente.
        factura_encontrada = None
        try:
            facturas = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
            if facturas:
                factura_encontrada = facturas[0]
        except Exception as e:
            logger.warning(
                f'[RECONCILIACION] Señal 1 (get_factura_desde_pedido) falló para '
                f'{tarea.numero_pedido_siesa}: {e} — continuando con Señal 2'
            )

        # Señal 2: estado del pedido en Siesa == 9 (cumplido/ya procesado)
        if not factura_encontrada:
            try:
                estado_siesa = connekta.get_estado_pedido(tipo_docto, consec_docto)
                if str(estado_siesa) in _ESTADOS_CUMPLIDO:
                    factura_encontrada = {'via': 'estado_pedido', 'estado': estado_siesa}
            except Exception as e:
                logger.warning(
                    f'[RECONCILIACION] get_estado_pedido falló '
                    f'para {tarea.numero_pedido_siesa}: {e}'
                )

        if not factura_encontrada:
            return {'reconciliado': False}

        logger.warning(
            f'[RECONCILIACION] Pedido {tarea.numero_pedido_siesa} (tarea {tarea.id}) '
            f'ya procesado en Siesa pero siesa_triggered=False — corrigiendo WMS'
        )

        try:
            tarea.siesa_triggered = True
            tarea.siesa_triggered_at = datetime.utcnow()
            tarea.estado = 'DESPACHADO'
            tarea.fecha_despachado = tarea.fecha_despachado or datetime.utcnow()
            tarea.siesa_response = json.dumps(factura_encontrada)
            db.session.commit()
            logger.info(
                f'[RECONCILIACION] Tarea {tarea.id} ({tarea.numero_pedido_siesa}) '
                f'reconciliada → DESPACHADO'
            )
            return {'reconciliado': True}
        except Exception as e:
            db.session.rollback()
            logger.error(
                f'[RECONCILIACION] Fallo al guardar reconciliación '
                f'para tarea {tarea.id}: {e}'
            )
            return {'reconciliado': False}

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

        # Tareas VERIFICADO o DESPACHADO con siesa_triggered=False.
        # No se exige bultos: tareas bloqueadas por guard anti-duplicado en cerrar_packing
        # nunca alcanzan a crear bultos pero Siesa ya procesó la factura.
        # La doble señal de Connekta (factura activa o estado==4) es suficiente garantía.
        tareas = (
            TareaPacking.query
            .filter(
                TareaPacking.siesa_triggered == False,
                TareaPacking.estado.in_(['VERIFICADO', 'DESPACHADO']),
                TareaPacking.tipo_docto_pedido_siesa.isnot(None),
                TareaPacking.consec_docto_pedido_siesa.isnot(None),
            )
            .limit(10)
            .all()
        )

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
    scheduler.add_job(
        func=ReconciliacionService.sweep_despachos_pendientes,
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
