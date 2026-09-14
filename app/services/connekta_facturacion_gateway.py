"""
Dominio Facturación/Despacho del gateway Connekta — extraído de
ConnektaGateway (2026-09-09, paso 7 de la deuda de tamaño; ver pasos 1-6:
app/utils/siesa_formato.py, connekta_circuit_breaker.py,
connekta_compras_gateway.py, connekta_ajustes_gateway.py,
connekta_consultas_gateway.py, connekta_traslados_gateway.py).

4 métodos: comprometer pedido (244328, prerequisito de despacho), despacho
(142945, remisión desde pedido), factura desde pedido (238925, FE directa)
y factura desde remisión (142943, FE sobre una RM existente — el flujo de
ruta con recaudo del conductor).

**No recibe config propia.** Mismo patrón que los pasos anteriores: toma la
instancia completa de `ConnektaGateway` como `core`. `ConnektaGateway`
conserva cada método como delegado delgado; el singleton `connekta` y sus
callers (`despacho_parcial_service.py`, `siesa_job_service.py`, rutas de
picking/despacho) no cambian. `core.get_estado_pedido` (ya delegado a
`ConnektaConsultasGateway`) se llama vía `core.get_estado_pedido(...)` —
sigue siendo un método público de `core`, no cambia por venir de otro
dominio.
"""
import logging

logger = logging.getLogger(__name__)


