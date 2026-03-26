"""
Gateway canónico para Connekta/Siesa.
Todas las comunicaciones con Siesa pasan por aquí — un solo punto de integración.
Funciona en modo simulación cuando no hay credenciales configuradas.
"""
import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class ConnektaGateway:

    def __init__(self):
        self.base_url = os.getenv('CONNEKTA_URL', '')
        self.ikey = os.getenv('CONNEKTA_IKEY', '')
        self.itoken = os.getenv('CONNEKTA_ITOKEN', '')
        self.conector_cumplido = os.getenv('CONNEKTA_CONECTOR_CUMPLIDO', '')
        self.conector_entrada = os.getenv('CONNEKTA_CONECTOR_ENTRADA', '')
        self.conector_salida = os.getenv('CONNEKTA_CONECTOR_SALIDA', '')
        self.modo_simulacion = not all([self.base_url, self.ikey, self.itoken])

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación activo — credenciales no configuradas')

    @property
    def headers(self):
        return {
            'Content-Type': 'application/json',
            'I-Key': self.ikey,
            'I-Token': self.itoken
        }

    def _simular_respuesta(self, operacion: str, payload: dict):
        """Respuesta simulada cuando no hay credenciales."""
        logger.info(f'[CONNEKTA SIMULADO] {operacion}: {payload}')
        return {
            'simulado': True,
            'operacion': operacion,
            'timestamp': datetime.utcnow().isoformat(),
            'mensaje': f'Operación {operacion} simulada exitosamente',
            'payload_enviado': payload
        }

    def marcar_pedido_cumplido(self, numero_pedido: str, datos_packing: dict):
        """
        Marca un pedido de Siesa como Cumplido.
        Esto dispara automáticamente: Remisión + Factura electrónica en Siesa.
        """
        payload = {
            'numero_pedido': numero_pedido,
            'estado': 'CUMPLIDO',
            'fecha_cumplimiento': datetime.utcnow().isoformat(),
            'items_empacados': datos_packing.get('items', []),
            'observaciones': datos_packing.get('observaciones', ''),
            'origen': 'WMS_PACKING'
        }

        if self.modo_simulacion:
            return self._simular_respuesta('MARCAR_CUMPLIDO', payload)

        try:
            url = f'{self.base_url}/{self.conector_cumplido}'
            response = requests.post(url, json=payload, headers=self.headers, timeout=30)
            response.raise_for_status()
            logger.info(f'[CONNEKTA] Pedido {numero_pedido} marcado como CUMPLIDO')
            return response.json()
        except requests.exceptions.Timeout:
            logger.error(f'[CONNEKTA] Timeout al marcar pedido {numero_pedido}')
            raise Exception('Connekta no respondió en tiempo esperado — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] Error al marcar pedido {numero_pedido}: {str(e)}')
            raise Exception(f'Error comunicando con Siesa: {str(e)}')

    def get_pedidos_aprobados(self, almacen_codigo: str = None):
        """
        Trae pedidos de venta aprobados desde Siesa.
        Retorna Cantidad Disponible — NUNCA existencia física.
        """
        if self.modo_simulacion:
            return self._simular_respuesta('GET_PEDIDOS_APROBADOS', {
                'almacen': almacen_codigo
            })

        try:
            url = f'{self.base_url}/pedidos/aprobados'
            params = {'almacen': almacen_codigo} if almacen_codigo else {}
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] Error obteniendo pedidos: {str(e)}')
            raise Exception(f'Error obteniendo pedidos de Siesa: {str(e)}')

    def enviar_ajuste_inventario(self, tipo: str, items: list, referencia: str):
        """
        Envía ajuste de inventario a Siesa.
        tipo: 'ENTRADA' (sobrante/conector 05) o 'SALIDA' (faltante/conector 06)
        """
        conector = self.conector_entrada if tipo == 'ENTRADA' else self.conector_salida

        payload = {
            'tipo': tipo,
            'referencia': referencia,
            'fecha': datetime.utcnow().isoformat(),
            'items': items,
            'origen': 'WMS_CONTEO_CICLICO'
        }

        if self.modo_simulacion:
            return self._simular_respuesta(f'AJUSTE_{tipo}', payload)

        try:
            url = f'{self.base_url}/{conector}'
            response = requests.post(url, json=payload, headers=self.headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] Error enviando ajuste {tipo}: {str(e)}')
            raise Exception(f'Error enviando ajuste a Siesa: {str(e)}')
        
    def confirmar_entrada_compras(self, numero_oc: str, datos_recepcion: dict):
      
        payload = {
            'numero_oc': numero_oc,
            'fecha_entrada': datetime.utcnow().isoformat(),
            'es_parcial': datos_recepcion.get('es_parcial', False),
            'items': datos_recepcion.get('items', []),
            'codigo_recepcion_wms': datos_recepcion.get('codigo_recepcion', ''),
            'observaciones': datos_recepcion.get('observaciones', ''),
            'origen': 'WMS_RECEPCION'
        }

        if self.modo_simulacion:
            return self._simular_respuesta('CONFIRMAR_ENTRADA_COMPRAS', payload)

        try:
            url = f'{self.base_url}/entradas/compras'
            response = requests.post(
                url, json=payload, headers=self.headers, timeout=30
            )
            response.raise_for_status()
            logger.info(f'[CONNEKTA] Entrada compras confirmada para OC {numero_oc}')
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] Error confirmando entrada OC {numero_oc}: {str(e)}')
            raise Exception(f'Error confirmando entrada en Siesa: {str(e)}')

    def estado(self):
        """Verifica el estado de la conexión con Connekta."""
        return {
            'modo_simulacion': self.modo_simulacion,
            'connekta_configurado': not self.modo_simulacion,
            'url_configurada': bool(self.base_url),
            'credenciales_configuradas': bool(self.ikey and self.itoken),
            'mensaje': 'Listo para producción' if not self.modo_simulacion else 'Configurar CONNEKTA_URL, CONNEKTA_IKEY, CONNEKTA_ITOKEN en variables de entorno'
        }


# Instancia global — un solo gateway en todo el sistema
connekta = ConnektaGateway()