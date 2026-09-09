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


class ConnektaPaginacionError(Exception):
    """Una página de una consulta paginada falló.

    Se levanta en vez de devolver lo que sí llegó: un inventario al que le
    faltan páginas **no se distingue de uno completo** — los productos ausentes
    se leen como inexistentes, no como desconocidos. Es la regla 0 aplicada a
    una consulta: ante dato ausente, declararlo, no rellenarlo con silencio.
    """


class CompromisosNoDisponibles(Exception):
    """No se pudo preguntar por los compromisos del pedido.

    **No es lo mismo que «no hay compromisos».** Y esa confusión costaba lo
    peor que puede pasar en este flujo:

    `get_compromisos_pedido` devolvía `[]` ante cualquier excepción —red caída,
    timeout, 429, Siesa fuera— y `DespachoParialService` lee la lista vacía
    como «la automatización de Siesa ya procesó el pedido completo». Con eso
    marcaba la tarea `DESPACHADO` y `siesa_triggered=True` **sin remisión y sin
    factura**.

    Mercancía saliendo del centro de distribución sin respaldo fiscal, en verde
    en el tablero. Y la guarda `if tarea.siesa_triggered` bloqueaba el
    reintento **para siempre**.

    Es la misma regla 0 de `ConnektaPaginacionError`, y el mismo criterio que
    `get_factura_desde_pedido` ya aplicaba en este archivo: ante dato ausente,
    declararlo, no rellenarlo con silencio.
    """


class ConnektaCircuitOpenError(Exception):
    """Raised when circuit breaker is OPEN — Siesa no disponible.
    DLQ handlers catch this to NOT waste retries."""
    pass


class ConnektaConsultaRechazada(Exception):
    """Siesa rechazó el filtro y contestó **HTTP 200**.

    Cuando no le gusta la consulta, Connekta no devuelve un código de error:
    devuelve una tabla con **una fila y una clave**::

        {"detalle": {"Table": [{"alerta": "Por favor verifique los
                                parámetros o filtros enviados en la petición."}]}}

    Para el código eso es tan válido como una factura. Y el WMS lo trataba de
    dos maneras, las dos malas:

    · `rows = [r for r in rows if 'alerta' not in r]` — la tira y sigue con
      `[]`. **«Tu consulta fue rechazada» se convierte en «no hay nada».**
    · `if len(rows) == 1 and 'alerta' in rows[0]: break` — corta la
      paginación y devuelve lo que haya, sin declarar que se cortó.

    Es la misma forma de `CompromisosNoDisponibles`, que ya costó mercancía
    saliendo del CD sin respaldo fiscal: un adaptador que **degrada hacia la
    respuesta buena**.

    Lleva el filtro en el mensaje a propósito: sin él hay que adivinar cuál de
    las veinte consultas del sistema fue la que rebotó.
    """


def _alerta_de(rows, filtro: str = None) -> str:
    """El texto de la alerta si la respuesta es un rechazo; `''` si son datos.

    **Reconocimiento estrecho, y es deliberado:** exactamente una fila, con
    exactamente una clave, que se llame `alerta`. Una consulta que devuelve un
    único registro de un solo campo es rara pero legítima —un `COUNT`, un
    maestro de una columna— y tratarla como error escondería datos buenos.
    Ensanchar esto cambia un modo de fallo ruidoso por uno silencioso.
    """
    if not isinstance(rows, list) or len(rows) != 1:
        return ''
    fila = rows[0]
    if not isinstance(fila, dict) or len(fila) != 1 or 'alerta' not in fila:
        return ''
    return str(fila['alerta'])


def _exigir_datos(rows, consulta: str, filtro: str = None):
    """Devuelve `rows`, o levanta si Siesa rechazó la consulta."""
    alerta = _alerta_de(rows)
    if alerta:
        raise ConnektaConsultaRechazada(
            f'Siesa rechazó la consulta {consulta}: {alerta}'
            + (f' — filtro: {filtro}' if filtro else ''))
    return rows


#: Lectura de un POST. **Medido: Siesa tarda entre 30 y 60 s** en procesar una
#: escritura (lecciones del Gestor de Cartera, 2026-08-19). Estaba en 30, así
#: que la mitad de las escrituras exitosas se reportaban como fallidas.
#: Gunicorn corta a los 120: no tiene sentido esperar más que él.
_POST_READ_TIMEOUT = 120


