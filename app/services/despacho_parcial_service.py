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

        # 3. Idempotencia para 142945: si la RM ya existe (reintento DLQ tras fallo de red
        # o "Respuesta Simplificada" activo en el conector), reutilizarla sin crear otra.
        # Sin este guard, un reintento llama 142945 de nuevo y puede crear una RM duplicada
        # o fallar porque el pedido ya está en estado=4 (Cumplido) en Siesa.
        rm_data = connekta.get_remision_desde_pedido(tipo_docto, consec_docto)
        if rm_data:
            tipo_rm  = rm_data['tipo']
            consec_rm = rm_data['consec']
            logger.info(
                '[DESPACHO_PARCIAL] RM ya existe %s-%s — saltando 142945 (idempotencia)',
                tipo_rm, consec_rm
            )
        else:
            try:
                resp_rm = connekta.trigger_despacho(tipo_docto, consec_docto, items)
            except Exception as e_rm:
                # Siesa rechaza 142945 cuando el pedido ya no está comprometido (RM ya existe).
                # Recuperar la RM existente y continuar hacia la FE en lugar de abortar.
                if 'comprometido' in str(e_rm).lower():
                    logger.warning(
                        '[DESPACHO_PARCIAL] 142945 rechazado (pedido no comprometido) — '
                        'buscando RM existente para %s%s', tipo_docto, consec_docto
                    )
                    rm_data = connekta.get_remision_desde_pedido(tipo_docto, consec_docto)
                    if not rm_data:
                        raise ValueError(
                            f'Pedido {tipo_docto}{consec_docto} no está comprometido y no se '
                            f'encontró RM existente — verificar estado en Siesa.'
                        )
                    tipo_rm  = rm_data['tipo']
                    consec_rm = rm_data['consec']
                    logger.info(
                        '[DESPACHO_PARCIAL] RM existente recuperada: %s-%s', tipo_rm, consec_rm
                    )
                else:
                    raise
            else:
                try:
                    tipo_rm, consec_rm = DespachoParialService._parsear_rm(resp_rm)
                except ValueError:
                    # Fallback: "Respuesta Simplificada" activo en el conector, sin consecutivo.
                    logger.warning(
                        '[DESPACHO_PARCIAL] _parsear_rm falló — consultando Siesa por RM del pedido %s%s',
                        tipo_docto, consec_docto
                    )
                    rm_data = connekta.get_remision_desde_pedido(tipo_docto, consec_docto)
                    if not rm_data:
                        raise ValueError(
                            f'142945 reportó éxito pero no se pudo recuperar el número de RM '
                            f'para el pedido {tipo_docto}{consec_docto}. '
                            'Verificar en Siesa que la remisión fue creada y reintentar.'
                        )
                    tipo_rm  = rm_data['tipo']
                    consec_rm = rm_data['consec']
                    logger.info('[DESPACHO_PARCIAL] RM recuperada por query: %s-%s', tipo_rm, consec_rm)

        # 4. Convertir RM → FE con 142943 — guard anti-duplicado antes de disparar.
        # Usamos get_factura_desde_pedido (papeleriamedellin_monitos_facturas_wms) en lugar de
        # get_factura_desde_remision (API_v2_Ventas_Facturas_DesdePedido) porque esta última
        # filtra por f460_* (RelacionDoctos) que ese API no expone — causaba FAIL-FAST en cada
        # intento, dejando la FE sin crear aunque la RM existía.
        facturas_existentes = connekta.get_factura_desde_pedido(tipo_docto, consec_docto)
        if facturas_existentes:
            logger.info(
                '[DESPACHO_PARCIAL] tarea=%s pedido=%s%s ya tiene FE — omitiendo 142943',
                tarea.id, tipo_docto, consec_docto
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
        Extrae tipo y consecutivo de la RM buscando en todos los campos string del response.
        Cubre el formato "Se generó el documento RM-XXXX" (cuando el consultor habilite
        el mensaje dinámico en 142945) y cualquier variante futura de Siesa.
        Lanza ValueError si no encuentra el patrón — el caller hace fallback a query Siesa.
        """
        if not resp:
            raise ValueError('Response de 142945 vacío')

        if isinstance(resp, dict):
            candidatos = [str(v) for v in resp.values() if v]
        elif isinstance(resp, list) and resp:
            primer = resp[0]
            candidatos = [str(v) for v in (primer.values() if isinstance(primer, dict) else [primer]) if v]
        else:
            candidatos = [str(resp)]

        for texto in candidatos:
            match = _RE_RM.search(texto)
            if match:
                tipo_rm   = match.group(1).upper()
                consec_rm = int(match.group(2))
                logger.info('[DESPACHO_PARCIAL] RM parseado del response: %s-%s', tipo_rm, consec_rm)
                return tipo_rm, consec_rm

        raise ValueError(f'Patrón RM-XXXX no encontrado en response: {resp!r}')
