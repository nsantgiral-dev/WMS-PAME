"""
LiquidacionService — Automatización del ciclo financiero post-entrega.

Procesa cada RecaudoEntrega de una ruta y dispara los conectores Siesa:
  - 142888 ReciboCaja      → cobro del conductor (CONTADO)
  - 142946 NotaFactura     → nota crédito por devolución (PARCIAL/RECHAZADO)
  - 142882 DocumentoContable → retenciones (retefuente, reteIVA, ICA)

Reglas de oro:
  1. Secuencialidad: NC primero → esperar HTTP 200 → luego RC. Nunca en paralelo
     contra la misma factura (deadlock T353 en SQL Server).
  2. F_CONSEC_AUTO_REG=1 y F350_IND_ESTADO=1 siempre.
  3. Trazabilidad: F350_NOTAS / f470_notas con ruta, conductor, fecha.

Opción A: NO se crea PCF en Siesa — WMS controla el dispatch, los conectores
financieros operan directamente contra facturas libres (sin amarre).
"""

import logging
from datetime import datetime
from app.extensions import db
from app.models.recaudo_entrega import RecaudoEntrega
from app.models.ruta_despacho import RutaDespacho, EstadoFinancieroRuta
from app.models.siesa_job import SiesaJob

logger = logging.getLogger(__name__)


# Mapeo motivo_descuento → cuenta PUC auxiliar Siesa (serie 1355)
RETENCION_PUC = {
    'RETEFUENTE_2.5':   '13551501',   # Retefuente Compras 2.5%
    'RETEFUENTE_1.5':   '13551502',   # Retefuente Bancos 1.5%
    'RETEIVA':          '13551701',   # ReteIVA 15%
    'ICA_3':            '13551801',   # ICA 3x1000
    'ICA_4.14':         '13551802',   # ICA 4.14x1000
    'ICA_6.9':          '13551803',   # ICA 6.9x1000
    'ICA_8':            '13551804',   # ICA 8x1000
    'ICA_11.04':        '13551805',   # ICA 11.04x1000
    'AUTORETENCION_ICA_3':    '13559501',
    'AUTORETENCION_ICA_4.14': '13559502',
    'AUTORETENCION_ICA_6.9':  '13559503',
    'AUTORETENCION_ICA_8':    '13559504',
    'AUTORETENCION_ICA_11.04':'13559505',
}

# Tasas de retención para cálculo automático
RETENCION_TASA = {
    'RETEFUENTE_2.5': 0.025,
    'RETEFUENTE_1.5': 0.015,
    'RETEIVA':        0.15,
    'ICA_3':          0.003,
    'ICA_4.14':       0.00414,
    'ICA_6.9':        0.0069,
    'ICA_8':          0.008,
    'ICA_11.04':      0.01104,
    'AUTORETENCION_ICA_3':    0.003,
    'AUTORETENCION_ICA_4.14': 0.00414,
    'AUTORETENCION_ICA_6.9':  0.0069,
    'AUTORETENCION_ICA_8':    0.008,
    'AUTORETENCION_ICA_11.04':0.01104,
}