class ConnektaResultadoDesconocido(Exception):
    """El POST salió y **no sabemos si Siesa lo procesó**.

    No es un fallo. Es el tercer estado que faltaba, y confundirlo con
    «falló» es la forma exacta del incidente RC-00002744.

    Un timeout de lectura significa que la petición viajó y la respuesta no
    volvió a tiempo. Con Siesa tardando 30-60 s, **lo más probable es que el
    documento exista**. Tratarlo como fallo dispara las dos reacciones que
    duplican:

    · el pre-flag se revierte —«fallo explícito, no se creó nada»— y el DLQ
      reintenta con la guardia anti-duplicado ya abajo;
    · el mensaje manda al operario a reintentar a mano.

    La Regla 3 dice que un POST no reintenta ante timeout. Esta excepción es
    lo que hace que esa regla **se pueda cumplir**: sin un tipo propio, el
    timeout llega a los handlers como `Exception` y es indistinguible de un
    rechazo de Siesa, que sí se puede reintentar sin riesgo.

    Quien la reciba tiene exactamente dos salidas honestas: **verificar**
    contra Siesa si el documento entró (lo que hace `RECIBO_CAJA` con
    `cxc_cruce.esta_saldada`), o **declararlo y parar**. Nunca reintentar a
    ciegas, y nunca revertir el pre-flag.
    """


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
        # Liquidación de ruta — conectores financieros
        self.conector_recibo_caja     = os.getenv('CONNEKTA_CONECTOR_RECIBO_CAJA',     '142888')
        self.conector_nota_factura    = os.getenv('CONNEKTA_CONECTOR_NOTA_FACTURA',    '250696')
        # Nombre real del documento en Connekta — el estándar 142946
        # (API_v1_Ventas_Comercial_NotaFactura) rechazaba por tamaño de registro
        # (le faltaba f470_id_concepto y el formato de decimales de ancho fijo,
        # ver PapeleriaMedellin_NotaCredito_Desde_Factura_WMS/250696, clonado y
        # corregido vía el Asistente UnoEE de Generic Transfer, 2026-07-28).
        self.nombre_conector_nota_factura = os.getenv(
            'CONNEKTA_NOMBRE_CONECTOR_NOTA_FACTURA',
            'PapeleriaMedellin_NotaCredito_Desde_Factura_WMS',
        )
        # Crea la NC Y cruza la cartera contra la factura en un solo POST (a
        # diferencia de 250696, que no tiene sección Cuotas CxC) — construido
        # sobre Docto. ventas comercial v3 (no exige entidades dinámicas al
        # crear, a diferencia de v9/250878). Verificado en vivo 2026-07-31,
        # ver CLAUDE.md "Cruce de cartera SÍ se pudo automatizar". Motivo DIAN
        # y aprobación siguen manuales — ver Regla #21.
        self.conector_nota_credito_cruzar = os.getenv(
            'CONNEKTA_CONECTOR_NOTA_CREDITO_CRUZAR', '251126'
        )
        self.nombre_conector_nota_credito_cruzar = os.getenv(
            'CONNEKTA_NOMBRE_CONECTOR_NOTA_CREDITO_CRUZAR',
            'PapeleriaMedellin_NotaCredito_CrearCruzar_WMS_v2',
        )
        # Motivo DIAN sobre una NC YA creada — segundo POST, nunca el mismo que
        # crea (Entidades dinámicas exige que el documento exista de verdad,
        # con su consecutivo real: no acepta consec_docto=0 como "el de esta
        # misma transacción", a diferencia de Cuotas CxC y Movimientos).
        # Verificado en vivo 2026-08-03 contra NCE-00000057 — ver CLAUDE.md
        # "Motivo DIAN SÍ se pudo automatizar".
        self.conector_nc_motivo_dian = os.getenv(
            'CONNEKTA_CONECTOR_NC_MOTIVO_DIAN', '251546'
        )
        self.nombre_conector_nc_motivo_dian = os.getenv(
            'CONNEKTA_NOMBRE_CONECTOR_NC_MOTIVO_DIAN',
            'PapeleriaMedellin_NotaCredito_CrearCruzarDian_WMS',
        )
        # Consulta dinámica que devuelve el encabezado (t350_co_docto_contable)
        # para averiguar qué consecutivo asignó Siesa. **Sin default**: la
        # exploración del 2026-08-03 usó una consulta de SQL crudo que no es
        # apta para producción. Mientras esta variable esté vacía el motivo DIAN
        # sigue siendo manual, y `/api/health/siesa` lo dice con todas las
        # letras — un paso manual invisible es peor que uno declarado.
        self.consulta_nc_consecutivo = os.getenv('CONNEKTA_CONSULTA_NC_CONSECUTIVO', '')
        # Concepto DIAN de nota crédito (t741_mm_maestro_detalle, maestro
        # MUNOECO017): 1=Devolución parcial de bienes (genérico de devolución,
        # el que usa hoy contabilidad a mano), 2=Anulación de FE, 3=Rebaja o
        # descuento parcial, 4=Ajuste de precio, 5=Otros.
        self.concepto_dian_nc = os.getenv('SIESA_CONCEPTO_DIAN_NC', '1')
        self.conector_nota_directa    = os.getenv('CONNEKTA_CONECTOR_NOTA_DIRECTA',    '142903')
        self.conector_docto_contable  = os.getenv('CONNEKTA_CONECTOR_DOCTO_CONTABLE',  '142882')
        # Tipo documento nota crédito electrónica en Siesa
        self.tipo_docto_nota_credito  = os.getenv('SIESA_TIPO_DOCTO_NOTA_CREDITO', 'NCE')
        # Tipo documento recibo de caja en Siesa
        self.tipo_docto_recibo_caja   = os.getenv('SIESA_TIPO_DOCTO_RECIBO_CAJA', 'RC')
        # Tipo documento de causación en Siesa (retenciones)
        # Default 'NI' — no 'DC'. 'DC' nunca se verificó contra el maestro
        # real de Siesa (confirmado 2026-09-04: en este Siesa, 'DC' es
        # "Documento de Causación" de COMPRAS, sin ningún origen habilitado
        # del lado de Cuentas por Cobrar). 'NI' (Nota de legalización) es el
        # tipo ya probado en producción para este mismo conector (142882,
        # clase 30) por el proyecto hermano gestor-cartera-pame — ver su
        # CLAUDE.md, entrada RC1 (26-ago-2026): documento 004-NI-7 Aprobado,
        # con retención + base gravable, cruzando cartera real. Mismo Siesa
        # (F_CIA=1), mismo conector, mismo problema ("El tipo de documento no
        # está autorizado para moverse en la clase de importación") probado
        # antes con 'RC' y resuelto con 'NI'.
        self.tipo_docto_docto_contable = os.getenv('SIESA_TIPO_DOCTO_DOCTO_CONTABLE', 'NI')
        # Causal de devolución (f470_id_causal_devol, char(2)) — campo opcional
        # (nullable en el spec, no aparece en la lista de obligatorios que Siesa
        # exige en 'Movimientos'; el propio flujo manual de Siesa lo deja en
        # blanco al generar la NC — ver 'Datos por defecto para la devolución').
        # Se deja vacío salvo que el negocio confirme un código real y activo.
        self.causal_devolucion_default = os.getenv('SIESA_CAUSAL_DEVOLUCION', '')
        # --- 142888 ReciboCaja: campos requeridos por spec ---
        # Cobrador: código en Siesa (CxC → Maestros → Cobradores). "9876" = APP RECAUDO.
        self.cobrador_rc = os.getenv('SIESA_COBRADOR', '9876')
        # Flujo de efectivo: código en Siesa (Tesorería → Flujos de efectivo).
        self.flujo_efectivo_rc = os.getenv('SIESA_FLUJO_EFECTIVO', '1103')
        # Cuenta auxiliar CxC para cruces (CxC → Plan de cuentas). 13050501 = CxC comercial.
        self.cxc_auxiliar = os.getenv('SIESA_CXC_AUXILIAR', '13050501')
        # --- Ajuste al peso (faltante/sobrante entre lo cobrado y la factura) ---
        # Mismo par de cuentas que usa gestor-cartera-pame contra el mismo Siesa
        # (`recaudo/modelo.py::CUENTA_SOBRANTE/CUENTA_FALTANTE`) — procedimiento
        # manual de cartera, no una decisión nueva de este archivo.
        self.cuenta_ajuste_sobrante = os.getenv('SIESA_RC_CUENTA_SOBRANTE', '42958101')
        self.cuenta_ajuste_faltante = os.getenv('SIESA_RC_CUENTA_FALTANTE', '53959503')
        # Centro de costo de las cuentas de ajuste que SÍ lo manejan (familia 5xxxxx,
        # el faltante — el sobrante es cuenta de ingreso y no lo maneja). Medido por
        # gestor-cartera-pame el 25-ago-2026 contra el mismo Siesa: 165/165 movimientos
        # del último año en las seis sedes caen en '301' (GASTOS FINANCIEROS) — no es
        # un valor elegido acá, es el que contabilidad ya usa.
        self.ccosto_ajuste = os.getenv('SIESA_RC_CCOSTO_AJUSTE', '301')
        # Sucursal/UN del bloque "otros ingresos" del sobrante — decisión de Santiago
        # (gestor-cartera-pame, 20-ago-2026): U.N. '001' fija, CO el del recibo mismo.
        self.sucursal_ajuste = os.getenv('SIESA_RC_SUCURSAL_AJUSTE', '001')
        self.un_ajuste = os.getenv('SIESA_RC_UN_AJUSTE', '001')
        # Medios de pago: código Siesa (CxC → Maestros → Medios de pago)
        self.medio_pago_efectivo = os.getenv('SIESA_MEDIO_PAGO_EFECTIVO', 'EFE')
        self.medio_pago_transferencia = os.getenv('SIESA_MEDIO_PAGO_TRANSFERENCIA', 'TBA')
        self.medio_pago_tarjeta = os.getenv('SIESA_MEDIO_PAGO_TARJETA', 'TDC')
        # Mapa CO → Caja (Siesa: Tesorería → Cajas). Cada CO tiene su caja asignada.
        # Formato: JSON string o fallback a mapa hardcoded de SIESA_LEARNINGS.
        self._co_caja_map = {
            '001': '001', '002': '004', '003': '999', '004': '999',
            '005': '999', '006': '013', '007': '999', '008': '999', '009': '999',
        }
        _co_caja_override = os.getenv('SIESA_CO_CAJA_MAP', '')
        if _co_caja_override:
            try:
                import json
                self._co_caja_map.update(json.loads(_co_caja_override))
            except Exception:
                logger.warning('[CONNEKTA] SIESA_CO_CAJA_MAP no es JSON válido, usando mapa por defecto')
        # Mapa medio de pago WMS → código Siesa (para forma_pago del RecaudoEntrega)
        #
        # Alineado 1:1 con `MedioPago` de gestor-cartera-pame
        # (`dominio/recaudo/modelo.py`) — mismo Siesa, mismo maestro de medios
        # de pago (CxC → Maestros → Medios de pago), y el mismo cobrador de
        # ruta puede terminar pagando por cualquiera de estos bancos. Antes
        # WMS solo distinguía un "TRANSFERENCIA" genérico (siempre TBA,
        # Bancolombia Ahorros) sin importar el banco real — el gestor de
        # cartera ya resolvía esto por banco desde antes, y liquidación de
        # ruta es el mismo hecho económico con otro origen.
        #
        # `TRANSFERENCIA` y `CONSIGNACION` (los nombres viejos) se conservan
        # como sinónimos de la Bancolombia Ahorros genérica — retrocompatible
        # con cualquier RecaudoEntrega ya guardado con esos valores.
        self._forma_pago_map = {
            'EFECTIVO': self.medio_pago_efectivo,
            'TRANSFERENCIA_BANCOLOMBIA_AH': os.getenv('SIESA_MEDIO_PAGO_TBA', 'TBA'),
            'TRANSFERENCIA_BANCOLOMBIA_CTE': os.getenv('SIESA_MEDIO_PAGO_TBC', 'TBC'),
            'TRANSFERENCIA_BBVA': os.getenv('SIESA_MEDIO_PAGO_TBB', 'TBB'),
            'TRANSFERENCIA_BOGOTA': os.getenv('SIESA_MEDIO_PAGO_TBG', 'TBG'),
            'TRANSFERENCIA_AGRARIO_AH': os.getenv('SIESA_MEDIO_PAGO_TAA', 'TAA'),
            'TRANSFERENCIA_AGRARIO_CTE': os.getenv('SIESA_MEDIO_PAGO_TAC', 'TAC'),
            'TRANSFERENCIA_DAVIVIENDA': os.getenv('SIESA_MEDIO_PAGO_TDV', 'TDV'),
            # Cuenta `014` (Consignaciones), creada en Siesa el 26-ago-2026 —
            # ver el comentario del mismo medio en gestor-cartera-pame.
            'TRANSFERENCIA_IHO_CTE': os.getenv('SIESA_MEDIO_PAGO_TCI', 'TCI'),
            'TARJETA': self.medio_pago_tarjeta,
            # Sinónimos retrocompatibles — mismo valor que antes de este cambio.
            'TRANSFERENCIA': self.medio_pago_transferencia,
            'CONSIGNACION': self.medio_pago_transferencia,
        }
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
        # Consulta v2 (GET) para recovery de consecutivo — no el conector POST de arriba.
        # Verificado en vivo 2026-08-25 contra ST-20260706-21B0 (consec=19). Un solo
        # nombre, leído acá y en get_consec_entrada_transito_by_alterno — la primera
        # versión de este código lo repitió como string literal en dos archivos y una
        # corrección quedó aplicada a uno solo (Regla 0).
        self.consulta_transito_entrada = os.getenv(
            'CONNEKTA_CONSULTA_TRANSITO_ENTRADA',
            'API_v2_Inventarios_Transferencia_Transito_Entrada')
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
        # El código que la empresa usa para CONTADO. **No es un valor a emitir:
        # es el que hay que reconocer para no emitirlo.** Ver
        # `cond_pago.aprobable_en_ruta` — una FE de contado no se aprueba sin
        # recaudo, y en ruta el recaudo lo hace el conductor horas después.
        # Verificar en Siesa: Ventas → Condiciones de pago.
        self.cond_pago_ventas = os.getenv('SIESA_COND_PAGO_VENTAS', '')
        # La condición que lleva la FE de ruta cuando el pedido no trae ninguna.
        # Crédito a un día: la factura nace con CxC y el RC del conductor la
        # salda. Hasta el 2026-08-13 este hueco lo tapaba `cond_pago_ventas`
        # —el código de contado— y eso produce una factura que Siesa deja en
        # Elaboración con el inventario ya descargado.
        self.cond_pago_ruta = os.getenv('SIESA_COND_PAGO_RUTA', '')
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
        self.tipo_docto_ajuste = os.getenv('SIESA_TIPO_DOCTO_AJUSTE', 'ADI')
        self.tipo_docto_traslado = os.getenv('SIESA_TIPO_DOCTO_TRASLADO', 'TRA')
        # Sin default — SIESA_MOTIVO_TRASLADO es obligatorio en producción.
        # '01' era un fallback genérico que generaba rechazos en Siesas que usan otro código.
        self.motivo_traslado = os.getenv('SIESA_MOTIVO_TRASLADO', '')
        self.motivo_traslado_entrada = os.getenv('SIESA_MOTIVO_TRASLADO_ENTRADA', '02')
        # Motivo específico para transferencias a bodega de averías (142951).
        # El maestro "Conceptos y Motivos" de Siesa puede tener un código distinto al de traslados
        # normales. Verificar: Maestros Asociados → Conceptos y Motivos → código para averías.
        # Si no se configura, cae al motivo_traslado genérico (puede causar rechazo en Siesa).
        self.motivo_averia = os.getenv('SIESA_MOTIVO_AVERIA', '') or self.motivo_traslado
        # Motivos para ajuste físico ADI (Clase 63, Concepto 603) en PAME:
        # '01' = Entrada Ajuste (Sobrante), '02' = Salida Ajuste (Faltante).
        # Verificar en Siesa: Inventarios → Maestros → Conceptos y Motivos → Concepto 603.
        self.motivo_ajuste_entrada = os.getenv('SIESA_MOTIVO_AJUSTE_ENTRADA', '01')
        self.motivo_ajuste_salida  = os.getenv('SIESA_MOTIVO_AJUSTE_SALIDA',  '02')
        # Conceptos de movimiento en Siesa (Inventarios → Maestros → Conceptos y Motivos).
        # Los valores por defecto son los estándar de Siesa Enterprise; pueden variar por compañía.
        # Verificar en Siesa si los conceptos fueron renumerados antes de cambiar estos valores.
        self.concepto_ventas       = self._safe_int_env('SIESA_CONCEPTO_VENTAS',    501)
        self.concepto_compras      = self._safe_int_env('SIESA_CONCEPTO_COMPRAS',  401)
        self.concepto_ajustes      = self._safe_int_env('SIESA_CONCEPTO_AJUSTES',  603)
        self.concepto_traslados    = self._safe_int_env('SIESA_CONCEPTO_TRASLADOS', 607)

        _base = os.getenv('CONNEKTA_URL', 'https://serviciosqa.siesacloud.com').rstrip('/')
        #: El host base, expuesto. Era una variable local y quien lo
        #: necesitara tenía que releer `CONNEKTA_URL` con su propio default —
        #: dos defaults para un valor es cómo una consulta termina yendo a una
        #: compañía y la otra a otra (lecciones del Gestor, §7).
        self.base_url = _base
        self.id_sistema = os.getenv('CONNEKTA_ID_SISTEMA', '')
        self.url_get = f'{_base}/api/siesa/v3/ejecutarconsultaestandar'
        self.url_get_dinamico = f'{_base}/api/connekta/v3/ejecutarconsulta'
        self.url_post = f'{_base}/api/siesa/v3/conectoresimportarestandar'
        self.url_post_dinamico = f'{_base}/api/siesa/v3.1/conectoresimportar'

        self.modo_simulacion = not all([self.ikey, self.itoken])

        # ── Circuit Breaker ──────────────────────────────────────────────
        # Detecta caída de Connekta/Siesa y entra en modo degradado automáticamente.
        # CLOSED (normal) → OPEN (5 fallos en 5 min) → HALF_OPEN (probe cada 60s) → CLOSED
        # Estado real en `ConnektaCircuitBreaker` (connekta_circuit_breaker.py) —
        # las propiedades _cb_*/_CB_* de abajo son compatibilidad hacia atrás,
        # ver ese módulo.
        from app.services.connekta_circuit_breaker import ConnektaCircuitBreaker

        self._circuit = ConnektaCircuitBreaker(
            failure_threshold=5, window_seconds=300, probe_interval=60)

        # ── Dominios extraídos (paso 3 de la deuda de tamaño) ─────────────
        # Reciben `self` como `core` — no duplican config, leen/escriben a
        # través de esta misma instancia. Ver connekta_compras_gateway.py.
        from app.services.connekta_compras_gateway import ConnektaComprasGateway
        from app.services.connekta_ajustes_gateway import ConnektaAjustesGateway
        from app.services.connekta_consultas_gateway import ConnektaConsultasGateway
        from app.services.connekta_traslados_gateway import ConnektaTrasladosGateway
        from app.services.connekta_facturacion_gateway import ConnektaFacturacionGateway
        from app.services.connekta_liquidacion_gateway import ConnektaLiquidacionGateway

        self._compras = ConnektaComprasGateway(self)
        self._ajustes = ConnektaAjustesGateway(self)
        self._consultas = ConnektaConsultasGateway(self)
        self._traslados = ConnektaTrasladosGateway(self)
        self._facturacion = ConnektaFacturacionGateway(self)
        self._liquidacion = ConnektaLiquidacionGateway(self)

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
            #
            # La lista NO se escribe acá. Estaba escrita acá —cuatro variables— y
            # `health.py` tenía otra de nueve, con solo cuatro en común. Diez
            # variables con guard de fallo duro no estaban en ninguna de las dos,
            # entre ellas `SIESA_TIPO_DOCTO_AJUSTE`: 93 jobs en FALLIDO y dos
            # meses sin que un solo conteo cíclico llegara a Siesa.
            from app.services.vars_criticas import problemas as _problemas_vars

            for _p in _problemas_vars():
                logger.critical(
                    '[CONNEKTA] %s [%s] — %s Rompe: %s',
                    _p['variable'], _p['estado'], _p['detalle'], _p['rompe'],
                )

    # ── Compatibilidad con el circuit breaker extraído ────────────────────
    # `siesa_job_service.py` lee `connekta._cb_state` directo, y la suite de
    # tests del circuit breaker muta estos atributos a mano para forzar
    # escenarios (`gw._cb_state = 'OPEN'`, `gw._CB_PROBE_INTERVAL = 0.1`...).
    # Estas propiedades delegan a `self._circuit` sin que ningún caller note
    # el cambio — ni siquiera los que escriben, no solo los que leen.

    @property
    def _cb_state(self):
        return self._circuit.state

    @_cb_state.setter
    def _cb_state(self, value):
        self._circuit.state = value

    @property
    def _cb_failures(self):
        return self._circuit.failures

    @_cb_failures.setter
    def _cb_failures(self, value):
        self._circuit.failures = value

    @property
    def _cb_opened_at(self):
        return self._circuit.opened_at

    @_cb_opened_at.setter
    def _cb_opened_at(self, value):
        self._circuit.opened_at = value

    @property
    def _cb_last_probe(self):
        return self._circuit.last_probe

    @_cb_last_probe.setter
    def _cb_last_probe(self, value):
        self._circuit.last_probe = value

    @property
    def _CB_FAILURE_THRESHOLD(self):
        return self._circuit.failure_threshold

    @_CB_FAILURE_THRESHOLD.setter
    def _CB_FAILURE_THRESHOLD(self, value):
        self._circuit.failure_threshold = value

    @property
    def _CB_WINDOW_SECONDS(self):
        return self._circuit.window_seconds

    @_CB_WINDOW_SECONDS.setter
    def _CB_WINDOW_SECONDS(self, value):
        self._circuit.window_seconds = value

    @property
    def _CB_PROBE_INTERVAL(self):
        return self._circuit.probe_interval

    @_CB_PROBE_INTERVAL.setter
    def _CB_PROBE_INTERVAL(self, value):
        self._circuit.probe_interval = value

    @staticmethod
    def _ahora_bogota() -> datetime:
        """LA fuente de la fecha para todo lo que va a Siesa.

        Una política, una función. Antes había un helper que devolvía el string
        formateado y **doce sitios que calculaban la fecha por su cuenta** con
        `datetime.utcnow()`. Los tres formatos que hacen falta —YYYYMMDD, ISO, y
        hoy+N días— salen ahora de acá, así que corregir la zona en un lugar los
        corrige a todos.
        """
        from app.utils.fecha import ahora_bogota

        return ahora_bogota()

    @staticmethod
    def _fecha_hoy_bogota() -> str:
        """Fecha actual en zona horaria Bogotá (UTC-5) formato YYYYMMDD.

        Siesa rechaza documentos con fecha futura o período contable cerrado, y
        `datetime.utcnow()` da la fecha de MAÑANA después de las 7 p.m. Colombia
        — que es justo cuando se cierra el despacho del día.

        **Existía desde el 2026-07-21 y se usaba en 4 de 16 sitios.** Los otros
        12 seguían con `utcnow()`, y la suite estaba en verde porque los tests
        verificaban ESTE MÉTODO en aislamiento, no que alguien lo llamara. Un
        test sobre la implementación y no sobre la propiedad.
        """
        from app.utils.fecha import fecha_hoy_bogota

        return fecha_hoy_bogota()

    @staticmethod
    def _fecha_iso_bogota() -> str:
        """YYYY-MM-DD en hora Colombia. Para los campos que llevan guiones
        (`f421_fecha_entrega`), que son la excepción y no la regla."""
        from app.utils.fecha import fecha_iso_bogota

        return fecha_iso_bogota()

    @staticmethod
    def _fecha_bogota_mas(dias: int) -> str:
        """Hoy + N días, en hora Colombia, formato YYYYMMDD.

        Para vencimientos de cartera. Sumar días sobre `utcnow()` arrastra el
        error del día base: un vencimiento a 30 días calculado a las 8 p.m.
        vencía un día antes de lo pactado.
        """
        from app.utils.fecha import fecha_bogota_mas

        return fecha_bogota_mas(dias)

    @staticmethod
    def _fmt_valor(v) -> str:
        """Formato DecimalConSigno requerido por Siesa: +000000000000000.0000 (21 chars).
        Spec: signo(1) + enteros(15) + punto(1) + decimales(4) = 21 chars exactos."""
        from app.utils.siesa_formato import fmt_valor

        return fmt_valor(v)

    @staticmethod
    def _fmt_decimal_sin_signo(v, enteros: int, decimales: int = 4) -> str:
        """Decimal sin signo de ancho fijo (ej. f470_cant_base/f462_cajas):
        enteros + punto + decimales, cero-rellenado. Encontrado en el Asistente
        UnoEE de Generic Transfer: estos campos NO se auto-rellenan como los
        de tipo Entero/FIJO — hay que mandarlos ya formateados al ancho exacto
        o el registro plano queda corto (Siesa lo rechaza por tamaño)."""
        from app.utils.siesa_formato import fmt_decimal_sin_signo

        return fmt_decimal_sin_signo(v, enteros, decimales)

    @staticmethod
    def _verificar_partida_doble_dc(payload: dict) -> None:
        """Débitos == créditos en el DocumentoContable (142882), medido sobre
        el payload que se va a mandar. Hasta que solo llevaba una retención
        esto cuadraba por construcción — el débito y el crédito salían del
        mismo monto, no había forma de descuadrarlo. Con el ajuste al peso
        como segundo concepto (entra por el débito, tiene que salir por el
        crédito de cartera) esa garantía deja de ser estructural.

        Revienta ACÁ, antes del POST — mismo patrón que gestor-cartera-pame
        (`retencion_payload.py::_cuadra_la_partida_doble`) contra el mismo
        conector: un descuadre de un peso lo rechaza Siesa después de 30 a 60
        segundos y con el documento a medio camino, no antes de intentarlo.
        """
        from app.utils.siesa_formato import verificar_partida_doble_dc

        verificar_partida_doble_dc(payload)

    # ── Circuit Breaker Methods ───────────────────────────────────────────────

    def _cb_record_failure(self):
        """Registra un fallo. Si alcanza el threshold, trip a OPEN."""
        self._circuit.record_failure(on_trip=self._cb_trip_alert)

    def _cb_record_success(self):
        """Registra un éxito. Si estamos en HALF_OPEN, cierra el circuit."""
        self._circuit.record_success()

    def _cb_consumir_permiso(self) -> bool:
        """Pide permiso para UNA llamada HTTP. **Consume estado.** Se llama
        EXACTAMENTE UNA VEZ por intento — ver el docstring completo en
        `ConnektaCircuitBreaker.consumir_permiso`."""
        return self._circuit.consumir_permiso()

    def _cb_trip_alert(self):
        """Alerta inmediata cuando el circuit se abre (CLOSED → OPEN)."""
        try:
            from app.models.siesa_job import SiesaJob
            from app.extensions import db as _db
            # Deduplicar: no crear otra alerta si ya hay una pendiente
            existente = SiesaJob.query.filter(
                SiesaJob.tipo == 'ALERTA_EMAIL',
                SiesaJob.estado.in_(['PENDIENTE', 'PROCESANDO']),
            ).filter(SiesaJob.payload.contains('CIRCUIT_BREAKER_OPEN')).first()
            if existente:
                return
            SiesaJob.encolar(
                'ALERTA_EMAIL',
                {
                    'tipo_alerta': 'CIRCUIT_BREAKER_OPEN',
                    'asunto': '[WMS ALERTA CRÍTICA] Siesa/Connekta no disponible — circuit breaker activado',
                    'cuerpo_html': (
                        '<h2>Circuit Breaker OPEN</h2>'
                        f'<p>Siesa no responde después de {self._CB_FAILURE_THRESHOLD} fallos '
                        f'consecutivos en {self._CB_WINDOW_SECONDS // 60} minutos.</p>'
                        '<p>El WMS sigue operando (picking, packing, recepción) pero los '
                        'jobs Siesa están PAUSADOS hasta que Connekta responda.</p>'
                        '<p>El sistema intentará reconectar automáticamente cada '
                        f'{self._CB_PROBE_INTERVAL} segundos.</p>'
                    ),
                    'cuerpo_texto': (
                        f'Circuit Breaker OPEN — {self._CB_FAILURE_THRESHOLD} fallos en '
                        f'{self._CB_WINDOW_SECONDS // 60} min. DLQ pausado. '
                        f'Probe cada {self._CB_PROBE_INTERVAL}s.'
                    ),
                },
            )
            _db.session.flush()
        except Exception as e:
            logger.error('[CONNEKTA CB] Error creando alerta de circuit breaker: %s', e)

    def circuit_state(self) -> dict:
        """Estado actual del circuit breaker para health check y dashboard."""
        return self._circuit.snapshot()

    @staticmethod
    def _safe_int_env(var_name: str, default: int) -> int:
        """Parse int env var safely — logs warning and falls back to default on bad value."""
        from app.utils.siesa_formato import safe_int_env

        return safe_int_env(var_name, default)

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
        """CO Siesa de una bodega. Delega en `services.bodegas.co_de_bodega`.

        Siesa exige CO(documento) == CO(bodega_salida) — errores 46089/46090.

        Antes esta función leía `almacenes` y, si no encontraba la fila, caía a
        `centro_op_traslado` (003). Para cualquier bodega que no fuera NB1 eso
        es el CO equivocado, y el documento se rechaza. Peor: los llamadores del
        ETS resolvían el CO de **destino** con un diccionario literal propio, así
        que un mismo payload 173079 podía traer el CO base resuelto por esta
        función y el CO de entrada resuelto por otra fuente que no coincidía.
        Una pregunta, dos políticas, dentro del mismo documento.
        """
        from app.services.bodegas import co_de_bodega
        return co_de_bodega(bodega_siesa_id, por_defecto=self.centro_op_traslado)

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
            'timestamp': ConnektaGateway._ahora_bogota().isoformat(),
            'mensaje': f'{operacion} simulado exitosamente',
            'payload': payload or {}
        }

    def _get(self, nombre_api: str, params_extra: dict = None, timeout: int = 30, url: str = None):
        if self.modo_simulacion:
            # En simulación no hay HTTP que proteger: pedir permiso acá gastaría
            # el probe sin salir a la red. El bloque que estaba antes de esta
            # línea ya venía condicionado a `not modo_simulacion`, así que solo
            # servía para consumir el permiso DOS veces.
            return self._simular(f'GET_{nombre_api}', params_extra)

        # Circuit breaker: una sola vez por intento. Ver `_cb_consumir_permiso`.
        if not self._cb_consumir_permiso():
            logger.warning('[CONNEKTA CB] GET %s bloqueado — circuit %s', nombre_api, self._cb_state)
            return None

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
            if r.status_code == 400:
                # `API_v2_Ventas_Facturas_DesdePedido` (confirmado en vivo
                # contra Siesa QA, 2026-09-04) reporta "sin resultados" como
                # HTTP 400 con codigo=1 — no como 200 + lista vacía. Es una
                # convención de esta API, no un error real: sin distinguirla
                # acá, cualquier consulta que legítimamente no encuentre nada
                # (ej. `get_factura_desde_pedido` en el caso normal — la
                # mayoría de pedidos no tienen FE todavía) se ve idéntica a un
                # fallo de red, y el guard fail-fast de FE duplicada aborta el
                # cierre siempre, no solo cuando de verdad hay un problema.
                try:
                    _body_400 = r.json()
                except ValueError:
                    _body_400 = {}
                _detalle_400 = str(_body_400.get('detalle') or '').lower()
                if _body_400.get('codigo') == 1 and 'no se encontraron registros' in _detalle_400:
                    logger.info(
                        f'[CONNEKTA] GET {nombre_api}: sin resultados '
                        '(400 "no encontrados" — vacío, no error)')
                    self._cb_record_success()
                    return {'codigo': 0, 'mensaje': 'Transacción Exitosa (sin resultados)',
                            'detalle': {'Table': []}}
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                _codigo = data.get('codigo')
                if _codigo is not None and _codigo != 0:
                    _msg = data.get('mensaje') or data.get('descripcion') or f'codigo={_codigo}'
                    logger.warning(f'[CONNEKTA] GET {nombre_api}: error interno Siesa — {_msg}')
                    raise Exception(f'Siesa retornó error interno (codigo={_codigo}): {_msg}')
            self._cb_record_success()
            return data
        except requests.exceptions.Timeout:
            self._cb_record_failure()
            raise Exception('Connekta no respondió — reintenta')
        except requests.exceptions.RequestException as e:
            self._cb_record_failure()
            logger.error(f'[CONNEKTA] GET {nombre_api}: {e}')
            raise Exception(f'Error consultando Siesa: {e}')

    def _post(self, id_conector: str, nombre_conector: str, payload: dict,
              url: str = None, extra_params: dict = None):
        # Si el singleton arrancó sin credenciales (Railway timing issue), reintenta leer del entorno.
        if self.modo_simulacion:
            _ikey_env  = os.getenv('CONNEKTA_IKEY', '')
            _itoken_env = os.getenv('CONNEKTA_ITOKEN', '')
            if _ikey_env and _itoken_env:
                self.ikey = _ikey_env
                self.itoken = _itoken_env
                self.modo_simulacion = False
                logger.warning('[CONNEKTA] Credenciales cargadas en diferido — modo producción activado')

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
                'timestamp': ConnektaGateway._ahora_bogota().isoformat()
            }

        # Circuit breaker: si OPEN, fail-fast sin HTTP
        if not self._cb_consumir_permiso():
            raise ConnektaCircuitOpenError(
                f'Circuit breaker OPEN — POST {id_conector} bloqueado. '
                f'Siesa no disponible desde {self._cb_opened_at}'
            )

        params = {
            'idCompania': self.id_compania,
            'idDocumento': id_conector,
            'nombreDocumento': nombre_conector
        }
        if extra_params:
            params.update(extra_params)

        # REGLA INQUEBRANTABLE: POST NUNCA reintenta en 5xx/timeout.
        # Un timeout no significa que la operación falló — puede haber sido
        # procesada por Siesa. Reintentar = duplicar (incidente RC-00002744).
        # Solo se reintenta en 429 (rate-limit = request NO procesado).
        # La DLQ maneja reintentos con pre-flag de idempotencia.
        logger.info(f'[CONNEKTA] POST → conector={id_conector} nombre={nombre_conector} url={url or self.url_post}')
        try:
            r = requests.post(
                url or self.url_post,
                headers=self.headers,
                params=params,
                json=payload,
                # connect=10s, read=120s. Estaba en 30s y **medido, un POST a
                # Siesa tarda entre 30 y 60** (lecciones del Gestor de
                # Cartera, 2026-08-19). Cortar a los 30 no evitaba nada: el
                # documento se seguía creando allá, y acá se reportaba fallo.
                # «Fallar rápido» sirve cuando fallar rápido es *cierto*.
                timeout=(10, _POST_READ_TIMEOUT),
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
            self._cb_record_success()
            return resp_json
        except requests.exceptions.Timeout:
            self._cb_record_failure()
            # **Un timeout no es un fallo: es no saber.** El POST pudo haberse
            # procesado —de hecho es lo más probable, porque Siesa tarda entre
            # 30 y 60s— y la respuesta no llegó a tiempo.
            #
            # El mensaje anterior decía «reintenta confirmar». La Regla 3
            # impide que el DLQ reintente, pero ese texto le entregaba el
            # duplicado a una persona, y para entonces el pre-flag ya se
            # revirtió (el `except` de la Regla 6 revierte ante cualquier
            # excepción, incluida ésta): la guardia anti-duplicado está abajo
            # justo cuando el operario hace lo que el sistema le pidió.
            # Es el incidente RC-00002744 por el camino que la Regla 3 no
            # cubre. Regla 0, aplicada al texto que lee un humano.
            logger.error(
                f'[CONNEKTA] POST {id_conector}: timeout a los '
                f'{_POST_READ_TIMEOUT}s — el documento PUEDE existir en Siesa')
            raise ConnektaResultadoDesconocido(
                f'Siesa no respondió en {_POST_READ_TIMEOUT}s. '
                'NO se sabe si el documento quedó creado — probablemente sí. '
                'Verificá en Siesa (Auditoría de documentos) antes de volver '
                'a intentar: reintentar a ciegas crea un segundo documento.')
        except requests.exceptions.RequestException as e:
            self._cb_record_failure()
            logger.error(f'[CONNEKTA] POST {id_conector}: {e}')
            raise Exception(f'Error inyectando en Siesa: {e}')

    # ==========================================
    # GETs
    # ==========================================

    def get_estado_pedido(self, tipo_docto: str, consec_docto) -> int | None:
        """Estado actual de un pedido en Siesa. Delegado — ver
        `ConnektaConsultasGateway.get_estado_pedido` (paso 5, sub-lote 2)."""
        return self._consultas.get_estado_pedido(tipo_docto, consec_docto)

    def get_factura_desde_pedido(self, tipo_docto: str, consec_docto) -> list:
        """Guard anti-duplicado de FE por pedido. Delegado — ver
        `ConnektaConsultasGateway.get_factura_desde_pedido`."""
        return self._consultas.get_factura_desde_pedido(tipo_docto, consec_docto)

    def get_factura_desde_remision(self, tipo_docto_rm: str, consec_rm) -> list:
        """Guard anti-duplicado de FE por remisión. Delegado — ver
        `ConnektaConsultasGateway.get_factura_desde_remision`."""
        return self._consultas.get_factura_desde_remision(tipo_docto_rm, consec_rm)

    def get_punto_envio_factura(self, f350_rowid) -> dict:
        """Consulta dinámica del punto de envío de una FE. Delegado — ver
        `ConnektaConsultasGateway.get_punto_envio_factura`."""
        return self._consultas.get_punto_envio_factura(f350_rowid)

    def get_detalle_factura(self, tipo_docto_rm: str, consec_rm,
                             consec_pedido=None) -> list:
        """Detalle completo de una FE para impresión. Delegado — ver
        `ConnektaConsultasGateway.get_detalle_factura`."""
        return self._consultas.get_detalle_factura(tipo_docto_rm, consec_rm,
                                                     consec_pedido=consec_pedido)

    def get_pedidos_aprobados(self, sin_filtros: bool = False):
        """Cola viva de picking. Delegado — ver
        `ConnektaConsultasGateway.get_pedidos_aprobados`."""
        return self._consultas.get_pedidos_aprobados(sin_filtros=sin_filtros)

    def get_ordenes_compra_aprobadas(self, sin_filtros: bool = False, consec: str = None):
        """Muelle de recepción ciega. Delegado — ver
        `ConnektaConsultasGateway.get_ordenes_compra_aprobadas`."""
        return self._consultas.get_ordenes_compra_aprobadas(sin_filtros=sin_filtros, consec=consec)

    def validar_tipo_proveedor(self, nit: str) -> dict:
        """Verifica tipo_proveedor vía OCs activas. Delegado — ver
        `ConnektaConsultasGateway.validar_tipo_proveedor`."""
        return self._consultas.validar_tipo_proveedor(nit)

    def get_inventario_fecha(self, item_codigo: str, bodega: str = None):
        """Existencia real para conteo cíclico. Delegado — ver
        `ConnektaConsultasGateway.get_inventario_fecha`."""
        return self._consultas.get_inventario_fecha(item_codigo, bodega=bodega)

    # `get_item_por_barras()` se borró el 2026-08-19: no tenía **ningún**
    # llamador. El escaneo resuelve el EAN contra el catálogo local
    # (`productos`), que alimenta el sync — no contra Siesa en vivo. El camino
    # inverso, `buscar_barras_por_referencia()`, sí se usa y queda abajo.

    def buscar_barras_por_referencia(self, referencia: str):
        """Códigos de barras de un ítem por referencia. Delegado — ver
        `ConnektaConsultasGateway.buscar_barras_por_referencia`."""
        return self._consultas.buscar_barras_por_referencia(referencia)

    def get_items_catalogo(self, pagina: int = 1):
        """Catálogo completo de productos Siesa. Delegado — ver
        `ConnektaConsultasGateway.get_items_catalogo`."""
        return self._consultas.get_items_catalogo(pagina=pagina)

    def buscar_item_por_referencia(self, referencia: str):
        """Consulta en vivo de un ítem por referencia exacta. Delegado — ver
        `ConnektaConsultasGateway.buscar_item_por_referencia`."""
        return self._consultas.buscar_item_por_referencia(referencia)

    def get_items_unidades_medida(self, pagina: int = 1):
        """Factores de conversión de empaques por ítem. Delegado — ver
        `ConnektaConsultasGateway.get_items_unidades_medida`."""
        return self._consultas.get_items_unidades_medida(pagina=pagina)

    def get_clasificacion_items(self, pagina: int = 1):
        """238920 — clasificación ABC por ítem. Delegado — ver
        `ConnektaConsultasGateway.get_clasificacion_items`."""
        return self._consultas.get_clasificacion_items(pagina=pagina)

    def get_monitor_facturas_raw(self, fecha: str = None, pagina: int = 1):
        """Consulta dinámica de monitor de facturas. Delegado — ver
        `ConnektaConsultasGateway.get_monitor_facturas_raw`."""
        return self._consultas.get_monitor_facturas_raw(fecha=fecha, pagina=pagina)

    def get_compromisos_pedido(self, tipo_docto: str, consec_docto, f430_rowid=None) -> list:
        """Líneas comprometidas pendientes de remisionar. Delegado — ver
        `ConnektaConsultasGateway.get_compromisos_pedido`."""
        return self._consultas.get_compromisos_pedido(tipo_docto, consec_docto, f430_rowid=f430_rowid)

    def get_remision_desde_pedido(self, tipo_docto_pedido: str, consec_docto_pedido) -> dict | None:
        """Fallback: la RM más reciente creada para un pedido. Delegado — ver
        `ConnektaConsultasGateway.get_remision_desde_pedido`."""
        return self._consultas.get_remision_desde_pedido(tipo_docto_pedido, consec_docto_pedido)

    def get_pedido_cabecera(self, tipo_docto: str, consec_docto) -> dict | None:
        """Cabecera del pedido para trigger_factura_desde_remision. Delegado
        — ver `ConnektaConsultasGateway.get_pedido_cabecera`."""
        return self._consultas.get_pedido_cabecera(tipo_docto, consec_docto)

    def get_compromisos_t405(self) -> dict:
        """Mapeo T405 {f431_rowid: f405_rowid} (código no invocado en
        producción). Delegado — ver `ConnektaConsultasGateway.get_compromisos_t405`."""
        return self._consultas.get_compromisos_t405()

    def get_pedido_rowid_map(self, tipo_docto: str, consec_docto, f430_rowid=None) -> dict:
        """{referencia: f431_rowid} para las líneas del pedido. Delegado —
        ver `ConnektaConsultasGateway.get_pedido_rowid_map`."""
        return self._consultas.get_pedido_rowid_map(tipo_docto, consec_docto, f430_rowid=f430_rowid)

    def trigger_comprometer_pedido(self, consec_docto: str, compromisos: list) -> dict:
        """244328 — comprometer pedido antes de despacho. Delegado — ver
        `ConnektaFacturacionGateway.trigger_comprometer_pedido`."""
        return self._facturacion.trigger_comprometer_pedido(consec_docto, compromisos)

    def trigger_despacho(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                          items: list, url: str = None, extra_params: dict = None):
        """142945 — remisión desde pedido. Delegado — ver
        `ConnektaFacturacionGateway.trigger_despacho`."""
        return self._facturacion.trigger_despacho(
            tipo_docto_pedido, consec_docto_pedido, items, url=url, extra_params=extra_params)

    def trigger_factura(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                        items: list):
        """238925 — factura electrónica directa desde pedido. Delegado — ver
        `ConnektaFacturacionGateway.trigger_factura`."""
        return self._facturacion.trigger_factura(tipo_docto_pedido, consec_docto_pedido, items)

    def trigger_factura_desde_remision(self, tipo_docto_rm: str, consec_rm: int,
                                        cabecera: dict):
        """142943 — factura electrónica desde una remisión existente. Delegado
        — ver `ConnektaFacturacionGateway.trigger_factura_desde_remision`."""
        return self._facturacion.trigger_factura_desde_remision(tipo_docto_rm, consec_rm, cabecera)

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

        Lógica movida a `ConnektaComprasGateway` (2026-09-09, paso 3 de la
        deuda de tamaño) — este método queda como delegado delgado.
        """
        return self._compras.confirmar_entrada_compras(
            id_co_oc, tipo_docto_oc, consec_docto_oc, items,
            es_parcial=es_parcial, proveedor_id=proveedor_id,
            sucursal_prov=sucursal_prov, tercero_comprador=tercero_comprador,
            moneda_docto=moneda_docto, moneda_conv=moneda_conv,
            moneda_local=moneda_local, tasa_conv=tasa_conv, tasa_local=tasa_local,
            num_docto_referencia=num_docto_referencia, cond_pago=cond_pago,
        )

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str,
                                  bodega: str = None, centro_op: str = None,
                                  item_id_siesa: str = None):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Ajuste físico tras conteo cíclico double-blind. Ver el docstring
        completo en `ConnektaAjustesGateway.enviar_ajuste_inventario` — este
        método queda como delegado delgado (paso 4 de la deuda de tamaño).
        """
        return self._ajustes.enviar_ajuste_inventario(
            motivo_codigo, item_codigo, cantidad, referencia,
            bodega=bodega, centro_op=centro_op, item_id_siesa=item_id_siesa,
        )

    def transferir_a_averias(self, item_codigo: str, cantidad: int, referencia: str = ''):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Traslado físico NB1 → AV1 por avería. Delegado delgado — ver
        `ConnektaAjustesGateway.transferir_a_averias`.
        """
        return self._ajustes.transferir_a_averias(item_codigo, cantidad, referencia)

    def get_bodegas_siesa(self):
        """API_v2_Bodegas (ID 2) — lista todas las bodegas configuradas en Siesa.
        Delegado a ConnektaConsultasGateway (paso 5 de la deuda de tamaño)."""
        return self._consultas.get_bodegas_siesa()

    def get_ubicaciones_siesa(self, bodega_id: str = None, pagina: int = 1):
        """API_v2_Ubicaciones (ID 43) — maestro de ubicaciones auxiliares de
        una bodega. Delegado — ver `ConnektaConsultasGateway.get_ubicaciones_siesa`."""
        return self._consultas.get_ubicaciones_siesa(bodega_id=bodega_id, pagina=pagina)

    # transferir_entre_ubicaciones() (conector 173066 para RESERVA→PICKING) se
    # retiró 2026-09-07: ambas ubicaciones son la misma bodega Siesa (NB1 no
    # tiene sub-bodegas para picking/reserva), así que no había ningún
    # documento real que declarar. Ver el comentario completo en
    # ConnektaConsultasGateway.get_ubicaciones_siesa.

    def get_stock_bodega(self, bodega_id: str):
        """API_v2_Inventarios_InvFecha — existencia real en una bodega.
        Delegado — ver `ConnektaConsultasGateway.get_stock_bodega`."""
        return self._consultas.get_stock_bodega(bodega_id)

    def _fetch_stock_pages(self, bodega_id: str):
        """Una pasada completa de paginación paralela. Delegado — ver
        `ConnektaConsultasGateway._fetch_stock_pages`."""
        return self._consultas._fetch_stock_pages(bodega_id)

    # ── Traslados entre bodegas ────────────────────────────────────────────────

    def crear_requisicion_traslado(self, bodega_origen: str, bodega_destino: str,
                                    items: list, codigo_solicitud: str):
        """174646 — RIT. Delegado — ver
        `ConnektaTrasladosGateway.crear_requisicion_traslado`."""
        return self._traslados.crear_requisicion_traslado(
            bodega_origen, bodega_destino, items, codigo_solicitud)

    def compromisos_desde_requisicion(self, consec_rit: int, bodega_origen: str,
                                      bodega_destino: str, items: list):
        """174720 — compromisos sobre una RIT existente. Delegado — ver
        `ConnektaTrasladosGateway.compromisos_desde_requisicion`."""
        return self._traslados.compromisos_desde_requisicion(
            consec_rit, bodega_origen, bodega_destino, items)

    def transferencia_transito_salida(self, bodega_origen: str, bodega_transito: str,
                                       items: list, codigo_solicitud: str,
                                       consec_requisicion: int = None,
                                       bodega_destino: str = None):
        """173076 — STS. Delegado — ver
        `ConnektaTrasladosGateway.transferencia_transito_salida`."""
        return self._traslados.transferencia_transito_salida(
            bodega_origen, bodega_transito, items, codigo_solicitud,
            consec_requisicion=consec_requisicion, bodega_destino=bodega_destino)

    def transferencia_desde_requisicion(self, consec_rit: int) -> dict:
        """174930 — STS creado directo desde una RIT. Delegado — ver
        `ConnektaTrasladosGateway.transferencia_desde_requisicion`."""
        return self._traslados.transferencia_desde_requisicion(consec_rit)

    def transferencia_transito_entrada(self, bodega_transito: str, bodega_destino: str,
                                        items: list, codigo_solicitud: str,
                                        consec_salida: int = None,
                                        co_destino: str = None,
                                        bodega_origen: str = None):
        """173079 — ETS. Delegado — ver
        `ConnektaTrasladosGateway.transferencia_transito_entrada`."""
        return self._traslados.transferencia_transito_entrada(
            bodega_transito, bodega_destino, items, codigo_solicitud,
            consec_salida=consec_salida, co_destino=co_destino, bodega_origen=bodega_origen)

    def get_consec_salida_transito_by_alterno(self, codigo_solicitud: str) -> int | None:
        """Recovery del consecutivo del STS. Delegado — ver
        `ConnektaTrasladosGateway.get_consec_salida_transito_by_alterno`."""
        return self._traslados.get_consec_salida_transito_by_alterno(codigo_solicitud)

    def get_sts_info_by_alterno(self, codigo_solicitud: str) -> dict | None:
        """Consec + bodega_transito real del STS. Delegado — ver
        `ConnektaTrasladosGateway.get_sts_info_by_alterno`."""
        return self._traslados.get_sts_info_by_alterno(codigo_solicitud)

    def get_consec_entrada_transito_by_alterno(self, codigo_solicitud: str) -> int | None:
        """Recovery del consecutivo del ETS. Delegado — ver
        `ConnektaTrasladosGateway.get_consec_entrada_transito_by_alterno`."""
        return self._traslados.get_consec_entrada_transito_by_alterno(codigo_solicitud)

    def get_consec_rit_by_referencia(self, codigo_solicitud: str) -> int | None:
        """Recovery del consecutivo de la RIT. Delegado — ver
        `ConnektaTrasladosGateway.get_consec_rit_by_referencia`."""
        return self._traslados.get_consec_rit_by_referencia(codigo_solicitud)

    def transferencia_directa(self, bodega_origen: str, bodega_destino: str,
                               items: list, codigo_solicitud: str):
        """173066 — Plan B sin bodega de tránsito. Delegado — ver
        `ConnektaTrasladosGateway.transferencia_directa`."""
        return self._traslados.transferencia_directa(
            bodega_origen, bodega_destino, items, codigo_solicitud)

    # ==========================================
    # Estado
    # ==========================================

    @property
    def apunta_a_pruebas(self) -> bool:
        """El host de Connekta parece un ambiente de pruebas.

        Se mira `url_get_dinamico` porque es la que de verdad se usa para el
        kardex — el dato que después alimenta decisiones de compra.
        """
        from urllib.parse import urlparse
        host = urlparse(self.url_get_dinamico or '').netloc.lower()
        # `pru` y no solo `pruebas`: el backend real de Siesa QA se llama
        # `wspapeleriamedpru.siesacloud.com` — apareció en los errores del DLQ
        # el 2026-08-10 y `pruebas` no lo habría reconocido.
        #
        # Se amplía hacia la sobre-detección a propósito. Los dos errores no
        # cuestan lo mismo: un falso «datos de prueba» hace que alguien
        # verifique; un falso «producción» hace que confíe en números de QA
        # para decidir compras. Regla 0.
        return any(x in host for x in ('qa', 'test', 'dev', 'pru'))

    @property
    def host_siesa(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.url_get_dinamico or '').netloc

    def modo_datos(self) -> str:
        """`datos_de_prueba` | `ensayo` | `simulacion` | `produccion`.

        **LA función que contesta en qué ambiente estamos.** Había cuatro, y dos
        de ellas no miraban el host: el 2026-08-10 la pantalla de Siesa mostraba
        «PRODUCCIÓN · Listo para operar» en verde, con el banner rojo de «DATOS
        DE PRUEBA» arriba, en la misma vista. Las dos salían del mismo backend.

        El orden no es arbitrario: **el destino manda sobre el modo**. Tener
        credenciales reales y POSTs habilitados no es producción si los
        documentos aterrizan en el Siesa de pruebas. Un «PRODUCCIÓN» en verde
        sobre QA es exactamente la evidencia falsa que este proyecto existe para
        no producir.
        """
        import os as _os

        if self.apunta_a_pruebas:
            return 'datos_de_prueba'
        if (_os.environ.get('WMS_ENSAYO', '') or '').lower() == 'true':
            return 'ensayo'
        if self.modo_simulacion:
            return 'simulacion'
        if self.modo_ensayo:
            return 'ensayo'
        return 'produccion'

    def estado(self):
        return {
            # Lo primero que se lee, y lo que decide el color en pantalla.
            'modo_datos': self.modo_datos(),
            'apunta_a_pruebas': self.apunta_a_pruebas,
            'siesa_host': self.host_siesa or None,
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
        """Clientes activos con celular/teléfono/email (Consultas Dinámicas).
        Delegado — ver `ConnektaConsultasGateway.get_terceros_contacto`."""
        return self._consultas.get_terceros_contacto(nit=nit, pagina=pagina, tam_pagina=tam_pagina)

    def get_vendedor_contacto(self, codigo: str = None) -> list[dict]:
        """Nombre y teléfono real de cada vendedor. Delegado — ver
        `ConnektaConsultasGateway.get_vendedor_contacto`."""
        return self._consultas.get_vendedor_contacto(codigo=codigo)

    # ==========================================
    # Liquidación de ruta — conectores financieros
    # ==========================================

    def get_rowids_factura(self, tipo_docto_fe: str, consec_fe) -> list:
        """f470_rowid por línea de factura, para 142946. Delegado — ver
        `ConnektaConsultasGateway.get_rowids_factura`."""
        return self._consultas.get_rowids_factura(tipo_docto_fe, consec_fe)

    def _build_transportador_vacio(self) -> dict:
        """Bloque f462_* (transportador) vacío para NC. Delegado — ver
        `ConnektaLiquidacionGateway._build_transportador_vacio`."""
        return self._liquidacion._build_transportador_vacio()

    def _build_header_docto_ventas_nc(self, tipo_docto_fe: str, consec_fe,
                                       fecha: str) -> dict:
        """Campos base de 'Docto ventas comercial' para NC. Delegado — ver
        `ConnektaLiquidacionGateway._build_header_docto_ventas_nc`."""
        return self._liquidacion._build_header_docto_ventas_nc(tipo_docto_fe, consec_fe, fecha)

    def trigger_nota_factura(self, tipo_docto_fe: str, consec_fe,
                              lineas: list, notas: str = '') -> dict:
        """142946 — NC amarrada a factura, solo crea. Delegado — ver
        `ConnektaLiquidacionGateway.trigger_nota_factura`."""
        return self._liquidacion.trigger_nota_factura(tipo_docto_fe, consec_fe, lineas, notas=notas)

    def get_vencimiento_factura(self, tipo_docto_fe: str, consec_fe) -> str:
        """Saldo y fecha de vencimiento reales de la factura, para el cruce
        de 251126. Delegado — ver `ConnektaConsultasGateway.get_vencimiento_factura`."""
        return self._consultas.get_vencimiento_factura(tipo_docto_fe, consec_fe)

    def get_cxc_general(self, nit: str) -> list:
        """Cartera completa de un tercero. Delegado — ver
        `ConnektaConsultasGateway.get_cxc_general`."""
        return self._consultas.get_cxc_general(nit)

    def trigger_nota_factura_crear_cruzar(self, tipo_docto_fe: str, consec_fe,
                                           lineas: list, valor_cruce: float,
                                           notas: str = '') -> dict:
        """251126 — NC que crea y cruza cartera en el mismo POST. Delegado —
        ver `ConnektaLiquidacionGateway.trigger_nota_factura_crear_cruzar`."""
        return self._liquidacion.trigger_nota_factura_crear_cruzar(
            tipo_docto_fe, consec_fe, lineas, valor_cruce, notas=notas)

    @property
    def puede_fijar_motivo_dian(self) -> bool:
        """¿Está todo lo necesario para automatizar el motivo DIAN? Delegado
        — ver `ConnektaLiquidacionGateway.puede_fijar_motivo_dian`."""
        return self._liquidacion.puede_fijar_motivo_dian

    def _filas_nc_encabezado(self) -> list:
        """Filas recientes de NCE para este CO. Delegado — ver
        `ConnektaLiquidacionGateway._filas_nc_encabezado`."""
        return self._liquidacion._filas_nc_encabezado()

    def get_max_rowid_nc(self) -> int | None:
        """Mayor rowid de NCE antes de crear la nuestra. Delegado — ver
        `ConnektaLiquidacionGateway.get_max_rowid_nc`."""
        return self._liquidacion.get_max_rowid_nc()

    def get_consec_nc_creada(self, valor_cruce: float, fecha: str,
                             rowid_minimo: int | None = None) -> int:
        """Consecutivo real de la NC recién creada. Delegado — ver
        `ConnektaLiquidacionGateway.get_consec_nc_creada`."""
        return self._liquidacion.get_consec_nc_creada(valor_cruce, fecha, rowid_minimo=rowid_minimo)

    def trigger_motivo_dian_nc(self, consec_nc: int, concepto: str = '') -> dict:
        """251546 — motivo DIAN sobre una NC ya creada. Delegado — ver
        `ConnektaLiquidacionGateway.trigger_motivo_dian_nc`."""
        return self._liquidacion.trigger_motivo_dian_nc(consec_nc, concepto=concepto)

    def trigger_recibo_caja(self, tercero_nit: str, sucursal: str,
                             monto: float, forma_pago: str,
                             tipo_docto_fe: str, consec_fe,
                             co_factura: str = '',
                             cuenta_cxc: str = '',
                             unidad_negocio: str = '',
                             notas: str = '',
                             ajuste_valor: float = 0.0,
                             ajuste_es_sobrante: bool = False) -> dict:
        """142888 — cobro del conductor, cruza contra la factura. Delegado —
        ver `ConnektaLiquidacionGateway.trigger_recibo_caja`."""
        return self._liquidacion.trigger_recibo_caja(
            tercero_nit, sucursal, monto, forma_pago, tipo_docto_fe, consec_fe,
            co_factura=co_factura, cuenta_cxc=cuenta_cxc, unidad_negocio=unidad_negocio,
            notas=notas, ajuste_valor=ajuste_valor, ajuste_es_sobrante=ajuste_es_sobrante,
        )

    def trigger_documento_contable(self, tercero_nit: str, sucursal: str,
                                     cuenta_puc: str, monto: float,
                                     base_gravable: float,
                                     tipo_docto_fe: str, consec_fe,
                                     co_factura: str = '',
                                     cuenta_cxc: str = '',
                                     unidad_negocio: str = '',
                                     notas: str = '',
                                     ajuste_valor: float = 0.0,
                                     ajuste_razon: str = '') -> dict:
        """142882 — retenciones como documento contable, cruza contra la
        factura. Delegado — ver
        `ConnektaLiquidacionGateway.trigger_documento_contable`."""
        return self._liquidacion.trigger_documento_contable(
            tercero_nit, sucursal, cuenta_puc, monto, base_gravable,
            tipo_docto_fe, consec_fe, co_factura=co_factura, cuenta_cxc=cuenta_cxc,
            unidad_negocio=unidad_negocio, notas=notas, ajuste_valor=ajuste_valor,
            ajuste_razon=ajuste_razon,
        )


connekta = ConnektaGateway()
