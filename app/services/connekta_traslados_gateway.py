"""
Dominio Traslados/RIT del gateway Connekta — extraído de ConnektaGateway
(2026-09-09, paso 6 de la deuda de tamaño; ver pasos 1-5: app/utils/
siesa_formato.py, connekta_circuit_breaker.py, connekta_compras_gateway.py,
connekta_ajustes_gateway.py, connekta_consultas_gateway.py).

10 métodos: RIT (174646/174720), STS (173076/174930), ETS (173079),
transferencia directa (173066), y los 4 recovery `get_consec_*_by_alterno`/
`get_consec_rit_by_referencia` que reconstruyen el consecutivo cuando Siesa
acepta el documento (HTTP 200) sin devolverlo parseable en la respuesta.

**No recibe config propia.** Mismo patrón que los pasos anteriores: toma la
instancia completa de `ConnektaGateway` como `core`. `ConnektaGateway`
conserva cada método como delegado delgado; el singleton `connekta` y sus
callers (`traslado_service.py`, `siesa_traslado_adapter.py`,
`routes/traslados.py`, `tests/test_10_traslados.py` — 76 tests) no cambian.

`_fmt_alterno` y `_co_de_bodega` (helpers compartidos) se quedan en
`ConnektaGateway` — se llaman vía `core._fmt_alterno(...)`/
`core._co_de_bodega(...)`. `get_consec_salida_transito_by_alterno` llama a
`self.get_sts_info_by_alterno` (mismo objeto, ambos migraron juntos, no
`core.X`).
"""
import logging

logger = logging.getLogger(__name__)