class LiquidacionService:

    @staticmethod
    def liquidar_ruta_siesa(ruta_id: int, admin_id: int = None) -> dict:
        """
        Procesa todos los recaudos de una ruta y encola los jobs Siesa correspondientes.
        Cambia estado_financiero a EN_LIQUIDACION y luego a LIQUIDADA al completar el encolado.

        Retorna resumen con conteos de jobs encolados por tipo.
        """
        ruta = RutaDespacho.query.get(ruta_id)
        if not ruta:
            raise LookupError('Ruta no encontrada')

        recaudos = RecaudoEntrega.query.filter_by(ruta_id=ruta_id).all()
        if not recaudos:
            raise ValueError('No hay recaudos registrados en esta ruta')

        conductor = ruta.conductor
        vehiculo = ruta.vehiculo
        notas_base = (
            f'WMS Ruta #{ruta_id} | '
            f'Conductor: {conductor.nombre} ({conductor.cedula}) | '
            f'Vehículo: {vehiculo.placa if vehiculo else "N/A"} | '
            f'Fecha: {datetime.utcnow().strftime("%Y-%m-%d")}'
        )

        resumen = {'rc_encolados': 0, 'nc_encolados': 0, 'dc_encolados': 0,
                    'credito_omitidos': 0, 'ya_procesados': 0, 'errores': []}

        for recaudo in recaudos:
            try:
                r = _procesar_recaudo(recaudo, notas_base, admin_id)
                resumen['rc_encolados'] += r.get('rc', 0)
                resumen['nc_encolados'] += r.get('nc', 0)
                resumen['dc_encolados'] += r.get('dc', 0)
                resumen['credito_omitidos'] += r.get('credito', 0)
                resumen['ya_procesados'] += r.get('ya_procesado', 0)
            except Exception as e:
                logger.error(
                    '[LIQUIDACION] Error procesando recaudo %d (tarea %d): %s',
                    recaudo.id, recaudo.tarea_id, e
                )
                resumen['errores'].append({
                    'recaudo_id': recaudo.id,
                    'tarea_id': recaudo.tarea_id,
                    'error': str(e),
                })

        db.session.commit()

        # Disparar DLQ inmediato para procesar los jobs sin esperar el cron
        try:
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
        except Exception:
            pass

        logger.info(
            '[LIQUIDACION] Ruta %d liquidada: %d RC, %d NC, %d DC encolados, '
            '%d crédito omitidos, %d ya procesados, %d errores',
            ruta_id, resumen['rc_encolados'], resumen['nc_encolados'],
            resumen['dc_encolados'], resumen['credito_omitidos'],
            resumen['ya_procesados'], len(resumen['errores'])
        )
        return resumen


