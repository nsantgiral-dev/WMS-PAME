"""
Gateway Connekta — Arquitectura doble autopista certificada.

GET  → v3/ejecutarconsultaestandar
POST → v3/conectoresimportarestandar

Bodies oficiales confirmados desde Ver Guía en Connekta:
  142945 → RemisionPedido:  Inicial, Remision, Movtoventascomercial, Final
  142948 → EntradaOC:       Inicial, Documentos, Movimientos, Final
  142951 → DocumentoInv:    Inicial, Documentos, Movimientos, Final

Diccionario real Siesa confirmado:
  f430_id_co           → Centro de operación
  f150_id              → Bodega
  f430_ind_estado      → Estado (1=aprobado)
  f120_referencia      → Código producto
  f431_cant1_pedida    → Cantidad pedida
  f431_cant1_remisionada → Cantidad despachada
  f470_referencia_item → Código producto en movimiento
  f470_cant_base       → Cantidad del movimiento
  f470_id_bodega       → Bodega en movimiento
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

        self.api_pedidos = os.getenv('CONNEKTA_API_PEDIDOS', 'API_v2_Ventas_Pedidos')
        self.api_ordenes = os.getenv('CONNEKTA_API_ORDENES', 'API_v2_Compras_Ordenes')
        self.api_inventario = os.getenv('CONNEKTA_API_INVENTARIO', 'API_v2_Inventarios_InvFecha')
        self.api_barras = os.getenv('CONNEKTA_API_BARRAS', 'API_v2_ItemsBarras')

        self.conector_factura  = os.getenv('CONNEKTA_CONECTOR_FACTURA',  '238925')  # FacturaPedido (reemplaza 142945)
        self.conector_despacho = os.getenv('CONNEKTA_CONECTOR_DESPACHO', '142945')  # RemisionPedido (legacy — no usar)
        self.conector_entrada  = os.getenv('CONNEKTA_CONECTOR_ENTRADA',  '142948')
        self.conector_ajuste   = os.getenv('CONNEKTA_CONECTOR_AJUSTE',   '142951')
        self.api_clasificacion = os.getenv('CONNEKTA_API_CLASIFICACION', '238920')  # CLASIFICACION DE ITEMS
        # Traslados entre bodegas (puntos de venta)
        self.conector_requisicion_traslado = os.getenv('CONNEKTA_CONECTOR_REQ_TRASLADO', '174646')
        self.conector_transito_salida = os.getenv('CONNEKTA_CONECTOR_TRANSITO_SALIDA', '173076')
        self.conector_transito_entrada = os.getenv('CONNEKTA_CONECTOR_TRANSITO_ENTRADA', '173079')
        self.conector_transferencia_directa = os.getenv('CONNEKTA_CONECTOR_TRANSF_DIRECTA', '173066')
        # Tipo documento requisición de traslado (Siesa: Inventarios → Tipos de documento → clase 75)
        self.tipo_docto_req_traslado = os.getenv('SIESA_TIPO_DOCTO_TRASLADO', '')
        # Tipo documento tránsito salida/entrada (verificar con consultor Siesa)
        self.tipo_docto_transito_salida = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_SALIDA', '')
        self.tipo_docto_transito_entrada = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_ENTRADA', '')
        # Código del solicitante en requisiciones (Siesa: Inventarios → Solicitantes)
        self.req_solicitante = os.getenv('SIESA_REQ_SOLICITANTE', '')
        # Bodega de tránsito (verificar si existe en Siesa — si no, usar TransferenciaDirecta)
        self.bodega_transito = os.getenv('SIESA_BODEGA_TRANSITO', '')
        # id_cia interno de Siesa (distinto de idCompania Connekta)
        # Verificar en Siesa Enterprise → Parámetros de empresa → Código de compañía
        self.id_cia_siesa = os.getenv('SIESA_ID_CIA', '1')
        # Tipo documento factura electrónica en Siesa (normalmente 'FE')
        self.tipo_docto_factura  = os.getenv('SIESA_TIPO_DOCTO_FACTURA',  'FE')
        # Tipo de documento remisión en Siesa (ej. 'RS', 'REMI', 'RM') — legacy
        # Verificar en Siesa: Ventas → Tipos de documento → código del tipo Remisión
        self.tipo_docto_remision = os.getenv('SIESA_TIPO_DOCTO_REMISION', '')
        # Motivo de ventas en Siesa — campo requerido f470_id_motivo (pos 131, ancho 2)
        # Verificar en Siesa: Ventas → Motivos → código del motivo para ventas/remisiones
        self.motivo_ventas = os.getenv('SIESA_ID_MOTIVO_VENTAS', '')
        # Lista de precio en Siesa — campo requerido f470_id_lista_precio (pos 169, ancho 3)
        # Verificar en Siesa: Ventas → Listas de precio → código de la lista activa
        self.lista_precio = os.getenv('SIESA_LISTA_PRECIO', '')
        self.bodega_averias = os.getenv('SIESA_BODEGA_AVERIAS', 'AV1')
        self.tipo_docto_traslado = os.getenv('SIESA_TIPO_DOCTO_TRASLADO', 'TRA')
        self.motivo_traslado = os.getenv('SIESA_MOTIVO_TRASLADO', '01')

        _base = os.getenv('CONNEKTA_URL', 'https://serviciosqa.siesacloud.com').rstrip('/')
        self.id_sistema = os.getenv('CONNEKTA_ID_SISTEMA', '')
        self.url_get = f'{_base}/api/siesa/v3/ejecutarconsultaestandar'
        self.url_get_dinamico = f'{_base}/api/connekta/v3/ejecutarconsulta'
        self.url_post = f'{_base}/api/siesa/v3/conectoresimportarestandar'
        self.url_post_dinamico = f'{_base}/api/siesa/v3.1/conectoresimportar'

        self.modo_simulacion = not all([self.ikey, self.itoken])
        # MODO_ENSAYO: credenciales reales, GETs reales, POSTs bloqueados en servidor.
        # Activar con variable de entorno MODO_ENSAYO=true en Railway para pruebas UX.
        # Desactivar (borrar la variable) para producción real.
        self.modo_ensayo = os.getenv('MODO_ENSAYO', '').lower() == 'true'

        if self.modo_simulacion:
            logger.warning('[CONNEKTA] Modo simulación — faltan: CONNEKTA_IKEY, CONNEKTA_ITOKEN')
        elif self.modo_ensayo:
            logger.warning('[CONNEKTA] MODO ENSAYO activo — GETs reales, POSTs bloqueados en servidor')

    @property
    def headers(self):
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
        if self.modo_simulacion:
            return self._simular(f'GET_{nombre_api}', params_extra)

        params = {'idCompania': self.id_compania, 'descripcion': nombre_api}
        if params_extra:
            params.update(params_extra)

        try:
            r = requests.get(self.url_get, headers=self.headers, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] GET {nombre_api}: {e}')
            raise Exception(f'Error consultando Siesa: {e}')

    def _post(self, id_conector: str, nombre_conector: str, payload: dict,
              url: str = None, extra_params: dict = None):
        if self.modo_simulacion:
            return self._simular(f'POST_{id_conector}', payload)

        if self.modo_ensayo:
            logger.info(
                f'[CONNEKTA ENSAYO] POST bloqueado — conector={id_conector} '
                f'nombre={nombre_conector}\nPAYLOAD:\n{payload}'
            )
            return {
                'modo_ensayo': True,
                'conector': id_conector,
                'nombre': nombre_conector,
                'mensaje': 'POST bloqueado en modo ensayo — payload certificado, sin impacto en Siesa',
                'payload': payload,
                'timestamp': datetime.utcnow().isoformat()
            }

        params = {
            'idCompania': self.id_compania,
            'idDocumento': id_conector,
            'nombreDocumento': nombre_conector
        }
        if extra_params:
            params.update(extra_params)

        try:
            r = requests.post(url or self.url_post, headers=self.headers, params=params, json=payload, timeout=30)
            if not r.ok:
                try:
                    detalle = r.json()
                except Exception:
                    detalle = r.text
                logger.error(f'[CONNEKTA] POST {id_conector} HTTP {r.status_code}: {detalle}')
                raise Exception(f'Siesa rechazó el documento (HTTP {r.status_code}): {detalle}')
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] POST {id_conector}: {e}')
            raise Exception(f'Error inyectando en Siesa: {e}')

    # ==========================================
    # GETs
    # ==========================================

    def get_pedidos_aprobados(self, sin_filtros: bool = False):
        """
        Cola viva de picking: filtra por CO y estado directo en Connekta.
        Sintaxis oficial: strings con comillas dobles simples ''valor''.
        Pagina solo los resultados filtrados (~pocos registros).
        """
        if self.modo_simulacion:
            return self._simular('GET_pedidos_aprobados')

        # Sintaxis oficial Connekta: strings con ''valor'', enteros sin comillas
        if sin_filtros:
            parametros = 'f430_ind_estado = 3'
        else:
            # estado=3 → Comprometido: inventario físicamente reservado en Siesa
            # estado=2 (Aprobado) NO entra — el inventario no está reservado aún
            parametros = f"f430_id_co = ''{self.centro_op}'' AND f430_ind_estado = 3"

        all_items = []
        for pag in range(1, 200):
            res = self._get(self.api_pedidos, {
                'paginacion': f'numPag={pag}|tamPag=100',
                'parametros': parametros
            })
            rows = res.get('detalle', {}).get('Table', [])
            all_items.extend(rows)
            if len(rows) < 100:
                break

        items_pendientes = []
        for item in all_items:
            if not sin_filtros:
                if item.get('f150_id', '').strip() != self.bodega:
                    continue
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

        logger.info(f'[CONNEKTA] pedidos: {len(items_pendientes)} pendientes de {len(all_items)} CO{self.centro_op}')
        return {
            'codigo': 0,
            'total_siesa': len(all_items),
            'total_pendientes': len(items_pendientes),
            'items': items_pendientes
        }

    def get_ordenes_compra_aprobadas(self, sin_filtros: bool = False):
        """API_v2_Compras_Ordenes — muelle de recepción ciega.
        Pagina automáticamente (tamPag=100) hasta agotar los registros,
        porque la API no acepta filtros por CO/bodega.
        """
        base_params = {} if sin_filtros else {'parametros': 'f420_ind_estado=1'}
        todos = []
        for pag in range(1, 6):  # máximo 5 páginas = 500 items
            params = {**base_params, 'paginacion': f'numPag={pag}|tamPag=100'}
            resp = self._get(self.api_ordenes, params)
            if self.modo_simulacion:
                return resp
            rows = resp.get('detalle', {}).get('Table', [])
            if not rows or (len(rows) == 1 and 'alerta' in rows[0]):
                break
            todos.extend(rows)
            if len(rows) < 100:
                break
        return {'detalle': {'Table': todos}}

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

    def get_items_catalogo(self, pagina: int = 1):
        """API_v2_Items — catálogo completo de productos Siesa (para sync)."""
        api_items = os.getenv('CONNEKTA_API_ITEMS', 'API_v2_Items')
        return self._get(api_items, {
            'paginacion': f'numPag={pagina}|tamPag=100'
        })

    def get_clasificacion_items(self, pagina: int = 1):
        """
        238920 — CLASIFICACION DE ITEMS (conector dinámico)
        Devuelve la clasificación ABC por ítem directamente desde Siesa.
        Reemplaza la carga manual de CSV del reporte 'Recalculo de rotación ABC'.
        Los campos exactos se descubren con /api/siesa/debug-clasificacion-raw.
        """
        return self._get(self.api_clasificacion, {
            'paginacion': f'numPag={pagina}|tamPag=100'
        })

    def get_monitor_facturas_raw(self, fecha: str = None, pagina: int = 1):
        """
        Consulta dinámica papeleriamedellin_monitos_facturas_wms.
        Usa el endpoint /api/connekta/v3/ejecutarconsulta (distinto del estándar).
        fecha: AAAAMMDD — si None usa hoy. Devuelve JSON crudo para descubrir campos.
        """
        if not fecha:
            fecha = datetime.utcnow().strftime('%Y%m%d')

        if self.modo_simulacion:
            return self._simular('GET_monitor_facturas', {'fecha': fecha})

        import requests as _req
        params = {
            'idCompania': self.id_compania,
            'descripcion': 'papeleriamedellin_monitos_facturas_wms',
            'paginacion': f'numPag={pagina}|tamPag=100',
        }
        try:
            r = _req.get(self.url_get_dinamico, headers=self.headers, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except _req.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except _req.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] GET monitor_facturas: {e}')
            raise Exception(f'Error consultando monitor facturas: {e}')

    # ==========================================
    # POSTs — Bodies oficiales desde Ver Guía
    # ==========================================

    def trigger_despacho(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                          items: list):
        """
        142945 → API_v1_Ventas_Comercial_RemisionPedido
        Genera remisión desde pedido — descarga inventario cuenta 14.
        Siesa factura automáticamente. El WMS solo inyecta el documento.
        """
        # Siesa: fecha en formato YYYYMMDD (8 chars max)
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [
                {'F_CIA': self.id_cia_siesa}
            ],
            'Remision': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'F_CONSEC_AUTO_REG': 0,
                    'F350_ID_CO': self.centro_op,
                    'F350_ID_TIPO_DOCTO': self.tipo_docto_remision,
                    'F350_CONSEC_DOCTO': 0,
                    'F350_FECHA': fecha_hoy,        # YYYYMMDD — 8 chars
                    'F350_IND_ESTADO': 0,
                    'F350_IND_IMPRESION': 0,
                    'F430_ID_TIPO_DOCTO': tipo_docto_pedido,
                    'F430_CONSEC_DOCTO': int(consec_docto_pedido) if str(consec_docto_pedido).isdigit() else consec_docto_pedido,
                    'f462_id_vehiculo': None,
                    'f462_id_tercero_transp': None,
                    'f462_id_sucursal_transp': None,
                    'f462_id_tercero_conductor': None,
                    'f462_nombre_conductor': None,
                    'f462_identif_conductor': None,
                    'f462_numero_guia': None,
                    'f462_cajas': None,
                    'f462_peso': None,
                    'f462_volumen': None,
                    'f462_valor_seguros': None,
                    'f462_notas': None,
                    'f460_id_cond_pago': None
                }
            ],
            'Movtoventascomercial': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_remision,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': 0,
                    'f470_id_bodega': self.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': i.get('lote') or None,
                    'f470_id_concepto': 501,                          # 501 = Ventas (maestro Siesa)
                    'f470_id_motivo': self.motivo_ventas or None,     # SIESA_ID_MOTIVO_VENTAS (pos 131, ancho 2)
                    'f470_ind_obsequio': 0,
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_lista_precio': self.lista_precio or None,  # SIESA_LISTA_PRECIO (pos 169, ancho 3)
                    'f470_id_unidad_precio': i.get('unidad_medida') or None,
                    'f470_id_unidad_medida': i.get('unidad_medida') or None,
                    'f470_cant_base': i.get('cantidad_empacada'),
                    'f470_cant_2': None,
                    'f470_vlr_bruto': None,
                    'f470_ind_naturaleza': 2,                         # 2 = Salida/Venta
                    'f470_ind_solo_valor': 0,
                    'f470_ind_impto_asumido': 0,
                    'f470_notas': None,
                    'f470_desc_variable': None,
                    'F_DESC_ITEM': None,
                    'F_ID_UM_INVENTARIO': i.get('unidad_medida') or None,
                    'f470_id_item': i.get('item_id_siesa') or None,
                    'f470_referencia_item': i.get('producto_codigo'),
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': self.centro_op,
                    'f470_id_causal_devol': None
                }
                for i in items
            ],
            'Final': [
                {'F_CIA': self.id_cia_siesa}
            ]
        }

        logger.info(f'[CONNEKTA] Despacho {tipo_docto_pedido}{consec_docto_pedido}')
        return self._post(self.conector_despacho, 'API_v1_Ventas_Comercial_RemisionPedido', payload)

    def trigger_factura(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                        items: list):
        """
        238925 → FACTURA_DESDE_PEDIDO (conector dinámico v3.1)
        Genera factura electrónica (FE) directamente desde el pedido comprometido.
        Siesa toma los ítems del pedido original — no se envían líneas de detalle.
        La automatización 'Factura → Remisión' descarga el inventario automáticamente.
        """
        from datetime import timedelta
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        consec_int = int(consec_docto_pedido) if str(consec_docto_pedido).isdigit() else consec_docto_pedido
        # Vencimiento a 30 días — Siesa usará condición de pago del pedido si la tiene
        fecha_vcto = (datetime.utcnow() + timedelta(days=30)).strftime('%Y%m%d')

        payload = {
            'Docto_ventas_comercial': [{
                'F_CIA': int(self.id_cia_siesa),
                'F_CONSEC_AUTO_REG': 1,
                'F350_ID_CO': self.centro_op,
                'F350_ID_TIPO_DOCTO': self.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F350_FECHA': fecha_hoy,
                'F430_CONSEC_DOCTO': consec_int
            }],
            'Cuotas_CxC': [{
                'F350_ID_CO': self.centro_op,
                'F350_ID_TIPO_DOCTO': self.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F353_FECHA_VCTO': fecha_vcto,
                'F353_FECHA_DSCTO_PP': fecha_vcto
            }]
        }

        logger.info(f'[CONNEKTA] Factura desde pedido {consec_docto_pedido}')
        return self._post(
            self.conector_factura, 'FACTURA_DESDE_PEDIDO', payload,
            url=self.url_post_dinamico,
            extra_params={'idSistema': self.id_sistema}
        )

    def confirmar_entrada_compras(self, id_co_oc: str, tipo_docto_oc: str,
                                   consec_docto_oc: str, items: list,
                                   es_parcial: bool = False):
        """
        142948 → API_v1_Compras_Comercial_EntradaOC
        Genera entrada desde OC — debita cuenta 1435.
        """
        fecha_hoy = datetime.utcnow().strftime('%Y-%m-%d')

        payload = {
            'Inicial': [
                {'F_CIA': self.id_compania}
            ],
            'Documentos': [
                {
                    'F_CIA': self.id_compania,
                    'F_CONSEC_AUTO_REG': '',
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': '',
                    'f350_consec_docto': '',
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': '',
                    'f350_ind_estado': '',
                    'f350_ind_impresion': '',
                    'f350_notas': '',
                    'f451_id_sucursal_prov': '',
                    'f451_id_tercero_comprador': '',
                    'f451_num_docto_referencia': '',
                    'f451_id_moneda_docto': '',
                    'f451_id_moneda_conv': '',
                    'f451_tasa_conv': '',
                    'f451_id_moneda_local': '',
                    'f451_tasa_local': '',
                    'f451_tasa_dscto_global1': '',
                    'f451_tasa_dscto_global2': '',
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
                    'f451_ind_consignacion': '',
                    'f420_id_co_docto': id_co_oc,
                    'f420_id_tipo_docto': tipo_docto_oc,
                    'f420_consec_docto': consec_docto_oc,
                    'f420_ind_modo_sobrecosto': ''
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': self.id_compania,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': '',
                    'f470_consec_docto': '',
                    'f470_nro_registro': '',
                    'f470_id_bodega': self.bodega,
                    'f470_id_ubicacion_aux': '',
                    'f470_id_lote': '',
                    'f470_id_unidad_medida': '',
                    'f421_fecha_entrega': fecha_hoy,
                    'f470_cant_base': i.get('cantidad_recibida'),
                    'f470_cant_2': '',
                    'f470_notas': '',
                    'f470_id_item': '',
                    'f470_referencia_item': i.get('producto_codigo'),
                    'f470_codigo_barras': '',
                    'f470_id_ext1_detalle': '',
                    'f470_id_ext2_detalle': '',
                    'f470_id_ccosto_movto': '',
                    'f470_id_proyecto': '',
                    'f470_rowid': ''
                }
                for i in items
            ],
            'Final': [
                {'F_CIA': self.id_compania}
            ]
        }

        logger.info(f'[CONNEKTA] Entrada OC {id_co_oc}/{tipo_docto_oc}/{consec_docto_oc}')
        return self._post(self.conector_entrada, 'API_v1_Compras_Comercial_EntradaOC', payload)

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Ajuste físico tras conteo cíclico double-blind.
        AJ-ENT: sobrante. AJ-SAL: faltante. Cantidad siempre positiva.
        """
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}')

        fecha_hoy = datetime.utcnow().strftime('%Y-%m-%d')

        payload = {
            'Inicial': [
                {'F_CIA': self.id_compania}
            ],
            'Documentos': [
                {
                    'F_CIA': self.id_compania,
                    'F_CONSEC_AUTO_REG': '',
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': '',
                    'f350_consec_docto': '',
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': '',
                    'f350_id_clase_docto': '',
                    'f350_ind_estado': '',
                    'f350_ind_impresion': '',
                    'f350_notas': referencia,
                    'f450_id_concepto': motivo_codigo,
                    'f450_id_bodega_salida': self.bodega if motivo_codigo == 'AJ-SAL' else '',
                    'f450_id_bodega_entrada': self.bodega if motivo_codigo == 'AJ-ENT' else '',
                    'f450_docto_alterno': '',
                    'f350_id_co_base': '',
                    'f350_id_tipo_docto_base': '',
                    'f350_consec_docto_base': '',
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
                    'f462_notas': ''
                }
            ],
            'Movimientos': [
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
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': '',
                    'f470_id_proyecto': '',
                    'f470_id_unidad_medida': '',
                    'f470_cant_base': abs(cantidad),
                    'f470_cant_2': '',
                    'f470_costo_prom_uni': '',
                    'f470_notas': '',
                    'f470_desc_varible': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': '',
                    'f470_id_ubicacion_aux_ent': '',
                    'f470_id_lote_ent': '',
                    'f470_id_item': '',
                    'f470_referencia_item': item_codigo,
                    'f470_codigo_barras': '',
                    'f470_id_ext1_detalle': '',
                    'f470_id_ext2_detalle': '',
                    'f470_id_un_movto': self.centro_op
                }
            ],
            'Final': [
                {'F_CIA': self.id_compania}
            ]
        }

        logger.info(f'[CONNEKTA] Ajuste {motivo_codigo} {item_codigo}:{cantidad}')
        return self._post(self.conector_ajuste, 'API_v1_Inventarios_Comercial_DocumentoInv', payload)

    def transferir_a_averias(self, item_codigo: str, cantidad: int, referencia: str = ''):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Traslado físico NB1 → AV1 cuando el recepcionista marca mercancía como averiada.
        Usa SIESA_TIPO_DOCTO_TRASLADO (TRA) y SIESA_MOTIVO_TRASLADO (01).
        Siesa mueve el stock entre bodegas — vendedores ya no ven las unidades averiadas.
        """
        fecha_hoy = datetime.utcnow().strftime('%Y-%m-%d')

        payload = {
            'Inicial': [
                {'F_CIA': self.id_compania}
            ],
            'Documentos': [
                {
                    'F_CIA': self.id_compania,
                    'F_CONSEC_AUTO_REG': '',
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_traslado,
                    'f350_consec_docto': '',
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': '',
                    'f350_id_clase_docto': '',
                    'f350_ind_estado': '',
                    'f350_ind_impresion': '',
                    'f350_notas': referencia or f'Avería detectada por WMS · {item_codigo}',
                    'f450_id_concepto': self.motivo_traslado,
                    'f450_id_bodega_salida': self.bodega,
                    'f450_id_bodega_entrada': self.bodega_averias,
                    'f450_docto_alterno': '',
                    'f350_id_co_base': '',
                    'f350_id_tipo_docto_base': '',
                    'f350_consec_docto_base': '',
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
                    'f462_notas': ''
                }
            ],
            'Movimientos': [
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
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': '',
                    'f470_id_proyecto': '',
                    'f470_id_unidad_medida': '',
                    'f470_cant_base': abs(cantidad),
                    'f470_cant_2': '',
                    'f470_costo_prom_uni': '',
                    'f470_notas': '',
                    'f470_desc_varible': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': '',
                    'f470_id_ubicacion_aux_ent': '',
                    'f470_id_lote_ent': '',
                    'f470_id_item': '',
                    'f470_referencia_item': item_codigo,
                    'f470_codigo_barras': '',
                    'f470_id_ext1_detalle': '',
                    'f470_id_ext2_detalle': '',
                    'f470_id_un_movto': self.centro_op
                }
            ],
            'Final': [
                {'F_CIA': self.id_compania}
            ]
        }

        logger.info(f'[CONNEKTA] Traslado averías {item_codigo}:{cantidad} {self.bodega}→{self.bodega_averias}')
        return self._post(self.conector_ajuste, 'API_v1_Inventarios_Comercial_DocumentoInv', payload)

    def get_bodegas_siesa(self):
        """API_v2_Bodegas (ID 2) — lista todas las bodegas configuradas en Siesa.
        Usar para descubrir los IDs de bodega de los puntos de venta sin esperar a Siesa."""
        return self._get('API_v2_Bodegas', {
            'paginacion': 'numPag=1|tamPag=200'
        })

    def get_stock_bodega(self, bodega_id: str):
        """API_v2_Inventarios_InvFecha — existencia real en una bodega específica.
        Tienda consulta disponibilidad en NB1 antes de armar su solicitud."""
        all_rows = []
        for pag in range(1, 20):
            res = self._get(self.api_inventario, {
                'paginacion': f'numPag={pag}|tamPag=100',
                'parametros': f"f150_id = ''{bodega_id}'' AND f400_cant_existencia_1 > 0"
            })
            if self.modo_simulacion:
                return res
            rows = res.get('detalle', {}).get('Table', [])
            if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                break
            all_rows.extend(rows)
            if len(rows) < 100:
                break
        return {'detalle': {'Table': all_rows}}

    # ── Traslados entre bodegas ────────────────────────────────────────────────

    def crear_requisicion_traslado(self, bodega_origen: str, bodega_destino: str,
                                    items: list, codigo_solicitud: str):
        """
        174646 → API_v1_Inventarios_Comercial_RequisicionesParaTransferir
        Compromete inventario en bodega_origen para traslado a bodega_destino.
        Usa f440_* para documentos y f441_* para movimientos (schema propio, distinto
        al f350_*/f470_* de los demás conectores de inventario).
        """
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': self.id_cia_siesa}],
            'Documentos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'F_CONSEC_AUTO_REG': 0,
                    'f440_id_co': self.centro_op,
                    'f440_id_tipo_docto': self.tipo_docto_req_traslado,
                    'f440_consec_docto': 0,
                    'f440_fecha': fecha_hoy,
                    'f440_id_tercero': '',
                    'f440_id_solicitante': self.req_solicitante,   # REQUIRED
                    'f440_fecha_entrega': fecha_hoy,               # REQUIRED
                    'f440_num_dias_entrega': 0,
                    'f440_ind_estado': 1,
                    'f440_ind_impresion': 0,
                    'f440_notas': f'WMS {codigo_solicitud}',
                    'f440_id_bodega_salida': bodega_origen,
                    'f440_id_bodega_entrada': bodega_destino,
                    'f440_referencia': codigo_solicitud,
                    'f440_id_ubicacion_ent': '',
                    'f440_id_cargue': '',
                    'f440_num_docto_referencia': '',
                    'f440_id_proyecto': '',
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'f441_id_co': self.centro_op,
                    'f441_id_tipo_docto': self.tipo_docto_req_traslado,
                    'f441_consec_docto': 0,
                    'f441_nro_registro': idx + 1,
                    'f441_id_item': 0,
                    'f441_referencia_item': item.get('codigo_siesa') or item.get('codigo'),
                    'f441_codigo_barras': ' ',
                    'f441_id_ext1_detalle': ' ',
                    'f441_id_ext2_detalle': ' ',
                    'f441_id_bodega': bodega_origen,
                    'f441_id_motivo': self.motivo_traslado,        # REQUIRED
                    'f441_id_unidad_medida': item.get('unidad_medida') or 'UND',
                    'f441_cant_base': f'{abs(item.get("cantidad", 0)):020.4f}',
                    'f441_cant_2': f'{0:020.4f}',
                    'f441_fecha_entrega': fecha_hoy,               # REQUIRED
                    'f441_num_dias_entrega': 0,
                    'f441_id_co_movto': self.centro_op,
                    'f441_id_ccosto_movto': ' ',
                    'f441_id_proyecto': ' ',
                    'f441_notas': ' ',
                    'f441_id_un_movto': item.get('unidad_negocio_id') or ' ',
                    'f441_precio_unitario': f'{0:020.4f}',
                    'f441_id_ubicacion_sal': ' ',
                    'f441_id_proy_etapa': ' ',
                    'f441_id_rubro_pof': ' ',
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': self.id_cia_siesa}]
        }

        logger.info(f'[CONNEKTA] Requisicion traslado {codigo_solicitud} '
                    f'{bodega_origen}→{bodega_destino} ({len(items)} items)')
        return self._post(self.conector_requisicion_traslado,
                          'API_v1_Inventarios_Comercial_RequisicionesParaTransferir', payload)

    def transferencia_transito_salida(self, bodega_origen: str, bodega_transito: str,
                                       items: list, codigo_solicitud: str,
                                       consec_requisicion: int = None):
        """
        173076 → API_v1_Inventarios_Comercial_TransferenciaEnTransitoSalida
        Sale de bodega_origen → queda en bodega_transito (limbo contable).
        El inventario NO está en la tienda hasta que se confirme la entrada (173079).
        NO lleva f350_id_co_base/f350_id_tipo_docto_base — esas son solo de 173079.
        """
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': self.id_cia_siesa}],
            'Documentos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'F_CONSEC_AUTO_REG': 0,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_transito_salida,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Despacho {codigo_solicitud}',
                    'f450_id_bodega_salida': bodega_origen,
                    'f450_id_bodega_entrada': bodega_transito,
                    'f450_docto_alterno': codigo_solicitud,
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_transito_salida,  # REQUIRED
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_origen,   # debe == f450_id_bodega_salida
                    'f470_id_motivo': self.motivo_traslado,                 # REQUIRED
                    'f470_referencia_item': item.get('codigo_siesa') or item.get('codigo'),
                    'f470_cant_base': f'{abs(item.get("cantidad", 0)):015.4f}',
                    'f470_id_unidad_medida': item.get('unidad_medida') or '',
                    'f470_id_co_movto': self.centro_op,                     # REQUIRED
                    'f470_id_un_movto': item.get('unidad_negocio_id') or '',
                    'f470_notas': '',
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': self.id_cia_siesa}]
        }

        logger.info(f'[CONNEKTA] Tránsito salida {codigo_solicitud} '
                    f'{bodega_origen}→{bodega_transito}')
        return self._post(self.conector_transito_salida,
                          'API_v1_Inventarios_Comercial_TransferenciaEnTransitoSalida', payload)

    def transferencia_transito_entrada(self, bodega_transito: str, bodega_destino: str,
                                        items: list, codigo_solicitud: str,
                                        consec_salida: int = None):
        """
        173079 → API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada
        Confirma llegada: bodega_transito → bodega_destino.
        Solo en este momento el inventario ingresa a la tienda y puede ser facturado en POS.
        Lleva f350_id_co_base/f350_id_tipo_docto_base/f350_consec_docto_base referenciando
        el documento 173076 de salida — obligatorio para cerrar el tránsito en Siesa.
        f470_id_bodega debe ser bodega_transito (== f450_id_bodega_salida de este doc).
        """
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': self.id_cia_siesa}],
            'Documentos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'F_CONSEC_AUTO_REG': 0,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_transito_entrada,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Recepcion {codigo_solicitud}',
                    'f450_id_bodega_salida': bodega_transito,
                    'f450_id_bodega_entrada': bodega_destino,
                    'f450_docto_alterno': codigo_solicitud,
                    # Referencia obligatoria al doc 173076 de salida
                    'f350_id_co_base': self.centro_op if consec_salida else '',
                    'f350_id_tipo_docto_base': self.tipo_docto_transito_salida if consec_salida else '',
                    'f350_consec_docto_base': consec_salida or '',
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_transito_entrada,  # REQUIRED
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_transito,  # debe == f450_id_bodega_salida
                    'f470_id_motivo': self.motivo_traslado,                  # REQUIRED
                    'f470_referencia_item': item.get('codigo_siesa') or item.get('codigo'),
                    'f470_cant_base': f'{abs(item.get("cantidad", 0)):015.4f}',
                    'f470_id_unidad_medida': item.get('unidad_medida') or '',
                    'f470_id_co_movto': self.centro_op,                      # REQUIRED
                    'f470_id_un_movto': item.get('unidad_negocio_id') or '',
                    'f470_notas': '',
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': self.id_cia_siesa}]
        }

        logger.info(f'[CONNEKTA] Tránsito entrada {codigo_solicitud} '
                    f'{bodega_transito}→{bodega_destino}')
        return self._post(self.conector_transito_entrada,
                          'API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada', payload)

    def transferencia_directa(self, bodega_origen: str, bodega_destino: str,
                               items: list, codigo_solicitud: str):
        """
        173066 → API_v1_Inventarios_Comercial_TransferenciaDirecta
        Plan B: sin bodega de tránsito. El inventario ingresa a la tienda
        inmediatamente — usar solo si Siesa no tiene bodega de tránsito configurada.
        Riesgo: si el camión no llega, la tienda tiene stock fantasma.

        VERIFICAR desde Connekta → Ver Guía del conector 173066.
        """
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': self.id_cia_siesa}],
            'Documentos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'F_CONSEC_AUTO_REG': 0,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_traslado,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Transferencia directa {codigo_solicitud}',
                    'f450_id_bodega_salida': bodega_origen,
                    'f450_id_bodega_entrada': bodega_destino,
                    'f450_docto_alterno': codigo_solicitud,
                    'f350_id_co_base': '',
                    'f350_id_tipo_docto_base': '',
                    'f350_consec_docto_base': '',
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': self.id_cia_siesa,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_traslado,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_origen,
                    'f470_id_motivo': self.motivo_traslado,
                    'f470_referencia_item': item.get('codigo_siesa') or item.get('codigo'),
                    'f470_cant_base': f'{abs(item.get("cantidad", 0)):015.4f}',
                    'f470_id_unidad_medida': item.get('unidad_medida') or '',
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_un_movto': item.get('unidad_negocio_id') or '',
                    'f470_notas': '',
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': self.id_cia_siesa}]
        }

        logger.info(f'[CONNEKTA] Transferencia directa {codigo_solicitud} '
                    f'{bodega_origen}→{bodega_destino}')
        return self._post(self.conector_transferencia_directa,
                          'API_v1_Inventarios_Comercial_TransferenciaDirecta', payload)

    # ==========================================
    # Estado
    # ==========================================

    def estado(self):
        return {
            'modo_simulacion': self.modo_simulacion,
            'modo_ensayo': self.modo_ensayo,
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
                'ajuste': f'{self.conector_ajuste} DocumentoInv',
                'req_traslado': f'{self.conector_requisicion_traslado} RequisicionesParaTransferir',
                'transito_salida': f'{self.conector_transito_salida} TransferenciaEnTransitoSalida',
                'transito_entrada': f'{self.conector_transito_entrada} TransferenciaEnTransitoEntrada',
                'transf_directa': f'{self.conector_transferencia_directa} TransferenciaDirecta',
            },
            'traslados_config': {
                'tipo_docto_req_traslado': self.tipo_docto_req_traslado or 'NO CONFIGURADO',
                'tipo_docto_transito_salida': self.tipo_docto_transito_salida or 'NO CONFIGURADO',
                'tipo_docto_transito_entrada': self.tipo_docto_transito_entrada or 'NO CONFIGURADO',
                'req_solicitante': self.req_solicitante or 'NO CONFIGURADO',
                'bodega_transito': self.bodega_transito or 'NO CONFIGURADO',
                'motivo_traslado': self.motivo_traslado,
            },
            'apis_get': {
                'pedidos': self.api_pedidos,
                'ordenes': self.api_ordenes,
                'inventario': self.api_inventario,
                'barras': self.api_barras
            },
            'mensaje': 'Listo para producción' if not self.modo_simulacion
                       else 'Pendiente: CONNEKTA_IKEY, CONNEKTA_ITOKEN'
        }


connekta = ConnektaGateway()