class ConnektaTrasladosGateway:

    def __init__(self, core):
        self._core = core

    def crear_requisicion_traslado(self, bodega_origen: str, bodega_destino: str,
                                    items: list, codigo_solicitud: str):
        """
        174646 → API_v1_Inventarios_Comercial_RequisicionesParaTransferir
        Compromete inventario en bodega_origen para traslado a bodega_destino.
        Usa f440_* para documentos y f441_* para movimientos (schema propio, distinto
        al f350_*/f470_* de los demás conectores de inventario).
        """
        core = self._core
        if not core.tipo_docto_req_traslado:
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
        fecha_hoy = core._fecha_hoy_bogota()

        payload = {
            'Inicial': [{'F_CIA': int(core.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(core.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f440_id_co': core.centro_op,
                    'f440_id_tipo_docto': core.tipo_docto_req_traslado,
                    'f440_consec_docto': 0,
                    'f440_fecha': fecha_hoy,
                    'f440_id_tercero': None,
                    'f440_id_solicitante': core.req_solicitante or None,  # [A12] None when empty — Siesa rejects ''
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
                    'F_CIA': int(core.id_cia_siesa),
                    'f441_id_co': core.centro_op,
                    'f441_id_tipo_docto': core.tipo_docto_req_traslado,
                    'f441_consec_docto': 0,
                    'f441_nro_registro': idx + 1,
                    'f441_id_item': 0,
                    'f441_referencia_item': item.get('codigo_siesa'),
                    'f441_codigo_barras': None,
                    'f441_id_ext1_detalle': None,
                    'f441_id_ext2_detalle': None,
                    'f441_id_bodega': bodega_origen,
                    'f441_id_concepto': 607,
                    'f441_id_motivo': core.motivo_traslado,
                    'f441_id_unidad_medida': item.get('unidad_medida') or core.uom_default,
                    'f441_cant_base': round(float(abs(item.get('cantidad', 0))), 4),
                    'f441_cant_2': 0,
                    'f441_fecha_entrega': fecha_hoy,
                    'f441_num_dias_entrega': 0,
                    'f441_id_co_movto': core.centro_op,
                    'f_campo': None,
                    'f441_id_ccosto_movto': None,
                    'f441_id_proyecto': None,
                    'f441_notas': None,
                    'f441_desc_varible': None,
                    'f441_id_un_movto': core.unidad_negocio,
                    'f441_precio_unitario': 0.0,
                    'f441_id_ubicacion_sal': None,
                    'f441_id_proy_etapa': None,
                    'f441_id_rubro_pof': None,
                    'f441_id_moneda_sug': None,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(core.id_cia_siesa)}]
        }

        logger.info(f'[CONNEKTA] Requisicion traslado {codigo_solicitud} '
                    f'{bodega_origen}→{bodega_destino} ({len(items)} items)')
        _es_estandar = core.conector_requisicion_traslado in ('174646',)
        return core._post(core.conector_requisicion_traslado,
                          core.nombre_conector_req_traslado, payload,
                          url=core.url_post if _es_estandar else core.url_post_dinamico,
                          extra_params=None if _es_estandar else {'idSistema': core.id_sistema})

    def compromisos_desde_requisicion(self, consec_rit: int, bodega_origen: str,
                                      bodega_destino: str, items: list):
        """
        174720 — Registra compromisos sobre una RIT existente con cantidades y
        ubicaciones reales del packing. Dispara después del segundo conteo (EN_PACKING).
        bodega_origen: bodega de salida real del traslado (f441_id_bodega).
        Cada item: {codigo_siesa, cantidad, unidad_medida, ubicacion_codigo, lote}
        """
        core = self._core
        if not consec_rit:
            raise ValueError('compromisos_desde_requisicion: consec_rit obligatorio')
        if not core.tipo_docto_req_traslado:
            raise ValueError(
                'SIESA_TIPO_DOCTO_RIT no configurado — requerido en 174720 para f440_id_tipo_docto'
            )
        _bodega_sal = bodega_origen or core.bodega
        payload = {
            'Inicial': [{'F_CIA': int(core.id_cia_siesa)}],
            'Compromisos': [
                {
                    'F_CIA':                       int(core.id_cia_siesa),
                    'f440_id_co':                  core.centro_op,
                    'f440_id_tipo_docto':           core.tipo_docto_req_traslado,
                    'f440_consec_docto':            consec_rit,
                    'f441_id_item':                 0,
                    'f441_referencia_item':         item.get('codigo_siesa'),
                    'f441_codigo_barras':           None,
                    'f441_id_ext1_detalle':         None,
                    'f441_id_ext2_detalle':         None,
                    'f441_id_bodega':               _bodega_sal,
                    'f441_id_ubicacion_aux':        item.get('ubicacion_codigo') or None,
                    'f441_id_lote':                 item.get('lote') or None,
                    'f441_id_unidad_medida':        item.get('unidad_medida') or core.uom_default,
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
            'Final': [{'F_CIA': int(core.id_cia_siesa)}],
        }
        logger.info('[CONNEKTA] compromisos_desde_requisicion RIT=%s (%d ítems)',
                    consec_rit, len(payload['Compromisos']))
        return core._post('174720',
                          'API_v1_Inventarios_Comercial_CompromisosDesdeRequisicion', payload)

    def transferencia_transito_salida(self, bodega_origen: str, bodega_transito: str,
                                       items: list, codigo_solicitud: str,
                                       consec_requisicion: int = None,
                                       bodega_destino: str = None):
        """
        173076 → API_v1_Inventarios_Comercial_TransferenciaEnTransitoSalida
        Conector estándar (v3) — mismo motor que el ETS 173079.
        f450_id_bodega_entrada = bodega_destino: validación 62485 del ETS
        exige que ambos coincidan.
        """
        core = self._core
        if not core.tipo_docto_transito_salida:
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
        fecha_hoy = core._fecha_hoy_bogota()
        # CO del documento debe coincidir con el CO de la bodega de salida (46089).
        # _co_de_bodega resuelve desde Almacen.centro_op_siesa: NB1→003, NS1→001, etc.
        _co_sts = core._co_de_bodega(bodega_origen)

        payload = {
            'Inicial': [{'F_CIA': int(core.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(core.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': _co_sts,
                    'f350_id_tipo_docto': core.tipo_docto_transito_salida,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': core.nit_empresa or None,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Despacho {codigo_solicitud}',
                    'f450_id_bodega_salida': bodega_origen,
                    'f450_id_bodega_entrada': bodega_destino or bodega_transito,
                    'f450_docto_alterno': core._fmt_alterno(codigo_solicitud),
                    'f462_id_vehiculo': core.vehiculo_traslado or None,
                    'f462_id_vehículo': core.vehiculo_traslado or None,
                    'f462_id_tercero_transp': core.nit_transportador or None,
                    'f462_id_sucursal_transp': core.sucursal_transportador or None,
                    'f462_id_tercero_conductor': core.nit_transportador or None,
                    'f462_nombre_conductor': core.nombre_conductor,
                    'f462_identif_conductor': core.nit_transportador or None,
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
                    'F_CIA': int(core.id_cia_siesa),
                    'f470_id_co': _co_sts,
                    'f470_id_tipo_docto': core.tipo_docto_transito_salida,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_origen,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': None,
                    'f470_id_motivo': core.motivo_traslado,
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
                    # DEBE ser '' (no None): None omite el campo y Siesa rechaza por tamaño de registro.
                    'f470_desc_varible': '',
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_lote_ent': None,
                    'f470_id_item': None,
                    'f470_referencia_item': item.get('codigo_siesa'),
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': core.unidad_negocio,  # SIESA_UNIDAD_NEGOCIO — obligatorio
                    'f470_rowid_movto': 0,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(core.id_cia_siesa)}]
        }

        logger.info(f'[CONNEKTA] Tránsito salida {codigo_solicitud} '
                    f'{bodega_origen}→{bodega_transito}')
        # Conectores UnoEE (Tecnocedi_*, WMS_PAME_*) usan endpoint dinámico v3.1.
        # Conectores estándar (API_v1_*) usan endpoint estándar v3.
        _sts_din = not core.nombre_conector_transito_salida.startswith('API_v1_')
        return core._post(core.conector_transito_salida,
                          core.nombre_conector_transito_salida, payload,
                          url=core.url_post_dinamico if _sts_din else None,
                          extra_params={'idSistema': core.id_sistema} if _sts_din else None)

    def transferencia_desde_requisicion(self, consec_rit: int) -> dict:
        """
        174930 → API_v1_Inventarios_Comercial_TransferenciasDesdeRequisicion
        Crea el documento STS (clase 65) directamente desde la RIT existente.
        Siesa hereda bodegas e ítems del RIT — no requiere Movimientos ni datos de transporte.
        """
        core = self._core
        if not core.tipo_docto_transito_salida:
            raise ValueError(
                'SIESA_TIPO_DOCTO_TRANSITO_SALIDA no configurado — requerido para crear STS'
            )
        if not core.tipo_docto_req_traslado:
            raise ValueError(
                'SIESA_TIPO_DOCTO_RIT no configurado — requerido como referencia en 174930'
            )
        fecha_hoy = core._fecha_hoy_bogota()
        payload = {
            'Inicial': [{'F_CIA': int(core.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(core.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': core.centro_op_traslado,
                    'f350_id_tipo_docto': core.tipo_docto_transito_salida,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f440_id_co_req_int': core.centro_op,
                    'f440_id_tipo_docto_req_int': core.tipo_docto_req_traslado,
                    'f440_consec_docto_req_int': int(consec_rit),
                }
            ],
            'Final': [{'F_CIA': int(core.id_cia_siesa)}],
        }
        logger.info(f'[CONNEKTA] TransferenciaDesdeRequisicion RIT={consec_rit} → STS')
        return core._post('174930',
                          'API_v1_Inventarios_Comercial_TransferenciasDesdeRequisicion', payload)

    def transferencia_transito_entrada(self, bodega_transito: str, bodega_destino: str,
                                        items: list, codigo_solicitud: str,
                                        consec_salida: int = None,
                                        co_destino: str = None,
                                        bodega_origen: str = None):
        """
        173079 → API_v1_Inventarios_Comercial_TransferenciaEnTransitoEntrada
        Confirma llegada: bodega_transito → bodega_destino.

        Conector estándar (v3) — único que ejecuta la lógica de liquidación
        de tránsito de Clase 66. El registro plano debe medir exactamente
        2700 bytes; todos los campos Dep van como None.

        f450_id_bodega_salida y f470_id_bodega DEBEN ser bodega_origen (la
        bodega real de origen del STS), NUNCA bodega_transito (TRA1, bodega
        lógica sin stock físico). Probado en vivo contra Siesa QA el
        2026-06-12 (commit e3b7d89): con bodega_transito, Siesa rechaza con
        62485 "bodega de salida diferente a la capturada en el STS" — TRA1
        no es el origen que el STS registró.

        ⚠️ Este valor SE REVIRTIÓ SOLO una vez sin que nadie lo pidiera: el
        commit 1344c7a ("feat: motor estadístico... Vigía CUSUM...", una
        funcionalidad sin relación alguna con traslados) pisó este bloque de
        vuelta a bodega_transito el 2026-07-24, y desde entonces NINGÚN ETS
        se completó (0/69 recepciones EN_TRANSITO en la auditoría del
        2026-08-25). Si esto vuelve a fallar con 62485, revisar PRIMERO si
        alguna migración de código reintrodujo bodega_transito acá antes de
        investigar cualquier otra causa.
        """
        core = self._core
        if not core.tipo_docto_transito_entrada:
            raise ValueError(
                'SIESA_TIPO_DOCTO_TRANSITO_ENTRADA no configurado — crear tipo doc '
                'amarrado a Clase 66 en Siesa (Inventarios → Tipos de documento)'
            )
        if not consec_salida:
            raise ValueError(
                'consec_salida obligatorio para 173079 — no se puede recibir tránsito '
                'sin el consecutivo del documento de salida (173076)'
            )
        if not core.tipo_docto_transito_salida:
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
        fecha_hoy = core._fecha_hoy_bogota()

        # CO del documento destino (bodega_entrada = NC1, NS1, etc.)
        _co_ent = co_destino or core.centro_op
        # CO del STS base: debe coincidir con el CO usado al crear el STS (bodega_origen).
        # Si NB1→003, si NS1→001. El ETS usa este valor en f350_id_co_base para el vínculo.
        _co_sts_base = core._co_de_bodega(bodega_origen) if bodega_origen else core.centro_op_traslado

        payload = {
            'Inicial': [{'F_CIA': int(core.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(core.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    # CO del documento == CO de bodega_entrada (NC1=002).
                    # Siesa también valida CO(bodega_entrada)==CO(doc).
                    'f350_id_co': _co_ent,
                    'f350_id_tipo_docto': core.tipo_docto_transito_entrada,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': core.nit_empresa or None,
                    'f350_id_clase_docto': 66,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Recepcion {codigo_solicitud}',
                    'f450_id_concepto': 605,
                    # bodega_origen, NUNCA bodega_transito (TRA1) — ver docstring.
                    'f450_id_bodega_salida': bodega_origen or core.bodega,
                    # NC1: destino final. CO(NC1)==CO(doc)==_co_ent. Sin stock check.
                    'f450_id_bodega_entrada': bodega_destino,
                    'f450_docto_alterno': core._fmt_alterno(codigo_solicitud),
                    # Referencia obligatoria al doc 173076 de salida
                    'f350_id_co_base': _co_sts_base if consec_salida else None,
                    'f350_id_tipo_docto_base': (core.tipo_docto_transito_salida or None) if consec_salida else None,
                    'f350_consec_docto_base': int(consec_salida) if consec_salida else 0,
                    'f462_id_vehiculo': core.vehiculo_traslado or None,
                    'f462_id_tercero_transp': core.nit_transportador or None,
                    'f462_id_sucursal_transp': core.sucursal_transportador or None,
                    'f462_id_tercero_conductor': core.nit_transportador or None,
                    'f462_nombre_conductor': core.nombre_conductor,
                    'f462_identif_conductor': core.nit_transportador or None,
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
                    'F_CIA': int(core.id_cia_siesa),
                    'f470_id_co': _co_ent,
                    'f470_id_tipo_docto': core.tipo_docto_transito_entrada,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    # bodega_origen, NUNCA bodega_transito (TRA1) — ver docstring.
                    'f470_id_bodega': bodega_origen or core.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': None,
                    'f470_ind_naturaleza': 1,
                    'f470_id_motivo': core.motivo_traslado_entrada,
                    'f470_id_co_movto': _co_ent,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': item.get('unidad_empaque') or item.get('unidad_medida') or core.uom_default,
                    'f470_cant_base': round(float(abs(item.get('cantidad', 0))) / item.get('factor_empaque', 1), 4)
                        if item.get('factor_empaque', 1) > 1
                        else round(float(abs(item.get('cantidad', 0))), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': None,
                    # Typo intencional: 'varible' no 'variable' — nombre exacto del spec 173079.
                    # DEBE ser '' (no None): None omite el campo y Siesa rechaza por tamaño de registro.
                    'f470_desc_varible': '',
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_lote_ent': None,
                    'f470_id_item': None,
                    'f470_referencia_item': item.get('codigo_siesa'),
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': core.unidad_negocio,
                    'f470_rowid_movto': 0,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(core.id_cia_siesa)}]
        }

        logger.info(f'[CONNEKTA] Tránsito entrada {codigo_solicitud} '
                    f'{bodega_transito}→{bodega_destino}')
        _ets_din = not core.nombre_conector_transito_entrada.startswith('API_v1_')
        return core._post(core.conector_transito_entrada,
                          core.nombre_conector_transito_entrada, payload,
                          url=core.url_post_dinamico if _ets_din else None,
                          extra_params={'idSistema': core.id_sistema} if _ets_din else None)

    def get_consec_salida_transito_by_alterno(self, codigo_solicitud: str) -> int | None:
        """
        Recovery: API_v2_Inventarios_Transferencia_Salida_Transito filtrada por f450_docto_alterno.
        Retorna f350_consec_docto del STS creado para este traslado.
        """
        # Mismo dominio, mismo objeto — llamada intra-clase, no core.X.
        info = self.get_sts_info_by_alterno(codigo_solicitud)
        return info.get('consec') if info else None

    def get_sts_info_by_alterno(self, codigo_solicitud: str) -> dict | None:
        """
        Consulta el STS por f450_docto_alterno y devuelve consec + bodega_transito real.
        Útil para diagnosticar/corregir mismatch entre bodega_transito_siesa en WMS
        y la bodega_entrada que Siesa asignó al STS.
        Retorna {'consec': int, 'bodega_transito': str} o None si no encuentra.
        """
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        alterno = core._fmt_alterno(codigo_solicitud)
        if not alterno:
            return None
        try:
            res = core._get(
                'API_v2_Inventarios_Transferencia_Salida_Transito',
                params_extra={
                    'paginacion': 'numPag=1|tamPag=5',
                    # f450_docto_alterno es único por traslado — no filtrar por CO para
                    # soportar traslados desde distintas bodegas (NB1→003, NS1→001, etc.)
                    'parametros': f"f450_docto_alterno = {_lit(alterno)}",
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

    def get_consec_entrada_transito_by_alterno(self, codigo_solicitud: str) -> int | None:
        """
        Recovery: API_v2_Inventarios_Transferencia_Transito_Entrada filtrada por
        f450_docto_alterno. Retorna f350_consec_docto del ETS creado para este traslado.

        Espejo de `get_consec_salida_transito_by_alterno` (173076/STS) — mismo hueco,
        mismo remedio: 173079 puede aceptar el documento (HTTP 200) sin devolver un
        consecutivo parseable en la respuesta, y sin este recovery el ETS quedaba
        indistinguible de "nunca se envió" (`siesa_entrada_consec` null para las 68
        recepciones EN_TRANSITO existentes, verificado 2026-08-25).

        ✅ VERIFICADO en vivo contra Siesa QA (2026-08-25, vía
        `/api/health/ets-consecutivo`) contra ST-20260706-21B0: la primera
        hipótesis por simetría — `API_v2_Inventarios_Transferencia_Entrada_Transito`
        (mismo orden de palabras que el de salida) — devolvía 401 (no
        registrada). El nombre real invierte el orden: "Transito_Entrada", no
        "Entrada_Transito". Confirmado con datos reales: `f350_id_tipo_docto=ETS`,
        `f350_consec_docto=19`, `f350_consec_docto_salida=53` (coincide con el STS
        de ese mismo traslado) — el documento YA EXISTÍA en Siesa y el WMS nunca
        había capturado su consecutivo.
        """
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        try:
            res = core._get(
                core.consulta_transito_entrada,
                params_extra={
                    'paginacion': 'numPag=1|tamPag=5',
                    'parametros': f"f450_docto_alterno = {_lit(core._fmt_alterno(codigo_solicitud))}",
                },
            )
            rows = (
                res.get('detalle', {}).get('Table') or
                res.get('detalle', {}).get('Datos') or []
            )
            if rows:
                consec = rows[0].get('f350_consec_docto')
                logger.info('[CONNEKTA] ETS %s: consec recuperado=%s',
                            codigo_solicitud, consec)
                return int(consec) if consec else None
        except Exception as e:
            logger.warning('[CONNEKTA] get_consec_entrada_transito_by_alterno(%s): %s',
                           codigo_solicitud, e)
        return None

    def get_consec_rit_by_referencia(self, codigo_solicitud: str) -> int | None:
        """
        Recovery: consulta dinámica (Connekta → Consultas Dinámicas)
        `api_tecnocedi_requisiciones_traslado`, filtrada en memoria por
        f440_referencia. Retorna f440_consec_docto de la RIT creada para
        este traslado.

        `API_v2_Inventarios_RequisicionesParaTransferir` (nombre anterior,
        por analogía v1→v2 con el conector POST 174646) nunca existió en
        Connekta — daba 401 indistinguible de un problema de permisos
        (2026-09-04, ver CLAUDE.md "Los 12 traslados inter-bodega reales").
        El nombre real se encontró en Siesa QA → Administración → Permisos
        servicios → buscador de consultas dinámicas del usuario.

        Es consulta DINÁMICA, no estándar: va por `url_get_dinamico`
        (`ejecutarconsulta`) y sin `parametros` — las consultas dinámicas
        custom de este ambiente no soportan filtro en tiempo real (mismo
        hallazgo que `get_terceros_contacto`/`get_vendedor_contacto`), así
        que se trae la página y se filtra acá.

        Nombres de campo (`f440_referencia`, `f440_consec_docto`) asumidos
        iguales a los del payload POST de 174646 (misma tabla t440) — sin
        verificar contra una respuesta real: el 401 de permisos (issue
        abierto, ver CLAUDE.md) bloqueó ver el schema real en vivo.
        """
        core = self._core
        try:
            res = core._get(
                'api_tecnocedi_requisiciones_traslado',
                params_extra={'paginacion': 'numPag=1|tamPag=100'},
                url=core.url_get_dinamico,
            )
            rows = (
                res.get('detalle', {}).get('Table') or
                res.get('detalle', {}).get('Datos') or []
            )
            rows = [r for r in rows
                    if str(r.get('f440_referencia', '')).strip() == str(codigo_solicitud).strip()]
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
        core = self._core
        for _item in items:
            if not _item.get('codigo_siesa'):
                raise ValueError(
                    f'Item sin codigo_siesa en transferencia_directa: {_item.get("codigo") or _item}. '
                    'Nunca usar código interno WMS como fallback hacia Siesa.'
                )
        fecha_hoy = core._fecha_hoy_bogota()

        payload = {
            'Inicial': [{'F_CIA': int(core.id_cia_siesa)}],
            'Documentos': [
                {
                    'F_CIA': int(core.id_cia_siesa),
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': core.centro_op,
                    'f350_id_tipo_docto': core.tipo_docto_traslado,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': f'WMS Transferencia directa {codigo_solicitud}',
                    'f350_id_tercero': core.nit_empresa or None,                     # obligatorio spec 173066 — mismo que 173076
                    'f450_id_bodega_salida': bodega_origen,
                    'f450_id_bodega_entrada': bodega_destino,
                    # f450_docto_alterno, f350_id_co_base, f350_id_tipo_docto_base,
                    # f350_consec_docto_base no existen en el spec 173066 — omitidos
                }
            ],
            'Movimientos': [
                {
                    'F_CIA': int(core.id_cia_siesa),
                    'f470_id_co': core.centro_op,
                    'f470_id_tipo_docto': core.tipo_docto_traslado,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': bodega_origen,
                    'f470_id_ubicacion_aux': None,       # Dep — si bodega maneja ubicaciones
                    'f470_id_lote': None,                # Dep — si ítem maneja lotes
                    'f470_id_motivo': core.motivo_traslado,
                    'f470_id_co_movto': core.centro_op,
                    'f470_id_ccosto_movto': None,        # Dep — si cuenta contable exige ccosto
                    'f470_id_proyecto': None,             # No — opcional
                    'f470_id_unidad_medida': item.get('unidad_medida') or core.uom_default,
                    'f470_cant_base': round(float(abs(item.get('cantidad', 0))), 4),
                    'f470_cant_2': None,                 # Dep — si ítem maneja unidad adicional
                    'f470_costo_prom_uni': None,          # Dep — costo unitario
                    'f470_notas': None,
                    'f470_desc_varible': '',              # Typo intencional: spec 173066 pos 487, 2000 chars
                    'f470_id_ubicacion_aux_ent': None,   # Dep — si bodega entrada maneja ubicaciones
                    'f470_id_lote_ent': None,             # Dep — si ítem+bodega entrada manejan lotes
                    'f470_id_item': None,                # Dep — usamos referencia_item
                    'f470_referencia_item': item.get('codigo_siesa'),
                    'f470_codigo_barras': None,           # Dep
                    'f470_id_ext1_detalle': None,        # Dep — si ítem maneja extensión 1
                    'f470_id_ext2_detalle': None,        # Dep — si ítem maneja extensión 2
                    'f470_id_un_movto': core.unidad_negocio,
                }
                for idx, item in enumerate(items)
            ],
            'Final': [{'F_CIA': int(core.id_cia_siesa)}]
        }

        logger.info(f'[CONNEKTA] Transferencia directa {codigo_solicitud} '
                    f'{bodega_origen}→{bodega_destino}')
        return core._post(core.conector_transferencia_directa,
                          'API_v1_Inventarios_Comercial_TransferenciaDirecta', payload)


__all__ = ['ConnektaTrasladosGateway']