def _procesar_recaudo(recaudo: RecaudoEntrega, notas_base: str,
                       admin_id: int = None) -> dict:
    """
    Determina qué conectores disparar para un recaudo individual.
    Encola SiesaJobs. NO hace commit (el caller lo maneja).

    Flujos:
      CONTADO + ENTREGADO completo        → RC
      CONTADO + ENTREGADO + retención     → RC + DC
      CONTADO + PARCIAL                   → NC → RC (secuencial en DLQ)
      CRÉDITO + ENTREGADO                 → noop (queda en cartera para Gestor)
      CRÉDITO + PARCIAL                   → NC solamente
      RECHAZADO                           → NC total
    """
    tarea = recaudo.tarea
    if not tarea:
        raise ValueError(f'Recaudo {recaudo.id} sin tarea asociada')

    # Datos de la factura
    tipo_docto_fe = tarea.tipo_docto_pedido_siesa or ''
    consec_fe = tarea.consec_docto_pedido_siesa or ''
    if not tipo_docto_fe or not consec_fe:
        raise ValueError(
            f'Tarea {tarea.id} ({tarea.codigo}) sin tipo_docto/consec_docto — '
            'no se puede vincular a factura Siesa'
        )

    # Obtener NIT del cliente desde la tarea
    # El NIT viene del pedido original — buscar en PedidoSiesa
    tercero_nit, sucursal = _obtener_tercero(tarea)

    estado = recaudo.estado_entrega
    forma_pago = (recaudo.forma_pago or '').upper()
    es_credito = forma_pago == 'CREDITO'
    monto = float(recaudo.monto_cobrado or 0)
    resultado = {'rc': 0, 'nc': 0, 'dc': 0, 'credito': 0, 'ya_procesado': 0}

    # ── RECHAZADO: NC total ──────────────────────────────────────
    if estado == 'RECHAZADO':
        if recaudo.siesa_nc_triggered:
            resultado['ya_procesado'] = 1
            return resultado
        _encolar_nota_credito(
            recaudo, tipo_docto_fe, consec_fe,
            items_devueltos=None,  # None = devolución total
            notas=f'{notas_base} | RECHAZADO total',
            admin_id=admin_id,
        )
        resultado['nc'] = 1
        return resultado

    # ── CRÉDITO + ENTREGADO: noop ────────────────────────────────
    if es_credito and estado == 'ENTREGADO':
        resultado['credito'] = 1
        return resultado

    # ── CRÉDITO + PARCIAL: solo NC ───────────────────────────────
    if es_credito and estado == 'PARCIAL':
        if recaudo.siesa_nc_triggered:
            resultado['ya_procesado'] = 1
            return resultado
        _encolar_nota_credito(
            recaudo, tipo_docto_fe, consec_fe,
            items_devueltos=recaudo.items_entregados,
            notas=f'{notas_base} | PARCIAL crédito — devolución',
            admin_id=admin_id,
        )
        resultado['nc'] = 1
        return resultado

    # ── CONTADO + PARCIAL: NC → luego RC (secuencial) ────────────
    if not es_credito and estado == 'PARCIAL':
        if not recaudo.siesa_nc_triggered:
            _encolar_nota_credito(
                recaudo, tipo_docto_fe, consec_fe,
                items_devueltos=recaudo.items_entregados,
                notas=f'{notas_base} | PARCIAL contado — devolución',
                admin_id=admin_id,
            )
            resultado['nc'] = 1

        if not recaudo.siesa_rc_triggered and monto > 0:
            _encolar_recibo_caja(
                recaudo, tipo_docto_fe, consec_fe,
                tercero_nit, sucursal, monto, forma_pago,
                notas=f'{notas_base} | PARCIAL contado — cobro',
                admin_id=admin_id,
                depende_de_nc=True,
            )
            resultado['rc'] = 1

        # Retención (si aplica)
        if not recaudo.siesa_dc_triggered and recaudo.motivo_descuento:
            _encolar_documento_contable(
                recaudo, tipo_docto_fe, consec_fe,
                tercero_nit, sucursal,
                notas=f'{notas_base} | Retención {recaudo.motivo_descuento}',
                admin_id=admin_id,
            )
            resultado['dc'] = 1

        return resultado

    # ── CONTADO + ENTREGADO: RC (+ DC si retención) ──────────────
    if not es_credito and estado == 'ENTREGADO':
        if not recaudo.siesa_rc_triggered and monto > 0:
            _encolar_recibo_caja(
                recaudo, tipo_docto_fe, consec_fe,
                tercero_nit, sucursal, monto, forma_pago,
                notas=f'{notas_base} | ENTREGADO contado',
                admin_id=admin_id,
            )
            resultado['rc'] = 1

        if not recaudo.siesa_dc_triggered and recaudo.motivo_descuento:
            _encolar_documento_contable(
                recaudo, tipo_docto_fe, consec_fe,
                tercero_nit, sucursal,
                notas=f'{notas_base} | Retención {recaudo.motivo_descuento}',
                admin_id=admin_id,
            )
            resultado['dc'] = 1

        if recaudo.siesa_rc_triggered and not recaudo.motivo_descuento:
            resultado['ya_procesado'] = 1

        return resultado

    # Forma de pago EXENTO o caso no contemplado
    logger.warning(
        '[LIQUIDACION] Recaudo %d: estado=%s forma_pago=%s — sin acción Siesa',
        recaudo.id, estado, forma_pago
    )
    return resultado


def _obtener_tercero(tarea) -> tuple:
    """Obtiene NIT y sucursal del cliente desde Connekta (get_pedido_cabecera)."""
    from app.services.connekta_gateway import connekta
    cabecera = connekta.get_pedido_cabecera(
        tarea.tipo_docto_pedido_siesa,
        tarea.consec_docto_pedido_siesa,
    )
    if cabecera:
        nit = cabecera.get('f200_id_pedido_fact') or ''
        sucursal = cabecera.get('f461_id_sucursal_pedido_rem') or '001'
        return nit, sucursal

    logger.warning(
        '[LIQUIDACION] get_pedido_cabecera vacío para %s-%s — '
        'tercero no disponible para conectores financieros',
        tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa
    )
    return '', '001'


