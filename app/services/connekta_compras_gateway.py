"""
Dominio Compras/Recepción (OC) del gateway Connekta — extraído de
ConnektaGateway (2026-09-09, paso 3 de la deuda de tamaño; ver paso 1:
app/utils/siesa_formato.py, paso 2: connekta_circuit_breaker.py).

Un solo método hoy (`confirmar_entrada_compras`, conector 142948) — el
dominio más chico del gateway, elegido a propósito como piloto del patrón
de separación por dominio antes de tocar los más grandes (Facturación,
Traslados, NC/Liquidación).

**No recibe config propia.** Toma la instancia completa de `ConnektaGateway`
como `core` y accede a `core.X` para credenciales/config/`_get`/`_post`/
helpers de formato — el gateway sigue siendo la única fuente de esas cosas
(mover el `__init__` de ~330 líneas de config es un paso aparte, de mayor
riesgo, que se deja para cuando de verdad haga falta). Esta clase solo
aísla la LÓGICA de armar el payload de 142948, no la configuración.

`ConnektaGateway.confirmar_entrada_compras` queda como delegado delgado —
el singleton `connekta` y sus callers (`siesa_job_service.py`,
`tests/test_09_guards_criticos.py`) no cambian.
"""
import logging

logger = logging.getLogger(__name__)


class ConnektaComprasGateway:

    def __init__(self, core):
        self._core = core

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
        core = self._core
        if not core.tipo_docto_entrada_oc:
            raise ValueError(
                'SIESA_TIPO_DOCTO_ENTRADA_OC no está configurado en variables de entorno. '
                'Agrega la variable en Railway con el código de tipo de documento de entrada OC en Siesa.'
            )
        # Siesa espera fecha sin guiones: YYYYMMDD (8 chars). f421_fecha_entrega usa YYYY-MM-DD.
        fecha_hoy = core._fecha_hoy_bogota()
        fecha_hoy_iso = core._fecha_iso_bogota()

        # F_CIA debe ser entero según especificación Siesa/Connekta
        cia = int(core.id_cia_siesa)

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
        if not (cond_pago or core.cond_pago_compras):
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
                    'f350_id_co': f'{int(id_co_oc or core.centro_op):03d}',            # CO del documento (usa CO de la OC para multi-bodega)
                    'f350_id_tipo_docto': core.tipo_docto_entrada_oc or None,        # tipo doc entrada OC (SIESA_TIPO_DOCTO_ENTRADA_OC)
                    'f350_consec_docto': 0,
                    'f350_fecha': fecha_hoy,
                    'f350_id_tercero': proveedor_id or None,                         # NIT proveedor (pos 43-58)
                    'f350_ind_estado': 1,                                            # 1 = Aprobado — contabiliza automáticamente contra pasivo estimado (26059501 configurado en tipo EA/CO003)
                    'f350_ind_impresion': 0,
                    'f350_notas': None,
                    'f451_id_cond_pago': cond_pago or core.cond_pago_compras or None,  # [A2] condición pago — obligatorio spec 142948 pos 324
                    'f451_id_sucursal_prov': sucursal_prov_fmt,                      # sucursal proveedor (pos 324-327) — 3 chars, zfill aplicado
                    'f451_id_tercero_comprador': tercero_comprador or core.nit_empresa or None,  # comprador exacto de la OC
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
                    'f470_id_co': f'{int(id_co_oc or core.centro_op):03d}',
                    'f470_id_tipo_docto': core.tipo_docto_entrada_oc or None,        # tipo doc movimiento (pos 22-25)
                    'f470_consec_docto': 0,
                    'f470_nro_registro': idx + 1,
                    'f470_id_bodega': i.get('bodega') or core.bodega,
                    'f470_id_ubicacion_aux': None,
                    'f470_id_lote': i.get('lote') or None,
                    # [A10] 142948 spec does NOT have f470_id_concepto, f470_ind_naturaleza,
                    # f470_ind_obsequio, f470_ind_solo_valor, f470_ind_impto_asumido — removed
                    # Bonificación usa motivo '04' (obsequio/bonif en Siesa). OC usa motivo de la OC o motivo_compras.
                    'f470_id_motivo': i.get('motivo_siesa') or ('04' if i.get('tipo') == 'BONIFICACION' else core.motivo_compras),
                    # UOM y fecha_entrega deben coincidir exactamente con los de la OC (Siesa los valida)
                    'f470_id_unidad_medida': i.get('uom') or i.get('unidad_medida') or core.uom_default,
                    'f421_fecha_entrega': core._fmt_fecha_iso(i.get('fecha_entrega')) or fecha_hoy,
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

        return core._post(core.conector_entrada, 'API_v1_Compras_Comercial_EntradaOC', payload)


__all__ = ['ConnektaComprasGateway']
