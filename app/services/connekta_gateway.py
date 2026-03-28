"""
Gateway Connekta — Arquitectura doble autopista certificada.

GET  → v3/ejecutarconsultaestandar
POST → v3/conectoresimportarestandar  ← URL REAL confirmada desde Ver Guía

Sintaxis GET confirmada:
  Headers:    ConniKey / ConniToken
  Textos:     comillas dobles → f150_id="NB1"
  Enteros:    sin comillas    → f430_ind_estado=1
  Paginacion: numPag=1|tamPag=100

Body POST confirmado desde Ver Guía conector 142945:
  Secciones: Inicial, Remision, Movtoventascomercial, Final
  F430_ID_TIPO_DOCTO + F430_CONSEC_DOCTO = identifican el pedido origen
  f470_referencia_item = código del producto
  f470_cant_base = cantidad empacada
  f470_id_bodega = bodega NB1
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
        self.bodega = os.getenv('CONNEKTA_BODEGA', 'NB1')
        self.centro_op = os.getenv('CONNEKTA_CENTRO_OP', '003')

        # APIs V2 para GETs
        self.api_pedidos = os.getenv('CONNEKTA_API_PEDIDOS', 'API_v2_Ventas_Pedidos')
        self.api_ordenes = os.getenv('CONNEKTA_API_ORDENES', 'API_v2_Compras_Ordenes')
        self.api_inventario = os.getenv('CONNEKTA_API_INVENTARIO', 'API_v2_Inventarios_InvFecha')
        self.api_barras = os.getenv('CONNEKTA_API_BARRAS', 'API_v2_ItemsBarras')

        # Conectores V1 Genery Transfer para POSTs
        self.conector_despacho = os.getenv('CONNEKTA_CONECTOR_DESPACHO', '142945')
        self.conector_entrada = os.getenv('CONNEKTA_CONECTOR_ENTRADA', '142948')
        self.conector_ajuste = os.getenv('CONNEKTA_CONECTOR_AJUSTE', '142951')

        # URLs base — ambas en v3, confirmadas desde Ver Guía
        self.url_get = 'https://serviciosqa.siesacloud.com/api/siesa/v3/ejecutarconsultaestandar'
        self.url_post = 'https://serviciosqa.siesacloud.com/api/siesa/v3/conectoresimportarestandar'

        self.modo_simulacion = not all([self.ikey, self.itoken])

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación — faltan: CONNEKTA_IKEY, CONNEKTA_ITOKEN')

    @property
    def headers(self):
        """Headers confirmados desde Ver Guía de Connekta."""
        return {
            'Content-Type': 'application/json',
            'ConniKey': self.ikey,
            'ConniToken': self.itoken
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
        Siempre filtrado. Sin filtros = colapso de memoria en PDA.
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

    def _post(self, id_conector: str, nombre_conector: str, payload: dict):
        """
        POST → v3/conectoresimportarestandar
        Sin idSistema — confirmado desde Ver Guía.
        Params: idCompania, idDocumento, nombreDocumento.
        """
        if self.modo_simulacion:
            return self._simular(f'POST_{id_conector}', payload)

        params = {
            'idCompania': self.id_compania,
            'idDocumento': id_conector,
            'nombreDocumento': nombre_conector
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
            logger.error(f'[CONNEKTA] POST {id_conector}: {e}')
            raise Exception(f'Error inyectando en Siesa: {e}')

    # ==========================================
    # GETs
    # ==========================================

    def get_pedidos_aprobados(self):
        """
        API_v2_Ventas_Pedidos — cola de picking.
        Solo ítems con cant_pendiente > 0.
        """
        parametros = (
            f'f150_id="{self.bodega}"'
            f' AND f430_id_co="{self.centro_op}"'
            f' AND f430_ind_estado=1'
        )
        resultado = self._get(self.api_pedidos, {
            'paginacion': 'numPag=1|tamPag=100',
            'parametros': parametros
        })

        if self.modo_simulacion or resultado.get('simulado'):
            return resultado

        items_raw = resultado.get('detalle', {}).get('Table', [])
        if not items_raw:
            return {'codigo': 0, 'total_siesa': 0, 'total_pendientes': 0, 'items': []}

        items_pendientes = []
        for item in items_raw:
            try:
                cant_pedida = float(item.get('f431_cant1_pedida', 0))
                cant_remisionada = float(item.get('f431_cant1_remisionada', 0))
                cant_pendiente = cant_pedida - cant_remisionada
                if cant_pendiente > 0:
                    items_pendientes.append({
                        'numero_pedido': f"{item.get('f430_id_tipo_docto','').strip()}{item.get('f430_consec_docto','')}",
                        'tipo_docto': item.get('f430_id_tipo_docto', '').strip(),
                        'consec_docto': item.get('f430_consec_docto'),
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
                logger.warning(f'[CONNEKTA] Item inválido: {e}')
                continue

        logger.info(f'[CONNEKTA] {len(items_pendientes)} pendientes de {len(items_raw)}')
        return {
            'codigo': 0,
            'total_siesa': len(items_raw),
            'total_pendientes': len(items_pendientes),
            'items': items_pendientes
        }

    def get_ordenes_compra_aprobadas(self):
        """API_v2_Compras_Ordenes — muelle de recepción ciega."""
        parametros = (
            f'f150_id="{self.bodega}"'
            f' AND f430_id_co="{self.centro_op}"'
            f' AND f430_ind_estado=1'
        )
        return self._get(self.api_ordenes, {
            'paginacion': 'numPag=1|tamPag=100',
            'parametros': parametros
        })

    def get_inventario_fecha(self, item_codigo: str):
        """API_v2_Inventarios_InvFecha — existencia real para conteo cíclico."""
        return self._get(self.api_inventario, {
            'paginacion': 'numPag=1|tamPag=10',
            'parametros': f'f120_referencia="{item_codigo}" AND f150_id="{self.bodega}"'
        })

    def get_item_por_barras(self, codigo_barras: str):
        """API_v2_ItemsBarras — traduce EAN del escáner al código Siesa."""
        return self._get(self.api_barras, {
            'paginacion': 'numPag=1|tamPag=5',
            'parametros': f'f178_id="{codigo_barras}"'
        })

    # ==========================================
    # POSTs — Estructura oficial desde Ver Guía
    # ==========================================

    def trigger_despacho(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                          items: list):
        """
        142945 → API_v1_Ventas_Comercial_RemisionPedido
        Genera remisión desde pedido — descarga inventario cuenta 14.
        Siesa factura automáticamente por debajo.

        Campos clave confirmados desde Ver Guía:
          F430_ID_TIPO_DOCTO  → tipo documento del pedido origen
          F430_CONSEC_DOCTO   → consecutivo del pedido origen
          f470_referencia_item → código del producto
          f470_cant_base       → cantidad empacada
          f470_id_bodega       → bodega NB1
        """
        fecha_hoy = datetime.utcnow().strftime('%Y-%m-%d')

        payload = {
            'Inicial': [
                {'F_CIA': self.id_compania}
            ],
            'Remision': [
                {
                    'F_CIA': self.id_compania,
                    'F_CONSEC_AUTO_REG': '',
                    'F350_ID_CO': self.centro_op,
                    'F350_ID_TIPO_DOCTO': '',
                    'F350_CONSEC_DOCTO': '',
                    'F350_FECHA': fecha_hoy,
                    'F350_IND_ESTADO': '',
                    'F350_IND_IMPRESION': '',
                    'F430_ID_TIPO_DOCTO': tipo_docto_pedido,
                    'F430_CONSEC_DOCTO': consec_docto_pedido,
                    'f462_id_vehiculo': '',
                    'f462_id_tercero_transp': '',
                    'f462_id_sucursal_transp': '',
                    'f462_id_tercero_conductor': '',
                    'f462_nombre_conductor': '',
                    'f462_identif_conductor': '',
                    'f462_numero_guia': '',
                    'f462_cajas': '',
                    'f462_peso': '',
                    'f462_volumen': '',
                    'f462_valor_seguros': '',
                    'f462_notas': '',
                    'f460_id_cond_pago': ''
                }
            ],
            'Movtoventascomercial': [
                {
                    'F_CIA': self.id_compania,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': '',
                    'f470_consec_docto': '',
                    'f470_nro_registro': '',
                    'f470_id_bodega': self.bodega,
                    'f470_id_ubicacion_aux': '',
                    'f470_id_lote': '',
                    'f470_id_concepto': '',
                    'f470_id_motivo': '',
                    'f470_ind_obsequio': '',
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': '',
                    'f470_id_proyecto': '',
                    'f470_id_lista_precio': '',
                    'f470_id_unidad_precio': '',
                    'f470_id_unidad_medida': '',
                    'f470_cant_base': i.get('cantidad_empacada'),
                    'f470_cant_2': '',
                    'f470_vlr_bruto': '',
                    'f470_ind_naturaleza': '',
                    'f470_ind_solo_valor': '',
                    'f470_ind_impto_asumido': '',
                    'f470_notas': '',
                    'f470_desc_variable': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': '',
                    'f470_id_item': '',
                    'f470_referencia_item': i.get('producto_codigo'),
                    'f470_codigo_barras': '',
                    'f470_id_ext1_detalle': '',
                    'f470_id_ext2_detalle': '',
                    'f470_id_un_movto': self.centro_op,
                    'f470_id_causal_devol': ''
                }
                for i in items
            ],
            'Final': [
                {'F_CIA': self.id_compania}
            ]
        }

        logger.info(f'[CONNEKTA] Despacho pedido {tipo_docto_pedido}{consec_docto_pedido}')
        return self._post(
            self.conector_despacho,
            'API_v1_Ventas_Comercial_RemisionPedido',
            payload
        )

    def confirmar_entrada_compras(self, numero_oc: str, items: list,
                                   es_parcial: bool = False):
        """
        142948 → API_v1_Compras_Comercial_EntradaOC
        Body pendiente de confirmar con Ver Guía del conector.
        """
        payload = {
            'Inicial': [{'F_CIA': self.id_compania}],
            'Entrada': [
                {
                    'F_CIA': self.id_compania,
                    'numero_oc': numero_oc,
                    'bodega': self.bodega,
                    'centro_op': self.centro_op,
                    'es_parcial': es_parcial,
                    'fecha': datetime.utcnow().strftime('%Y-%m-%d')
                }
            ],
            'Movimiento': [
                {
                    'F_CIA': self.id_compania,
                    'referencia_item': i.get('producto_codigo'),
                    'cantidad': i.get('cantidad_recibida'),
                    'bodega': self.bodega
                }
                for i in items
            ],
            'Final': [{'F_CIA': self.id_compania}]
        }
        logger.info(f'[CONNEKTA] Entrada OC {numero_oc}')
        return self._post(
            self.conector_entrada,
            'API_v1_Compras_Comercial_EntradaOC',
            payload
        )

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Body pendiente de confirmar con Ver Guía del conector.
        AJ-ENT: sobrante. AJ-SAL: faltante.
        """
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}')

        payload = {
            'Inicial': [{'F_CIA': self.id_compania}],
            'Documento': [
                {
                    'F_CIA': self.id_compania,
                    'motivo': motivo_codigo,
                    'bodega': self.bodega,
                    'centro_op': self.centro_op,
                    'fecha': datetime.utcnow().strftime('%Y-%m-%d'),
                    'referencia': referencia
                }
            ],
            'Movimiento': [
                {
                    'F_CIA': self.id_compania,
                    'referencia_item': item_codigo,
                    'cantidad': abs(cantidad),
                    'bodega': self.bodega
                }
            ],
            'Final': [{'F_CIA': self.id_compania}]
        }
        logger.info(f'[CONNEKTA] Ajuste {motivo_codigo} {item_codigo}:{cantidad}')
        return self._post(
            self.conector_ajuste,
            'API_v1_Inventarios_Comercial_DocumentoInv',
            payload
        )

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
                       else 'Pendiente: CONNEKTA_IKEY, CONNEKTA_ITOKEN'
        }


connekta = ConnektaGateway()