def _encolar_nota_credito(recaudo: RecaudoEntrega, tipo_docto_fe: str,
                           consec_fe, items_devueltos: list,
                           notas: str, admin_id: int = None):
    """Encola job NOTA_CREDITO_FACTURA en la DLQ."""
    # Construir lista de ítems devueltos para el payload
    items_para_nc = []
    if items_devueltos:
        for it in items_devueltos:
            cant_devuelta = it.get('cantidad_devuelta') or it.get('devuelto', 0)
            if int(cant_devuelta) > 0:
                items_para_nc.append({
                    'codigo': it.get('codigo', ''),
                    'cantidad_devuelta': int(cant_devuelta),
                })

    SiesaJob.encolar(
        tipo='NOTA_CREDITO_FACTURA',
        payload={
            'recaudo_id': recaudo.id,
            'tipo_docto_fe': tipo_docto_fe,
            'consec_fe': str(consec_fe),
            'items_devueltos': items_para_nc,
            'es_total': not bool(items_devueltos),
            'causal_devolucion': recaudo.causal_devolucion or '',
            'notas': notas,
        },
        referencia_tipo='RecaudoEntrega',
        referencia_id=recaudo.id,
        creado_por_id=admin_id,
    )
    logger.info(
        '[LIQUIDACION] Encolado NOTA_CREDITO_FACTURA para recaudo %d (FE %s-%s)',
        recaudo.id, tipo_docto_fe, consec_fe
    )


def _encolar_recibo_caja(recaudo: RecaudoEntrega, tipo_docto_fe: str,
                          consec_fe, tercero_nit: str, sucursal: str,
                          monto: float, forma_pago: str, notas: str,
                          admin_id: int = None, depende_de_nc: bool = False):
    """Encola job RECIBO_CAJA en la DLQ."""
    SiesaJob.encolar(
        tipo='RECIBO_CAJA',
        payload={
            'recaudo_id': recaudo.id,
            'tipo_docto_fe': tipo_docto_fe,
            'consec_fe': str(consec_fe),
            'tercero_nit': tercero_nit,
            'sucursal': sucursal,
            'monto': monto,
            'forma_pago': forma_pago,
            'notas': notas,
            'depende_de_nc': depende_de_nc,
        },
        referencia_tipo='RecaudoEntrega',
        referencia_id=recaudo.id,
        creado_por_id=admin_id,
    )
    logger.info(
        '[LIQUIDACION] Encolado RECIBO_CAJA para recaudo %d (FE %s-%s, $%.2f)',
        recaudo.id, tipo_docto_fe, consec_fe, monto
    )


def _encolar_documento_contable(recaudo: RecaudoEntrega, tipo_docto_fe: str,
                                  consec_fe, tercero_nit: str, sucursal: str,
                                  notas: str, admin_id: int = None):
    """Encola job DOCUMENTO_CONTABLE_RET en la DLQ."""
    motivo = recaudo.motivo_descuento or ''
    cuenta_puc = RETENCION_PUC.get(motivo, '')
    if not cuenta_puc:
        logger.error(
            '[LIQUIDACION] motivo_descuento=%s sin cuenta PUC mapeada — DC no encolado',
            motivo
        )
        return

    monto_descuento = float(recaudo.monto_descuento or 0)
    if monto_descuento <= 0:
        # Calcular automáticamente si no se especificó
        tasa = RETENCION_TASA.get(motivo, 0)
        base = float(recaudo.monto_cobrado or 0) + monto_descuento
        monto_descuento = round(base * tasa, 2) if tasa else 0
        if monto_descuento <= 0:
            logger.warning(
                '[LIQUIDACION] monto_descuento=0 para recaudo %d — DC no encolado',
                recaudo.id
            )
            return

    SiesaJob.encolar(
        tipo='DOCUMENTO_CONTABLE_RET',
        payload={
            'recaudo_id': recaudo.id,
            'tipo_docto_fe': tipo_docto_fe,
            'consec_fe': str(consec_fe),
            'tercero_nit': tercero_nit,
            'sucursal': sucursal,
            'cuenta_puc': cuenta_puc,
            'monto': monto_descuento,
            'base_gravable': float(recaudo.monto_cobrado or 0),
            'notas': notas,
        },
        referencia_tipo='RecaudoEntrega',
        referencia_id=recaudo.id,
        creado_por_id=admin_id,
    )
    logger.info(
        '[LIQUIDACION] Encolado DOCUMENTO_CONTABLE_RET para recaudo %d (PUC %s, $%.2f)',
        recaudo.id, cuenta_puc, monto_descuento
    )
