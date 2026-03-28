"""
Gateway Connekta V2/V3 — URLs reales Siesa Cloud QA confirmadas.
GET:  v3/ejecutarconsultaestandar  — APIs estándar con filtros reales
POST: v3.1/conectoresimportar      — Conectores Genery Transfer

Campos reales confirmados desde Siesa:
  f430_id_co       → Centro de operación
  f150_id          → Bodega
  f430_ind_estado  → Estado del pedido
  f120_referencia  → Código del producto
  f431_cant1_pedida      → Cantidad pedida
  f431_cant1_remisionada → Cantidad ya despachada

Headers reales que acepta Siesa: conniKey y conniToken.
"""
import os
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class ConnektaGateway:

    def __init__(self):
        self.ikey = os.getenv('CONNEKTA_IKEY', '')
        self.itoken = os.getenv('CONNEKTA_ITOKEN', '')
        self.id_compania = os.getenv('CONNEKTA_ID_COMPANIA', '8215')
        self.id_sistema = os.getenv('CONNEKTA_ID_SISTEMA', '')
        self.bodega = os.getenv('CONNEKTA_BODEGA', 'NB1')
        self.centro_op = os.getenv('CONNEKTA_CENTRO_OP', '003')

        # APIs V2 para GETs
        self.api_pedidos = os.getenv('CONNEKTA_API_PEDIDOS', 'API_v2_Ventas_Pedidos')
        self.api_ordenes = os.getenv('CONNEKTA_API_ORDENES', 'API_v2_Compras_Ordenes')
        self.api_inventario = os.getenv('CONNEKTA_API_INVENTARIO', 'API_v2_Inventarios_InvFecha')
        self.api_barras = os.getenv('CONNEKTA_API_BARRAS', 'API_v2_ItemsBarras')

        # Conectores Genery Transfer para POSTs
        self.conector_despacho = os.getenv('CONNEKTA_CONECTOR_DESPACHO', '142945')
        self.conector_entrada = os.getenv('CONNEKTA_CONECTOR_ENTRADA', '142948')
        self.conector_ajuste = os.getenv('CONNEKTA_CONECTOR_AJUSTE', '142951')

        # URLs base reales confirmadas
        self.url_get = 'https://serviciosqa.siesacloud.com/api/siesa/v3/ejecutarconsultaestandar'
        self.url_post = 'https://serviciosqa.siesacloud.com/api/siesa/v3.1/conectoresimportar'

        self.modo_simulacion = not all([self.ikey, self.itoken, self.id_sistema])

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación — faltan: CONNEKTA_IKEY, CONNEKTA_ITOKEN, CONNEKTA_ID_SISTEMA')

    @property
    def headers(self):
        """
        Headers reales confirmados por Siesa en campo de batalla.
        conniKey y conniToken — no coniki/conitoken.
        """
        return {
            'Content-Type': 'application/json',
            'conniKey': self.ikey,
            'conniToken': self.itoken
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

    def _get(self, nombre_api: str, params_extra: dict = None):
        """
        GET → v3/ejecutarconsultaestandar
        Python maneja URL encoding automáticamente con requests.
        Siempre filtrado — sin filtros = colapso de memoria en PDA.
        """
        if self.modo_simulacion:
            return self._simular(f'GET_{nombre_api}', params_extra)

        params = {
            'idCompania': self.id_compania,
            'descripcion': nombre_api
        }
        if params_extra:
            params.update(params_extra)

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
        idDocumento = ID numérico 6 dígitos Genery Transfer.
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
    # GETs — Extracción desde Siesa
    # ==========================================

    def get_pedidos_aprobados(self):
        """
        API_v2_Ventas_Pedidos — cola de picking.
        Filtros con nombres reales confirmados del diccionario Siesa:
          f150_id      = bodega
          f430_id_co   = centro de operación
          f430_ind_estado = estado (A = aprobado)
        Regla WMS: solo ítems con saldo pendiente > 0.
        cant_pendiente = cant1_pedida - cant1_remisionada
        """
        params = {
            'paginacion': 'numPag=1|tamPag=100',
            'parametros': f"f150_id='{self.bodega}' AND f430_id_co='{self.centro_op}' AND f430_ind_estado='A'"
        }
        resultado = self._get(self.api_pedidos, params)

        if self.modo_simulacion or resultado.get('simulado'):
            return resultado

        # Procesar en servidor — enviar a PDA solo ítems pendientes
        items_raw = resultado.get('detalle', {}).get('Table', [])
        items_pendientes = []

        for item in items_raw:
            try:
                cant_pedida = float(item.get('f431_cant1_pedida', 0))
                cant_remisionada = float(item.get('f431_cant1_remisionada', 0))
                cant_pendiente = cant_pedida - cant_remisionada

                if cant_pendiente > 0:
                    items_pendientes.append({
                        'numero_pedido': f"{item.get('f430_id_tipo_docto','').strip()}{item.get('f430_consec_docto','')}",
                        'centro_op': item.get('f430_id_co'),
                        'bodega': item.get('f150_id'),
                        'item_codigo': item.get('f120_referencia'),
                        'item_descripcion': item.get('f120_descripcion'),
                        'item_id_siesa': item.get('f120_id'),
                        'cantidad_pedida': cant_pedida,
                        'cantidad_remisionada': cant_remisionada,
                        'cantidad_pendiente': cant_pendiente,
                        'cliente': item.get('f200_razon_social_pedido_fact'),
                        'fecha_entrega': item.get('f430_fecha_entrega'),
                        'estado': item.get('f430_ind_estado')
                    })
            except (ValueError, TypeError) as e:
                logger.warning(f'[CONNEKTA] Item con datos inválidos: {e}')
                continue

        logger.info(f'[CONNEKTA] {len(items_pendientes)} ítems pendientes de {len(items_raw)} en Siesa')
        return {
            'codigo': 0,
            'total_siesa': len(items_raw),
            'total_pendientes': len(items_pendientes),
            'items': items_pendientes
        }

    def get_ordenes_compra_aprobadas(self):
        """
        API_v2_Compras_Ordenes — muelle de recepción.
        """
        return self._get(self.api_ordenes, {
            'paginacion': 'numPag=1|tamPag=100',
            'parametros': f"f150_id='{self.bodega}' AND f430_id_co='{self.centro_op}'"
        })

    def get_inventario_fecha(self, item_codigo: str):
        """
        API_v2_Inventarios_InvFecha — existencia real para conteo cíclico.
        """
        return self._get(self.api_inventario, {
            'paginacion': 'numPag=1|tamPag=10',
            'parametros': f"f120_referencia='{item_codigo}' AND f150_id='{self.bodega}'"
        })

    def get_item_por_barras(self, codigo_barras: str):
        """
        API_v2_ItemsBarras — traduce EAN del escáner al código Siesa.
        """
        return self._get(self.api_barras, {
            'paginacion': 'numPag=1|tamPag=10',
            'parametros': f"f178_id='{codigo_barras}'"
        })

    # ==========================================
    # POSTs — Transacciones reales en Siesa
    # ==========================================

    def trigger_despacho(self, numero_pedido: str, items: list):
        """
        142945 → RemisionPedido
        Descarga inventario cuenta 14 + genera remisión.
        Siesa factura automáticamente.
        Estructura: confirmar con Ver Guía en Connekta.
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
        logger.info(f'[CONNEKTA] Despacho pedido {numero_pedido}')
        return self._post(self.conector_despacho, payload)

    def confirmar_entrada_compras(self, numero_oc: str, items: list,
                                   es_parcial: bool = False):
        """
        142948 → EntradaOC
        Debita cuenta 1435. OC queda viva si es parcial.
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
        142951 → DocumentoInv
        Ajuste físico tras conteo cíclico double-blind.
        AJ-ENT: sobrante. AJ-SAL: faltante.
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
        logger.info(f'[CONNEKTA] Ajuste {motivo_codigo} {item_codigo}:{cantidad}')
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
            'conectores_post': {
                'despacho': f'{self.conector_despacho} RemisionPedido',
                'entrada': f'{self.conector_entrada} EntradaOC',
                'ajuste': f'{self.conector_ajuste} DocumentoInv'
            },
            'mensaje': 'Listo para producción' if not self.modo_simulacion
                       else 'Pendiente: CONNEKTA_IKEY, CONNEKTA_ITOKEN, CONNEKTA_ID_SISTEMA'
        }


connekta = ConnektaGateway()