class ConnektaFacturacionGateway:

    def __init__(self, core):
        self._core = core

    def trigger_comprometer_pedido(self, consec_docto: str, compromisos: list) -> dict:
        """
        244328 → Compromiso_PididosV1
        Actualiza f405_cant_por_remisionar_base en T405 con las cantidades
        reales picadas por el operario.

        PREREQUISITO OBLIGATORIO antes de trigger_despacho() (142945).
        Reemplaza consulta 7811 + GRANT UPDATE — usa API oficial Siesa.

        Payload confirmado en Postman QA (2026-05-26, corregido 2026-07-23):
          · No lleva Inicial / Final
          · No lleva f430_id_tipo_docto en el body (Siesa lo infiere)
          · f431_nro_registro = f431_rowid (confirmado: 470418 = rowid real de T431)
          · f431_cant_base     = MISMO valor que f405_cant_por_remisionar_base, no la
            cantidad comprometida original (esa lectura de mayo era incorrecta — con
            cant_base = original, T431 nunca liberaba el compromiso y el pedido se
            quedaba en Comprometido pese a factura/remisión ok; caso real PD1320).
            Confirmado en Postman 2026-07-23: cant_base = cant_por_remisionar en ambos
            campos (0 y 8, pedido de 2 líneas) → pedido pasó a Cumplido correctamente.
          · f405_cant_por_remisionar_base = cant. REAL a despachar (WMS)

        compromisos: [{
            'referencia_item':     str,    # f431_referencia_item (codigo_siesa)
            'cant_base':           float,  # = cant_por_remisionar (ver nota arriba)
            'nro_registro':        int,    # f431_rowid de la línea en T431
            'cant_por_remisionar': float,  # cant. REAL picada (a despachar)
            'lote':                str|None,
        }]
        """
        core = self._core
        if core.modo_simulacion:
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
                    'f431_id_bodega':                core.bodega,
                    'f431_id_ubicacion_aux':          None,
                    'f431_id_lote':                  c.get('lote') or None,
                    # UOM real de la línea (f405_id_unidad_medida del GET API_v2_Ventas_Pedidos_Compromisos).
                    # Fallback a uom_default ('UND') si el caller no la propagó (compatibilidad futura).
                    # Campo OBLIGATORIO en 244328 (spec: Si) — nunca debe quedar vacío.
                    'f431_id_unidad_medida':          c.get('uom') or core.uom_default,
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
        return core._post(
            '244328',
            'Compromiso_PididosV1',
            payload,
            url=core.url_post_dinamico,
            extra_params={'idSistema': core.id_sistema},
        )

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
        core = self._core
        if not core.modo_simulacion:
            if not core.tipo_docto_remision:
                raise ValueError(
                    'SIESA_TIPO_DOCTO_REMISION no está configurado. '
                    'Si se usa trigger_despacho, agrega la variable en Railway.'
                )
            if not core.motivo_ventas:
                raise ValueError(
                    'SIESA_ID_MOTIVO_VENTAS no está configurado. '
                    'Es obligatorio (pos 131, ancho 2) en connector 142945.'
                )
        # Siesa: fecha en formato YYYYMMDD (8 chars max)
        fecha_hoy = core._fecha_hoy_bogota()

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

        cia = int(core.id_cia_siesa)

        payload = {
            'Inicial': [
                {'F_CIA': cia}
            ],
            'Remision': [
                {
                    'F_CIA': cia,
                    'F_CONSEC_AUTO_REG': 1,
                    'F350_ID_CO': core.centro_op,
                    'F350_ID_TIPO_DOCTO': core.tipo_docto_remision,
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
                    'f470_id_co': core.centro_op,
                    'f470_id_tipo_docto': core.tipo_docto_remision,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': core.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': i.get('lote') or None,
                    'f470_id_concepto': core.concepto_ventas,         # 501 = Ventas (maestro Siesa), override: SIESA_CONCEPTO_VENTAS
                    'f470_id_motivo': core.motivo_ventas or None,     # SIESA_ID_MOTIVO_VENTAS (pos 131, ancho 2) — DEBE configurarse en Railway
                    'f470_ind_obsequio': 0,
                    'f470_id_co_movto': core.centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_lista_precio': core.lista_precio or None,  # SIESA_LISTA_PRECIO (pos 169, ancho 3)
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
                    'f470_id_un_movto': core.unidad_negocio,   # spec: unidad de negocio, no centro_op
                    'f470_id_causal_devol': None
                }
                for idx, i in enumerate(items)
            ],
            'Final': [
                {'F_CIA': cia}
            ]
        }

        logger.info(f'[CONNEKTA] Despacho {tipo_docto_pedido}{consec_docto_pedido}')
        return core._post(core.conector_despacho, 'API_v1_Ventas_Comercial_RemisionPedido', payload,
                          url=url, extra_params=extra_params)

    def trigger_factura(self, tipo_docto_pedido: str, consec_docto_pedido: str,
                        items: list):
        """
        238925 → FACTURA_DESDE_PEDIDO (conector dinámico v3.1)
        Genera factura electrónica (FE) directamente desde el pedido comprometido.
        Siesa toma los ítems del pedido original — no se envían líneas de detalle.
        La automatización 'Factura → Remisión' descarga el inventario automáticamente.
        """
        core = self._core
        # [48] Validar tipo_docto antes de enviar (solo en modo producción real)
        # Un valor vacío causaría rechazo silencioso en Siesa sin mensaje de error claro.
        if not core.modo_simulacion and (not tipo_docto_pedido or not str(tipo_docto_pedido).strip()):
            raise ValueError(
                'tipo_docto_pedido está vacío — configura SIESA_TIPO_DOCTO_FACTURA '
                'o verifica que el pedido tenga tipo de documento asignado'
            )
        # Pre-check idempotencia: si el pedido ya fue facturado (estado=4 Cumplido),
        # no reenviar POST — evita factura FE duplicada en retry de DLQ tras timeout.
        if not core.modo_simulacion:
            try:
                estado_pre = core.get_estado_pedido(tipo_docto_pedido, consec_docto_pedido)
                if estado_pre is not None and str(estado_pre) == '4':
                    logger.warning(
                        f'[CONNEKTA] trigger_factura: pedido {tipo_docto_pedido}{consec_docto_pedido} '
                        f'ya está Cumplido (estado=4) en Siesa — omitiendo POST para evitar duplicado'
                    )
                    return {'idempotente': True, 'mensaje': 'Pedido ya facturado en Siesa (estado=4)'}
            except Exception as _e:
                logger.warning(f'[CONNEKTA] Pre-check factura falló: {_e} — continuando con POST')

        fecha_hoy = core._fecha_hoy_bogota()
        consec_int = int(consec_docto_pedido) if str(consec_docto_pedido).isdigit() else consec_docto_pedido
        # Vencimiento a 30 días — Siesa usará condición de pago del pedido si la tiene
        fecha_vcto = core._fecha_bogota_mas(30)

        payload = {
            'Docto_ventas_comercial': [{
                'F_CIA': int(core.id_cia_siesa),
                'F_CONSEC_AUTO_REG': 1,
                'F350_ID_CO': core.centro_op,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F350_FECHA': fecha_hoy,
                'F350_IND_ESTADO': 1,
                'F430_ID_TIPO_DOCTO': tipo_docto_pedido or None,
                'F430_CONSEC_DOCTO': consec_int
            }],
            'Cuotas_CxC': [{
                'F_CIA': int(core.id_cia_siesa),
                'F350_ID_CO': core.centro_op,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_factura,
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
        return core._post(
            core.conector_factura, 'FACTURA_DESDE_PEDIDO', payload,
            url=core.url_post_dinamico,
            extra_params={'idSistema': core.id_sistema}
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
        core = self._core
        fecha_hoy = core._fecha_hoy_bogota()
        fecha_vcto = core._fecha_bogota_mas(30)
        cia = int(core.id_cia_siesa)

        # Nombres de campo verificados empíricamente contra JSON real de API_v2_Ventas_Pedidos
        # El procedimiento almacenado de Siesa usa aliases propios — NO son los nombres de tabla base.
        tercero      = cabecera.get('f200_id_pedido_fact') or ''
        sucursal     = cabecera.get('f461_id_sucursal_pedido_rem') or None  # None → Siesa hereda del maestro
        tipo_cli     = cabecera.get('f430_id_tipo_cli_fact') or None        # None → Siesa hereda del maestro
        from app.services import cond_pago as _cp
        _cond_pago_siesa = cabecera.get('f430_id_cond_pago')
        # El hueco se tapa con la condición de RUTA (crédito a un día), NO con
        # el código de contado. Ver `cond_pago.aprobable_en_ruta`: una FE de
        # contado queda en Elaboración —probado en producción el 2026-08-13—
        # y para entonces la remisión ya descargó el inventario.
        # **Una función, no un `or` acá.** Este mismo fallback vivía también en
        # la pantalla del conductor, con el resultado opuesto: la FE salía en
        # C02 —que hay que cobrar— y la parada quedaba en LIBRE, sin pedir
        # cobro. Es el corolario de la Regla 0, y ya costó una vez.
        cond_pago    = _cp.cond_pago_efectiva(_cond_pago_siesa, core.cond_pago_ruta) or None
        if not cond_pago:
            raise ValueError(
                f'RM {tipo_docto_rm}-{consec_rm}: el pedido no trae f430_id_cond_pago y '
                'SIESA_COND_PAGO_RUTA no está configurado. No se emite la factura: '
                'Connekta V2 colapsa con HTTP 500 si el campo va en null, y caer al '
                'código de contado produce una FE que Siesa no aprueba. '
                'La RM ya está en BD — el reintento del DLQ va directo al 142943.'
            )
        # La lectura de la condición vive en `services/cond_pago.py` — misma
        # política que usa la pantalla del conductor y que cuenta el desglose.
        _clase = _cp.clasificar(_cond_pago_siesa, core.cond_pago_ventas)
        _tercero_alerta = cabecera.get('f200_id_pedido_fact') or 'desconocido'

        # Dos avisos distintos, y el segundo es el grave. Hasta el 2026-08-13
        # solo existía el primero, y decía «factura emitida como CONTADO» —
        # que era falso en los dos casos: con el fallback nuevo no sale de
        # contado, y cuando el pedido SÍ declara contado la factura no se
        # emite aprobada, se queda en Elaboración.
        _aviso = None
        if _clase == _cp.AUSENTE:
            logger.warning(
                '[CONNEKTA] RM %s-%s: f430_id_cond_pago vacío — se usa la condición '
                'de ruta %s. Maestro del cliente %s sin condición asignada.',
                tipo_docto_rm, consec_rm, cond_pago, _tercero_alerta
            )
            _aviso = (
                'DATA_MAESTRA_COND_PAGO',
                '[WMS ALERTA] Pedido sin condición de pago en el maestro de Siesa',
                f'El pedido de la remisión {tipo_docto_rm}-{consec_rm} (cliente '
                f'{_tercero_alerta}) no trae condición de pago: el maestro del '
                f'tercero en Siesa está incompleto.\n\n'
                f'La factura se emitió con la condición de ruta ({cond_pago}), que '
                f'es la correcta para este flujo — la cartera la salda el recibo de '
                f'caja del conductor. No hay que hacer nada con este documento.\n\n'
                f'Acción requerida: asignarle condición de pago al tercero '
                f'{_tercero_alerta} en Siesa Enterprise.'
            )
        elif not _cp.aprobable_en_ruta(_cond_pago_siesa, core.cond_pago_ventas):
            # El pedido declara CONTADO. Se emite igual —bloquear acá dejaría la
            # remisión hecha y el inventario descargado sin factura, que es peor—
            # pero se declara, porque el documento va a quedar en Elaboración y
            # hoy nadie se entera hasta que la liquidación no encuentra la CxC.
            logger.error(
                '[CONNEKTA] RM %s-%s: el pedido declara CONTADO (%s). La FE va a '
                'quedar en ELABORACIÓN — Siesa no aprueba una factura de contado '
                'sin recaudo. Cliente %s.',
                tipo_docto_rm, consec_rm, cond_pago, _tercero_alerta
            )
            _aviso = (
                'FE_CONTADO_NO_APROBABLE',
                '[WMS ALERTA] Factura de ruta emitida como CONTADO — no se va a aprobar',
                f'La remisión {tipo_docto_rm}-{consec_rm} (cliente {_tercero_alerta}) '
                f'viene de un pedido con condición de pago {cond_pago}, que es '
                f'CONTADO.\n\n'
                f'Siesa no aprueba una factura de contado sin el recaudo en el mismo '
                f'documento, y en ruta ese recaudo no existe todavía: lo hace el '
                f'conductor. Probado el 2026-08-13 con dos facturas ($263.963 y '
                f'$14.200), las dos quedaron en Elaboración con «el valor de la '
                f'cartera debe ser igual al valor de las CxC».\n\n'
                f'Consecuencia: el inventario ya salió (la remisión existe), la '
                f'factura queda sin aprobar y la liquidación no va a encontrar la '
                f'cuenta por cobrar contra la cual cruzar el recibo de caja.\n\n'
                f'Acción requerida: cambiar la condición de pago del pedido en Siesa '
                f'a la de ruta (crédito a un día) y volver a aprobar el documento. '
                f'Los pedidos de ruta no deben salir de contado.'
            )

        if _aviso:
            # Encolar alerta asincrona via DLQ — NO enviar email sync desde el hot
            # path de facturación (el POST HTTP a Resend tiene timeout 15s y bloquea
            # el worker; esta alerta no es operacionalmente urgente).
            try:
                from app.models.siesa_job import SiesaJob
                from app.extensions import db as _db_alert
                _tipo_alerta, _asunto, _cuerpo = _aviso
                SiesaJob.encolar(
                    'ALERTA_EMAIL',
                    {
                        'tipo_alerta': _tipo_alerta,
                        'asunto': _asunto,
                        'cuerpo_html': f'<pre>{_cuerpo}</pre>',
                        'cuerpo_texto': _cuerpo,
                        # Campos propios, no prosa. Hasta hoy el número de la
                        # remisión solo existía dentro del texto del correo, y
                        # sacarlo de ahí obliga a parsear una frase —
                        # exactamente lo que dejó el contador del desglose en
                        # cero cuando ese texto se reescribió.
                        'rm_tipo': tipo_docto_rm,
                        'rm_consec': str(consec_rm),
                        'tercero': _tercero_alerta,
                        'cond_pago_emitida': cond_pago,
                    },
                )
                # flush (no commit) — el job se persiste cuando el caller haga commit.
                # Un commit aquí flusheaba estado intermedio del DLQ handler si esta
                # función era invocada dentro de _ejecutar_job().
                _db_alert.session.flush()
            except Exception as _e_alert:
                logger.error('[CONNEKTA] Email alerta data maestra falló: %s', _e_alert)
        moneda_docto = cabecera.get('f430_id_moneda_docto') or 'COP'
        moneda_conv  = cabecera.get('f430_id_moneda_conv') or moneda_docto
        moneda_local = cabecera.get('f430_id_moneda_local') or moneda_docto
        tasa_conv    = float(cabecera.get('f430_tasa_conv') or 1)
        tasa_local   = float(cabecera.get('f430_tasa_local') or 1)
        vendedor     = cabecera.get('f200_id_pedido_vend') or None  # None → Siesa hereda del maestro
        punto_envio  = cabecera.get('f461_id_punto_envio') or core.punto_envio_default
        if not cabecera.get('f461_id_punto_envio'):
            _cabecera_keys = list(cabecera.keys()) if cabecera else []
            logger.warning(
                '[CONNEKTA] RM %s-%s: f461_id_punto_envio no devuelto por API_v2_Ventas_Pedidos. '
                'Fallback a SIESA_PUNTO_ENVIO_DEFAULT=%r. Keys disponibles en cabecera: %s',
                tipo_docto_rm, consec_rm, core.punto_envio_default, _cabecera_keys
            )
        if not punto_envio:
            raise ValueError(
                f'f461_id_punto_envio no disponible para RM {tipo_docto_rm}-{consec_rm}. '
                'API_v2_Ventas_Pedidos no devuelve el campo y SIESA_PUNTO_ENVIO_DEFAULT no está configurado. '
                'Verificar con consultor Siesa el código de punto de envío del cliente '
                f"(tercero={cabecera.get('f200_id_pedido_fact', 'desconocido')}) "
                'y configurar la variable en Railway.'
            )

        if not core.modo_simulacion and not tercero:
            raise ValueError(
                'get_pedido_cabecera no devolvió f200_id_pedido_fact — '
                'no se puede construir la FE sin el código de tercero cliente'
            )

        payload = {
            'Inicial': [{'F_CIA': cia}],
            'Doctoventascomercial': [{
                'F_CIA': cia,
                'F_CONSEC_AUTO_REG': 1,
                'F350_ID_CO': core.centro_op,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F350_FECHA': fecha_hoy,
                'F350_ID_TERCERO': tercero,
                'F350_IND_ESTADO': 1,
                'F350_IND_IMPRESION': 0,
                'f461_id_sucursal_fact': sucursal,
                'f461_id_tipo_cli_fact': tipo_cli,
                'f461_id_co_fact': core.centro_op,
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
                'F461_ID_UN_CXC': core.unidad_negocio,
                'F461_ID_CCOSTO_CXC': None,
                'f461_tasa_dscto_global_cap': None,
                'f461_valor_dscto_global_cap': None,
                'f461_num_docto_referencia': None,
            }],
            'RelacionDoctos': [{
                'F_CIA': cia,
                'F350_ID_CO': core.centro_op,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_factura,
                'F350_CONSEC_DOCTO': 0,
                'F460_ID_CO': core.centro_op,
                'F460_ID_TIPO_DOCTO': tipo_docto_rm,
                'F460_CONSEC_DOCTO': int(consec_rm),
            }],
            'CuotasCxC': [{
                'F_CIA': cia,
                'F350_ID_CO': core.centro_op,
                'F350_ID_TIPO_DOCTO': core.tipo_docto_factura,
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
        return core._post(
            core.conector_factura_remision,
            'API_v1_Ventas_Comercial_FacturaRemision',
            payload
        )


__all__ = ['ConnektaFacturacionGateway']
