import json
import logging
from datetime import datetime
from app.extensions import db

logger = logging.getLogger(__name__)


class ReconciliacionService:
    """
    Detecta y corrige la inconsistencia donde Siesa ya tiene la factura
    pero WMS aún tiene siesa_triggered=False.

    Ocurre cuando la respuesta HTTP de Siesa se pierde (restart del servidor,
    timeout de red) después de que Siesa ya procesó el despacho.
    """

    @staticmethod
    def reconciliar_despacho(tarea, tipo_docto: str, consec_docto: str) -> dict:
        """
        Verifica si la factura ya existe en Siesa para el pedido dado.
        Si existe y WMS aún no lo sabe, actualiza tarea a DESPACHADO.

        Retorna:
          {'reconciliado': True, 'factura': {...}}  — si se corrigió
          {'reconciliado': False}                   — si no hay factura en Siesa aún
        """
        from app.services.connekta_gateway import connekta

        if not tipo_docto or not consec_docto:
            return {'reconciliado': False}

        try:
            facturas = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
        except Exception as e:
            logger.warning(
                f'[RECONCILIACION] No se pudo consultar facturas en Siesa '
                f'para {tarea.numero_pedido_siesa}: {e}'
            )
            return {'reconciliado': False}

        if not facturas:
            return {'reconciliado': False}

        factura = facturas[0]
        logger.warning(
            f'[RECONCILIACION] Factura detectada en Siesa para '
            f'{tarea.numero_pedido_siesa} (tarea {tarea.id}) '
            f'pero siesa_triggered=False — corrigiendo estado en WMS'
        )

        try:
            tarea.siesa_triggered = True
            tarea.siesa_triggered_at = datetime.utcnow()
            tarea.estado = 'DESPACHADO'
            tarea.fecha_despachado = tarea.fecha_despachado or datetime.utcnow()
            tarea.siesa_response = json.dumps(factura)
            db.session.commit()
            logger.info(
                f'[RECONCILIACION] Tarea {tarea.id} ({tarea.numero_pedido_siesa}) '
                f'reconciliada → DESPACHADO'
            )
            return {'reconciliado': True, 'factura': factura}
        except Exception as e:
            db.session.rollback()
            logger.error(
                f'[RECONCILIACION] Fallo al guardar reconciliación '
                f'para tarea {tarea.id}: {e}'
            )
            return {'reconciliado': False}
