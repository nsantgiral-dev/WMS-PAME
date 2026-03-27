"""
Gateway Connekta V2 — APIs nativas Siesa Enterprise.
Arquitectura correcta: POST transaccional, no PATCH de estado.
El trigger de despacho inyecta el documento 45 (Factura desde Pedido)
para que Siesa mueva inventario, genere remisión y facture automáticamente.
Headers: coniki (I-Key) y conitoken (I-Token SSO).
"""
import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class ConnektaGateway:

    def __init__(self):
        self.base_url = os.getenv('CONNEKTA_URL', '').rstrip('/')
        self.ikey = os.getenv('CONNEKTA_IKEY', '')
        self.itoken = os.getenv('CONNEKTA_ITOKEN', '')
        self.bodega = os.getenv('CONNEKTA_BODEGA', '001')
        self.centro_op = os.getenv('CONNEKTA_CENTRO_OP', '001')

        # IDs de APIs V2 — configurables por si Connekta cambia numeración
        self.api_pedidos = os.getenv('CONNEKTA_API_PEDIDOS', '47')
        self.api_ordenes = os.getenv('CONNEKTA_API_ORDENES', '7')
        self.api_inventario = os.getenv('CONNEKTA_API_INVENTARIO', '26')
        self.api_barras = os.getenv('CONNEKTA_API_BARRAS', '28')
        self.api_factura_pedido = os.getenv('CONNEKTA_API_FACTURA_PEDIDO', '45')
        self.api_ajustes = os.getenv('CONNEKTA_API_AJUSTES', '175')

        self.modo_simulacion = not all([self.base_url, self.ikey, self.itoken])

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación — configurar variables en Railway')

    @property
    def headers(self):
        return {
            'Content-Type': 'application/json',
            'coniki': self.ikey,
            'conitoken': self.itoken
        }

    def _simular(self, operacion: str, payload: dict = None):
        logger.info(f'[CONNEKTA SIMULADO] {operacion}')
        return {
            'simulado': True,
            'operacion': operacion,
            'timestamp': datetime.utcnow().isoformat(),
            'mensaje': f'{operacion} simulado exitosamente',
            'payload': payload or {}
        }

    def _get(self, api_id: str, params: dict = None):
        """GET a API estándar V2. Siempre filtrado por bodega y estado."""
        if self.modo_simulacion:
            return self._simular(f'GET_API_{api_id}', params)
        try:
            url = f'{self.base_url}/{api_id}'
            r = requests.get(url, headers=self.headers, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] GET {api_id}: {e}')
            raise Exception(f'Error consultando Connekta: {e}')

    def _post(self, api_id: str, payload: dict):
        """
        POST transaccional a Connekta V2.
        Inyecta documentos reales que afectan Kardex y contabilidad en Siesa.
        """
        if self.modo_simulacion:
            return self._simular(f'POST_API_{api_id}', payload)
        try:
            url = f'{self.base_url}/{api_id}'
            r = requests.post(url, headers=self.headers, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] POST {api_id}: {e}')
            raise Exception(f'Error enviando a Connekta: {e}')

    # ==========================================
    # GETs — Extracción desde Siesa
    # ==========================================

    def get_pedidos_aprobados(self):
        """
        47 API_v2_Ventas_Pedidos
        Cola de picking — pedidos aprobados listos para despacho.
        Filtro obligatorio: estado aprobado + bodega. Cero grasa.
        """
        return self._get(self.api_pedidos, {
            'indicador_estado': 'A',
            'bodega': self.bodega
        })

    def get_ordenes_compra_aprobadas(self):
        """
        7 API_v2_Compras_Ordenes
        Muelle de recepción — OCs aprobadas, excluye importaciones.
        """
        return self._get(self.api_ordenes, {
            'indicador_estado': 'A',
            'bodega': self.bodega,
            'es_importacion': 0
        })

    def get_inventario_fecha(self, item_codigo: str):
        """
        26 API_v2_Inventarios_InvFecha
        Existencia real a hoy — para conciliación conteo cíclico.
        """
        return self._get(self.api_inventario, {
            'item_codigo': item_codigo,
            'bodega': self.bodega,
            'fecha': datetime.utcnow().strftime('%Y-%m-%d')
        })

    def get_item_por_barras(self, codigo_barras: str):
        """
        28 API_v2_ItemsBarras
        Traduce EAN del escáner láser al código interno de Siesa.
        Crítico para validación en picking y recepción.
        """
        return self._get(self.api_barras, {
            'codigo_barras': codigo_barras
        })

    # ==========================================
    # POSTs — Transacciones reales en Siesa
    # ==========================================

    def trigger_despacho(self, numero_pedido: str, items: list):
        """
        45 API_v2_Ventas_Facturas_DesdePedido
        Trigger principal de despacho — inyecta documento transaccional completo.
        Siesa ejecuta automáticamente:
          - Descarga inventario (cuenta 14)
          - Aplica costo promedio
          - Genera remisión
          - Dispara factura electrónica
        El WMS inyecta el documento. Siesa hace todo lo demás.
        """
        payload = {
            'inicial': {
                'bodega': self.bodega,
                'centro_operacion': self.centro_op,
                'fecha': datetime.utcnow().strftime('%Y-%m-%d')
            },
            'documento': {
                'numero_pedido': numero_pedido
            },
            'movimiento': [
                {
                    'item_codigo': i.get('producto_codigo'),
                    'cantidad': i.get('cantidad_empacada'),
                    'bodega': self.bodega
                }
                for i in items
            ]
        }
        logger.info(f'[CONNEKTA] Trigger despacho pedido {numero_pedido}')
        return self._post(self.api_factura_pedido, payload)

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str):
        """
        175 API_v2_Inventarios_Ajustes_Fisicos
        Ajuste de inventario tras conteo cíclico double-blind confirmado.
        motivo_codigo: 'AJ-ENT' (sobrante) o 'AJ-SAL' (faltante).
        cantidad: siempre positivo — el motivo define la dirección contable.
        Siesa genera el asiento contable automáticamente.
        """
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}. Usar AJ-ENT o AJ-SAL')

        payload = {
            'inicial': {
                'bodega': self.bodega,
                'centro_operacion': self.centro_op,
                'fecha': datetime.utcnow().strftime('%Y-%m-%d'),
                'motivo': motivo_codigo,
                'referencia': referencia
            },
            'movimiento': [
                {
                    'item_codigo': item_codigo,
                    'cantidad': abs(cantidad),
                    'bodega': self.bodega
                }
            ]
        }
        logger.info(f'[CONNEKTA] Ajuste {motivo_codigo} — {item_codigo}: {cantidad}')
        return self._post(self.api_ajustes, payload)

    def confirmar_entrada_compras(self, numero_oc: str, items: list,
                                   es_parcial: bool = False):
        """
        7 API_v2_Compras_Ordenes — entrada por OC.
        Siesa debita cuenta 1435 automáticamente.
        es_parcial=True: OC queda viva esperando el resto.
        """
        payload = {
            'inicial': {
                'bodega': self.bodega,
                'centro_operacion': self.centro_op,
                'fecha': datetime.utcnow().strftime('%Y-%m-%d'),
                'numero_oc': numero_oc,
                'es_parcial': es_parcial
            },
            'movimiento': [
                {
                    'item_codigo': i.get('producto_codigo'),
                    'cantidad': i.get('cantidad_recibida'),
                    'bodega': self.bodega
                }
                for i in items
            ]
        }
        logger.info(f'[CONNEKTA] Entrada OC {numero_oc}')
        return self._post(self.api_ordenes, payload)

    # ==========================================
    # Estado del gateway
    # ==========================================

    def estado(self):
        return {
            'modo_simulacion': self.modo_simulacion,
            'connekta_configurado': not self.modo_simulacion,
            'url_configurada': bool(self.base_url),
            'credenciales_configuradas': bool(self.ikey and self.itoken),
            'bodega': self.bodega,
            'centro_operacion': self.centro_op,
            'apis_configuradas': {
                'GET_pedidos': f'{self.api_pedidos} API_v2_Ventas_Pedidos',
                'GET_ordenes': f'{self.api_ordenes} API_v2_Compras_Ordenes',
                'GET_inventario': f'{self.api_inventario} API_v2_Inventarios_InvFecha',
                'GET_barras': f'{self.api_barras} API_v2_ItemsBarras',
                'POST_despacho': f'{self.api_factura_pedido} API_v2_Ventas_Facturas_DesdePedido',
                'POST_ajustes': f'{self.api_ajustes} API_v2_Inventarios_Ajustes_Fisicos'
            },
            'mensaje': 'Listo para producción' if not self.modo_simulacion
                       else 'Configurar CONNEKTA_URL + variables en Railway'
        }


connekta = ConnektaGateway()
