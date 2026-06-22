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
        self.centro_op_traslado = os.getenv('SIESA_CO_TRASLADO') or self.centro_op

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
        self.nombre_conector_req_traslado  = os.getenv('CONNEKTA_NOMBRE_REQ_TRASLADO',
                                                        'API_v1_Inventarios_Comercial_RequisicionesParaTransferir')
        self.conector_transito_salida = os.getenv('CONNEKTA_CONECTOR_TRANSITO_SALIDA', '173076')
        self.nombre_conector_transito_salida = os.getenv(
            'CONNEKTA_NOMBRE_TRANSITO_SALIDA',
            'API_v1_Inventarios_Comercial_TransferenciaEnTransitoSalida')
        self.conector_transito_entrada = os.getenv('CONNEKTA_CONECTOR_TRANSITO_ENTRADA', '173079')
        self.nombre_conector_transito_entrada = os.getenv(
            'CONNEKTA_NOMBRE_TRANSITO_ENTRADA',
            'API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada')
        self.conector_transferencia_directa = os.getenv('CONNEKTA_CONECTOR_TRANSF_DIRECTA', '173066')
        # Tipo documento requisición de traslado (Siesa: clase 75 — distinto de clase 65 STS)
        # SIESA_TIPO_DOCTO_RIT toma precedencia; fallback a SIESA_TIPO_DOCTO_TRASLADO para
        # instancias que aún no hayan separado las variables.
        self.tipo_docto_req_traslado = (
            os.getenv('SIESA_TIPO_DOCTO_RIT') or os.getenv('SIESA_TIPO_DOCTO_TRASLADO', '')
        )
        # Tipo documento tránsito salida/entrada (verificar con consultor Siesa)
        self.tipo_docto_transito_salida = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_SALIDA', '')
        self.tipo_docto_transito_entrada = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_ENTRADA', '')
        # Datos de transporte para 173076/173079 — Siesa exige vehículo+transportador+conductor en STS/ETS
        self.vehiculo_traslado = os.getenv('SIESA_VEHICULO_TRASLADO', '')
        self.nit_transportador = os.getenv('SIESA_NIT_TRANSPORTADOR', '')
        self.sucursal_transportador = os.getenv('SIESA_SUCURSAL_TRANSPORTADOR', '001')
        self.nombre_conductor = os.getenv('SIESA_NOMBRE_CONDUCTOR', '') or None
        # Código del solicitante en requisiciones (Siesa: Inventarios → Solicitantes)
        self.req_solicitante = os.getenv('SIESA_REQ_SOLICITANTE', '')[:5]
        # Bodega de tránsito (verificar si existe en Siesa — si no, usar TransferenciaDirecta)
        self.bodega_transito = os.getenv('SIESA_BODEGA_TRANSITO', '')
        # En traslados Siesa NO hereda la UN de la bodega — debe enviarse explícita en f470_id_un_movto.
        # Sin default: solicitar a finanzas el código exacto y configurarlo en Railway.
        self.unidad_negocio = os.getenv('SIESA_UNIDAD_NEGOCIO', '') or None
        self.ubicacion_entrada_default = os.getenv('SIESA_UBICACION_ENTRADA_DEFAULT') or None
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
            if not self.lista_precio:
                _faltantes.append('SIESA_LISTA_PRECIO')
            if not self.unidad_negocio:
                _faltantes.append('SIESA_UNIDAD_NEGOCIO')
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

    def _co_de_bodega(self, bodega_siesa_id: str) -> str:
        """CO Siesa de una bodega desde el modelo Almacen.
        Siesa exige CO(documento) == CO(bodega_salida) — errores 46089/46090.
        Fallback a centro_op_traslado si no hay registro configurado."""
        if not bodega_siesa_id:
            return self.centro_op_traslado
        try:
            from app.models.almacen import Almacen
            alm = Almacen.query.filter_by(bodega_siesa_id=bodega_siesa_id).first()
            if alm and alm.centro_op_siesa:
                return alm.centro_op_siesa
        except Exception:
            pass
        return self.centro_op_traslado

    @staticmethod
    def _fmt_alterno(codigo: str) -> str:
        """Truncate to Siesa f450_docto_alterno max length (15 chars), keeping the tail for uniqueness."""
        s = codigo or ''
        return s[-15:] if len(s) > 15 else s

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

    def get_detalle_factura(self, tipo_docto_rm: str, consec_rm) -> list:
        """
        GET API_v2_Ventas_Facturas_DesdePedido — detalle completo de la FE para impresión.
        Retorna todas las líneas con precios, IVA y totales.
        Falla silenciosamente (retorna []) — uso exclusivo de display, nunca de anti-duplicado.
        Campos clave: f350_consec_docto, f350_id_tipo_docto, f350_fecha,
          f200_nit_fact, f120_referencia, f470_cant_base, f470_precio_uni,
          f470_vlr_imp, f461_vlr_neto.
        """
        if self.modo_simulacion:
            return []
        if not tipo_docto_rm or not str(tipo_docto_rm).strip():
            return []
        try:
            consec_int = int(consec_rm) if str(consec_rm).isdigit() else consec_rm
            res = self._get('API_v2_Ventas_Facturas_DesdePedido', {
                'paginacion': 'numPag=1|tamPag=100',
                'parametros': (
                    f"f350_id_co = ''{self.centro_op}'' "
                    f"AND f460_id_tipo_docto = ''{tipo_docto_rm}'' "
                    f"AND f460_consec_docto = {consec_int}"
                )
            })
            rows = res.get('detalle', {}).get('Table', [])
            return [r for r in rows if 'alerta' not in r]
        except Exception as e:
            logger.warning('[CONNEKTA] get_detalle_factura falló silenciosamente: %s', e)
            return []

    def get_punto_envio_factura(self, f350_rowid) -> dict:
        """
        Consulta dinámica papeleriamedellin_WMS_PuntoEnvio_FE.
        Retorna datos del punto de envío (T462): contacto, ciudad, dirección,
        barrio, teléfono, celular.

        REQUIERE crear esta consulta en Connekta QA con el SQL:
          SELECT TOP 1
            f462_contacto, f462_barrio, f462_direccion,
            f462_telefono, f462_celular,
            f202_descripcion AS ciudad
          FROM t462_cm_venta_docto_penv penv
          INNER JOIN t461_cm_venta_docto v461
            ON v461.f461_rowid = penv.f462_rowid_cm_venta_docto
          LEFT JOIN t202_co_municipio mun
            ON mun.f202_id_cia = penv.f462_id_cia
            AND mun.f202_id = penv.f462_id_ciudad
          WHERE v461.f461_rowid_co_docto_contable = @rowid
            AND penv.f462_id_cia = 1
        """
        if self.modo_simulacion or not f350_rowid:
            return {}
        try:
            # Pasar rowid como parámetro directo de URL (no en 'parametros') —
            # el endpoint ejecutarconsulta sustituye @rowid por el valor del param rowid.
            res = self._get(
                'papeleriamedellin_WMS_PuntoEnvio_FE',
                params_extra={
                    'paginacion': 'numPag=1|tamPag=5',
                    'rowid': int(f350_rowid),
                },
                url=self.url_get_dinamico,
            )
            rows = (
                res.get('detalle', {}).get('Datos') or
                res.get('detalle', {}).get('Table') or []
            )
            if rows:
                logger.info('[CONNEKTA] get_punto_envio_factura OK rowid=%s keys=%s',
                            f350_rowid, list(rows[0].keys()))
            return rows[0] if rows else {}
        except Exception as e:
            logger.warning('[CONNEKTA] get_punto_envio_factura falló silenciosamente: %s', e)
            return {}

    def get_detalle_factura(self, tipo_docto_rm: str, consec_rm,
                             consec_pedido=None) -> list:
        """
        GET API_v2_Ventas_Facturas_DesdePedido — detalle completo de la FE para impresión.
        Intento 1: filtra por RM (f460_id_tipo_docto / f460_consec_docto).
        Intento 2 (fallback): filtra por consec_pedido si intento 1 devuelve vacío.
        Falla silenciosamente: uso exclusivo de display, nunca de anti-duplicado.
        """
        if self.modo_simulacion:
            return []

        def _query(parametros: str) -> list:
            res = self._get('API_v2_Ventas_Facturas_DesdePedido', {
                'paginacion': 'numPag=1|tamPag=100',
                'parametros': parametros,
            })
            rows = res.get('detalle', {}).get('Table', [])
            rows = [r for r in rows if 'alerta' not in r]
            if rows:
                logger.info('[CONNEKTA] get_detalle_factura: %d filas, keys=%s',
                            len(rows), list(rows[0].keys()) if rows else [])
            return rows

        try:
            # Intento 1: filtrar por documento base (RM)
            if tipo_docto_rm and str(tipo_docto_rm).strip():
                consec_int = int(consec_rm) if str(consec_rm).isdigit() else consec_rm
                rows = _query(
                    f"f350_id_co = ''{self.centro_op}'' "
                    f"AND f460_id_tipo_docto = ''{tipo_docto_rm}'' "
                    f"AND f460_consec_docto = {consec_int}"
                )
                if rows:
                    return rows
                logger.info('[CONNEKTA] get_detalle_factura intento RM vacío — probando por pedido')

            # Intento 2: filtrar por pedido origen
            if consec_pedido:
                consec_ped_int = int(consec_pedido) if str(consec_pedido).isdigit() else consec_pedido
                rows = _query(
                    f"f350_id_co = ''{self.centro_op}'' "
                    f"AND f430_consec_docto = {consec_ped_int}"
                )
                if rows:
                    return rows
                logger.info('[CONNEKTA] get_detalle_factura intento pedido también vacío')

            return []
        except Exception as e:
            logger.warning('[CONNEKTA] get_detalle_factura falló silenciosamente: %s', e)
            return []

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

    def get_inventario_fecha(self, item_codigo: str, bodega: str = None):
        """API_v2_Inventarios_InvFecha — existencia real para conteo cíclico.
        bodega: código de bodega Siesa (ej 'NB1'). Si None usa self.bodega del env.
        Timeout reducido a 8s: es user-facing, no puede bloquear un worker Gunicorn.
        """
        _bodega = bodega or self.bodega
        return self._get(self.api_inventario, {
            'paginacion': 'numPag=1|tamPag=10',
            'parametros': f"f120_referencia = ''{item_codigo}'' AND f150_id = ''{_bodega}''"
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

    def get_compromisos_pedido(self, tipo_docto: str, consec_docto, f430_rowid=None) -> list:
        """
        GET API_v2_Ventas_Pedidos_Compromisos
        Retorna líneas comprometidas pendientes de remisionar (f405_cant_por_remisionar_base > 0).
        Filtra por f430_rowid cuando está disponible — único campo T430 en el response de la API.
        Campos clave: f120_referencia (SKU), f431_rowid (rowid T431), f405_cant_por_remisionar_base.
        """
        if self.modo_simulacion:
            return []
        if not tipo_docto or not str(tipo_docto).strip():
            return []
        try:
            if f430_rowid:
                parametros = f"f430_rowid = {int(f430_rowid)}"
            else:
                consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
                parametros = (
                    f"f430_id_co = ''{self.centro_op}'' "
                    f"AND f430_id_tipo_docto = ''{tipo_docto}'' "
                    f"AND f430_consec_docto = {consec_int}"
                )
            res = self._get('API_v2_Ventas_Pedidos_Compromisos', {
                'paginacion': 'numPag=1|tamPag=100',
                'parametros': parametros,
            })
            rows = res.get('detalle', {}).get('Table', [])
            rows = [r for r in rows if 'alerta' not in r]
            return [r for r in rows if float(r.get('f405_cant_por_remisionar_base') or 0) > 0]
        except Exception as e:
            logger.warning('[CONNEKTA] get_compromisos_pedido falló: %s', e)
            return []

    def get_remision_desde_pedido(self, tipo_docto_pedido: str, consec_docto_pedido) -> dict | None:
        """
        Consulta dinámica papeleriamedellin_WMS_Remision_DesdePedido.
        Busca en BD Siesa la RM más reciente creada para el pedido dado.
        Fallback cuando 142945 no devuelve el consecutivo en su response.
        Retorna {'tipo': 'RM', 'consec': 1234} o None si no existe.

        La query devuelve el MAX(consec_rm) por pedido de los últimos 30 días.
        Filtramos client-side por consec_pd para aislar el pedido específico.
        """
        if self.modo_simulacion:
            return None
        if not consec_docto_pedido:
            return None
        try:
            consec_int = int(str(consec_docto_pedido).strip())
            res = self._get(
                'papeleriamedellin_WMS_Remision_DesdePedido',
                params_extra={'paginacion': 'numPag=1|tamPag=200'},
                url=self.url_get_dinamico,
            )
            rows = res.get('detalle', {}).get('Datos', [])
            matches = [r for r in rows if r.get('consec_pd') == consec_int]
            if not matches:
                return None
            fila = max(matches, key=lambda r: int(r.get('consec_rm', 0)))
            return {
                'tipo':   str(fila.get('tipo_rm', 'RM')).strip(),
                'consec': int(fila['consec_rm']),
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

    def get_compromisos_t405(self) -> dict:
        """
        NOTA: Código no invocado en producción — get_pedido_rowid_map usa
        get_compromisos_pedido en su lugar. Conservado por si se necesita
        para reportes bulk. Paginación secuencial hasta 50 páginas.

        papeleriamedellin_compromisos_wms — mapeo {f431_rowid: f405_rowid}.

        T405 no expone claves naturales (co, consec, referencia) directamente.
        Usa f405_rowid_pv_movto (FK a t431_cm_pv_movto) como puente.
        Se combina con get_compromisos_pedido en get_pedido_rowid_map para
        producir {referencia: f405_rowid} por pedido.
        Pagina hasta agotar resultados — sin TOP arbitrario.
        """
        if self.modo_simulacion:
            return {}
        try:
            result = {}
            for pag in range(1, 50):
                res = self._get(
                    'papeleriamedellin_compromisos_wms',
                    params_extra={'paginacion': f'numPag={pag}|tamPag=100'},
                    url=self.url_get_dinamico,
                )
                rows = res.get('detalle', {}).get('Datos') or []
                if not rows:
                    break
                for r in rows:
                    if r.get('rowid_linea_pedido') and r.get('rowid_compromiso'):
                        result[int(r['rowid_linea_pedido'])] = int(r['rowid_compromiso'])
                if len(rows) < 100:
                    break
            logger.info('[CONNEKTA] get_compromisos_t405: %d compromisos activos', len(result))
            return result
        except Exception as e:
            logger.error('[CONNEKTA] get_compromisos_t405 falló: %s', e)
            raise

    def get_pedido_rowid_map(self, tipo_docto: str, consec_docto, f430_rowid=None) -> dict:
        """
        Devuelve {referencia: f431_rowid} para las líneas del pedido.
        f431_rowid es el ID único de la línea en T431 — valor que 142945 exige en
        f470_rowid_movto para que Siesa respete f470_cant_base (cantidad parcial del picking).
        """
        if self.modo_simulacion:
            return {}
        if not tipo_docto or not str(tipo_docto).strip():
            return {}
        try:
            compromisos = self.get_compromisos_pedido(tipo_docto, consec_docto, f430_rowid)
            result = {
                str(r.get('f120_referencia', '')).strip(): int(r['f431_rowid'])
                for r in compromisos
                if r.get('f431_rowid') and r.get('f120_referencia')
            }
            logger.info('[CONNEKTA] get_pedido_rowid_map %s%s: %s', tipo_docto, consec_docto, result)
            return result
        except Exception as e:
            logger.warning('[CONNEKTA] get_pedido_rowid_map falló: %s', e)
            return {}

    def trigger_comprometer_pedido(self, consec_docto: str, compromisos: list) -> dict:
        """
        244328 → Compromiso_PididosV1
        Actualiza f405_cant_por_remisionar_base en T405 con las cantidades
        reales picadas por el operario.

        PREREQUISITO OBLIGATORIO antes de trigger_despacho() (142945).
        Reemplaza consulta 7811 + GRANT UPDATE — usa API oficial Siesa.

        Payload confirmado en Postman QA (2026-05-26):
          · No lleva Inicial / Final
          · No lleva f430_id_tipo_docto en el body (Siesa lo infiere)
          · f431_nro_registro = f431_rowid (confirmado: 470418 = rowid real de T431)
          · f431_cant_base     = cant. comprometida original (Siesa)
          · f405_cant_por_remisionar_base = cant. REAL a despachar (WMS)

        compromisos: [{
            'referencia_item':     str,    # f431_referencia_item (codigo_siesa)
            'cant_base':           float,  # cant. comprometida original en Siesa
            'nro_registro':        int,    # f431_rowid de la línea en T431
            'cant_por_remisionar': float,  # cant. REAL picada (a despachar)
            'lote':                str|None,
        }]
        """
        if self.modo_simulacion:
            logger.info(
                '[CONNEKTA SIM] 244328 comprometer_pedido: consec=%s líneas=%d %s',
                consec_docto, len(compromisos),
                [(c['referencia_item'], c['cant_por_remisionar']) for c in compromisos],
            )
            return {'simulado': True}

        consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto

        payload = {
            'Compromisos': [
                {
                    'f430_consec_docto':             consec_int,
                    # Prioridad de identificación de ítem (igual que Postman QA 2026-05-26):
                    # 1. f431_id_item = ID numérico interno Siesa (PedidoSiesa.item_id_siesa).
                    #    El conector 244328 resuelve confiablemente por este campo.
                    # 2. f431_referencia_item = SKU texto — solo si no hay ID numérico.
                    'f431_id_item':                  int(c['id_item']) if c.get('id_item') else None,
                    'f431_referencia_item':           None if c.get('id_item') else c['referencia_item'],
                    'f431_codigo_barras':             None,
                    'f431_id_bodega':                self.bodega,
                    'f431_id_ubicacion_aux':          None,
                    'f431_id_lote':                  c.get('lote') or None,
                    # UOM real de la línea (f405_id_unidad_medida del GET API_v2_Ventas_Pedidos_Compromisos).
                    # Fallback a uom_default ('UND') si el caller no la propagó (compatibilidad futura).
                    # Campo OBLIGATORIO en 244328 (spec: Si) — nunca debe quedar vacío.
                    'f431_id_unidad_medida':          c.get('uom') or self.uom_default,
                    'f431_cant_base':                round(float(c['cant_base']), 4),
                    'f431_cant_2':                   None,
                    'f431_nro_registro':             int(c['nro_registro']),   # = f431_rowid
                    'f405_cant_por_remisionar_base': round(float(c['cant_por_remisionar']), 4),
                    'f405_cant_por_remisionar_2':    None,
                }
                for c in compromisos
            ]
        }

        logger.info(
            '[CONNEKTA] 244328 comprometer_pedido consec=%s — %d líneas: %s',
            consec_docto, len(compromisos),
            [(c['referencia_item'], c['cant_por_remisionar']) for c in compromisos],
        )
        return self._post(
            '244328',
            'Compromiso_PididosV1',
            payload,
            url=self.url_post_dinamico,
            extra_params={'idSistema': self.id_sistema},
        )

    # ==========================================
    # POSTs — Bodies oficiales desde Ver Guía
    # ==========================================

    def trigger_despacho(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                          items: list, url: str = None, extra_params: dict = None):
        """
        142945 → API_v1_Ventas_Comercial_RemisionPedido
        Genera remisión desde pedido — descarga inventario cuenta 14.

        url / extra_params opcionales: permiten llamar al conector por la URL dinámica v3.1
        (misma autorización que 244328) en vez de la URL estándar v3 que puede dar HTTP 401.
        Uso desde despacho_parcial_service:
          connekta.trigger_despacho(..., url=connekta.url_post_dinamico,
                                        extra_params={'idSistema': connekta.id_sistema})
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
        logger.info(
            '[CONNEKTA] 142945 f470_cant_base por ítem: %s',
            [(i['producto_codigo'], float(i.get('cantidad_empacada') or 0)) for i in items],
        )

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
                    'f470_rowid_movto': i.get('rowid_movto') or None,
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
        return self._post(self.conector_despacho, 'API_v1_Ventas_Comercial_RemisionPedido', payload,
                          url=url, extra_params=extra_params)

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
            # Encolar alerta asincrona via DLQ — NO enviar email sync desde el hot
            # path de facturación (el POST HTTP a Resend tiene timeout 15s y bloquea
            # el worker; esta alerta no es operacionalmente urgente).
            try:
                from app.models.siesa_job import SiesaJob
                from app.extensions import db as _db_alert
                _cuerpo = (
                    f'El pedido RM-{consec_rm} del cliente {_tercero_alerta} fue facturado '
                    f'automáticamente como CONTADO ({cond_pago}) porque Siesa no devolvió '
                    f'condición de pago (f430_id_cond_pago vacío).\n\n'
                    f'Acción requerida: actualizar el maestro del tercero {_tercero_alerta} '
                    f'en Siesa Enterprise con la condición de pago correcta para evitar '
                    f'futuras facturas incorrectas y posibles fricciones con el cliente.\n\n'
                    f'Documento: {tipo_docto_rm}-{consec_rm}'
                )
                SiesaJob.encolar(
                    'ALERTA_EMAIL',
                    {
                        'tipo_alerta': 'DATA_MAESTRA_COND_PAGO',
                        'asunto': '[WMS ALERTA] Factura emitida como CONTADO por data incompleta en Siesa',
                        'cuerpo_html': f'<pre>{_cuerpo}</pre>',
                        'cuerpo_texto': _cuerpo,
                    },
                )
                _db_alert.session.commit()
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
                    'f350_id_co': f'{int(id_co_oc or self.centro_op):03d}',            # CO del documento (usa CO de la OC para multi-bodega)
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
                    'f470_id_co': f'{int(id_co_oc or self.centro_op):03d}',
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
                    'f421_fecha_entrega': self._fmt_fecha_iso(i.get('fecha_entrega')) or fecha_hoy,
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
                                  cantidad: int, referencia: str,
                                  bodega: str = None, centro_op: str = None):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Ajuste físico tras conteo cíclico double-blind.
        AJ-ENT: sobrante. AJ-SAL: faltante. Cantidad siempre positiva.
        bodega: código bodega Siesa (ej 'NB1','NB2'). Si None usa self.bodega.
        centro_op: centro de operación Siesa. Si None usa self.centro_op.
        """
        if not self.tipo_docto_ajuste:
            raise ValueError(
                'SIESA_TIPO_DOCTO_AJUSTE no está configurado en variables de entorno. '
                'Agrega la variable en Railway con el código de tipo de documento de ajuste en Siesa.'
            )
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}')

        _bodega = bodega or self.bodega
        _centro_op = centro_op or self.centro_op

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
                    'f350_id_co': _centro_op,
                    'f350_id_tipo_docto': self.tipo_docto_ajuste,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': self.nit_empresa or None,
                    'f350_id_clase_docto': 63,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': referencia,
                    'f450_id_concepto': self.concepto_ajustes,
                    'f450_id_bodega_salida': _bodega if not es_entrada else None,
                    'f450_id_bodega_entrada': _bodega if es_entrada else None,
                    'f450_docto_alterno': None,
                    'f350_id_co_base': None,
                    'f350_id_tipo_docto_base': None,
                    'f350_consec_docto_base': 0,
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
                    'f462_notas': None
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': cia,
                    'f470_id_co': _centro_op,
                    'f470_id_tipo_docto': self.tipo_docto_ajuste,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': 1,
                    'f470_id_bodega': _bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_ubicación_aux': None,
                    'f470_id_lote': None,
                    'f470_id_concepto': self.concepto_ajustes,                       # 603 = Ajustes (spec 142951, obligatorio), override: SIESA_CONCEPTO_AJUSTES
                    'f470_id_motivo': siesa_motivo,
                    'f470_id_co_movto': _centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': self.uom_default,
                    'f470_cant_base': round(float(abs(cantidad)), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': '',
                    # Typo intencional: 'varible' — nombre exacto del spec 142951 (pos 487, 2000 chars).
                    # Si se escribe 'variable' (correcto), Connekta omite el campo y Siesa rechaza
                    # por tamaño de registro (692 vs 2692).
                    'f470_desc_varible': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': self.uom_default,
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_ubicación_aux_ent': None,
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
                    'f450_id_concepto': self.concepto_traslados,                       # env SIESA_CONCEPTO_TRASLADOS (spec 142951, obligatorio)
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
                    # Typo intencional: 'varible' — nombre exacto del spec 142951.
                    'f470_desc_varible': '',
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
                                      cantidad: int, nota: str = '',
                                      centro_op: str = None):
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
        _centro_op = centro_op or self.centro_op

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [{
                'F_CIA': int(self.id_cia_siesa),
                'F_CONSEC_AUTO_REG': 1,
                'f350_id_co': _centro_op,
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
                'f470_id_co': _centro_op,
                'f470_id_tipo_docto': tipo_docto,
                'f470_consec_docto': 0,
                'f470_nro_registro': 1,
                'f470_id_bodega': bodega_id,
                'f470_id_ubicacion_aux': ubicacion_origen,       # origen (ej. RES-01-A)
                'f470_id_lote': None,                            # Dep — si ítem maneja lotes
                'f470_id_motivo': self.motivo_traslado or '01',
                'f470_id_co_movto': _centro_op,
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

    # Límite de página de Connekta para API_v2_Inventarios_InvFecha.
    # tamPag=120+ devuelve alerta "registros exceden el permitido"; 100 es el máximo seguro.
    _CONNEKTA_MAX_TAM_PAG = 100
    # Páginas paralelas por bodega — 3 es seguro sin riesgo de 429 (backoff=5min).
    _STOCK_BATCH_SIZE = 3

    def get_stock_bodega(self, bodega_id: str):
        """API_v2_Inventarios_InvFecha — existencia real en una bodega específica.

        La API de Connekta usa paginación offset-based que no es determinística
        bajo requests concurrentes: filas se barajan entre páginas, causando que
        productos se dupliquen en una página y desaparezcan de otra.

        Estrategia: dos pasadas paralelas + merge por f120_id para maximizar
        cobertura. La probabilidad de que el mismo producto falte en AMBAS
        pasadas es despreciable (~0.01%)."""
        from concurrent.futures import ThreadPoolExecutor

        if self.modo_simulacion:
            return self._get(self.api_inventario, {
                'paginacion': 'numPag=1|tamPag=3',
                'parametros': f"f150_id = ''{bodega_id}'' AND f400_cant_existencia_1 > 0"
            })

        rows_pass1 = self._fetch_stock_pages(bodega_id)
        by_id = {r.get('f120_id'): r for r in rows_pass1}
        dupes = len(rows_pass1) - len(by_id)

        if dupes > 0:
            logger.warning(
                '[CONNEKTA] stock %s pass1: %d filas, %d duplicados por f120_id — '
                'paginación inestable, ejecutando segunda pasada',
                bodega_id, len(rows_pass1), dupes,
            )
            rows_pass2 = self._fetch_stock_pages(bodega_id)
            for r in rows_pass2:
                fid = r.get('f120_id')
                if fid not in by_id:
                    by_id[fid] = r
            logger.info(
                '[CONNEKTA] stock %s merge: %d únicos (pass1=%d, pass2=%d, nuevos=%d)',
                bodega_id, len(by_id), len(rows_pass1), len(rows_pass2),
                len(by_id) - (len(rows_pass1) - dupes),
            )
        else:
            logger.info('[CONNEKTA] stock %s: %d filas, 0 duplicados — paginación OK', bodega_id, len(by_id))

        return {'detalle': {'Table': list(by_id.values())}}

    def _fetch_stock_pages(self, bodega_id: str):
        """Una pasada completa de paginación paralela (batch=3)."""
        from concurrent.futures import ThreadPoolExecutor

        tam = self._CONNEKTA_MAX_TAM_PAG
        batch = self._STOCK_BATCH_SIZE
        all_rows = []
        pag = 1

        while pag <= 200:
            pages_in_batch = list(range(pag, min(pag + batch, 201)))

            def _fetch(p, _bod=bodega_id, _tam=tam):
                try:
                    return self._get(self.api_inventario, {
                        'paginacion': f'numPag={p}|tamPag={_tam}',
                        'parametros': f"f150_id = ''{_bod}'' AND f400_cant_existencia_1 > 0"
                    })
                except Exception:
                    return {'_error': True, 'detalle': {'Table': []}}

            with ThreadPoolExecutor(max_workers=batch) as ex:
                batch_results = list(ex.map(_fetch, pages_in_batch))

            done = False
            for res in batch_results:
                if res.get('_error'):
                    continue
                rows = res.get('detalle', {}).get('Table', [])
                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    done = True
                    break
                all_rows.extend(rows)
                if len(rows) < tam:
                    done = True
                    break

            if done:
                break
            pag += batch

        return all_rows

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
                    'f440_id_tercero': None,
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
                    'f441_id_concepto': 607,
                    'f441_id_motivo': self.motivo_traslado,
                    'f441_id_unidad_medida': item.get('unidad_medida') or self.uom_default,
                    'f441_cant_base': round(float(abs(item.get('cantidad', 0))), 4),
                    'f441_cant_2': 0,
                    'f441_fecha_entrega': fecha_hoy,
                    'f441_num_dias_entrega': 0,
                    'f441_id_co_movto': self.centro_op,
                    'f_campo': None,
                    'f441_id_ccosto_movto': None,
                    'f441_id_proyecto': None,
                    'f441_notas': None,
                    'f441_desc_varible': None,
                    'f441_id_un_movto': self.unidad_negocio,
                    'f441_precio_unitario': 0.0,
                    'f441_id_ubicacion_sal': None,
                    'f441_id_proy_etapa': None,
                    'f441_id_rubro_pof': None,
                    'f441_id_moneda_sug': None,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}]
        }

        logger.info(f'[CONNEKTA] Requisicion traslado {codigo_solicitud} '
                    f'{bodega_origen}→{bodega_destino} ({len(items)} items)')
        _es_estandar = self.conector_requisicion_traslado in ('174646',)
        return self._post(self.conector_requisicion_traslado,
                          self.nombre_conector_req_traslado, payload,
                          url=self.url_post if _es_estandar else self.url_post_dinamico,
                          extra_params=None if _es_estandar else {'idSistema': self.id_sistema})

    def compromisos_desde_requisicion(self, consec_rit: int, bodega_origen: str,
                                      bodega_destino: str, items: list):
        """
        174720 — Registra compromisos sobre una RIT existente con cantidades y
        ubicaciones reales del packing. Dispara después del segundo conteo (EN_PACKING).
        bodega_origen: bodega de salida real del traslado (f441_id_bodega).
        Cada item: {codigo_siesa, cantidad, unidad_medida, ubicacion_codigo, lote}
        """
        if not consec_rit:
            raise ValueError('compromisos_desde_requisicion: consec_rit obligatorio')
        if not self.tipo_docto_req_traslado:
            raise ValueError(
                'SIESA_TIPO_DOCTO_RIT no configurado — requerido en 174720 para f440_id_tipo_docto'
            )
        _bodega_sal = bodega_origen or self.bodega
        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Compromisos': [
                {
                    'F_CIA':                       int(self.id_cia_siesa),
                    'f440_id_co':                  self.centro_op,
                    'f440_id_tipo_docto':           self.tipo_docto_req_traslado,
                    'f440_consec_docto':            consec_rit,
                    'f441_id_item':                 0,
                    'f441_referencia_item':         item.get('codigo_siesa'),
                    'f441_codigo_barras':           None,
                    'f441_id_ext1_detalle':         None,
                    'f441_id_ext2_detalle':         None,
                    'f441_id_bodega':               _bodega_sal,
                    'f441_id_ubicacion_aux':        item.get('ubicacion_codigo') or None,
                    'f441_id_lote':                 item.get('lote') or None,
                    'f441_id_unidad_medida':        item.get('unidad_medida') or self.uom_default,
                    'f441_cant_base':               round(float(abs(item.get('cantidad', 0))), 4),
                    'f441_cant_2':                  0,
                    'f441_id_bodega_ent':           bodega_destino,
                    'f441_id_ubicacion_aux_ent':    None,
                    'f441_id_lote_ent':             None,
                    'f441_cant_por_remisionar_base': round(float(abs(item.get('cantidad_packing',
                                                         item.get('cantidad', 0)))), 4),
                    'f441_cant_por_remisionar_2':   0,
                    'f441_nro_registro':            idx + 1,
                }
                for idx, item in enumerate(items)
                if item.get('codigo_siesa') and item.get('cantidad', 0) > 0
            ],
            'Movimiento de Seriales': [],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}],
        }
        logger.info('[CONNEKTA] compromisos_desde_requisicion RIT=%s (%d ítems)',
                    consec_rit, len(payload['Compromisos']))
        return self._post('174720',
                          'API_v1_Inventarios_Comercial_CompromisosDesdeRequisicion', payload)

    def transferencia_transito_salida(self, bodega_origen: str, bodega_transito: str,
                                       items: list, codigo_solicitud: str,
                                       consec_requisicion: int = None,
                                       bodega_destino: str = None):
        """
        173076 → API_v1_Inventarios_Comercial_TransferenciaEnTransitoSalida
        Sale de bodega_origen → queda en bodega_transito (limbo contable).
        bodega_destino (NC1): se usa como f450_id_bodega_entrada; el tránsito es
        propiedad de la Clase 65, no requiere bodega física. El ETS espeja este
        campo para la validación 62485 y NC1 tiene stock físico (TRA1 no tiene).
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
            logger.info(
                '[CONNEKTA] STS item debug: codigo=%s cant=%s factor=%s uom_empaque=%s → uom=%s cant_base=%s',
                _item.get('codigo_siesa'),
                _item.get('cantidad'),
                _item.get('factor_empaque', 1),
                _item.get('unidad_empaque', ''),
                _item.get('unidad_empaque') or _item.get('unidad_medida') or 'UND',
                round(float(abs(_item.get('cantidad', 0))) / _item.get('factor_empaque', 1), 4)
                if _item.get('factor_empaque', 1) > 1
                else round(float(abs(_item.get('cantidad', 0))), 4),
            )
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        # CO del documento debe coincidir con el CO de la bodega de salida (46089).
        # _co_de_bodega resuelve desde Almacen.centro_op_siesa: NB1→003, NS1→001, etc.
        _co_sts = self._co_de_bodega(bodega_origen)

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': _co_sts,
                    'f350_id_tipo_docto': self.tipo_docto_transito_salida,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': self.nit_empresa or None,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Despacho {codigo_solicitud}',
                    'f450_id_bodega_salida': bodega_origen,
                    # NC1 (bodega_destino): el ETS espeja este valor para validación 62485.
                    # Usar TRA1 haría que el ETS falle con 46035 (TRA1 sin stock físico).
                    'f450_id_bodega_entrada': bodega_destino or bodega_transito,
                    'f450_docto_alterno': self._fmt_alterno(codigo_solicitud),
                    'f462_id_vehiculo': self.vehiculo_traslado or None,
                    'f462_id_vehículo': self.vehiculo_traslado or None,
                    'f462_id_tercero_transp': self.nit_transportador or None,
                    'f462_id_sucursal_transp': self.sucursal_transportador or None,
                    'f462_id_tercero_conductor': self.nit_transportador or None,
                    'f462_nombre_conductor': self.nombre_conductor,
                    'f462_identif_conductor': self.nit_transportador or None,
                    'f462_numero_guia': None,
                    'f462_cajas': 0,
                    'f462_peso': 0.0,
                    'f462_volumen': 0.0,
                    'f462_valor_seguros': 0.0,
                    'f462_notas': None,
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'f470_id_co': _co_sts,
                    'f470_id_tipo_docto': self.tipo_docto_transito_salida,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_origen,
                    'f470_id_ubicacion_aux': None,
                    # El template UnoEE tiene estos campos con acento (ó) — enviar ambas
                    # variantes para que Connekta v3.1 no los marque como "no enviados".
                    'f470_id_ubicación_aux': None,
                    'f470_id_lote': None,
                    'f470_id_motivo': self.motivo_traslado,
                    'f470_id_co_movto': _co_sts,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': item.get('unidad_empaque') or item.get('unidad_medida') or 'UND',
                    'f470_cant_base': round(float(abs(item.get('cantidad', 0))) / item.get('factor_empaque', 1), 4)
                        if item.get('factor_empaque', 1) > 1
                        else round(float(abs(item.get('cantidad', 0))), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': None,
                    # Typo intencional: 'varible' no 'variable' — nombre exacto del spec 173076.
                    # Si difiere, Connekta trunca el registro en pos 487 y Siesa rechaza.
                    'f470_desc_varible': None,
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_ubicación_aux_ent': None,
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
        # Conectores UnoEE (Tecnocedi_*, WMS_PAME_*) usan endpoint dinámico v3.1.
        # Conectores estándar (API_v1_*) usan endpoint estándar v3.
        _sts_din = not self.nombre_conector_transito_salida.startswith('API_v1_')
        return self._post(self.conector_transito_salida,
                          self.nombre_conector_transito_salida, payload,
                          url=self.url_post_dinamico if _sts_din else None,
                          extra_params={'idSistema': self.id_sistema} if _sts_din else None)

    def transferencia_desde_requisicion(self, consec_rit: int) -> dict:
        """
        174930 → API_v1_Inventarios_Comercial_TransferenciasDesdeRequisicion
        Crea el documento STS (clase 65) directamente desde la RIT existente.
        Siesa hereda bodegas e ítems del RIT — no requiere Movimientos ni datos de transporte.
        """
        if not self.tipo_docto_transito_salida:
            raise ValueError(
                'SIESA_TIPO_DOCTO_TRANSITO_SALIDA no configurado — requerido para crear STS'
            )
        if not self.tipo_docto_req_traslado:
            raise ValueError(
                'SIESA_TIPO_DOCTO_RIT no configurado — requerido como referencia en 174930'
            )
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': self.centro_op_traslado,
                    'f350_id_tipo_docto': self.tipo_docto_transito_salida,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f440_id_co_req_int': self.centro_op,
                    'f440_id_tipo_docto_req_int': self.tipo_docto_req_traslado,
                    'f440_consec_docto_req_int': int(consec_rit),
                }
            ],
            'Final': [{'F_CIA': int(self.id_cia_siesa)}],
        }
        logger.info(f'[CONNEKTA] TransferenciaDesdeRequisicion RIT={consec_rit} → STS')
        return self._post('174930',
                          'API_v1_Inventarios_Comercial_TransferenciasDesdeRequisicion', payload)

    def transferencia_transito_entrada(self, bodega_transito: str, bodega_destino: str,
                                        items: list, codigo_solicitud: str,
                                        consec_salida: int = None,
                                        co_destino: str = None,
                                        bodega_origen: str = None):
        """
        173079 → API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada
        Confirma llegada: bodega_transito → bodega_destino.

        ETS referencia el STS via f350_consec_docto_base. Siesa valida:
          f450_id_bodega_salida = bodega_origen (NS1) == STS.bodega_salida.
          f450_id_bodega_entrada = bodega_destino (NC1) — destino final; CO(NC1)==CO(doc).
            No se usa TRA1 aquí: TRA1 es bodega lógica con 0 stock físico y
            Siesa haría chequeo 46035 si la ponemos como bodega_entrada.
          f350_id_co = _co_ent (CO de NC1) para satisfacer CO(bodega_entrada)==CO(doc).
          f470_id_co = _co_ent — debe coincidir con f350_id_co.
          f470_id_bodega = bodega_origen (NS1) — liquida el saldo de tránsito.
          f470_ind_naturaleza = 1 (Entrada) — evita chequeo de stock salida.
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

        # CO del documento destino (bodega_entrada = NC1, NS1, etc.)
        _co_ent = co_destino or self.centro_op
        # CO del STS base: debe coincidir con el CO usado al crear el STS (bodega_origen).
        # Si NB1→003, si NS1→001. El ETS usa este valor en f350_id_co_base para el vínculo.
        _co_sts_base = self._co_de_bodega(bodega_origen) if bodega_origen else self.centro_op_traslado

        payload = {
            'Inicial': [{'F_CIA': int(self.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    # CO del documento == CO de bodega_entrada (NC1=002).
                    # Siesa también valida CO(bodega_entrada)==CO(doc).
                    'f350_id_co': _co_ent,
                    'f350_id_tipo_docto': self.tipo_docto_transito_entrada,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': self.nit_empresa or None,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Recepcion {codigo_solicitud}',
                    # Siesa valida que ETS.bodega_salida == STS.bodega_salida (NS1)
                    'f450_id_bodega_salida': bodega_origen or self.bodega,
                    # NC1: destino final. CO(NC1)==CO(doc)==_co_ent. Sin stock check.
                    'f450_id_bodega_entrada': bodega_destino,
                    'f450_docto_alterno': self._fmt_alterno(codigo_solicitud),
                    # Referencia obligatoria al doc 173076 de salida
                    'f350_id_co_base': _co_sts_base if consec_salida else None,
                    'f350_id_tipo_docto_base': (self.tipo_docto_transito_salida or None) if consec_salida else None,
                    'f350_consec_docto_base': int(consec_salida) if consec_salida else 0,
                    'f462_id_vehiculo': self.vehiculo_traslado or None,
                    'f462_id_vehículo': self.vehiculo_traslado or None,
                    'f462_id_tercero_transp': self.nit_transportador or None,
                    'f462_id_sucursal_transp': self.sucursal_transportador or None,
                    'f462_id_tercero_conductor': self.nit_transportador or None,
                    'f462_nombre_conductor': self.nombre_conductor,
                    'f462_identif_conductor': self.nit_transportador or None,
                    'f462_numero_guia': None,
                    'f462_cajas': 0,
                    'f462_peso': 0.0,
                    'f462_volumen': 0.0,
                    'f462_valor_seguros': 0.0,
                    'f462_notas': None,
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': int(self.id_cia_siesa),
                    # f470_id_co debe coincidir con f350_id_co (_co_ent=002).
                    'f470_id_co': _co_ent,
                    'f470_id_tipo_docto': self.tipo_docto_transito_entrada,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    # bodega_origen (NB1): liquida el saldo de tránsito del STS.
                    'f470_id_bodega': bodega_origen or self.bodega,
                    # 1 = Entrada. Sin esto Siesa defaultea a 2 (Salida) y valida
                    # stock como si fuera despacho → "sin cantidad disponible".
                    'f470_ind_naturaleza': 1,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_ubicación_aux': None,
                    'f470_id_lote': None,
                    'f470_id_motivo': self.motivo_traslado,
                    'f470_id_co_movto': _co_ent,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': item.get('unidad_empaque') or item.get('unidad_medida') or self.uom_default,
                    'f470_cant_base': round(float(abs(item.get('cantidad', 0))) / item.get('factor_empaque', 1), 4)
                        if item.get('factor_empaque', 1) > 1
                        else round(float(abs(item.get('cantidad', 0))), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': None,
                    # Typo intencional: 'varible' no 'variable' — nombre exacto del spec 173079
                    'f470_desc_varible': None,
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_ubicación_aux_ent': None,
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

        logger.info(f'[CONNEKTA] Tránsito entrada {codigo_solicitud} '
                    f'{bodega_transito}→{bodega_destino}')
        _ets_din = not self.nombre_conector_transito_entrada.startswith('API_v1_')
        return self._post(self.conector_transito_entrada,
                          self.nombre_conector_transito_entrada, payload,
                          url=self.url_post_dinamico if _ets_din else None,
                          extra_params={'idSistema': self.id_sistema} if _ets_din else None)

    def get_consec_salida_transito_by_alterno(self, codigo_solicitud: str) -> int | None:
        """
        Recovery: API_v2_Inventarios_Transferencia_Salida_Transito filtrada por f450_docto_alterno.
        Retorna f350_consec_docto del STS creado para este traslado.
        """
        info = self.get_sts_info_by_alterno(codigo_solicitud)
        return info.get('consec') if info else None

    def get_sts_info_by_alterno(self, codigo_solicitud: str) -> dict | None:
        """
        Consulta el STS por f450_docto_alterno y devuelve consec + bodega_transito real.
        Útil para diagnosticar/corregir mismatch entre bodega_transito_siesa en WMS
        y la bodega_entrada que Siesa asignó al STS.
        Retorna {'consec': int, 'bodega_transito': str} o None si no encuentra.
        """
        alterno = self._fmt_alterno(codigo_solicitud)
        if not alterno:
            return None
        try:
            res = self._get(
                'API_v2_Inventarios_Transferencia_Salida_Transito',
                params_extra={
                    'paginacion': 'numPag=1|tamPag=5',
                    # f450_docto_alterno es único por traslado — no filtrar por CO para
                    # soportar traslados desde distintas bodegas (NB1→003, NS1→001, etc.)
                    'parametros': f"f450_docto_alterno = ''{alterno}''",
                },
            )
            rows = (
                res.get('detalle', {}).get('Table') or
                res.get('detalle', {}).get('Datos') or []
            )
            if rows:
                row = rows[0]
                consec = row.get('f350_consec_docto')
                bodega_ent = (row.get('f150_id_bodega_entrada') or '').strip() or None
                logger.info(
                    '[CONNEKTA] STS %s: consec=%s bodega_transito=%s',
                    codigo_solicitud, consec, bodega_ent,
                )
                return {
                    'consec': int(consec) if consec else None,
                    'bodega_transito': bodega_ent,
                }
        except Exception as e:
            logger.warning('[CONNEKTA] get_sts_info_by_alterno(%s): %s',
                           codigo_solicitud, e)
        return None

    def get_consec_rit_by_referencia(self, codigo_solicitud: str) -> int | None:
        """
        Recovery: API_v2_Inventarios_RequisicionesParaTransferir filtrada por f440_referencia.
        Retorna f440_consec_docto de la RIT creada para este traslado.
        """
        try:
            res = self._get(
                'API_v2_Inventarios_RequisicionesParaTransferir',
                params_extra={
                    'paginacion': 'numPag=1|tamPag=5',
                    'parametros': (
                        f"f440_id_co = ''{self.centro_op}''"
                        f" AND f440_referencia = ''{codigo_solicitud}''"
                    ),
                },
            )
            rows = (
                res.get('detalle', {}).get('Table') or
                res.get('detalle', {}).get('Datos') or []
            )
            if rows:
                consec = rows[0].get('f440_consec_docto')
                return int(consec) if consec else None
        except Exception as e:
            logger.warning('[CONNEKTA] get_consec_rit_by_referencia(%s): %s',
                           codigo_solicitud, e)
        return None

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


    def get_terceros_contacto(self, nit: str = None, pagina: int = 1,
                              tam_pagina: int = 100) -> list[dict]:
        """
        API_custom_TercerosContacto (ID 8232) — Connekta Consultas Dinámicas.
        JOIN T200 × T015: devuelve clientes activos con celular, teléfono y email.
        Parámetros opcionales:
          nit       — filtra por NIT exacto
          pagina    — número de página (paginación Connekta)
          tam_pagina — registros por página (máx 100 recomendado)
        Retorna lista de dicts con: f200_id, f200_nit, f200_razon_social,
          f200_nombres, f200_apellido1, f200_apellido2,
          f015_celular, f015_telefono, f015_email
        """
        parametros_extra = f"t200.f200_nit = ''{nit}''" if nit else None
        try:
            res = self._get(
                'papeleriamedellin_API_custom_TercerosContacto',
                params_extra={
                    'paginacion': f'numPag={pagina}|tamPag={tam_pagina}',
                    **(({'parametros': parametros_extra}) if parametros_extra else {}),
                },
            )
            rows = (
                res.get('detalle', {}).get('Table') or
                res.get('detalle', {}).get('Datos') or []
            )
            logger.info('[CONNEKTA] get_terceros_contacto: %d registros (pag %d)', len(rows), pagina)
            return rows
        except Exception as e:
            logger.warning('[CONNEKTA] get_terceros_contacto(nit=%s): %s', nit, e)
            return []


connekta = ConnektaGateway()
