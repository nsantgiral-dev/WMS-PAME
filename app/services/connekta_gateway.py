"""
Gateway Connekta V2 — APIs nativas estándar de Siesa Enterprise.
Headers oficiales: coniki (I-Key) y conitoken (I-Token SSO).
Todas las consultas filtradas por bodega y estado para máxima eficiencia.
El WMS es un Trigger — nunca factura, solo cambia estados.
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
        self.modo_simulacion = not all([self.base_url, self.ikey, self.itoken])

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación — configurar variables en Railway')

    @property
    def headers(self):
        """Headers oficiales Connekta."""
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

    def _get(self, api: str, params: dict = None):
        """GET a API estándar V2 de Connekta con filtros obligatorios."""
        if self.modo_simulacion:
            return self._simular(f'GET_{api}', params)
        try:
            url = f'{self.base_url}/{api}'
            r = requests.get(url, headers=self.headers, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] GET {api}: {e}')
            raise Exception(f'Error consultando Connekta: {e}')

    def _post(self, api: str, payload: dict):
        """POST a API estándar V2 de Connekta."""
        if self.modo_simulacion:
            return self._simular(f'POST_{api}', payload)
        try:
            url = f'{self.base_url}/{api}'
            r = requests.post(url, headers=self.headers, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] POST {api}: {e}')
            raise Exception(f'Error enviando a Connekta: {e}')

    # ==========================================
    # GETs — Extracción desde Siesa V2
    # ==========================================

    def get_pedidos_aprobados(self):
        """
        47 API_v2_Ventas_Pedidos
        Cola de picking — solo pedidos aprobados de nuestra bodega.
        CRÍTICO: cantidad_disponible, NUNCA existencia física.
        """
        return self._get('47', {
            'indicador_estado': 'A',
            'bodega': self.bodega
        })

    def get_ordenes_compra_aprobadas(self):
        """
        7 API_v2_Compras_Ordenes
        Muelle de recepción ciega — OCs aprobadas, excluye importaciones.
        """
        return self._get('7', {
            'indicador_estado': 'A',
            'bodega': self.bodega,
            'es_importacion': 0
        })

    def get_inventario_fecha(self, item_codigo: str):
        """
        26 API_v2_Inventarios_InvFecha
        Existencia real a fecha de hoy — para conciliación conteo cíclico.
        """
        return self._get('26', {
            'item_codigo': item_codigo,
            'bodega': self.bodega,
            'fecha': datetime.utcnow().strftime('%Y-%m-%d')
        })

    def get_item_por_barras(self, codigo_barras: str):
        """
        28 API_v2_ItemsBarras
        Traduce EAN del escáner láser al código interno de Siesa.
        Crítico para validación de producto en picking y recepción.
        """
        return self._get('28', {
            'codigo_barras': codigo_barras
        })

    # ==========================================
    # POSTs — Triggers a Siesa V2
    # ==========================================

    def marcar_pedido_cumplido(self, numero_pedido: str, items: list):
        """
        47 API_v2_Ventas_Pedidos — actualiza estado a CUMPLIDO.
        Siesa parametrizado para generar remisión + factura electrónica.
        El WMS NO factura. El WMS solo cambia el estado.
        """
        payload = {
            'numero_pedido': numero_pedido,
            'indicador_estado': 'C',
            'bodega': self.bodega,
            'items': [
                {
                    'item_codigo': i.get('producto_codigo'),
                    'cantidad_despachada': i.get('cantidad_empacada')
                }
                for i in items
            ]
        }
        logger.info(f'[CONNEKTA] Pedido {numero_pedido} → CUMPLIDO')
        return self._post('47', payload)

    def confirmar_entrada_compras(self, numero_oc: str, items: list,
                                   es_parcial: bool = False):
        """
        7 API_v2_Compras_Ordenes — confirma entrada por OC.
        Siesa debita cuenta 1435 automáticamente.
        es_parcial=True: OC queda viva esperando el resto del despacho.
        """
        payload = {
            'numero_oc': numero_oc,
            'bodega': self.bodega,
            'es_parcial': es_parcial,
            'items': [
                {
                    'item_codigo': i.get('producto_codigo'),
                    'cantidad_recibida': i.get('cantidad_recibida')
                }
                for i in items
            ]
        }
        logger.info(f'[CONNEKTA] Entrada OC {numero_oc} confirmada')
        return self._post('7', payload)

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str):
        """
        175 API_v2_Inventarios_Ajustes_Fisicos
        Ajuste tras conteo cíclico double-blind confirmado.
        motivo_codigo: 'AJ-ENT' (sobrante) o 'AJ-SAL' (faltante).
        cantidad: siempre positivo — el motivo define la dirección.
        """
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}')

        payload = {
            'motivo_codigo': motivo_codigo,
            'bodega': self.bodega,
            'centro_operacion': self.centro_op,
            'item_codigo': item_codigo,
            'cantidad': abs(cantidad),
            'referencia': referencia,
            'fecha': datetime.utcnow().strftime('%Y-%m-%d')
        }
        logger.info(f'[CONNEKTA] Ajuste {motivo_codigo} — {item_codigo}: {cantidad}')
        return self._post('175', payload)

    # ==========================================
    # Estado
    # ==========================================

    def estado(self):
        return {
            'modo_simulacion': self.modo_simulacion,
            'connekta_configurado': not self.modo_simulacion,
            'url_configurada': bool(self.base_url),
            'credenciales_configuradas': bool(self.ikey and self.itoken),
            'bodega': self.bodega,
            'centro_operacion': self.centro_op,
            'apis_v2': {
                'pedidos': '47 API_v2_Ventas_Pedidos',
                'ordenes_compra': '7 API_v2_Compras_Ordenes',
                'inventario_fecha': '26 API_v2_Inventarios_InvFecha',
                'items_barras': '28 API_v2_ItemsBarras',
                'ajustes_fisicos': '175 API_v2_Inventarios_Ajustes_Fisicos'
            },
            'mensaje': 'Listo para producción' if not self.modo_simulacion
                       else 'Configurar CONNEKTA_URL, CONNEKTA_IKEY, CONNEKTA_ITOKEN en Railway'
        }


connekta = ConnektaGateway()
