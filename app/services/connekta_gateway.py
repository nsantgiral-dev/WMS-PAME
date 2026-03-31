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

        self.conector_despacho = os.getenv('CONNEKTA_CONECTOR_DESPACHO', '142945')
        self.conector_entrada = os.getenv('CONNEKTA_CONECTOR_ENTRADA', '142948')
        self.conector_ajuste = os.getenv('CONNEKTA_CONECTOR_AJUSTE', '142951')
        self.bodega_averias = os.getenv('SIESA_BODEGA_AVERIAS', 'AV1')
        self.tipo_docto_traslado = os.getenv('SIESA_TIPO_DOCTO_TRASLADO', 'TRA')
        self.motivo_traslado = os.getenv('SIESA_MOTIVO_TRASLADO', '01')

        # Consulta dinámica para picking — filtra NB1/003 en SQL directo
        self.api_pedidos_picking = os.getenv(
            'CONNEKTA_API_PEDIDOS_PICKING',
            'papeleriamedellin_WMS_Picking_Pedidos_NB1'
        )

        self.url_get = 'https://serviciosqa.siesacloud.com/api/siesa/v3/ejecutarconsultaestandar'
        self.url_get_dinamica = os.getenv(
            'CONNEKTA_URL_DINAMICA',
            'https://serviciosqa.siesacloud.com/api/connekta/v3.0.1/ejecutarconsulta'
        )
        self.url_post = 'https://serviciosqa.siesacloud.com/api/siesa/v3/conectoresimportarestandar'

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

    def _get_dinamica(self, descripcion: str, paginacion: str = 'numPag=1|tamPag=100'):
        """GET a la API dinámica (Generador de consultas) — filtra en SQL directo en Siesa."""
        if self.modo_simulacion:
            return self._simular(f'GET_DINAMICA_{descripcion}')

        params = {
            'idCompania': self.id_compania,
            'descripcion': descripcion,
            'paginacion': paginacion
        }
        try:
            r = requests.get(self.url_get_dinamica, headers=self.headers, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] GET_DINAMICA {descripcion}: {e}')
            raise Exception(f'Error consultando Siesa: {e}')

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

    def _post(self, id_conector: str, nombre_conector: str, payload: dict):
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

        try:
            r = requests.post(self.url_post, headers=self.headers, params=params, json=payload, timeout=30)
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

    def get_pedidos_aprobados(self, sin_filtros: bool = False):
        """
        Cola viva de picking: pedidos aprobados de los últimos 3 días.
        Estrategia: filtrar por fecha en Siesa (reduce de ~10k a ~30 filas),
        luego Python filtra bodega NB1 y CO 003.
        Campo fecha: f430_fecha (nombre estándar Siesa para fecha del documento).
        """
        from datetime import date, timedelta
        fecha_desde = (date.today() - timedelta(days=3)).strftime('%Y-%m-%d')

        if sin_filtros:
            parametros = 'f430_ind_estado=1'
        else:
            parametros = f'f430_ind_estado=1 AND f430_fecha>="{fecha_desde}"'

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
            # Python filtra bodega y CO (API ignora esos campos en parametros)
            if not sin_filtros:
                if item.get('f150_id', '').strip() != self.bodega:
                    continue
                if item.get('f430_id_co', '').strip() != self.centro_op:
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

        logger.info(f'[CONNEKTA] pedidos: {len(items_pendientes)} NB1/003 de {len(items_raw)} últimos 3 días')
        return {
            'codigo': 0,
            'total_siesa': len(items_raw),
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

        logger.info(f'[CONNEKTA] Despacho {tipo_docto_pedido}{consec_docto_pedido}')
        return self._post(self.conector_despacho, 'API_v1_Ventas_Comercial_RemisionPedido', payload)

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
                'ajuste': f'{self.conector_ajuste} DocumentoInv'
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
