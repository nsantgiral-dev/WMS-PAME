"""
Gateway canónico para Connekta/Siesa Enterprise.
Punto único de integración — todas las comunicaciones con Siesa pasan por aquí.
Modo simulación automático cuando no hay credenciales configuradas.
Headers correctos: coniki e conitoken (nomenclatura oficial Connekta).
"""
import os
import json
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
        self.conector_cumplido = os.getenv('CONNEKTA_CONECTOR_CUMPLIDO', '')
        self.conector_entrada = os.getenv('CONNEKTA_CONECTOR_ENTRADA', '')
        self.conector_ajuste = os.getenv('CONNEKTA_CONECTOR_AJUSTE', '')
        self.modo_simulacion = not all([self.base_url, self.ikey, self.itoken])

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación — configurar variables en Railway')

    @property
    def headers(self):
        """Headers oficiales Connekta — coniki y conitoken."""
        return {
            'Content-Type': 'application/json',
            'coniki': self.ikey,
            'conitoken': self.itoken
        }

    def _simular(self, operacion: str, payload: dict):
        """Respuesta simulada cuando no hay credenciales."""
        logger.info(f'[CONNEKTA SIMULADO] {operacion}')
        return {
            'simulado': True,
            'operacion': operacion,
            'timestamp': datetime.utcnow().isoformat(),
            'mensaje': f'{operacion} simulado exitosamente',
            'payload': payload
        }

    def _get(self, endpoint: str, params: dict = None):
        """GET genérico a Connekta."""
        if self.modo_simulacion:
            return self._simular(f'GET_{endpoint}', params or {})
        try:
            url = f'{self.base_url}/{endpoint}'
            r = requests.get(url, headers=self.headers, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] GET {endpoint}: {str(e)}')
            raise Exception(f'Error consultando Connekta: {str(e)}')

    def _post(self, conector: str, payload: dict):
        """POST genérico a conector dinámico Connekta."""
        if self.modo_simulacion:
            return self._simular(f'POST_{conector}', payload)
        try:
            url = f'{self.base_url}/{conector}'
            r = requests.post(url, headers=self.headers, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] POST {conector}: {str(e)}')
            raise Exception(f'Error enviando a Connekta: {str(e)}')

    # ==========================================
    # GETs — Extracción desde Siesa
    # ==========================================

    def get_pedidos_aprobados(self):
        """
        Trae pedidos de venta aprobados desde Siesa.
        CRÍTICO: retorna cantidad_disponible, NUNCA existencia física.
        """
        return self._get('pedidos-aprobados', {
            'bodega': self.bodega,
            'estado': 'APROBADO',
            'fields': 'numero_pedido,item_codigo,cantidad_disponible,fecha_entrega'
        })

    def get_ordenes_compra_aprobadas(self):
        """
        Trae OCs aprobadas para recepción de mercancía.
        Excluye OCs de importación — Siesa las maneja por módulo diferente.
        """
        return self._get('ordenes-compra', {
            'bodega': self.bodega,
            'estado': 'APROBADO',
            'importacion': 'false'
        })

    def get_existencia_item(self, item_codigo: str):
        """
        Consulta existencia real + clasificación ABC de un ítem en Siesa.
        Usado en conteo cíclico para conciliación en tiempo real.
        """
        return self._get('existencias', {
            'item_codigo': item_codigo,
            'bodega': self.bodega
        })

    # ==========================================
    # POSTs — Triggers a Siesa
    # ==========================================

    def marcar_pedido_cumplido(self, numero_pedido: str, items: list):
        """
        Trigger principal — marca pedido como CUMPLIDO.
        Siesa genera automáticamente: Remisión + descarga cuenta 14 + Factura electrónica.
        El WMS no factura. El WMS solo dispara.
        """
        payload = {
            'numero_pedido': numero_pedido,
            'estado': 'CUMPLIDO',
            'bodega': self.bodega,
            'items': [
                {
                    'item_codigo': i.get('producto_codigo'),
                    'cantidad_empacada': i.get('cantidad_empacada')
                }
                for i in items
            ]
        }
        logger.info(f'[CONNEKTA] Marcando pedido {numero_pedido} como CUMPLIDO')
        return self._post(self.conector_cumplido, payload)

    def confirmar_entrada_compras(self, numero_oc: str, items: list, es_parcial: bool = False):
        """
        Confirma entrada por compras en Siesa.
        Siesa debita cuenta 1435 automáticamente.
        es_parcial=True: la OC queda viva en Siesa esperando el resto.
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
        logger.info(f'[CONNEKTA] Confirmando entrada OC {numero_oc}')
        return self._post(self.conector_entrada, payload)

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str):
        """
        Ajuste de inventario tras conteo cíclico double-blind.
        motivo_codigo: 'AJ-ENT' (sobrante) o 'AJ-SAL' (faltante).
        cantidad: siempre positivo — el motivo define la dirección.
        """
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}. Usar AJ-ENT o AJ-SAL')

        payload = {
            'motivo_codigo': motivo_codigo,
            'bodega': self.bodega,
            'centro_operacion': self.centro_op,
            'item_codigo': item_codigo,
            'cantidad': abs(cantidad),
            'referencia': referencia
        }
        logger.info(f'[CONNEKTA] Ajuste {motivo_codigo} — {item_codigo}: {cantidad}')
        return self._post(self.conector_ajuste, payload)

    # ==========================================
    # Estado del gateway
    # ==========================================

    def estado(self):
        """Estado de la conexión con Connekta — visible en tab Siesa del dashboard."""
        return {
            'modo_simulacion': self.modo_simulacion,
            'connekta_configurado': not self.modo_simulacion,
            'url_configurada': bool(self.base_url),
            'credenciales_configuradas': bool(self.ikey and self.itoken),
            'bodega': self.bodega,
            'centro_operacion': self.centro_op,
            'conectores': {
                'cumplido': bool(self.conector_cumplido),
                'entrada': bool(self.conector_entrada),
                'ajuste': bool(self.conector_ajuste)
            },
            'mensaje': 'Listo para producción' if not self.modo_simulacion
                       else 'Configurar CONNEKTA_URL, CONNEKTA_IKEY, CONNEKTA_ITOKEN en Railway'
        }


# Instancia global — un solo gateway en todo el sistema
connekta = ConnektaGateway()
