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
        self.api_unidades_medida = os.getenv('CONNEKTA_API_UNIDADES_MEDIDA', 'API_v2_ItemsUnidadesMedida')

        self.conector_factura  = os.getenv('CONNEKTA_CONECTOR_FACTURA',  '238925')  # FacturaPedido
        self.conector_despacho = os.getenv('CONNEKTA_CONECTOR_DESPACHO', '142945')  # RemisionPedido — despacho parcial
        self.conector_factura_remision = os.getenv('CONNEKTA_CONECTOR_FACTURA_REMISION', '142943')  # FacturaDesdeRemision — despacho parcial
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
        self.req_solicitante = os.getenv('SIESA_REQ_SOLICITANTE', '')[:5]
        # Bodega de tránsito (verificar si existe en Siesa — si no, usar TransferenciaDirecta)
        self.bodega_transito = os.getenv('SIESA_BODEGA_TRANSITO', '')
        # Sin default: el código de Unidad de Negocio se valida contra el maestro de Siesa
        # por compañía. Solicitar al área financiera el código exacto y configurarlo en Railway.
        # Si está vacío, los conectores envían None y Siesa hereda el valor de la bodega.
        self.unidad_negocio = os.getenv('SIESA_UNIDAD_NEGOCIO', '') or None
        # id_cia interno de Siesa (distinto de idCompania Connekta)
        # Verificar en Siesa Enterprise → Parámetros de empresa → Código de compañía
        self.id_cia_siesa = os.getenv('SIESA_ID_CIA', '1')
        # Tipo documento factura electrónica en Siesa (FEW para Papelería Medellín)
        self.tipo_docto_factura  = os.getenv('SIESA_TIPO_DOCTO_FACTURA',  'FEW')
        # Tipo de documento remisión en Siesa (ej. 'RS', 'REMI', 'RM') — legacy
        # Verificar en Siesa: Ventas → Tipos de documento → código del tipo Remisión
        self.tipo_docto_remision = os.getenv('SIESA_TIPO_DOCTO_REMISION', '')
        # Motivo de ventas en Siesa — campo requerido f470_id_motivo (pos 131, ancho 2)
        # Verificar en Siesa: Ventas → Motivos → código del motivo para ventas/remisiones
        self.motivo_ventas = os.getenv('SIESA_ID_MOTIVO_VENTAS', '')
        # Motivo de compras en Siesa — campo requerido f470_id_motivo en entradas de OC (pos 131, ancho 2)
        # '01' = Entrada por compras (Concepto 401). Verificar en Siesa: Compras → Maestros → Conceptos y Motivos
        self.motivo_compras = os.getenv('SIESA_ID_MOTIVO_COMPRAS', '01')
        # Condición de pago para entradas de OC — campo f451_id_cond_pago (pos 324, ancho 3/4)
        # Verificar en Siesa: Cartera → Condiciones de pago → código usado en OCs
        self.cond_pago_compras = os.getenv('SIESA_COND_PAGO_COMPRAS', '')
        # Condición de pago para facturas de venta (CxC) — f461_id_cond_pago en 142943.
        # Distinto de cond_pago_compras (CxP). El .NET serializer de Connekta V2 colapsa
        # con HTTP 500 si se envía null — SIESA_COND_PAGO_VENTAS es obligatorio.
        # Verificar en Siesa: Ventas → Condiciones de pago → código de la condición activa.
        self.cond_pago_ventas = os.getenv('SIESA_COND_PAGO_VENTAS', '')
        # Lista de precio en Siesa — campo requerido f470_id_lista_precio (pos 169, ancho 3)
        # Verificar en Siesa: Ventas → Listas de precio → código de la lista activa
        self.lista_precio = os.getenv('SIESA_LISTA_PRECIO', '')
        self.bodega_averias = os.getenv('SIESA_BODEGA_AVERIAS', 'AV1')
        # NIT de la empresa — usado como f451_id_tercero_comprador (comprador) en EntradaOC
        self.nit_empresa = os.getenv('SIESA_NIT_EMPRESA', '')
        # Tipo documento para EntradaOC (f350_id_tipo_docto y f470_id_tipo_docto)
        # Verificar en Siesa: Compras → Tipos de documento → código del tipo Entrada OC
        self.tipo_docto_entrada_oc = os.getenv('SIESA_TIPO_DOCTO_ENTRADA_OC', '')
        # Unidad de medida por defecto para movimientos de inventario
        self.uom_default = os.getenv('SIESA_UOM_DEFAULT', 'UND')
        # Punto de envío por defecto para 142943 (FacturaRemision) — campo f461_id_punto_envio.
        # Configurar en Railway con el código exacto del maestro Siesa (Maestros → Terceros → Puntos de envío).
        # Se usa solo cuando API_v2_Ventas_Pedidos no devuelve el campo en la cabecera del pedido.
        # Si no se configura y la API tampoco lo devuelve, trigger_factura_desde_remision lanza ValueError.
        self.punto_envio_default = os.getenv('SIESA_PUNTO_ENVIO_DEFAULT', '') or None
        # Tipo documento ajuste físico en Siesa (Inventarios → Tipos de documento)
        self.tipo_docto_ajuste = os.getenv('SIESA_TIPO_DOCTO_AJUSTE', '')
        self.tipo_docto_traslado = os.getenv('SIESA_TIPO_DOCTO_TRASLADO', 'TRA')
        # Sin default — SIESA_MOTIVO_TRASLADO es obligatorio en producción.
        # '01' era un fallback genérico que generaba rechazos en Siesas que usan otro código.
        self.motivo_traslado = os.getenv('SIESA_MOTIVO_TRASLADO', '')
        # Motivo específico para transferencias a bodega de averías (142951).
        # El maestro "Conceptos y Motivos" de Siesa puede tener un código distinto al de traslados
        # normales. Verificar: Maestros Asociados → Conceptos y Motivos → código para averías.
        # Si no se configura, cae al motivo_traslado genérico (puede causar rechazo en Siesa).
        self.motivo_averia = os.getenv('SIESA_MOTIVO_AVERIA', '') or self.motivo_traslado
        # Conceptos de movimiento en Siesa (Inventarios → Maestros → Conceptos y Motivos).
        # Los valores por defecto son los estándar de Siesa Enterprise; pueden variar por compañía.
        # Verificar en Siesa si los conceptos fueron renumerados antes de cambiar estos valores.
        self.concepto_ventas       = self._safe_int_env('SIESA_CONCEPTO_VENTAS',    501)
        self.concepto_compras      = self._safe_int_env('SIESA_CONCEPTO_COMPRAS',  401)
        self.concepto_ajustes      = self._safe_int_env('SIESA_CONCEPTO_AJUSTES',  603)
        self.concepto_traslados    = self._safe_int_env('SIESA_CONCEPTO_TRASLADOS', 607)

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
        else:
            # Validación de arranque — variables obligatorias en modo producción real.
            # El servidor no debe arrancar sin estas; fallarían silencios en producción.
            _faltantes = []
            if not self.cond_pago_ventas:
                _faltantes.append('SIESA_COND_PAGO_VENTAS')
            if not self.motivo_traslado:
                _faltantes.append('SIESA_MOTIVO_TRASLADO')
            if _faltantes:
                raise EnvironmentError(
                    f'[CONNEKTA] Variables obligatorias no configuradas: {", ".join(_faltantes)}. '
                    'Configurar en Railway antes de desplegar.'
                )

    @staticmethod
    def _safe_int_env(var_name: str, default: int) -> int:
        """Parse int env var safely — logs warning and falls back to default on bad value."""
        raw = os.getenv(var_name, '')
        if not raw:
            return default
        try:
            return int(raw)
        except (ValueError, TypeError):
            logger.warning(f'[CONNEKTA] {var_name}={raw!r} no es numérico — usando default {default}')
            return default

    @property
    def headers(self):
        return {
            'Content-Type': 'application/json',
            'ConniKey': self.ikey,
            'ConniToken': self.itoken
        }

    @staticmethod
    def _fmt_fecha(valor: str) -> str:
        """Normaliza cualquier formato de fecha Siesa a YYYYMMDD (8 dígitos, sin separadores)."""
        if not valor:
            return ''
        solo_digitos = ''.join(c for c in str(valor) if c.isdigit())
        return solo_digitos[:8] if len(solo_digitos) >= 8 else ''

    @staticmethod
    def _fmt_fecha_iso(valor: str) -> str:
        """Normaliza cualquier formato de fecha a YYYYMMDD (8 dígitos, sin separadores).
        Siesa exige exactamente 8 caracteres en f421_fecha_entrega — guiones causan rechazo."""
        if not valor:
            return ''
        solo_digitos = ''.join(c for c in str(valor) if c.isdigit())
        return solo_digitos[:8] if len(solo_digitos) >= 8 else ''

    def _simular(self, operacion: str, payload: dict = None):
        logger.info(f'[CONNEKTA SIMULADO] {operacion}')
        return {
            'simulado': True,
            'operacion': operacion,
            'timestamp': datetime.utcnow().isoformat(),
            'mensaje': f'{operacion} simulado exitosamente',
            'payload': payload or {}
        }

    def _get(self, nombre_api: str, params_extra: dict = None, timeout: int = 30, url: str = None):
        if self.modo_simulacion:
            return self._simular(f'GET_{nombre_api}', params_extra)

        params = {'idCompania': self.id_compania, 'descripcion': nombre_api}
        if params_extra:
            params.update(params_extra)

        target_url = url or self.url_get
        try:
            r = requests.get(target_url, headers=self.headers, params=params, timeout=timeout)
            if r.status_code == 429:
                retry_after = r.headers.get('Retry-After', '300')
                logger.warning(f'[CONNEKTA] GET {nombre_api}: rate-limit (429) — Retry-After={retry_after}s')
                raise Exception(f'Connekta rate-limit (429) — reintento en {retry_after}s')
            r.raise_for_status()
            data = r.json()
            # [A21] Connekta puede devolver HTTP 200 con body de error interno.
            # Verificar campo 'codigo' (0 = éxito, !=0 = error) igual que en _post().
            if isinstance(data, dict):
                _codigo = data.get('codigo')
                if _codigo is not None and _codigo != 0:
                    _msg = data.get('mensaje') or data.get('descripcion') or f'codigo={_codigo}'
                    logger.warning(f'[CONNEKTA] GET {nombre_api}: error interno Siesa — {_msg}')
                    raise Exception(f'Siesa retornó error interno (codigo={_codigo}): {_msg}')
            return data
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

        logger.info(f'[CONNEKTA] POST → conector={id_conector} nombre={nombre_conector} url={url or self.url_post}')
        try:
            r = requests.post(
                url or self.url_post,
                headers=self.headers,
                params=params,
                json=payload,
                timeout=(10, 30),  # connect=10s, read=30s — falla rápido; gunicorn timeout=120s
            )
            if r.status_code == 429:
                retry_after = r.headers.get('Retry-After', '300')
                logger.warning(
                    f'[CONNEKTA] POST {id_conector}: rate-limit (429) — '
                    f'Retry-After={retry_after}s — DLQ reintentará con backoff'
                )
                raise Exception(f'Connekta rate-limit (429) — reintento en {retry_after}s')
            if not r.ok:
                try:
                    detalle = r.json()
                except Exception:
                    detalle = r.text
                logger.error(f'[CONNEKTA] POST {id_conector} HTTP {r.status_code}: {detalle}')
                raise Exception(f'Siesa rechazó el documento (HTTP {r.status_code}): {detalle}')
            resp_json = r.json()
            logger.info(f'[CONNEKTA] POST {id_conector} HTTP 200 — respuesta: {str(resp_json)[:300]}')
            # Connekta V2/V3.1: HTTP 200 no garantiza éxito — verificar codigo==0 en body.
            if isinstance(resp_json, dict):
                codigo = resp_json.get('codigo')
                if codigo is not None and codigo != 0:
                    mensaje = resp_json.get('mensaje', 'Sin mensaje')
                    detalle = resp_json.get('detalle', '')
                    logger.error(
                        f'[CONNEKTA] POST {id_conector} rechazado por Siesa — '
                        f'codigo={codigo} mensaje={mensaje} detalle={detalle}'
                    )
                    raise Exception(
                        f'Siesa rechazó el documento (codigo={codigo}): {mensaje}. {detalle}'
                    )
            elif isinstance(resp_json, list):
                # V3.1 retorna lista — verificar si algún elemento señala error
                for _item in resp_json:
                    if isinstance(_item, dict):
                        _cod = _item.get('codigo')
                        if _cod is not None and _cod != 0:
                            _msg = _item.get('mensaje', 'Sin mensaje')
                            logger.error(
                                f'[CONNEKTA] POST {id_conector} (v3.1 list) rechazado — '
                                f'codigo={_cod} mensaje={_msg}'
                            )
                            raise Exception(
                                f'Siesa rechazó el documento (codigo={_cod}): {_msg}'
                            )
            return resp_json
        except requests.exceptions.Timeout:
            logger.error(f'[CONNEKTA] POST {id_conector}: timeout — Siesa tardó más de 30s')
            raise Exception('Siesa no respondió en 30s — la recepción quedó EN_PROCESO, reintenta confirmar')
        except requests.exceptions.RequestException as e:
            logger.error(f'[CONNEKTA] POST {id_conector}: {e}')
            raise Exception(f'Error inyectando en Siesa: {e}')

    # ==========================================
    # GETs
    # ==========================================

    def get_estado_pedido(self, tipo_docto: str, consec_docto) -> int | None:
        """
        Consulta el ind_estado actual de un pedido específico en Siesa.
        Retorna:
          - int positivo (1-9): estado real del pedido
          - -1: pedido no encontrado en Siesa (eliminado o nunca existió)
          - None: error de red / tipo_docto vacío (no se pudo consultar)
        Se usa como pre-check en cerrar_packing y detección de anulados en pedidos_sync.
        """
        if self.modo_simulacion:
            return 3  # simulación asume siempre comprometido

        if not tipo_docto or not str(tipo_docto).strip():
            logger.warning(
                '[CONNEKTA] get_estado_pedido: tipo_docto vacío — '
                'no se puede verificar estado en Siesa (consec=%s)', consec_docto
            )
            return None

        try:
            consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
            res = self._get(self.api_pedidos, {
                'paginacion': 'numPag=1|tamPag=1',
                'parametros': (
                    f"f430_id_co = ''{self.centro_op}'' "
                    f"AND f430_id_tipo_docto = ''{tipo_docto}'' "
                    f"AND f430_consec_docto = {consec_int}"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            if not rows:
                return -1  # pedido no encontrado en Siesa (eliminado o nunca existió)
            return rows[0].get('f430_ind_estado')
        except Exception as e:
            logger.warning(f'[CONNEKTA] get_estado_pedido falló silenciosamente: {e}')
            return None  # error de red — no bloqueamos, el POST revelará el error

    def get_factura_desde_pedido(self, tipo_docto: str, consec_docto) -> list:
        """
        Consulta si ya existe una factura activa (no anulada) generada desde un pedido.
        Retorna lista de facturas activas. Lista vacía = sin factura previa, proceder.
        Guard anti-duplicado en cerrar_packing antes de disparar trigger_factura (238925).
        SKIP_FE_CHECK=true omite el guard (solo QA — nunca en producción).
        """
        import os
        if self.modo_simulacion or os.getenv('SKIP_FE_CHECK', '').lower() == 'true':
            return []

        if not tipo_docto or not str(tipo_docto).strip():
            return []

        try:
            consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
            res = self._get('papeleriamedellin_monitos_facturas_wms', {
                'paginacion': 'numPag=1|tamPag=50',
                'parametros': (
                    f"f350_id_co = ''{self.centro_op}'' "
                    f"AND f430_consec_docto = {consec_int}"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            return [r for r in rows if str(r.get('f350_ind_estado', '9')) != '9']
        except Exception as e:
            # FAIL-FAST: no retornar [] ante error de red — el caller asumiría que no hay FE
            # y dispararía trigger_factura (238925) generando FE duplicada (riesgo fiscal / DIAN).
            logger.error('[CONNEKTA] get_factura_desde_pedido falló — abortando para evitar FE duplicada: %s', e)
            raise Exception(
                f'No se pudo verificar si ya existe FE para pedido {tipo_docto}-{consec_docto}: {e}. '
                'Reintenta cuando Connekta esté disponible.'
            )

    def get_factura_desde_remision(self, tipo_docto_rm: str, consec_rm) -> list:
        """
        Pre-check anti-duplicado para 142943 (FacturaDesdeRemision).
        Consulta si ya existe una FE activa (no anulada) vinculada a la remisión.
        Retorna lista de facturas activas. Lista vacía = sin factura previa, proceder.
        Usa el mismo API que get_factura_desde_pedido filtrando por el documento base
        (f460_id_tipo_docto / f460_consec_docto) que identifica la RM en RelacionDoctos.
        """
        if self.modo_simulacion:
            return []

        if not tipo_docto_rm or not str(tipo_docto_rm).strip():
            return []

        try:
            consec_int = int(consec_rm) if str(consec_rm).isdigit() else consec_rm
            res = self._get('API_v2_Ventas_Facturas_DesdePedido', {
                'paginacion': 'numPag=1|tamPag=50',
                'parametros': (
                    f"f350_id_co = ''{self.centro_op}'' "
                    f"AND f460_id_tipo_docto = ''{tipo_docto_rm}'' "
                    f"AND f460_consec_docto = {consec_int}"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            return [r for r in rows if str(r.get('f350_ind_estado', '9')) != '9']
        except Exception as e:
            # FAIL-FAST: no retornar [] ante error de red — eso haría creer que no hay FE
            # y el caller procedería a crear una FE duplicada (riesgo fiscal / DIAN).
            # El caller debe capturar esta excepción y abortar el despacho.
            logger.error('[CONNEKTA] get_factura_desde_remision falló — abortando para evitar FE duplicada: %s', e)
            raise Exception(
                f'No se pudo verificar si ya existe FE para RM {tipo_docto_rm}-{consec_rm}: {e}. '
                'Reintenta cuando Connekta esté disponible.'
            )

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
        _errores_consec = 0
        for pag in range(1, 200):
            try:
                res = self._get(self.api_pedidos, {
                    'paginacion': f'numPag={pag}|tamPag=100',
                    'parametros': parametros
                })
                _errores_consec = 0
            except Exception as e:
                _errores_consec += 1
                logger.warning(
                    f'[CONNEKTA] get_pedidos_aprobados pag={pag} error ({_errores_consec}/3): {e}'
                )
                if _errores_consec >= 3:
                    raise Exception(
                        f'get_pedidos_aprobados abortó tras 3 errores consecutivos en pág={pag} — '
                        f'{len(all_items)} ítems parciales descartados'
                    )
                continue
            rows = res.get('detalle', {}).get('Table', [])
            if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                break
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

    def get_ordenes_compra_aprobadas(self, sin_filtros: bool = False, consec: str = None):
        """API_v2_Compras_Ordenes — muelle de recepción ciega.
        Pagina automáticamente (tamPag=100) hasta agotar los registros.
        Si se pasa consec, filtra por f420_consec_docto (number, sin comillas)
        para traer solo las líneas de esa OC y evitar timeouts.
        """
        if consec:
            base_params = {'parametros': f'f420_consec_docto={consec}'}
        elif sin_filtros:
            base_params = {}
        else:
            base_params = {'parametros': 'f420_ind_estado=1'}
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

    def validar_tipo_proveedor(self, nit: str) -> dict:
        """
        Verifica que el NIT tenga tipo_proveedor configurado en el maestro de Siesa.
        Usa las OCs activas para inferirlo — si el proveedor aparece en alguna OC
        y tiene f200_id_tipo_prov, el resultado es positivo.
        Retorna: {configurado: bool, tipo_proveedor: str|None, mensaje: str}
        """
        if self.modo_simulacion:
            return {'configurado': True, 'tipo_proveedor': '0001', 'mensaje': 'simulado'}
        try:
            resultado = self.get_ordenes_compra_aprobadas(sin_filtros=True)
            rows = resultado.get('detalle', {}).get('Table', [])
            for row in rows:
                nit_row = (row.get('f200_nit_prov') or row.get('f200_id_prov') or '').strip()
                if nit_row == nit.strip():
                    tipo = (row.get('f200_id_tipo_prov') or '').strip()
                    if tipo:
                        return {'configurado': True, 'tipo_proveedor': tipo, 'mensaje': ''}
                    # f200_id_tipo_prov no viene en API_v2_Compras_Ordenes — no verificable
                    return {'configurado': None, 'tipo_proveedor': None, 'mensaje': ''}
            # NIT no aparece en ninguna OC activa — probablemente es correcto pero no podemos verificar
            logger.warning(f'[CONNEKTA] validar_tipo_proveedor: NIT {nit!r} no encontrado en OCs activas')
            return {'configurado': None, 'tipo_proveedor': None, 'mensaje': ''}
        except Exception as e:
            logger.warning(f'[CONNEKTA] validar_tipo_proveedor falló: {e}')
            return {'configurado': None, 'tipo_proveedor': None, 'mensaje': ''}

    def get_inventario_fecha(self, item_codigo: str):
        """API_v2_Inventarios_InvFecha — existencia real para conteo cíclico.
        Timeout reducido a 8s: es user-facing, no puede bloquear un worker Gunicorn.
        """
        return self._get(self.api_inventario, {
            'paginacion': 'numPag=1|tamPag=10',
            'parametros': f"f120_referencia = ''{item_codigo}'' AND f150_id = ''{self.bodega}''"
        }, timeout=8)

    def get_item_por_barras(self, codigo_barras: str):
        """API_v2_ItemsBarras — traduce EAN del escáner al código Siesa.
        Campo correcto: f131_id. Sintaxis filtro Connekta: ''valor'' (doble comilla simple).
        """
        return self._get(self.api_barras, {
            'paginacion': 'numPag=1|tamPag=5',
            'parametros': f"f131_id = ''{codigo_barras}''"
        })

    def get_items_catalogo(self, pagina: int = 1):
        """API_v2_Items — catálogo completo de productos Siesa (para sync)."""
        api_items = os.getenv('CONNEKTA_API_ITEMS', 'API_v2_Items')
        return self._get(api_items, {
            'paginacion': f'numPag={pagina}|tamPag=100'
        })

    def get_items_unidades_medida(self, pagina: int = 1):
        """API_v2_ItemsUnidadesMedida — factores de conversión de empaques por ítem.
        Campos esperados: f120_referencia, f120_id_unidad_medida, factor (o f120_factor),
        f121_id (código de barras del empaque). Verificar nombres exactos en Paso 0.
        """
        return self._get(self.api_unidades_medida, {
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

        return self._get(
            'papeleriamedellin_monitos_facturas_wms',
            params_extra={'paginacion': f'numPag={pagina}|tamPag=100'},
            url=self.url_get_dinamico,
        )

    def get_compromisos_pedido(self, tipo_docto: str, consec_docto) -> list:
        """
        GET API_v2_Ventas_Pedidos_Compromisos
        Retorna líneas comprometidas pendientes de remisionar (f405_cant_por_remisionar_base > 0).
        Fuente autoritativa de cantidades cuando el WMS no tiene cantidad_real/esperada.
        Campos clave: f120_referencia (SKU), f405_cant_por_remisionar_base (qty), f405_id_lote.
        """
        if self.modo_simulacion:
            return []
        if not tipo_docto or not str(tipo_docto).strip():
            return []
        try:
            consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
            res = self._get('API_v2_Ventas_Pedidos_Compromisos', {
                'paginacion': 'numPag=1|tamPag=200',
                'parametros': (
                    f"f430_id_co = ''{self.centro_op}'' "
                    f"AND f430_id_tipo_docto = ''{tipo_docto}'' "
                    f"AND f430_consec_docto = {consec_int}"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            return [r for r in rows if float(r.get('f405_cant_por_remisionar_base') or 0) > 0]
        except Exception as e:
            logger.warning('[CONNEKTA] get_compromisos_pedido falló: %s', e)
            return []

    def get_remision_desde_pedido(self, tipo_docto_pedido: str, consec_docto_pedido) -> dict | None:
        """
        GET API_v2_Ventas_Remisiones_DesdePedido
        Recupera la RM más reciente vinculada al pedido — fallback cuando el response
        de 142945 no incluye el consecutivo del documento generado.
        Retorna {'tipo': 'RM', 'consec': 1234} o None si no existe.
        """
        if self.modo_simulacion:
            return None
        if not tipo_docto_pedido or not str(tipo_docto_pedido).strip():
            return None
        try:
            consec_int = int(consec_docto_pedido) if str(consec_docto_pedido).isdigit() else consec_docto_pedido
            res = self._get('API_v2_Ventas_Remisiones_DesdePedido', {
                'paginacion': 'numPag=1|tamPag=10',
                'parametros': (
                    f"f350_id_co = ''{self.centro_op}'' "
                    f"AND f430_id_tipo_docto = ''{tipo_docto_pedido}'' "
                    f"AND f430_consec_docto = {consec_int} "
                    f"AND f350_ind_estado <> 9"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            if not rows:
                return None
            # Tomar la más reciente (mayor consecutivo)
            rows_validas = [r for r in rows if r.get('f350_consec_docto')]
            if not rows_validas:
                return None
            fila = max(rows_validas, key=lambda r: int(r.get('f350_consec_docto', 0)))
            return {
                'tipo':  str(fila.get('f350_id_tipo_docto', 'RM')).strip(),
                'consec': int(fila['f350_consec_docto']),
            }
        except Exception as e:
            logger.warning('[CONNEKTA] get_remision_desde_pedido falló: %s', e)
            return None

    def get_pedido_cabecera(self, tipo_docto: str, consec_docto) -> dict | None:
        """
        GET API_v2_Ventas_Pedidos — fila única de cabecera del pedido.
        Devuelve una fila por línea de ítem; se toma rows[0] para extraer campos de cabecera.

        IMPORTANTE — aliases reales vs spec oficial (2026-05-08):
        El procedimiento almacenado usa aliases que difieren del spec v2 (API_v2_Ventas_Pedidos.docx).
        Los nombres abajo son los que devuelve la API real, NO los del spec.
        Ejemplo: spec dice 'f200_id_fact', real devuelve 'f200_id_pedido_fact'.

          f200_id_pedido_fact         → NIT/código tercero cliente (F350_ID_TERCERO en 142943)
          f461_id_sucursal_pedido_rem → sucursal (alias del JOIN a t461/t202)
          f430_id_tipo_cli_fact       → tipo cliente facturación
          f430_id_cond_pago           → condición de pago (ej. 'C01', '30D')
          f430_id_moneda_docto        → moneda del documento
          f430_id_moneda_conv         → moneda conversión
          f430_id_moneda_local        → moneda local
          f430_tasa_conv              → tasa conversión
          f430_tasa_local             → tasa local
          f200_id_pedido_vend         → NIT del vendedor

        f461_id_punto_envio NO existe en esta API — confirmado contra spec oficial y
        respuesta real (120 keys, 2026-05-08). El trigger usa SIESA_PUNTO_ENVIO_DEFAULT
        como valor permanente. Ver trigger_factura_desde_remision().

        Usado exclusivamente por DespachoParialService → trigger_factura_desde_remision.
        """
        if self.modo_simulacion:
            return None
        if not tipo_docto or not str(tipo_docto).strip():
            return None
        try:
            consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
            res = self._get(self.api_pedidos, {
                'paginacion': 'numPag=1|tamPag=5',
                'parametros': (
                    f"f430_id_co = ''{self.centro_op}'' "
                    f"AND f430_id_tipo_docto = ''{tipo_docto}'' "
                    f"AND f430_consec_docto = {consec_int} "
                    f"AND f430_ind_estado <> 9"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            return rows[0] if rows else None
        except Exception as e:
            logger.error('[CONNEKTA] get_pedido_cabecera(%s%s) falló: %s', tipo_docto, consec_docto, e)
            return None

    # ==========================================
    # POSTs — Bodies oficiales desde Ver Guía
    # ==========================================

    def trigger_despacho(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                          items: list):
        """
        142945 → API_v1_Ventas_Comercial_RemisionPedido
        LEGACY — preferir trigger_factura (conector 238925).
        Genera remisión desde pedido — descarga inventario cuenta 14.
        """
        if not self.modo_simulacion:
            if not self.tipo_docto_remision:
                raise ValueError(
                    'SIESA_TIPO_DOCTO_REMISION no está configurado. '
                    'Si se usa trigger_despacho, agrega la variable en Railway.'
                )
            if not self.motivo_ventas:
                raise ValueError(
                    'SIESA_ID_MOTIVO_VENTAS no está configurado. '
                    'Es obligatorio (pos 131, ancho 2) en connector 142945.'
                )
        # Siesa: fecha en formato YYYYMMDD (8 chars max)
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        # Filtrar ítems con cantidad 0 — Siesa acepta líneas vacías sin rechazar el documento
        # pero no descarga inventario, causando discrepancias silenciosas.
        items_validos = [i for i in items if float(i.get('cantidad_empacada') or 0) > 0]
        if not items_validos:
            raise ValueError(
                'trigger_despacho: ningún ítem tiene cantidad_empacada > 0 — '
                'el despacho no puede enviarse a Siesa sin líneas de movimiento.'
            )
        items = items_validos

        cia = int(self.id_cia_siesa)

        payload = {
            'Inicial': [
                {'F_CIA': cia}
            ],
            'Remision': [
                {
                    'F_CIA': cia,
                    'F_CONSEC_AUTO_REG': 1,
                    'F350_ID_CO': self.centro_op,
                    'F350_ID_TIPO_DOCTO': self.tipo_docto_remision,
                    'F350_CONSEC_DOCTO': 0,
                    'F350_FECHA': fecha_hoy,        # YYYYMMDD — 8 chars
                    'F350_IND_ESTADO': 1,            # 1 = Aprobado — descarga inventario cuenta 14 y cierra pedido en Siesa
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
                    'f462_cajas': 0,
                    'f462_peso': 0.0,
                    'f462_volumen': 0.0,
                    'f462_valor_seguros': 0.0,
                    'f462_notas': None,
                    'f460_id_cond_pago': None
                }
            ],
            'Movtoventascomercial': [
                {
                    'F_CIA': cia,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_remision,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': self.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': i.get('lote') or None,
                    'f470_id_concepto': self.concepto_ventas,         # 501 = Ventas (maestro Siesa), override: SIESA_CONCEPTO_VENTAS
                    'f470_id_motivo': self.motivo_ventas or None,     # SIESA_ID_MOTIVO_VENTAS (pos 131, ancho 2) — DEBE configurarse en Railway
                    'f470_ind_obsequio': 0,
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_lista_precio': self.lista_precio or None,  # SIESA_LISTA_PRECIO (pos 169, ancho 3)
                    'f470_id_unidad_precio': i.get('unidad_medida') or None,
                    'f470_id_unidad_medida': i.get('unidad_medida') or None,
                    'f470_cant_base': round(float(abs(i.get('cantidad_empacada') or 0)), 4),
                    'f470_cant_2': None,
                    'f470_vlr_bruto': None,
                    'f470_ind_naturaleza': 2,                         # 2 = Salida/Venta (spec 142945: 1=Entrada/Devol, 2=Salida/Venta)
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
                    'f470_id_un_movto': self.unidad_negocio,   # spec: unidad de negocio, no centro_op
                    'f470_id_causal_devol': None
                }
                for idx, i in enumerate(items)
            ],
            'Final': [
                {'F_CIA': cia}
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
        # [48] Validar tipo_docto antes de enviar (solo en modo producción real)
        # Un valor vacío causaría rechazo silencioso en Siesa sin mensaje de error claro.
        if not self.modo_simulacion and (not tipo_docto_pedido or not str(tipo_docto_pedido).strip()):
            raise ValueError(
                'tipo_docto_pedido está vacío — configura SIESA_TIPO_DOCTO_FACTURA '
                'o verifica que el pedido tenga tipo de documento asignado'
            )
        # Pre-check idempotencia: si el pedido ya fue facturado (estado=4 Cumplido),
        # no reenviar POST — evita factura FE duplicada en retry de DLQ tras timeout.
        if not self.modo_simulacion:
            try:
                estado_pre = self.get_estado_pedido(tipo_docto_pedido, consec_docto_pedido)
                if estado_pre is not None and str(estado_pre) == '4':
                    logger.warning(
                        f'[CONNEKTA] trigger_factura: pedido {tipo_docto_pedido}{consec_docto_pedido} '
                        f'ya está Cumplido (estado=4) en Siesa — omitiendo POST para evitar duplicado'
                    )
                    return {'idempotente': True, 'mensaje': 'Pedido ya facturado en Siesa (estado=4)'}
            except Exception as _e:
                logger.warning(f'[CONNEKTA] Pre-check factura falló: {_e} — continuando con POST')

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
                'F350_IND_ESTADO': 1,
                'F430_ID_TIPO_DOCTO': tipo_docto_pedido or None,
                'F430_CONSEC_DOCTO': consec_int
            }],
            'Cuotas_CxC': [{
                'F_CIA': int(self.id_cia_siesa),
                'F350_ID_CO': self.centro_op,
                'F350_ID_TIPO_DOCTO': self.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F353_FECHA_VCTO': fecha_vcto,
                'F353_FECHA_DSCTO_PP': fecha_vcto
            }]
        }

        logger.info(
            f'[CONNEKTA] Factura desde pedido {tipo_docto_pedido}{consec_docto_pedido} '
            f'F430_ID_TIPO_DOCTO={payload["Docto_ventas_comercial"][0].get("F430_ID_TIPO_DOCTO")} '
            f'F430_CONSEC_DOCTO={payload["Docto_ventas_comercial"][0].get("F430_CONSEC_DOCTO")}'
        )
        return self._post(
            self.conector_factura, 'FACTURA_DESDE_PEDIDO', payload,
            url=self.url_post_dinamico,
            extra_params={'idSistema': self.id_sistema}
        )

    def trigger_factura_desde_remision(self, tipo_docto_rm: str, consec_rm: int,
                                        cabecera: dict):
        """
        142943 → API_v1_Ventas_Comercial_FacturaRemision
        Convierte una remisión (RM) en factura electrónica (FE).
        Estructura oficial confirmada en docx 142943.
        cabecera: dict devuelto por get_pedido_cabecera() con campos del pedido original.
        RelacionDoctos vincula la FE a la RM — Siesa no hereda campos del RM, se envían explícitamente.
        Usado exclusivamente por DespachoParialService — no toca flujo de packing.
        """
        from datetime import timedelta
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        fecha_vcto = (datetime.utcnow() + timedelta(days=30)).strftime('%Y%m%d')
        cia = int(self.id_cia_siesa)

        # Nombres de campo verificados empíricamente contra JSON real de API_v2_Ventas_Pedidos
        # El procedimiento almacenado de Siesa usa aliases propios — NO son los nombres de tabla base.
        tercero      = cabecera.get('f200_id_pedido_fact') or ''
        sucursal     = cabecera.get('f461_id_sucursal_pedido_rem') or None  # None → Siesa hereda del maestro
        tipo_cli     = cabecera.get('f430_id_tipo_cli_fact') or None        # None → Siesa hereda del maestro
        _cond_pago_siesa = cabecera.get('f430_id_cond_pago')
        cond_pago    = _cond_pago_siesa or self.cond_pago_ventas or None
        if not cond_pago:
            raise ValueError(
                'f430_id_cond_pago no disponible en cabecera y SIESA_COND_PAGO_VENTAS no configurado — '
                'Connekta V2 .NET serializer colapsa con HTTP 500 si se envía null'
            )
        if not _cond_pago_siesa and cond_pago == self.cond_pago_ventas:
            # Data maestra incompleta — factura se emite como CONTADO pero no bloquea el despacho.
            # Alerta asíncrona para que el equipo comercial corrija el maestro del tercero en Siesa.
            _tercero_alerta = cabecera.get('f200_id_pedido_fact') or 'desconocido'
            logger.warning(
                '[CONNEKTA] RM %s-%s: f430_id_cond_pago vacío — fallback %s (CONTADO). '
                'Maestro del cliente %s en Siesa sin condición de pago asignada.',
                tipo_docto_rm, consec_rm, cond_pago, _tercero_alerta
            )
            try:
                from app.services.alertas_service import _enviar_email_con_dlq
                _cuerpo = (
                    f'El pedido RM-{consec_rm} del cliente {_tercero_alerta} fue facturado '
                    f'automáticamente como CONTADO ({cond_pago}) porque Siesa no devolvió '
                    f'condición de pago (f430_id_cond_pago vacío).\n\n'
                    f'Acción requerida: actualizar el maestro del tercero {_tercero_alerta} '
                    f'en Siesa Enterprise con la condición de pago correcta para evitar '
                    f'futuras facturas incorrectas y posibles fricciones con el cliente.\n\n'
                    f'Documento: {tipo_docto_rm}-{consec_rm}'
                )
                _enviar_email_con_dlq(
                    asunto='[WMS ALERTA] Factura emitida como CONTADO por data incompleta en Siesa',
                    cuerpo_html=f'<pre>{_cuerpo}</pre>',
                    cuerpo_texto=_cuerpo,
                    tipo_alerta='DATA_MAESTRA_COND_PAGO'
                )
            except Exception as _e_alert:
                logger.error('[CONNEKTA] Email alerta data maestra falló: %s', _e_alert)
        moneda_docto = cabecera.get('f430_id_moneda_docto') or 'COP'
        moneda_conv  = cabecera.get('f430_id_moneda_conv') or moneda_docto
        moneda_local = cabecera.get('f430_id_moneda_local') or moneda_docto
        tasa_conv    = float(cabecera.get('f430_tasa_conv') or 1)
        tasa_local   = float(cabecera.get('f430_tasa_local') or 1)
        vendedor     = cabecera.get('f200_id_pedido_vend') or None  # None → Siesa hereda del maestro
        punto_envio  = cabecera.get('f461_id_punto_envio') or self.punto_envio_default
        if not cabecera.get('f461_id_punto_envio'):
            _cabecera_keys = list(cabecera.keys()) if cabecera else []
            logger.warning(
                '[CONNEKTA] RM %s-%s: f461_id_punto_envio no devuelto por API_v2_Ventas_Pedidos. '
                'Fallback a SIESA_PUNTO_ENVIO_DEFAULT=%r. Keys disponibles en cabecera: %s',
                tipo_docto_rm, consec_rm, self.punto_envio_default, _cabecera_keys
            )
        if not punto_envio:
            raise ValueError(
                f'f461_id_punto_envio no disponible para RM {tipo_docto_rm}-{consec_rm}. '
                'API_v2_Ventas_Pedidos no devuelve el campo y SIESA_PUNTO_ENVIO_DEFAULT no está configurado. '
                'Verificar con consultor Siesa el código de punto de envío del cliente '
                f"(tercero={cabecera.get('f200_id_pedido_fact', 'desconocido')}) "
                'y configurar la variable en Railway.'
            )

        if not self.modo_simulacion and not tercero:
            raise ValueError(
                'get_pedido_cabecera no devolvió f200_id_pedido_fact — '
                'no se puede construir la FE sin el código de tercero cliente'
            )

        payload = {
            'Inicial': [{'F_CIA': cia}],
            'Doctoventascomercial': [{
                'F_CIA': cia,
                'F_CONSEC_AUTO_REG': 1,
                'F350_ID_CO': self.centro_op,
                'F350_ID_TIPO_DOCTO': self.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F350_FECHA': fecha_hoy,
                'F350_ID_TERCERO': tercero,
                'F350_IND_ESTADO': 1,
                'F350_IND_IMPRESION': 0,
                'f461_id_sucursal_fact': sucursal,
                'f461_id_tipo_cli_fact': tipo_cli,
                'f461_id_co_fact': self.centro_op,
                'f461_id_cli_contado': None,
                'f461_id_tercero_rem': tercero,
                'f461_id_sucursal_rem': sucursal,
                'f461_id_tercero_vendedor': vendedor,
                'f461_referencia': None,
                'f461_id_cargue': None,
                'f461_id_cond_pago': cond_pago,
                'f461_id_moneda_docto': moneda_docto,
                'f461_id_moneda_conv': moneda_conv,
                'f461_tasa_conv': tasa_conv,
                'f461_id_moneda_local': moneda_local,
                'f461_tasa_local': tasa_local,
                'f461_notas': '.',
                'f461_id_punto_envio': punto_envio,
                'f462_id_vehiculo': None,
                'f462_id_tercero_transp': None,
                'f462_id_sucursal_transp': None,
                'f462_id_tercero_conductor': None,
                'f462_nombre_conductor': None,
                'f462_identif_conductor': None,
                'f462_numero_guia': None,
                'f462_cajas': 0,
                'f462_peso': 0.0,
                'f462_volumen': 0.0,
                'f462_valor_seguros': 0.0,
                'f462_notas': None,
                'f462_id_caja': None,
                'F461_IND_GENERA_KIT': 0,
                'F461_ID_TIPO_DOCTO_PROCESO': None,
                'F461_ID_BODEGA_COMPON_PROCESO': None,
                'F461_ID_MOTIVO_SALIDA_PROCESO': None,
                'F461_ID_MOTIVO_ENTRADA_PROCESO': None,
                'F461_ID_CLASE_DOCTO_PROCESO': None,
                'F461_ID_UN_CXC': self.unidad_negocio,
                'F461_ID_CCOSTO_CXC': None,
                'f461_tasa_dscto_global_cap': None,
                'f461_valor_dscto_global_cap': None,
                'f461_num_docto_referencia': None,
            }],
            'RelacionDoctos': [{
                'F_CIA': cia,
                'F350_ID_CO': self.centro_op,
                'F350_ID_TIPO_DOCTO': self.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F460_ID_CO': self.centro_op,
                'F460_ID_TIPO_DOCTO': tipo_docto_rm,
                'F460_CONSEC_DOCTO': int(consec_rm),
            }],
            'CuotasCxC': [{
                'F_CIA': cia,
                'F350_ID_CO': self.centro_op,
                'F350_ID_TIPO_DOCTO': self.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F353_ID_TIPO_DOCTO_CRUCE': None,
                'F353_CONSEC_DOCTO_CRUCE': None,
                'F353_NRO_CUOTA_CRUCE': 0,
                'F353_VLR_CRUCE': None,
                'F_PORCENTAJE_CUOTA': '100.00',
                'F353_FECHA_VCTO': fecha_vcto,
                'F353_VLR__DSCTO_PP': None,
                'F_PORCENTAJE_PP': '000.00',
                'F353_FECHA_DSCTO_PP': fecha_vcto,
            }],
            'Final': [{'F_CIA': cia}],
        }

        logger.info(
            '[CONNEKTA] FacturaDesdeRemision %s%s → FE tercero=%s',
            tipo_docto_rm, consec_rm, tercero
        )
        return self._post(
            self.conector_factura_remision,
            'API_v1_Ventas_Comercial_FacturaRemision',
            payload
        )

    def confirmar_entrada_compras(self, id_co_oc: str, tipo_docto_oc: str,
                                   consec_docto_oc: str, items: list,
                                   es_parcial: bool = False,
                                   proveedor_id: str = None,
                                   sucursal_prov: str = None,
                                   tercero_comprador: str = None,
                                   moneda_docto: str = None,
                                   moneda_conv: str = None,
                                   moneda_local: str = None,
                                   tasa_conv: float = 0.0,
                                   tasa_local: float = 0.0,
                                   num_docto_referencia: str = None,
                                   cond_pago: str = None):
        """
        142948 → API_v1_Compras_Comercial_EntradaOC
        Genera entrada desde OC — debita cuenta 1435.
        """
        if not self.tipo_docto_entrada_oc:
            raise ValueError(
                'SIESA_TIPO_DOCTO_ENTRADA_OC no está configurado en variables de entorno. '
                'Agrega la variable en Railway con el código de tipo de documento de entrada OC en Siesa.'
            )
        # Siesa espera fecha sin guiones: YYYYMMDD (8 chars). f421_fecha_entrega usa YYYY-MM-DD.
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        fecha_hoy_iso = datetime.utcnow().strftime('%Y-%m-%d')

        # F_CIA debe ser entero según especificación Siesa/Connekta
        cia = int(self.id_cia_siesa)

        # Siesa exige sucursal de 3 chars (ej. '1' → '001'). Sin NIT+sucursal, rechaza.
        sucursal_prov_fmt = sucursal_prov.strip().zfill(3) if sucursal_prov and sucursal_prov.strip() else None
        # f350_id_tercero es OBLIGATORIO en spec 142948 (pos 43-58). Bloquear localmente antes
        # de gastar ancho de banda en un POST que Siesa rechazará con error 500.
        if not proveedor_id:
            raise ValueError(
                'confirmar_entrada_compras: proveedor_id es None — '
                'f350_id_tercero es obligatorio en 142948 (pos 43-58). '
                'Verificar que la OC en Siesa expone f200_id_prov correctamente.'
            )
        if not sucursal_prov_fmt:
            raise ValueError(
                'confirmar_entrada_compras: sucursal_prov es None o vacío — '
                'f451_id_sucursal_prov es obligatorio en 142948 (pos 324-327).'
            )
        if not (cond_pago or self.cond_pago_compras):
            logger.warning(
                '[CONNEKTA] EntradaOC — f451_id_cond_pago vacío: campo obligatorio pos 324. '
                'Configura SIESA_COND_PAGO_COMPRAS en Railway o pasa cond_pago en la recepción.'
            )
        # Payload sanitizer anticipado — si todos los ítems tienen cantidad 0, abortar aquí.
        items_validos = [i for i in items if float(i.get('cantidad_recibida') or 0) > 0]
        if not items_validos:
            raise ValueError(
                'confirmar_entrada_compras: todos los ítems tienen cantidad_recibida=0 — '
                'nada que enviar a Siesa. Verificar recepción antes de confirmar.'
            )

        payload = {
            'Inicial': [
                {'F_CIA': cia}
            ],
            'Documentos': [
                {
                    'F_CIA': cia,
                    'F_CONSEC_AUTO_REG': 1,                                          # 1 = Siesa auto-asigna consecutivo
                    'f350_id_co': self.centro_op,                                    # CO del documento
                    'f350_id_tipo_docto': self.tipo_docto_entrada_oc or None,        # tipo doc entrada OC (SIESA_TIPO_DOCTO_ENTRADA_OC)
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': proveedor_id or None,                         # NIT proveedor (pos 43-58)
                    'f350_ind_estado': 1,                                            # 1 = Aprobado — contabiliza automáticamente contra pasivo estimado (26059501 configurado en tipo EA/CO003)
                    'f350_ind_impresion': 0,
                    'f350_notas': None,
                    'f451_id_cond_pago': cond_pago or self.cond_pago_compras or None,  # [A2] condición pago — obligatorio spec 142948 pos 324
                    'f451_id_sucursal_prov': sucursal_prov_fmt,                      # sucursal proveedor (pos 324-327) — 3 chars, zfill aplicado
                    'f451_id_tercero_comprador': tercero_comprador or self.nit_empresa or None,  # comprador exacto de la OC
                    'f451_num_docto_referencia': num_docto_referencia,
                    'f451_id_moneda_docto': moneda_docto,
                    'f451_id_moneda_conv': moneda_conv,
                    'f451_tasa_conv': tasa_conv if tasa_conv else 1.0,
                    'f451_id_moneda_local': moneda_local,
                    'f451_tasa_local': tasa_local if tasa_local else 1.0,
                    'f451_tasa_dscto_global1': 0.0,
                    'f451_tasa_dscto_global2': 0.0,
                    'f462_id_vehiculo': None,
                    'f462_id_tercero_transp': None,
                    'f462_id_sucursal_transp': None,
                    'f462_id_tercero_conductor': None,
                    'f462_nombre_conductor': None,
                    'f462_identif_conductor': None,
                    'f462_numero_guia': None,
                    'f462_cajas': 0,
                    'f462_peso': 0.0,
                    'f462_volumen': 0.0,
                    'f462_valor_seguros': 0.0,
                    'f462_notas': None,
                    'f451_ind_consignacion': 0,
                    'f420_id_co_docto': id_co_oc,
                    'f420_id_tipo_docto': tipo_docto_oc,
                    'f420_consec_docto': int(consec_docto_oc) if consec_docto_oc else 0,
                    'f420_ind_modo_sobrecosto': 0                                        # 0=No liquida — evita posteo a pasivo estimado (26059501 no tiene flag Proveedor habilitado)
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': cia,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_entrada_oc or None,        # tipo doc movimiento (pos 22-25)
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': i.get('bodega') or self.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': i.get('lote') or None,
                    # [A10] 142948 spec does NOT have f470_id_concepto, f470_ind_naturaleza,
                    # f470_ind_obsequio, f470_ind_solo_valor, f470_ind_impto_asumido — removed
                    # Bonificación usa motivo '04' (obsequio/bonif en Siesa). OC usa motivo de la OC o motivo_compras.
                    'f470_id_motivo': i.get('motivo_siesa') or ('04' if i.get('tipo') == 'BONIFICACION' else self.motivo_compras),
                    # UOM y fecha_entrega deben coincidir exactamente con los de la OC (Siesa los valida)
                    'f470_id_unidad_medida': i.get('uom') or i.get('unidad_medida') or self.uom_default,
                    'f421_fecha_entrega': self._fmt_fecha_iso(i.get('fecha_entrega')) or fecha_hoy_iso,
                    'f470_cant_base': round(float(i['_qty']), 4),  # filtrado previo garantiza > 0
                    'f470_cant_2': 0.0,
                    'f470_notas': None,
                    'f470_id_item': None,
                    'f470_referencia_item': i.get('producto_codigo'),
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_rowid': 0
                }
                # Payload sanitizer: filtrar ítems con cantidad_recibida <= 0 ANTES del POST.
                # Siesa rechaza f470_cant_base=0.0 con error duro (regla de cuenta 14).
                # Entregas parciales dejan el resto de la OC como backorder en Siesa.
                for idx, i in enumerate(
                    [dict(item, _qty=float(item.get('cantidad_recibida') or 0))
                     for item in items
                     if float(item.get('cantidad_recibida') or 0) > 0]
                )
            ],
            'Final': [
                {'F_CIA': cia}
            ]
        }

        return self._post(self.conector_entrada, 'API_v1_Compras_Comercial_EntradaOC', payload)

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Ajuste físico tras conteo cíclico double-blind.
        AJ-ENT: sobrante. AJ-SAL: faltante. Cantidad siempre positiva.
        """
        if not self.tipo_docto_ajuste:
            raise ValueError(
                'SIESA_TIPO_DOCTO_AJUSTE no está configurado en variables de entorno. '
                'Agrega la variable en Railway con el código de tipo de documento de ajuste en Siesa.'
            )
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}')

        # Mapeo WMS → Siesa: concepto 0603 (Ajuste a inventario)
        # Motivo 01 = Entrada Ajuste (sobrante), 02 = Salida Ajuste (faltante)
        es_entrada = motivo_codigo == 'AJ-ENT'
        siesa_motivo   = '01' if es_entrada else '02'

        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        cia = int(self.id_cia_siesa)

        payload = {
            'Inicial': [
                {'F_CIA': cia}
            ],
            'Documentos': [
                {
                    'F_CIA': cia,
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_ajuste,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': self.nit_empresa or None,
                    'f350_id_clase_docto': 63,           # Entero obligatorio: 63=Ajustes (spec 142951)
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': referencia,
                    'f450_id_concepto': 603,                                         # 603 = Ajustes (spec 142951, obligatorio)
                    'f450_id_bodega_salida': self.bodega if not es_entrada else None,
                    'f450_id_bodega_entrada': self.bodega if es_entrada else None,
                    'f450_docto_alterno': None,
                    'f350_id_co_base': None,          # None cuando no aplica tránsito; Siesa rechaza string vacío
                    'f350_id_tipo_docto_base': None,  # None cuando no aplica tránsito; Siesa rechaza string vacío
                    'f350_consec_docto_base': 0,      # Entero (spec 142951) — 0 cuando no aplica tránsito
                    'f462_id_vehiculo': None,        # Dep — None cuando no hay transportador
                    'f462_id_tercero_transp': None,
                    'f462_id_sucursal_transp': None,
                    'f462_id_tercero_conductor': None,
                    'f462_nombre_conductor': None,
                    'f462_identif_conductor': None,
                    'f462_numero_guia': None,
                    'f462_cajas': 0,
                    'f462_peso': 0.0,
                    'f462_volumen': 0.0,
                    'f462_valor_seguros': 0.0,
                    'f462_notas': None
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': cia,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_ajuste,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': 1,
                    'f470_id_bodega': self.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': None,
                    'f470_id_concepto': self.concepto_ajustes,                       # 603 = Ajustes (spec 142951, obligatorio), override: SIESA_CONCEPTO_AJUSTES
                    'f470_id_motivo': siesa_motivo,
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': self.uom_default,
                    'f470_cant_base': round(float(abs(cantidad)), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': '',
                    'f470_desc_variable': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': self.uom_default,
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_lote_ent': None,
                    'f470_id_item': None,
                    'f470_referencia_item': item_codigo,
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': self.unidad_negocio   # spec 142951: unidad de negocio, no centro_op
                }
            ],
            'Final': [
                {'F_CIA': cia}
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
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        cia_averias = int(self.id_cia_siesa)

        payload = {
            'Inicial': [
                {'F_CIA': cia_averias}
            ],
            'Documentos': [
                {
                    'F_CIA': cia_averias,
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_traslado,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': self.nit_empresa or None,                      # SIESA_NIT_EMPRESA — None si no configurado; Siesa rechaza string vacío
                    'f350_id_clase_docto': 67,           # Entero obligatorio: 67=Transferencias (spec 142951)
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': referencia or f'Avería detectada por WMS · {item_codigo}',
                    'f450_id_concepto': 607,                                         # 607 = Transferencias (spec 142951, obligatorio)
                    'f450_id_bodega_salida': self.bodega,
                    'f450_id_bodega_entrada': self.bodega_averias,
                    'f450_docto_alterno': None,
                    'f350_id_co_base': None,          # None cuando no aplica tránsito; Siesa rechaza string vacío
                    'f350_id_tipo_docto_base': None,  # None cuando no aplica tránsito; Siesa rechaza string vacío
                    'f350_consec_docto_base': 0,      # Entero (spec 142951) — 0 cuando no aplica tránsito
                    'f462_id_vehiculo': None,        # Dep — None cuando no hay transportador
                    'f462_id_tercero_transp': None,
                    'f462_id_sucursal_transp': None,
                    'f462_id_tercero_conductor': None,
                    'f462_nombre_conductor': None,
                    'f462_identif_conductor': None,
                    'f462_numero_guia': None,
                    'f462_cajas': 0,
                    'f462_peso': 0.0,
                    'f462_volumen': 0.0,
                    'f462_valor_seguros': 0.0,
                    'f462_notas': None
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': cia_averias,
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_traslado,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': 1,
                    'f470_id_bodega': self.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': None,
                    'f470_id_concepto': self.concepto_traslados,                     # 607 = Transferencias (spec 142951, obligatorio), override: SIESA_CONCEPTO_TRASLADOS
                    'f470_id_motivo': self.motivo_averia,                            # SIESA_MOTIVO_AVERIA — validado contra maestro Siesa por compañía
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': self.uom_default,
                    'f470_cant_base': round(float(abs(cantidad)), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': '',
                    'f470_desc_variable': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': self.uom_default,   # consistente con enviar_ajuste_inventario
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_lote_ent': None,
                    'f470_id_item': None,
                    'f470_referencia_item': item_codigo,
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': self.unidad_negocio
                }
            ],
            'Final': [
                {'F_CIA': cia_averias}
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

    def get_ubicaciones_siesa(self, bodega_id: str = None, pagina: int = 1):
        """
        API_v2_Ubicaciones (ID 43) — maestro de ubicaciones auxiliares de una bodega.

        Campos certificados (PDF oficial Connekta):
          f150_id          → código de bodega           (string, ej. 'NB1')
          f155_id_cia      → id empresa ERP             (number)
          f155_id          → código de la ubicación     (string, ej. 'PIK-01-A')
          f155_descripcion → descripción de la ubicación (string)
          f155_ind_estado  → 0=Inactivo, 1=Activo       (number)

        IMPORTANTE: La API no expone stock_minimo/stock_maximo — esos campos
        se configuran directamente en el WMS (tabla ubicaciones).

        El tipo_zona se deduce del prefijo del código:
          PIK* → PICKING   |   RES* → RESERVA   |   resto → GENERAL
        """
        api_name = os.getenv('CONNEKTA_API_UBICACIONES', 'API_v2_Ubicaciones')
        params: dict = {'paginacion': f'numPag={pagina}|tamPag=100'}
        if bodega_id:
            params['parametros'] = f"f150_id = ''{bodega_id}''"
        return self._get(api_name, params)

    def transferir_entre_ubicaciones(self, bodega_id: str, ubicacion_origen: str,
                                      ubicacion_destino: str, referencia_item: str,
                                      cantidad: int, nota: str = ''):
        """
        Conector 173066 (TransferenciaDirecta) — traslado interno en UN SOLO PASO
        entre ubicaciones dentro de la MISMA bodega (RESERVA → PICKING).

        Reemplaza al 173076 (TransitoSalida) que requería dos pasos y bodega de tránsito.
        Siesa requiere que la bodega tenga "Maneja multi ubicaciones" activo.

        Schema certificado (docx oficial 173066):
          Documentos: f350_* + f450_id_bodega_salida/entrada (misma bodega)
          Movimientos: f470_id_ubicacion_aux (origen) + f470_id_ubicacion_aux_ent (destino)
          Sin f450_docto_alterno (eso es exclusivo de 173076).
        """
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        tipo_docto = self.tipo_docto_traslado or 'TRA'

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [{
                'F_CIA': int(self.id_cia_siesa),
                'F_CONSEC_AUTO_REG': 1,
                'f350_id_co': self.centro_op,
                'f350_id_tipo_docto': tipo_docto,
                'f350_consec_docto': 0,
                'f350_fecha': fecha_hoy,
                'f350_id_tercero': self.nit_empresa or None,  # [C4] None en vez de '' — spec 173066
                'f350_ind_estado': 1,
                'f350_ind_impresion': 0,  # [M1] consistente con transferencia_directa (mismo conector)
                'f350_notas': nota[:200] if nota else '',
                'f450_id_bodega_salida': bodega_id,
                'f450_id_bodega_entrada': bodega_id,  # misma bodega — traslado interno
            }],
            'Movimientos': [{
                'F_CIA': int(self.id_cia_siesa),
                'f470_id_co': self.centro_op,
                'f470_id_tipo_docto': tipo_docto,
                'f470_consec_docto': 0,
                'f470_nro_registro': 1,
                'f470_id_bodega': bodega_id,
                'f470_id_ubicacion_aux': ubicacion_origen,       # origen (ej. RES-01-A)
                'f470_id_lote': None,                            # Dep — si ítem maneja lotes
                'f470_id_motivo': self.motivo_traslado or '01',
                'f470_id_co_movto': self.centro_op,
                'f470_id_ccosto_movto': None,                    # Dep — si cuenta contable exige ccosto
                'f470_id_proyecto': None,
                'f470_id_unidad_medida': self.uom_default or 'UND',
                'f470_cant_base': round(float(cantidad), 4),
                'f470_cant_2': None,                             # Dep — unidad adicional
                'f470_costo_prom_uni': None,                     # Dep — costo unitario
                'f470_notas': None,
                'f470_id_ubicacion_aux_ent': ubicacion_destino,  # destino (ej. PIK-01-B)
                'f470_id_lote_ent': None,
                'f470_id_item': None,                            # Dep — usamos referencia_item
                'f470_referencia_item': referencia_item,
                'f470_codigo_barras': None,
                'f470_id_ext1_detalle': None,
                'f470_id_ext2_detalle': None,
                'f470_id_un_movto': self.unidad_negocio,
            }],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}],
        }

        return self._post(
            self.conector_transferencia_directa,
            'API_v1_Inventarios_Comercial_TransferenciaDirecta',
            payload,
        )

    def get_stock_bodega(self, bodega_id: str):
        """API_v2_Inventarios_InvFecha — existencia real en una bodega específica.
        Tienda consulta disponibilidad en NB1 antes de armar su solicitud."""
        all_rows = []
        for pag in range(1, 6):  # 500 ítems máx (5 págs × 100) — suficiente para NB1
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
        if not self.tipo_docto_req_traslado:
            raise ValueError(
                'SIESA_TIPO_DOCTO_TRASLADO no está configurado. '
                'Agrega la variable en Railway con el código de tipo de documento '
                'de requisición para transferir en Siesa '
                '(Inventarios → Tipos de documento → clase 75).'
            )
        for _item in items:
            if not _item.get('codigo_siesa'):
                raise ValueError(
                    f'Item sin codigo_siesa en requisicion_traslado: {_item.get("codigo") or _item}. '
                    'Nunca usar código interno WMS como fallback hacia Siesa.'
                )
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f440_id_co': self.centro_op,
                    'f440_id_tipo_docto': self.tipo_docto_req_traslado,
                    'f440_consec_docto': 0,
                    'f440_fecha': fecha_hoy,
                    'f440_id_tercero': self.nit_empresa or None,
                    'f440_id_solicitante': self.req_solicitante or None,  # [A12] None when empty — Siesa rejects ''
                    'f440_fecha_entrega': fecha_hoy,
                    'f440_num_dias_entrega': 0,
                    'f440_ind_estado': 1,
                    'f440_ind_impresion': 0,
                    'f440_notas': f'WMS {codigo_solicitud}',
                    'f440_id_bodega_salida': bodega_origen,
                    'f440_id_bodega_entrada': bodega_destino,
                    'f440_referencia': codigo_solicitud,
                    'f440_id_ubicacion_ent': None,
                    'f440_id_cargue': None,
                    'f440_num_docto_referencia': None,
                    'f440_id_proyecto': None,
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'f441_id_co': self.centro_op,
                    'f441_id_tipo_docto': self.tipo_docto_req_traslado,
                    'f441_consec_docto': 0,
                    'f441_nro_registro': idx + 1,
                    'f441_id_item': 0,
                    'f441_referencia_item': item.get('codigo_siesa'),
                    'f441_codigo_barras': None,
                    'f441_id_ext1_detalle': None,
                    'f441_id_ext2_detalle': None,
                    'f441_id_bodega': bodega_origen,
                    'f441_id_motivo': self.motivo_traslado,
                    'f441_id_unidad_medida': item.get('unidad_medida') or self.uom_default,  # obligatorio en 174646 — fallback a SIESA_UOM_DEFAULT
                    'f441_cant_base': round(float(abs(item.get('cantidad', 0))), 4),  # número con precisión decimal — Connekta serializa a 20 chars fixed-width
                    'f441_cant_2': 0,
                    'f441_fecha_entrega': fecha_hoy,
                    'f441_num_dias_entrega': 0,
                    'f441_id_co_movto': self.centro_op,
                    'f441_id_ccosto_movto': None,
                    'f441_id_proyecto': None,
                    'f441_notas': None,
                    'f441_id_un_movto': self.unidad_negocio,
                    'f441_precio_unitario': 0,
                    'f441_id_ubicacion_sal': None,
                    'f441_id_proy_etapa': None,
                    'f441_id_rubro_pof': None,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}]
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
        if not self.tipo_docto_transito_salida:
            raise ValueError(
                'SIESA_TIPO_DOCTO_TRANSITO_SALIDA no configurado — crear tipo doc '
                'amarrado a Clase 65 en Siesa (Inventarios → Tipos de documento)'
            )
        for _item in items:
            if not _item.get('codigo_siesa'):
                raise ValueError(
                    f'Item sin codigo_siesa en transferencia_transito_salida: {_item.get("codigo") or _item}. '
                    'Nunca usar código interno WMS como fallback hacia Siesa.'
                )
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [
                {
                    # 13 keys obligatorias para f450 — tamaño exacto 826 bytes
                    'F_CIA': int(self.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_transito_salida,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': self.nit_empresa or None,                      # SIESA_NIT_EMPRESA — None si no configurado; Siesa rechaza string vacío
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
                    'F_CIA': int(self.id_cia_siesa),
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_transito_salida,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_origen,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': None,
                    # [A10] 173076 spec does NOT have f470_id_concepto, f470_ind_naturaleza,
                    # f470_ind_obsequio, f470_ind_solo_valor, f470_ind_impto_asumido — removed
                    'f470_id_motivo': self.motivo_traslado,
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': item.get('unidad_medida') or 'UND',
                    'f470_cant_base': round(float(abs(item.get('cantidad', 0))), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': None,
                    # Nombre exacto del docx 173076 (typo incluido: 'varible' no 'variable').
                    # El serializador de Connekta usa este nombre para construir el flat file
                    # posicional. Si el nombre difiere, el registro se trunca en pos 487 (702 chars)
                    # y Siesa rechaza con "Tamaño del registro no corresponde al exigido".
                    'f470_desc_varible': None,
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_lote_ent': None,
                    'f470_id_item': None,
                    'f470_referencia_item': item.get('codigo_siesa'),
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': self.unidad_negocio,  # SIESA_UNIDAD_NEGOCIO — obligatorio
                    'f470_rowid_movto': 0,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}]
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
        if not self.tipo_docto_transito_entrada:
            raise ValueError(
                'SIESA_TIPO_DOCTO_TRANSITO_ENTRADA no configurado — crear tipo doc '
                'amarrado a Clase 66 en Siesa (Inventarios → Tipos de documento)'
            )
        if not consec_salida:
            raise ValueError(
                'consec_salida obligatorio para 173079 — no se puede recibir tránsito '
                'sin el consecutivo del documento de salida (173076)'
            )
        if not self.tipo_docto_transito_salida:
            raise ValueError(
                'SIESA_TIPO_DOCTO_TRANSITO_SALIDA no configurado — requerido en 173079 '
                'para f350_id_tipo_docto_base (referencia al documento de salida 173076)'
            )
        for _item in items:
            if not _item.get('codigo_siesa'):
                raise ValueError(
                    f'Item sin codigo_siesa en transferencia_transito_entrada: {_item.get("codigo") or _item}. '
                    'Nunca usar código interno WMS como fallback hacia Siesa.'
                )
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_transito_entrada,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': self.nit_empresa or None,                      # SIESA_NIT_EMPRESA — None si no configurado; Siesa rechaza string vacío
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Recepcion {codigo_solicitud}',
                    'f450_id_bodega_salida': bodega_transito,
                    'f450_id_bodega_entrada': bodega_destino,
                    'f450_docto_alterno': codigo_solicitud,
                    # Referencia obligatoria al doc 173076 de salida
                    'f350_id_co_base': self.centro_op if consec_salida else None,
                    'f350_id_tipo_docto_base': (self.tipo_docto_transito_salida or None) if consec_salida else None,
                    'f350_consec_docto_base': int(consec_salida) if consec_salida else 0,
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_transito_entrada,  # REQUIRED
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_transito,  # debe == f450_id_bodega_salida
                    'f470_id_ubicacion_aux': None,       # Dep — si bodega maneja ubicaciones
                    'f470_id_lote': None,                # Dep — si ítem maneja lotes
                    'f470_id_motivo': self.motivo_traslado,
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': None,        # Dep — si cuenta contable exige ccosto
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': item.get('unidad_medida') or self.uom_default,
                    'f470_cant_base': round(float(abs(item.get('cantidad', 0))), 4),
                    'f470_cant_2': None,                 # Dep — unidad adicional
                    'f470_costo_prom_uni': None,          # Dep
                    'f470_notas': None,
                    'f470_desc_varible': None,           # typo intencional — nombre exacto del spec 173079
                    'f470_id_ubicacion_aux_ent': None,   # Dep — ubicación entrada
                    'f470_id_lote_ent': None,
                    'f470_id_item': None,                # Dep — usamos referencia_item
                    'f470_referencia_item': item.get('codigo_siesa'),
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': self.unidad_negocio,  # SIESA_UNIDAD_NEGOCIO — obligatorio
                    'f470_rowid_movto': 0,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}]
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
        for _item in items:
            if not _item.get('codigo_siesa'):
                raise ValueError(
                    f'Item sin codigo_siesa en transferencia_directa: {_item.get("codigo") or _item}. '
                    'Nunca usar código interno WMS como fallback hacia Siesa.'
                )
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': self.centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_traslado,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Transferencia directa {codigo_solicitud}',
                    'f350_id_tercero': self.nit_empresa or None,                     # obligatorio spec 173066 — mismo que 173076
                    'f450_id_bodega_salida': bodega_origen,
                    'f450_id_bodega_entrada': bodega_destino,
                    # f450_docto_alterno, f350_id_co_base, f350_id_tipo_docto_base,
                    # f350_consec_docto_base no existen en el spec 173066 — omitidos
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'f470_id_co': self.centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_traslado,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_origen,
                    'f470_id_ubicacion_aux': None,       # Dep — si bodega maneja ubicaciones
                    'f470_id_lote': None,                # Dep — si ítem maneja lotes
                    'f470_id_motivo': self.motivo_traslado,
                    'f470_id_co_movto': self.centro_op,
                    'f470_id_ccosto_movto': None,        # Dep — si cuenta contable exige ccosto
                    'f470_id_proyecto': None,             # No — opcional
                    'f470_id_unidad_medida': item.get('unidad_medida') or self.uom_default,
                    'f470_cant_base': round(float(abs(item.get('cantidad', 0))), 4),
                    'f470_cant_2': None,                 # Dep — si ítem maneja unidad adicional
                    'f470_costo_prom_uni': None,          # Dep — costo unitario
                    'f470_notas': None,
                    'f470_id_ubicacion_aux_ent': None,   # Dep — si bodega entrada maneja ubicaciones
                    'f470_id_lote_ent': None,             # Dep — si ítem+bodega entrada manejan lotes
                    'f470_id_item': None,                # Dep — usamos referencia_item
                    'f470_referencia_item': item.get('codigo_siesa'),
                    'f470_codigo_barras': None,           # Dep
                    'f470_id_ext1_detalle': None,        # Dep — si ítem maneja extensión 1
                    'f470_id_ext2_detalle': None,        # Dep — si ítem maneja extensión 2
                    'f470_id_un_movto': self.unidad_negocio,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}]
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
                'factura_remision': f'{self.conector_factura_remision} FacturaRemision',
                'entrada': f'{self.conector_entrada} EntradaOC',
                'ajuste': f'{self.conector_ajuste} DocumentoInv',
                'req_traslado': f'{self.conector_requisicion_traslado} RequisicionesParaTransferir',
                'transito_salida': f'{self.conector_transito_salida} TransferenciaEnTransitoSalida',
                'transito_entrada': f'{self.conector_transito_entrada} TransferenciaEnTransitoEntrada',
                'transf_directa': f'{self.conector_transferencia_directa} TransferenciaDirecta',
            },
            'despacho_config': {
                'tipo_docto_remision': self.tipo_docto_remision or 'NO CONFIGURADO — agregar SIESA_TIPO_DOCTO_REMISION en Railway',
                'tipo_docto_factura': self.tipo_docto_factura or 'NO CONFIGURADO',
                'cond_pago_ventas': self.cond_pago_ventas or 'NO CONFIGURADO — agregar SIESA_COND_PAGO_VENTAS en Railway',
                'lista_precio': self.lista_precio or 'NO CONFIGURADO',
                'motivo_ventas': self.motivo_ventas or 'NO CONFIGURADO',
                'listo_para_despacho': bool(self.tipo_docto_remision),
                'listo_para_factura': bool(self.cond_pago_ventas or True),
            },
            'traslados_config': {
                'tipo_docto_req_traslado': self.tipo_docto_req_traslado or 'NO CONFIGURADO',
                'tipo_docto_transito_salida': self.tipo_docto_transito_salida or 'NO CONFIGURADO',
                'tipo_docto_transito_entrada': self.tipo_docto_transito_entrada or 'NO CONFIGURADO',
                'req_solicitante': self.req_solicitante or 'NO CONFIGURADO',
                'bodega_transito': self.bodega_transito or 'NO CONFIGURADO',
                'motivo_traslado': self.motivo_traslado,
                'motivo_averia': self.motivo_averia,
                'unidad_negocio': self.unidad_negocio or 'NO CONFIGURADO (Siesa hereda de bodega)',
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
