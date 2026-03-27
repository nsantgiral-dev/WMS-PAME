"""
Gateway Connekta — URLs reales de Siesa Cloud QA.
GET:  v3/ejecutarconsultaestandar  → APIs estándar nativas
POST: v3.1/conectoresimportar      → Conectores dinámicos (Genery Transfer)
Datos confirmados desde el campo de batalla. Cero suposiciones.
"""
import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class ConnektaGateway:

    def __init__(self):
        # Credenciales
        self.ikey = os.getenv('CONNEKTA_IKEY', '')
        self.itoken = os.getenv('CONNEKTA_ITOKEN', '')

        # Empresa y configuración Siesa
        self.id_compania = os.getenv('CONNEKTA_ID_COMPANIA', '8215')
        self.id_sistema = os.getenv('CONNEKTA_ID_SISTEMA', '')
        self.bodega = os.getenv('CONNEKTA_BODEGA', '001')
        self.centro_op = os.getenv('CONNEKTA_CENTRO_OP', '001')

        # URLs base separadas — v3 para GET, v3.1 para POST
        self.url_get = 'https://serviciosqa.siesacloud.com/api/siesa/v3/ejecutarconsultaestandar'
        self.url_post = 'https://serviciosqa.siesacloud.com/api/siesa/v3.1/conectoresimportar'

        # IDs de conectores dinámicos — traer de Connekta > Conectores
        self.conector_despacho = os.getenv('CONNEKTA_CONECTOR_DESPACHO', '')
        self.conector_entrada = os.getenv('CONNEKTA_CONECTOR_ENTRADA', '')
        self.conector_ajuste = os.getenv('CONNEKTA_CONECTOR_AJUSTE', '')

        self.modo_simulacion = not all([self.ikey, self.itoken, self.id_sistema])

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación — faltan variables en Railway')

    @property
    def headers(self):
        """Headers oficiales Connekta — coniki y conitoken."""
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

    def _get(self, nombre_api: str, filtros: dict = None):
        """
        GET → v3/ejecutarconsultaestandar
        Siempre con idCompania + descripcion + filtros de bodega.
        Sin filtros = miles de registros = colapso de memoria. Prohibido.
        """
        if self.modo_simulacion:
            return self._simular(f'GET_{nombre_api}', filtros)

        params = {
            'idCompania': self.id_compania,
            'descripcion': nombre_api
        }
        if filtros:
            params.update(filtros)

        try:
            r = requests.get(
                self.url_get,
                headers=self.headers,
                params=params,
                timeout=30
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] GET {nombre_api}: {e}')
            raise Exception(f'Error consultando Siesa: {e}')

    def _post(self, id_conector: str, payload: dict):
        """
        POST → v3.1/conectoresimportar
        Inyecta documento transaccional real en Siesa.
        idDocumento es el ID numérico del conector en Connekta.
        Body con secciones: inicial, documento, movimiento.
        """
        if self.modo_simulacion:
            return self._simular(f'POST_CONECTOR_{id_conector}', payload)

        params = {
            'idCompania': self.id_compania,
            'idSistema': self.id_sistema,
            'idDocumento': id_conector
        }

        try:
            r = requests.post(
                self.url_post,
                headers=self.headers,
                params=params,
                json=payload,
                timeout=30
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] POST conector {id_conector}: {e}')
            raise Exception(f'Error inyectando en Siesa: {e}')

    # ==========================================
    # GETs — v3 ejecutarconsultaestandar
    # ==========================================

    def get_pedidos_aprobados(self):
        """
        API_v2_Ventas_Pedidos
        Cola de picking — solo aprobados de nuestra bodega.
        """
        return self._get('API_v2_Ventas_Pedidos', {
            'indicador_estado': 'A',
            'bodega': self.bodega,
            'centro_operacion': self.centro_op
        })

    def get_ordenes_compra_aprobadas(self):
        """
        API_v2_Compras_Ordenes
        Muelle de recepción — aprobadas, sin importaciones.
        """
        return self._get('API_v2_Compras_Ordenes', {
            'indicador_estado': 'A',
            'bodega': self.bodega,
            'es_importacion': 0
        })

    def get_inventario_fecha(self, item_codigo: str):
        """
        API_v2_Inventarios_InvFecha
        Existencia real para conciliación conteo cíclico.
        """
        return self._get('API_v2_Inventarios_InvFecha', {
            'item_codigo': item_codigo,
            'bodega': self.bodega,
            'fecha': datetime.utcnow().strftime('%Y-%m-%d')
        })

    def get_item_por_barras(self, codigo_barras: str):
        """
        API_v2_ItemsBarras
        Traduce EAN del escáner láser al código interno de Siesa.
        """
        return self._get('API_v2_ItemsBarras', {
            'codigo_barras': codigo_barras
        })

    # ==========================================
    # POSTs — v3.1 conectoresimportar
    # ==========================================

    def trigger_despacho(self, numero_pedido: str, items: list):
        """
        Conector despacho — inyecta documento transaccional completo.
        Siesa descarga inventario, aplica costo promedio,
        genera remisión y factura electrónica automáticamente.
        El WMS no factura. El WMS solo inyecta el documento.
        NOTA: estructura inicial/documento/movimiento
        se confirma con el "Ver Guía" del conector en Connekta.
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
        return self._post(self.conector_despacho, payload)

    def confirmar_entrada_compras(self, numero_oc: str, items: list,
                                   es_parcial: bool = False):
        """
        Conector entrada por OC.
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
        return self._post(self.conector_entrada, payload)

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str):
        """
        Conector ajuste físico — tras conteo cíclico double-blind.
        motivo_codigo: 'AJ-ENT' (sobrante) o 'AJ-SAL' (faltante).
        cantidad: siempre positivo.
        """
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}')

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
        return self._post(self.conector_ajuste, payload)

    # ==========================================
    # Estado
    # ==========================================

    def estado(self):
        return {
            'modo_simulacion': self.modo_simulacion,
            'connekta_configurado': not self.modo_simulacion,
            'credenciales_configuradas': bool(self.ikey and self.itoken),
            'id_compania': self.id_compania,
            'bodega': self.bodega,
            'centro_operacion': self.centro_op,
            'urls': {
                'get': self.url_get,
                'post': self.url_post
            },
            'conectores': {
                'despacho': bool(self.conector_despacho),
                'entrada': bool(self.conector_entrada),
                'ajuste': bool(self.conector_ajuste)
            },
            'mensaje': 'Listo para producción' if not self.modo_simulacion
                       else 'Faltan variables: CONNEKTA_IKEY, CONNEKTA_ITOKEN, CONNEKTA_ID_SISTEMA'
        }


connekta = ConnektaGateway()
