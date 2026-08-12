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
from app.models.recaudo_entrega import RecaudoEntrega, EstadoEntrega
from app.models.ruta_despacho import RutaDespacho, EstadoFinancieroRuta
from app.models.siesa_job import SiesaJob
from app.utils.fecha import ahora_bogota as _ahora_bogota

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
    def preparar_detalle_ruta(ruta_id: int) -> dict:
        """
        Prepara datos detallados de liquidación para una ruta:
        cada recaudo + info de factura Siesa (base gravable, IVA, líneas).
        Incluye catálogo de retenciones disponibles.
        """
        ruta = RutaDespacho.query.get(ruta_id)
        if not ruta:
            raise LookupError('Ruta no encontrada')

        recaudos = RecaudoEntrega.query.filter_by(ruta_id=ruta_id).all()
        from app.services.connekta_gateway import connekta

        retenciones_disponibles = [
            {'tipo': k, 'nombre': _nombre_retencion(k), 'puc': RETENCION_PUC[k], 'tasa': v}
            for k, v in RETENCION_TASA.items()
        ]

        resultado_recaudos = []
        warnings = []

        for recaudo in recaudos:
            tarea = recaudo.tarea
            rd = recaudo.to_dict()
            rd['cliente'] = tarea.cliente or '' if tarea else ''
            rd['numero_pedido'] = tarea.numero_pedido_siesa or '' if tarea else ''
            rd['tipo_docto'] = tarea.tipo_docto_pedido_siesa or '' if tarea else ''
            rd['consec_docto'] = tarea.consec_docto_pedido_siesa or '' if tarea else ''

            factura_siesa = None
            from app.services.fe_resolver import resolver_fe_o_none
            _tipo_fe, _consec_fe = resolver_fe_o_none(tarea) if tarea else (None, None)
            if _tipo_fe and _consec_fe:
                try:
                    lineas_raw = connekta.get_rowids_factura(_tipo_fe, _consec_fe)
                    if lineas_raw:
                        lineas = []
                        base_gravable = 0
                        total_iva = 0
                        total_neto = 0
                        for ln in lineas_raw:
                            vlr_bruto = float(ln.get('f470_vlr_bruto', 0))
                            vlr_imp = float(ln.get('f470_vlr_imp', 0))
                            vlr_neto = float(ln.get('f470_vlr_neto', 0))
                            base_gravable += vlr_bruto
                            total_iva += vlr_imp
                            total_neto += vlr_neto
                            lineas.append({
                                'f120_referencia': ln.get('f120_referencia', ''),
                                'f120_descripcion': ln.get('f120_descripcion', ''),
                                'f470_cant_base': ln.get('f470_cant_base', 0),
                                'f470_vlr_bruto': vlr_bruto,
                                'f470_vlr_imp': vlr_imp,
                                'f470_vlr_neto': vlr_neto,
                                'f470_precio_uni': float(ln.get('f470_precio_uni', 0)),
                                'f470_rowid': ln.get('f470_rowid', ''),
                            })
                        factura_siesa = {
                            'base_gravable': base_gravable,
                            'total_iva': total_iva,
                            'total_neto': total_neto,
                            'lineas': lineas,
                        }
                except Exception as e:
                    logger.warning(
                        '[LIQUIDACION] get_rowids_factura falló para recaudo %d '
                        '(FE %s-%s): %s — continuando sin datos Siesa',
                        recaudo.id, tarea.tipo_docto_pedido_siesa,
                        tarea.consec_docto_pedido_siesa, e,
                    )
                    warnings.append(
                        f'No se pudo obtener factura Siesa para pedido '
                        f'{tarea.numero_pedido_siesa or "?"}: {e}'
                    )

            rd['factura_siesa'] = factura_siesa
            rd['retenciones_disponibles'] = retenciones_disponibles
            resultado_recaudos.append(rd)

        result = {
            'ruta': ruta.to_dict(),
            'recaudos': resultado_recaudos,
        }
        if warnings:
            result['warnings'] = warnings
        return result

    # ──────────────────────────────────────────────────────────────────────
    #  Per-recaudo liquidation methods
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def preview_acciones_recaudo(recaudo_id: int) -> dict:
        """
        Returns pending Siesa actions + real financial data for a single recaudo.

        Used by the frontend to show what will happen BEFORE the user confirms.
        All Siesa calls are wrapped in try/except — if they fail, datos_disponibles=False.
        """
        recaudo = RecaudoEntrega.query.get(recaudo_id)
        if not recaudo:
            raise LookupError(f'RecaudoEntrega {recaudo_id} no encontrado')

        tarea = recaudo.tarea
        if not tarea:
            raise ValueError(f'Recaudo {recaudo_id} sin tarea asociada')

        # El PEDIDO — para mostrar. La factura se resuelve aparte: son
        # documentos distintos con numeraciones distintas.
        tipo_docto = tarea.tipo_docto_pedido_siesa or ''
        consec_docto = tarea.consec_docto_pedido_siesa or ''
        from app.services.fe_resolver import resolver_fe_o_none
        _tipo_fe, _consec_fe = resolver_fe_o_none(tarea)
        estado = recaudo.estado_entrega
        forma_pago = (recaudo.forma_pago or '').upper()

        # ── Siesa data fetching ─────────────────────────────────────
        datos_disponibles = False
        base_gravable = 0
        total_iva = 0
        total_neto = 0
        co_factura = ''
        cuenta_cxc = ''

        if _tipo_fe and _consec_fe:
            from app.services.connekta_gateway import connekta
            try:
                # Factura lines: base_gravable, IVA, neto
                lineas_raw = connekta.get_rowids_factura(_tipo_fe, _consec_fe)
                if lineas_raw:
                    for ln in lineas_raw:
                        base_gravable += float(ln.get('f470_vlr_bruto', 0))
                        total_iva += float(ln.get('f470_vlr_imp', 0))
                        total_neto += float(ln.get('f470_vlr_neto', 0))
                    datos_disponibles = True

                # Pedido cabecera: CO de la factura
                cabecera = connekta.get_pedido_cabecera(tipo_docto, consec_docto)
                if cabecera:
                    co_factura = cabecera.get('f430_id_co', '')
                    nit = cabecera.get('f200_id_pedido_fact', '')
                    # CxC account from API if available
                    try:
                        if nit:
                            cxc_data = connekta.get_cxc_general(nit)
                            # f253_id puede variar entre facturas del mismo
                            # cliente — matchear por la factura exacta, nunca
                            # tomar la primera fila (ver connekta_gateway.py,
                            # get_cxc_general).
                            fila_cxc = next((
                                r for r in cxc_data
                                if str(r.get('f353_id_tipo_docto_cruce', '')).strip() == tipo_docto
                                and str(r.get('f353_consec_docto_cruce', '')) == str(consec_docto)
                            ), None)
                            if fila_cxc:
                                cuenta_cxc = fila_cxc.get('f253_id', '')
                    except Exception as e_cxc:
                        logger.warning(
                            '[LIQUIDACION] get_cxc_general falló para recaudo %d (NIT %s): %s',
                            recaudo_id, nit, e_cxc
                        )
            except Exception as e:
                logger.warning(
                    '[LIQUIDACION] preview_acciones_recaudo: Siesa data fetch falló '
                    'para recaudo %d (FE %s-%s): %s',
                    recaudo_id, tipo_docto, consec_docto, e
                )
                datos_disponibles = False

        # ── Retenciones disponibles ─────────────────────────────────
        retenciones_disponibles = []
        for tipo_ret, tasa in RETENCION_TASA.items():
            # RETEIVA: base = total_iva, all others: base = base_gravable
            if tipo_ret == 'RETEIVA':
                base_calculo = total_iva
            else:
                base_calculo = base_gravable
            monto_estimado = round(base_calculo * tasa, 2) if datos_disponibles else 0
            retenciones_disponibles.append({
                'tipo': tipo_ret,
                'nombre': _nombre_retencion(tipo_ret),
                'puc': RETENCION_PUC[tipo_ret],
                'tasa': tasa,
                'base': base_calculo,
                'monto_estimado': monto_estimado,
            })

        # ── Siesa horario check (7am-8pm Colombia) ──────────────────
        try:
            from datetime import timezone, timedelta
            colombia_tz = timezone(timedelta(hours=-5))
            hora_colombia = datetime.now(colombia_tz).hour
            siesa_horario_ok = 7 <= hora_colombia < 20
        except Exception:
            siesa_horario_ok = True  # assume ok if tz check fails

        # ── SiesaJob states for this recaudo ─────────────────────────
        jobs_recaudo = SiesaJob.query.filter_by(
            referencia_tipo='RecaudoEntrega',
            referencia_id=recaudo_id,
        ).all()
        jobs_estado = {}
        for j in jobs_recaudo:
            jobs_estado[j.tipo] = {
                'job_id': j.id,
                'estado': j.estado,
                'intentos': j.intentos,
                'error_ultimo': j.error_ultimo,
            }

        # ── Determine pending actions ────────────────────────────────
        acciones_pendientes = []
        if estado in (EstadoEntrega.PARCIAL, EstadoEntrega.RECHAZADO):
            if not recaudo.siesa_nc_triggered:
                acciones_pendientes.append('NOTA_CREDITO_FACTURA')
        if forma_pago not in ('CREDITO', 'EXENTO', '') and estado != EstadoEntrega.RECHAZADO:
            if not recaudo.siesa_rc_triggered:
                acciones_pendientes.append('RECIBO_CAJA')
            if not recaudo.siesa_dc_triggered:
                acciones_pendientes.append('DOCUMENTO_CONTABLE_RET')

        return {
            'recaudo_id': recaudo_id,
            'estado_entrega': estado,
            'forma_pago': forma_pago,
            'datos_factura': {
                'base_gravable': base_gravable,
                'total_iva': total_iva,
                'total_neto': total_neto,
                'co_factura': co_factura,
                'cuenta_cxc': cuenta_cxc,
                'datos_disponibles': datos_disponibles,
            },
            'retenciones_disponibles': retenciones_disponibles,
            'acciones_pendientes': acciones_pendientes,
            'flags': {
                'siesa_nc_triggered': recaudo.siesa_nc_triggered or False,
                'siesa_rc_triggered': recaudo.siesa_rc_triggered or False,
                'siesa_dc_triggered': recaudo.siesa_dc_triggered or False,
            },
            'jobs_estado': jobs_estado,
            'siesa_horario_ok': siesa_horario_ok,
        }

    @staticmethod
    def enviar_nc_recaudo(recaudo_id: int, admin_id: int = None,
                          cantidades_verificadas: list = None) -> dict:
        """
        Enqueues NC (Nota Crédito) for a single recaudo.

        Used in per-recaudo liquidation flow. Only valid for PARCIAL/RECHAZADO.
        Idempotent: raises if NC already triggered.
        """
        recaudo = RecaudoEntrega.query.get(recaudo_id)
        if not recaudo:
            raise LookupError(f'RecaudoEntrega {recaudo_id} no encontrado')

        tarea = recaudo.tarea
        if not tarea:
            raise ValueError(f'Recaudo {recaudo_id} sin tarea asociada')

        # Validate estado
        estado = recaudo.estado_entrega
        if estado not in (EstadoEntrega.PARCIAL, EstadoEntrega.RECHAZADO):
            raise ValueError(
                f'NC solo aplica para PARCIAL/RECHAZADO, recaudo {recaudo_id} '
                f'está en estado {estado}'
            )

        # Idempotent guard
        if recaudo.siesa_nc_triggered:
            raise ValueError(
                f'NC ya fue disparada para recaudo {recaudo_id} — '
                'no se puede re-encolar (idempotencia)'
            )

        # La FE, no el pedido: `get_rowids_factura` filtra por `f350_*`
        # —el documento consultado— y acá se pasaba `*_pedido_siesa` con la
        # variable llamada `_fe`. Job 440 (2026-08-11): 400 de Siesa
        # buscando una factura de tipo 'PD'. Ver `fe_resolver`.
        from app.services.fe_resolver import FENoEncontrada, resolver_fe
        try:
            tipo_docto_fe, consec_fe = resolver_fe(tarea)
        except FENoEncontrada as _e_fe:
            tipo_docto_fe = consec_fe = ''
        if not tipo_docto_fe or not consec_fe:
            raise ValueError(
                f'Tarea {tarea.id} sin tipo_docto/consec_docto — '
                'no se puede vincular a factura Siesa'
            )

        # If verified quantities provided, update items_entregados
        if cantidades_verificadas is not None:
            recaudo.items_entregados = cantidades_verificadas

        # Determine items for NC
        items_devueltos = recaudo.items_entregados if estado == EstadoEntrega.PARCIAL else None

        notas = (
            f'Liquidación per-recaudo | NC recaudo #{recaudo_id} | '
            f'Estado: {estado} | Admin: {admin_id}'
        )

        # Pre-flag ANTES de encolar (patrón pre-flag estándar)
        recaudo.siesa_nc_triggered = True

        _encolar_nota_credito(
            recaudo, tipo_docto_fe, consec_fe,
            items_devueltos=items_devueltos,
            notas=notas,
            admin_id=admin_id,
        )

        # Add accion_origen to the last enqueued job's payload
        last_job = SiesaJob.query.filter_by(
            referencia_tipo='RecaudoEntrega',
            referencia_id=recaudo_id,
            tipo='NOTA_CREDITO_FACTURA',
        ).order_by(SiesaJob.id.desc()).first()

        if last_job:
            import json
            payload = json.loads(last_job.payload)
            payload['accion_origen'] = 'liquidacion_per_recaudo'
            last_job.payload = json.dumps(payload, ensure_ascii=False)

        db.session.commit()

        # Trigger immediate DLQ processing
        try:
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
        except Exception:
            pass

        logger.info(
            '[LIQUIDACION] enviar_nc_recaudo: recaudo %d, job %s encolado',
            recaudo_id, last_job.id if last_job else '?'
        )

        return {
            'ok': True,
            'job_id': last_job.id if last_job else None,
        }

    @staticmethod
    def registrar_cobro_recaudo(recaudo_id: int, admin_id: int = None,
                                retenciones: list = None,
                                monto_override: float = None) -> dict:
        """
        Enqueues RC + individual DCs for a single recaudo.

        Uses with_for_update() for concurrency protection.
        Validates sequencing (NC before RC for PARCIAL).
        Calculates retentions with correct bases (RETEIVA on IVA, others on base_gravable).
        """
        if retenciones is None:
            retenciones = []

        recaudo = db.session.query(RecaudoEntrega).with_for_update().get(recaudo_id)
        if not recaudo:
            raise LookupError(f'RecaudoEntrega {recaudo_id} no encontrado')

        tarea = recaudo.tarea
        if not tarea:
            raise ValueError(f'Recaudo {recaudo_id} sin tarea asociada')

        estado = recaudo.estado_entrega
        forma_pago = (recaudo.forma_pago or '').upper()

        # Validate: forma_pago not CREDITO/EXENTO
        if forma_pago in ('CREDITO', 'EXENTO'):
            raise ValueError(
                f'No se puede registrar cobro para recaudo {recaudo_id} '
                f'con forma_pago={forma_pago}'
            )

        # Idempotent guard
        if recaudo.siesa_rc_triggered:
            raise ValueError(
                f'RC ya fue disparado para recaudo {recaudo_id} — '
                'no se puede re-encolar (idempotencia)'
            )

        # If PARCIAL: NC must have been triggered first
        if estado == EstadoEntrega.PARCIAL and not recaudo.siesa_nc_triggered:
            raise ValueError(
                f'Recaudo {recaudo_id} es PARCIAL pero NC no ha sido disparada — '
                'secuencialidad: NC debe ir primero'
            )

        # La FE, no el pedido: `get_rowids_factura` filtra por `f350_*`
        # —el documento consultado— y acá se pasaba `*_pedido_siesa` con la
        # variable llamada `_fe`. Job 440 (2026-08-11): 400 de Siesa
        # buscando una factura de tipo 'PD'. Ver `fe_resolver`.
        from app.services.fe_resolver import FENoEncontrada, resolver_fe
        try:
            tipo_docto_fe, consec_fe = resolver_fe(tarea)
        except FENoEncontrada as _e_fe:
            tipo_docto_fe = consec_fe = ''
        if not tipo_docto_fe or not consec_fe:
            raise ValueError(
                f'Tarea {tarea.id} sin tipo_docto/consec_docto — '
                'no se puede vincular a factura Siesa'
            )

        # ── Get real Siesa data ─────────────────────────────────────
        from app.services.connekta_gateway import connekta
        co_factura = ''
        cuenta_cxc = ''
        base_gravable = 0
        total_iva = 0
        total_neto = 0
        datos_siesa_ok = False

        try:
            # Factura lines
            lineas_raw = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
            if lineas_raw:
                for ln in lineas_raw:
                    base_gravable += float(ln.get('f470_vlr_bruto', 0))
                    total_iva += float(ln.get('f470_vlr_imp', 0))
                    total_neto += float(ln.get('f470_vlr_neto', 0))
                datos_siesa_ok = True

            # Pedido cabecera: CO + NIT
            cabecera = connekta.get_pedido_cabecera(tipo_docto_fe, consec_fe)
            if cabecera:
                co_factura = cabecera.get('f430_id_co', '')
                nit = cabecera.get('f200_id_pedido_fact', '')
                sucursal = cabecera.get('f461_id_sucursal_pedido_rem', '001')
                # CxC account
                try:
                    if nit:
                        cxc_data = connekta.get_cxc_general(nit)
                        # f253_id puede variar entre facturas del mismo
                        # cliente — matchear por la factura exacta.
                        # OJO con la asimetría: `get_rowids_factura` necesita
                        # la FACTURA (filtra por f350_*), pero el cruce de CxC
                        # referencia el PEDIDO — `f353_*_docto_cruce` trae
                        # 'PD'/consec del pedido, verificado en vivo el
                        # 2026-08-11. Usar acá la FE resuelta hace que no
                        # matchee ninguna fila y la cuenta caiga al fallback
                        # `SIESA_CXC_AUXILIAR`, que es la regla 11 al revés.
                        _tipo_cruce = tarea.tipo_docto_pedido_siesa or ''
                        _consec_cruce = tarea.consec_docto_pedido_siesa or ''
                        fila_cxc = next((
                            r for r in cxc_data
                            if str(r.get('f353_id_tipo_docto_cruce', '')).strip() == _tipo_cruce
                            and str(r.get('f353_consec_docto_cruce', '')) == str(_consec_cruce)
                        ), None)
                        if fila_cxc:
                            cuenta_cxc = fila_cxc.get('f253_id', '')
                except Exception as e_cxc:
                    logger.warning(
                        '[LIQUIDACION] get_cxc_general falló para recaudo %d: %s',
                        recaudo_id, e_cxc
                    )
            else:
                nit = ''
                sucursal = '001'
        except Exception as e:
            logger.error(
                '[LIQUIDACION] registrar_cobro_recaudo: Siesa data fetch falló '
                'para recaudo %d: %s', recaudo_id, e
            )
            raise ValueError(f'Datos de Siesa no disponibles: {e}')

        # co_factura and cuenta_cxc are critical for RC cruce
        if not co_factura:
            raise ValueError(
                'Datos de Siesa no disponibles: co_factura vacío — '
                'no se puede crear cruce RC'
            )
        # cuenta_cxc can fall back to env var in connekta, but warn
        if not cuenta_cxc:
            logger.warning(
                '[LIQUIDACION] cuenta_cxc vacía para recaudo %d — '
                'RC usará fallback SIESA_CXC_AUXILIAR', recaudo_id
            )

        # ── Determine monto ─────────────────────────────────────────
        if monto_override is not None:
            monto = float(monto_override)
        elif datos_siesa_ok and total_neto > 0:
            monto = total_neto
        else:
            monto = float(recaudo.monto_cobrado or 0)

        # ── Calculate retentions ────────────────────────────────────
        import json
        dc_jobs_info = []
        retenciones_detalle = []
        total_retenciones = 0

        if retenciones:
            # Validate base is available
            if not datos_siesa_ok and base_gravable <= 0:
                raise ValueError(
                    'Base gravable no disponible — no se pueden calcular retenciones'
                )

            for ret in retenciones:
                tipo_ret = ret.get('tipo', '')
                cuenta_puc = RETENCION_PUC.get(tipo_ret, '')
                tasa = RETENCION_TASA.get(tipo_ret, 0)

                if not cuenta_puc:
                    logger.error(
                        '[LIQUIDACION] tipo retención %s sin PUC mapeado — omitido',
                        tipo_ret
                    )
                    continue
                if not tasa:
                    logger.error(
                        '[LIQUIDACION] tipo retención %s sin tasa — omitido',
                        tipo_ret
                    )
                    continue

                # RETEIVA: base = total_iva; others: base = base_gravable
                if tipo_ret == 'RETEIVA':
                    base_ret = total_iva
                else:
                    base_ret = base_gravable

                monto_ret = round(base_ret * tasa, 2)
                if monto_ret <= 0:
                    logger.warning(
                        '[LIQUIDACION] retención %s monto=0 (base=%.2f, tasa=%.4f) — omitido',
                        tipo_ret, base_ret, tasa
                    )
                    continue

                total_retenciones += monto_ret

                # Enqueue individual DC SiesaJob directly
                dc_notas = (
                    f'Liquidación per-recaudo | DC recaudo #{recaudo_id} | '
                    f'Retención {tipo_ret} | Admin: {admin_id}'
                )
                dc_job = SiesaJob.encolar(
                    tipo='DOCUMENTO_CONTABLE_RET',
                    payload={
                        'recaudo_id': recaudo_id,
                        'tipo_docto_fe': tipo_docto_fe,
                        'consec_fe': str(consec_fe),
                        'tercero_nit': nit,
                        'sucursal': sucursal,
                        'cuenta_puc': cuenta_puc,
                        'monto': monto_ret,
                        'base_gravable': base_ret,
                        'co_factura': co_factura,
                        'cuenta_cxc': cuenta_cxc,
                        'notas': dc_notas,
                        'accion_origen': 'liquidacion_per_recaudo',
                    },
                    referencia_tipo='RecaudoEntrega',
                    referencia_id=recaudo_id,
                    creado_por_id=admin_id,
                )
                # Flush to get dc_job.id
                db.session.flush()

                dc_jobs_info.append({
                    'tipo': tipo_ret,
                    'job_id': dc_job.id,
                    'monto': monto_ret,
                })
                retenciones_detalle.append({
                    'tipo': tipo_ret,
                    'puc': cuenta_puc,
                    'tasa': tasa,
                    'monto': monto_ret,
                    'base': base_ret,
                    'siesa_triggered': True,
                    'job_id': dc_job.id,
                })

                logger.info(
                    '[LIQUIDACION] Encolado DC individual recaudo %d: %s PUC %s $%.2f',
                    recaudo_id, tipo_ret, cuenta_puc, monto_ret
                )

        # ── Calculate monto_neto_rc ─────────────────────────────────
        monto_neto_rc = round(monto - total_retenciones, 2)

        # ── Enqueue RC ──────────────────────────────────────────────
        rc_notas = (
            f'Liquidación per-recaudo | RC recaudo #{recaudo_id} | '
            f'Monto neto: ${monto_neto_rc:.2f} | Admin: {admin_id}'
        )
        depende_de_nc = (estado == EstadoEntrega.PARCIAL)

        _encolar_recibo_caja(
            recaudo, tipo_docto_fe, consec_fe,
            nit, sucursal, monto_neto_rc, forma_pago,
            notas=rc_notas,
            admin_id=admin_id,
            depende_de_nc=depende_de_nc,
            co_factura=co_factura,
            cuenta_cxc=cuenta_cxc,
        )

        # Add accion_origen to RC job payload
        rc_job = SiesaJob.query.filter_by(
            referencia_tipo='RecaudoEntrega',
            referencia_id=recaudo_id,
            tipo='RECIBO_CAJA',
        ).order_by(SiesaJob.id.desc()).first()

        if rc_job:
            payload_rc = json.loads(rc_job.payload)
            payload_rc['accion_origen'] = 'liquidacion_per_recaudo'
            rc_job.payload = json.dumps(payload_rc, ensure_ascii=False)

        # Save retenciones_detalle on recaudo
        if retenciones_detalle:
            recaudo.retenciones_detalle = retenciones_detalle

        db.session.commit()

        # Trigger immediate DLQ processing
        try:
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
        except Exception:
            pass

        logger.info(
            '[LIQUIDACION] registrar_cobro_recaudo: recaudo %d — '
            'RC job %s ($%.2f neto), %d DCs encolados',
            recaudo_id, rc_job.id if rc_job else '?',
            monto_neto_rc, len(dc_jobs_info)
        )

        return {
            'ok': True,
            'rc_job_id': rc_job.id if rc_job else None,
            'dc_jobs': dc_jobs_info,
            'monto_neto_rc': monto_neto_rc,
        }

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
            f'Fecha: {_ahora_bogota().strftime("%Y-%m-%d")}'
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
    # La FE, no el pedido: `get_rowids_factura` filtra por `f350_*`
    # —el documento consultado— y acá se pasaba `*_pedido_siesa` con la
    # variable llamada `_fe`. Job 440 (2026-08-11): 400 de Siesa
    # buscando una factura de tipo 'PD'. Ver `fe_resolver`.
    from app.services.fe_resolver import FENoEncontrada, resolver_fe
    try:
        tipo_docto_fe, consec_fe = resolver_fe(tarea)
    except FENoEncontrada as _e_fe:
        tipo_docto_fe = consec_fe = ''
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
    if estado == EstadoEntrega.RECHAZADO:
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
    if es_credito and estado == EstadoEntrega.ENTREGADO:
        resultado['credito'] = 1
        return resultado

    # ── CRÉDITO + PARCIAL: solo NC ───────────────────────────────
    if es_credito and estado == EstadoEntrega.PARCIAL:
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
    if not es_credito and estado == EstadoEntrega.PARCIAL:
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
    if not es_credito and estado == EstadoEntrega.ENTREGADO:
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
                          admin_id: int = None, depende_de_nc: bool = False,
                          co_factura: str = '', cuenta_cxc: str = ''):
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
            'co_factura': co_factura,
            'cuenta_cxc': cuenta_cxc,
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
                                  notas: str, admin_id: int = None,
                                  co_factura: str = '', cuenta_cxc: str = ''):
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
            'co_factura': co_factura,
            'cuenta_cxc': cuenta_cxc,
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


# Nombres legibles para las retenciones
_NOMBRES_RETENCION = {
    'RETEFUENTE_2.5':   'Retefuente Compras 2.5%',
    'RETEFUENTE_1.5':   'Retefuente Bancos 1.5%',
    'RETEIVA':          'ReteIVA 15%',
    'ICA_3':            'ICA 3x1000',
    'ICA_4.14':         'ICA 4.14x1000',
    'ICA_6.9':          'ICA 6.9x1000',
    'ICA_8':            'ICA 8x1000',
    'ICA_11.04':        'ICA 11.04x1000',
    'AUTORETENCION_ICA_3':    'Autoretención ICA 3x1000',
    'AUTORETENCION_ICA_4.14': 'Autoretención ICA 4.14x1000',
    'AUTORETENCION_ICA_6.9':  'Autoretención ICA 6.9x1000',
    'AUTORETENCION_ICA_8':    'Autoretención ICA 8x1000',
    'AUTORETENCION_ICA_11.04':'Autoretención ICA 11.04x1000',
}


def _nombre_retencion(tipo: str) -> str:
    return _NOMBRES_RETENCION.get(tipo, tipo)
