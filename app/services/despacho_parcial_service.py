"""
DespachoParialService — despacho parcial vía 142945 → 142943.

Responsabilidad única: orquestar el encadenamiento RemisionPedido → FacturaRemision
para pedidos con compromisos parciales. Completamente separado del flujo de packing;
no importa ni modifica nada de packing.py, cerrar_packing ni TareaPacking directamente
(solo lee la tarea y escribe siesa_triggered/estado al finalizar).
"""
import re
import json
import logging
from datetime import datetime

from app.extensions import db

logger = logging.getLogger(__name__)

# Confirmado por consultor: "Transacción Exitosa. Se generó el documento RM-XXXX"
_RE_RM = re.compile(r'([A-Z]{1,6})-(\d+)', re.IGNORECASE)


class DespachoParialService:

    @staticmethod
    def obtener_compromisos(tipo_docto: str, consec_docto: str) -> list:
        """Devuelve las líneas de compromiso del pedido desde Siesa (API ID 103)."""
        from app.services.connekta_gateway import connekta
        return connekta.get_compromisos_pedido(tipo_docto, consec_docto)

    @staticmethod
    def despachar_parcial(tarea, cantidades: dict) -> dict:
        """
        Ejecuta el despacho parcial completo:
          1. Lee cabecera del pedido en Siesa (tercero, moneda, cond. pago)
          2. Construye items con las cantidades indicadas
          3. POST 142945 → crea RM, extrae consecutivo del campo `mensaje`
          4. POST 142943 → convierte RM a FE
          5. Marca tarea DESPACHADO + siesa_triggered=True

        cantidades: {producto_codigo: float}
        Retorna: {'rm': 'RM-1234', 'fe_response': {...}}
        Lanza ValueError con mensaje claro ante cualquier problema de datos.
        Lanza Exception ante fallo de Siesa/red (para que el route devuelva 502).
        """
        from app.services.connekta_gateway import connekta

        if tarea.siesa_triggered:
            raise ValueError(f'Tarea {tarea.id} ya tiene siesa_triggered=True')

        tipo_docto  = tarea.tipo_docto_pedido_siesa
        consec_docto = tarea.consec_docto_pedido_siesa
        if not tipo_docto or not consec_docto:
            raise ValueError('Tarea sin tipo_docto/consec_docto — imposible despachar')

        # 1. Cabecera del pedido (cliente, moneda, condición de pago)
        cabecera = connekta.get_pedido_cabecera(tipo_docto, consec_docto)
        if not cabecera:
            raise ValueError(
                f'Pedido {tarea.numero_pedido_siesa} no encontrado en Siesa'
            )

        # 2. Items con cantidades indicadas
        items = DespachoParialService._build_items(tarea, cantidades)
        if not items:
            raise ValueError('Ningún ítem con cantidad > 0 — nada que despachar')

        # 3. Crear RM con 142945 (trigger_despacho existente, sin modificar)
        resp_rm = connekta.trigger_despacho(tipo_docto, consec_docto, items)
        tipo_rm, consec_rm = DespachoParialService._parsear_rm(resp_rm)

        # 4. Convertir RM → FE con 142943 — guard anti-duplicado antes de disparar.
        # Si la RM ya tiene FE (reintento DLQ tras fallo en paso 4), no crear otra.
        facturas_existentes = connekta.get_factura_desde_remision(tipo_rm, consec_rm)
        if facturas_existentes:
            logger.info(
                '[DESPACHO_PARCIAL] tarea=%s RM=%s%s ya tiene FE — omitiendo 142943',
                tarea.id, tipo_rm, consec_rm
            )
            resp_fe = {'idempotente': True, 'facturas': facturas_existentes}
        else:
            resp_fe = connekta.trigger_factura_desde_remision(tipo_rm, consec_rm, cabecera)

        # 5. Persistir resultado en la tarea
        resultado = {'rm': f'{tipo_rm}-{consec_rm}', 'fe_response': resp_fe}
        tarea.siesa_triggered     = True
        tarea.siesa_triggered_at  = datetime.utcnow()
        tarea.estado              = 'DESPACHADO'
        tarea.fecha_despachado    = tarea.fecha_despachado or datetime.utcnow()
        tarea.siesa_response      = json.dumps(resultado)
        db.session.commit()

        logger.info(
            '[DESPACHO_PARCIAL] tarea=%s pedido=%s → %s FE=ok',
            tarea.id, tarea.numero_pedido_siesa, resultado['rm']
        )
        return resultado

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    @staticmethod
    def _build_items(tarea, cantidades: dict) -> list:
        items = []
        for item in tarea.items:
            if not item.producto:
                continue
            codigo_wms   = item.producto.codigo
            codigo_siesa = item.producto.codigo_siesa or codigo_wms
            qty = float(cantidades.get(codigo_wms, 0))
            if qty <= 0:
                continue
            items.append({
                'producto_codigo': codigo_siesa,   # Siesa espera el código interno (codigo_siesa)
                'cantidad_empacada': qty,
                'lote': item.lote or None,
                'unidad_medida': (item.producto.unidad_empaque or item.producto.unidad_medida or 'UND'),
                'item_id_siesa': None,
            })
        return items

    @staticmethod
    def _parsear_rm(resp) -> tuple[str, int]:
        """
        Extrae tipo y consecutivo del RM del campo `mensaje` del response de 142945.
        Formato confirmado por consultor: "Transacción Exitosa. Se generó el documento RM-XXXX"
        """
        if not resp:
            raise ValueError('Response de 142945 vacío — no se obtuvo número de RM')

        if isinstance(resp, dict):
            mensaje = str(resp.get('mensaje') or resp.get('message') or '')
        elif isinstance(resp, list) and resp:
            mensaje = str(resp[0].get('mensaje') or '')
        else:
            mensaje = str(resp)

        match = _RE_RM.search(mensaje)
        if not match:
            logger.error('[DESPACHO_PARCIAL] No se extrajo RM de: %r', mensaje)
            raise ValueError(
                f'No se pudo extraer número de RM del response de Siesa (mensaje={mensaje!r}). '
                'Verificar formato de respuesta del conector 142945.'
            )

        tipo_rm  = match.group(1).upper()
        consec_rm = int(match.group(2))
        logger.info('[DESPACHO_PARCIAL] RM parseado: %s-%s', tipo_rm, consec_rm)
        return tipo_rm, consec_rm
