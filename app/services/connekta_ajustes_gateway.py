"""
Dominio Ajustes/Averías del gateway Connekta — extraído de ConnektaGateway
(2026-09-09, paso 4 de la deuda de tamaño; ver paso 1: app/utils/
siesa_formato.py, paso 2: connekta_circuit_breaker.py, paso 3:
connekta_compras_gateway.py).

Dos métodos, ambos contra el mismo conector 142951
(API_v1_Inventarios_Comercial_DocumentoInv), con distinto propósito:
`enviar_ajuste_inventario` (sobrante/faltante de conteo cíclico) y
`transferir_a_averias` (traslado físico NB1→AV1 por avería).

**No recibe config propia.** Mismo patrón que `ConnektaComprasGateway`:
toma la instancia completa de `ConnektaGateway` como `core` y accede a
`core.X` — evita duplicar el `__init__` de config. `ConnektaGateway`
conserva los dos métodos como delegados delgados; el singleton `connekta`
y su único caller (`siesa_job_service.py`) no cambian.
"""
import logging

logger = logging.getLogger(__name__)


class ConnektaAjustesGateway:

    def __init__(self, core):
        self._core = core

    def enviar_ajuste_inventario(self, motivo_codigo: str, item_codigo: str,
                                  cantidad: int, referencia: str,
                                  bodega: str = None, centro_op: str = None,
                                  item_id_siesa: str = None):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Ajuste físico tras conteo cíclico double-blind.
        AJ-ENT: sobrante. AJ-SAL: faltante. Cantidad siempre positiva.
        bodega: código bodega Siesa (ej 'NB1','NB2'). Si None usa core.bodega.
        centro_op: centro de operación Siesa. Si None usa core.centro_op.
        """
        import os
        core = self._core
        _tipo_docto_ajuste = core.tipo_docto_ajuste or os.getenv('SIESA_TIPO_DOCTO_AJUSTE', '')
        if not _tipo_docto_ajuste:
            raise ValueError(
                'SIESA_TIPO_DOCTO_AJUSTE no está configurado en variables de entorno. '
                'Agrega la variable en Railway con el código de tipo de documento de ajuste en Siesa.'
            )
        if motivo_codigo not in ['AJ-ENT', 'AJ-SAL']:
            raise ValueError(f'Motivo inválido: {motivo_codigo}')

        _bodega = bodega or core.bodega
        _centro_op = centro_op or core.centro_op

        es_entrada = motivo_codigo == 'AJ-ENT'
        # PAME Concepto 0603: '01'=Entrada Ajuste a inventario (Naturaleza Entrada),
        # '02'=Salida Ajuste a inventario (Naturaleza Salida) — verificado en Siesa Enterprise
        # (Maestros > Conceptos y motivos > 0603), captura 2026-07-02. NO invertir.
        # REGLA REAL DEL CONECTOR 142951 (verificada 2026-07-02, item PAPELSP9218/NS1):
        # rechaza el ADI si el disponible RESULTANTE tras aplicar el movimiento sigue siendo
        # negativo, sin importar la dirección (entrada o salida). No es un problema de mapeo
        # de motivo. Prueba 1: existencia 110, comprometida 76, salida_sin_conf 47 (disponible
        # -13); AJ-ENT +5 → resultante -8 → RECHAZADO (HTTP 400 Faltante Inv). Prueba 2: mismo
        # estado, AJ-ENT +15 → resultante +2 → ACEPTADO (HTTP 200, existencia 110→125).
        # Implicación operativa: en una bodega con disponible negativo por compromisos, un
        # sobrante de conteo cíclico solo se registra si alcanza a cubrir todo el déficit; un
        # faltante ahí nunca podrá registrarse hasta que los compromisos se liberen.
        siesa_motivo = core.motivo_ajuste_entrada if es_entrada else core.motivo_ajuste_salida

        fecha_hoy = core._fecha_hoy_bogota()
        cia = int(core.id_cia_siesa)

        payload = {
            'Inicial': [
                {'F_CIA': cia}
            ],
            'Documentos': [
                {
                    'F_CIA': cia,
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': _centro_op,
                    'f350_id_tipo_docto': _tipo_docto_ajuste,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': core.nit_empresa or None,
                    'f350_id_clase_docto': 63,
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': referencia,
                    'f450_id_concepto': core.concepto_ajustes,
                    # ADI (Clase 63): bodegas de cabecera no aplican — la bodega real
                    # va únicamente en f470_id_bodega del bloque Movimientos.
                    'f450_id_bodega_salida': None,
                    'f450_id_bodega_entrada': None,
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
                    'f470_id_tipo_docto': _tipo_docto_ajuste,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': 1,
                    'f470_id_bodega': _bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_ubicación_aux': None,
                    'f470_id_lote': None,
                    'f470_id_concepto': core.concepto_ajustes,                       # 603 = Ajustes (spec 142951, obligatorio), override: SIESA_CONCEPTO_AJUSTES
                    'f470_id_motivo': siesa_motivo,
                    'f470_id_co_movto': _centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': core.uom_default,
                    'f470_cant_base': round(float(abs(cantidad)), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': '',
                    # Typo intencional: 'varible' — nombre exacto del spec 142951 (pos 487, 2000 chars).
                    # Si se escribe 'variable' (correcto), Connekta omite el campo y Siesa rechaza
                    # por tamaño de registro (692 vs 2692).
                    'f470_desc_varible': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': core.uom_default,
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_ubicación_aux_ent': None,
                    'f470_id_lote_ent': None,
                    'f470_id_item': item_id_siesa,
                    'f470_referencia_item': item_codigo,
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': core.unidad_negocio   # spec 142951: unidad de negocio, no centro_op
                }
            ],
            'Final': [
                {'F_CIA': cia}
            ]
        }

        logger.info(f'[CONNEKTA] Ajuste {motivo_codigo} {item_codigo}:{cantidad} siesa_motivo={siesa_motivo!r} f470_id_item={item_id_siesa!r} f470_id_un_movto={core.unidad_negocio!r}')
        return core._post(core.conector_ajuste, 'API_v1_Inventarios_Comercial_DocumentoInv', payload)

    def transferir_a_averias(self, item_codigo: str, cantidad: int, referencia: str = ''):
        """
        142951 → API_v1_Inventarios_Comercial_DocumentoInv
        Traslado físico NB1 → AV1 cuando el recepcionista marca mercancía como averiada.
        Usa SIESA_TIPO_DOCTO_TRASLADO (TRA) y SIESA_MOTIVO_TRASLADO (01).
        Siesa mueve el stock entre bodegas — vendedores ya no ven las unidades averiadas.
        """
        core = self._core
        fecha_hoy = core._fecha_hoy_bogota()
        cia_averias = int(core.id_cia_siesa)

        payload = {
            'Inicial': [
                {'F_CIA': cia_averias}
            ],
            'Documentos': [
                {
                    'F_CIA': cia_averias,
                    'F_CONSEC_AUTO_REG': 1,
                    'f350_id_co': core.centro_op,
                    'f350_id_tipo_docto': core.tipo_docto_traslado,
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': core.nit_empresa or None,                      # SIESA_NIT_EMPRESA — None si no configurado; Siesa rechaza string vacío
                    'f350_id_clase_docto': 67,           # Entero obligatorio: 67=Transferencias (spec 142951)
                    'f350_ind_estado': 1,
                    'f350_ind_impresion': 0,
                    'f350_notas': referencia or f'Avería detectada por WMS · {item_codigo}',
                    'f450_id_concepto': core.concepto_traslados,                       # env SIESA_CONCEPTO_TRASLADOS (spec 142951, obligatorio)
                    'f450_id_bodega_salida': core.bodega,
                    'f450_id_bodega_entrada': core.bodega_averias,
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
                    'f470_id_co': core.centro_op,
                    'f470_id_tipo_docto': core.tipo_docto_traslado,
                    'f470_consec_docto': 0,
                    'f470_nro_registro': 1,
                    'f470_id_bodega': core.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': None,
                    'f470_id_concepto': core.concepto_traslados,                     # 607 = Transferencias (spec 142951, obligatorio), override: SIESA_CONCEPTO_TRASLADOS
                    'f470_id_motivo': core.motivo_averia,                            # SIESA_MOTIVO_AVERIA — validado contra maestro Siesa por compañía
                    'f470_id_co_movto': core.centro_op,
                    'f470_id_ccosto_movto': None,
                    'f470_id_proyecto': None,
                    'f470_id_unidad_medida': core.uom_default,
                    'f470_cant_base': round(float(abs(cantidad)), 4),
                    'f470_cant_2': None,
                    'f470_costo_prom_uni': None,
                    'f470_notas': '',
                    # Typo intencional: 'varible' — nombre exacto del spec 142951.
                    'f470_desc_varible': '',
                    'F_DESC_ITEM': '',
                    'F_ID_UM_INVENTARIO': core.uom_default,   # consistente con enviar_ajuste_inventario
                    'f470_id_ubicacion_aux_ent': None,
                    'f470_id_lote_ent': None,
                    'f470_id_item': None,
                    'f470_referencia_item': item_codigo,
                    'f470_codigo_barras': None,
                    'f470_id_ext1_detalle': None,
                    'f470_id_ext2_detalle': None,
                    'f470_id_un_movto': core.unidad_negocio
                }
            ],
            'Final': [
                {'F_CIA': cia_averias}
            ]
        }

        logger.info(f'[CONNEKTA] Traslado averías {item_codigo}:{cantidad} {core.bodega}→{core.bodega_averias}')
        return core._post(core.conector_ajuste, 'API_v1_Inventarios_Comercial_DocumentoInv', payload)


__all__ = ['ConnektaAjustesGateway']
