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


# Catálogo de retenciones — FUENTE ÚNICA (tipo → puc/tasa/nombre).
# Verificado 2026-08-12 contra el gestor de cartera real de Papelería Medellín
# (captura del maestro de cuentas). El catálogo anterior vivía partido en 3
# diccionarios paralelos (PUC/tasa/nombre) sincronizados solo a mano por
# compartir las mismas claves — así fue como la tasa correcta terminó pegada
# a la cuenta PUC equivocada para las 5 cuentas ICA (13551801-805) sin que
# nada lo detectara. RETENCION_PUC/RETENCION_TASA/_NOMBRES_RETENCION abajo
# son vistas derivadas de este dict, nunca se editan por separado.
# 13551800 (ICA 2024) y 13559500 (AUTORRETENCION 2023) quedan fuera: son
# cuentas legacy sin tasa x1000 en el nombre, no hay dato confiable para
# mapearlas — Regla 0, ante dato ausente no se inventa.
CATALOGO_RETENCIONES = {
    'RETEFUENTE_2.5': {'puc': '13551501', 'tasa': 0.025,  'nombre': 'Retención por Compras 2.5%'},
    'RETEFUENTE_1.5': {'puc': '13551502', 'tasa': 0.015,  'nombre': 'Retención Bancos 1.5%'},
    'RETEIVA':        {'puc': '13551701', 'tasa': 0.15,   'nombre': 'ReteIVA Ventas 15%'},
    'ICA_4X1000':     {'puc': '13551801', 'tasa': 0.004,  'nombre': 'ICA Retenido a Favor 4x1000'},
    'ICA_3X1000':     {'puc': '13551802', 'tasa': 0.003,  'nombre': 'ICA Retenido a Favor 3x1000'},
    'ICA_6X1000':     {'puc': '13551803', 'tasa': 0.006,  'nombre': 'ICA Retenido a Favor 6x1000'},
    'ICA_10X1000':    {'puc': '13551804', 'tasa': 0.010,  'nombre': 'ICA Retenido a Favor 10x1000'},
    'ICA_11X1000':    {'puc': '13551805', 'tasa': 0.011,  'nombre': 'ICA Retenido a Favor 11x1000'},
    'AUTORRETENCION_ICA_NEIVA_3X1000':    {'puc': '13559501', 'tasa': 0.003,  'nombre': 'Autorretención ICA Neiva 3x1000'},
    'AUTORRETENCION_ICA_NEIVA_3.5X1000':  {'puc': '13559502', 'tasa': 0.0035, 'nombre': 'Autorretención ICA Neiva 3.5x1000'},
    'AUTORRETENCION_ICA_NEIVA_4.5X1000':  {'puc': '13559503', 'tasa': 0.0045, 'nombre': 'Autorretención ICA Neiva 4.5x1000'},
    'AUTORRETENCION_ICA_NEIVA_8X1000':    {'puc': '13559504', 'tasa': 0.008,  'nombre': 'Autorretención ICA Neiva 8x1000'},
    'AUTORRETENCION_ICA_PITALITO_4X1000': {'puc': '13559505', 'tasa': 0.004,  'nombre': 'Autorretención ICA Pitalito 4x1000'},
}

# Vistas derivadas — solo lectura, generadas del catálogo único de arriba.
RETENCION_PUC  = {k: v['puc']  for k, v in CATALOGO_RETENCIONES.items()}
RETENCION_TASA = {k: v['tasa'] for k, v in CATALOGO_RETENCIONES.items()}


def base_de_retencion(tipo_ret: str, base_gravable: float, total_iva: float) -> float:
    """Sobre qué valor se calcula esa retención. **Una función.**

    El reteIVA va sobre el **IVA**; todo lo demás sobre el subtotal. Parece
    trivial y estaba escrito en tres sitios, uno de ellos así:

        base_gravable * 0.19 * tasa        ← rutas.py, hasta el 2026-08-13

    Eso **inventa** un IVA del 19% sobre el subtotal en vez de usar el que
    Siesa reporta. Es exactamente lo que CLAUDE.md prohíbe —«usar API 45
    (`f461_vlr_bruto`, `f461_vlr_imp`), NO dividir por 1.19»— en su forma
    multiplicativa.

    En una factura con líneas **exentas** el IVA real es menor que el 19% del
    subtotal, así que la retención salía inflada: plata de más retenida a un
    cliente, en un documento contable que alguien tiene que corregir a mano.
    """
    return float(total_iva or 0) if tipo_ret == 'RETEIVA' else float(base_gravable or 0)


def monto_de_retencion(tipo_ret: str, base_gravable: float, total_iva: float) -> float:
    """El valor a retener, redondeado a centavos."""
    tasa = RETENCION_TASA.get(tipo_ret, 0)
    if not tasa:
        return 0.0
    return round(base_de_retencion(tipo_ret, base_gravable, total_iva) * tasa, 2)


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
        from app.models.devolucion_cliente import DevolucionCliente

        retenciones_disponibles = [
            {'tipo': k, 'nombre': v['nombre'], 'puc': v['puc'], 'tasa': v['tasa']}
            for k, v in CATALOGO_RETENCIONES.items()
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
            # `siesa_rc_triggered` se enciende recién cuando el DLQ procesa
            # el job (pre-flag), no al encolar — la pantalla necesita saber
            # "ya se pidió" para ocultar el botón de inmediato tras el
            # primer clic, sin esperar a que el DLQ corra. Ver
            # `_hay_rc_en_cola` / el guard nuevo en `registrar_cobro_recaudo`.
            rd['rc_en_cola'] = _hay_rc_en_cola(recaudo.id)

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

            # Parcial/Rechazado ya no disparan la NC directo — arman una
            # DevolucionCliente pendiente que recepción confirma (ver
            # _crear_devolucion_pendiente). El admin ve acá si ya se envió.
            devolucion = (DevolucionCliente.query
                          .filter_by(recaudo_entrega_id=recaudo.id)
                          .filter(DevolucionCliente.estado != 'CANCELADA')
                          .first())
            rd['devolucion_pendiente'] = (
                {'id': devolucion.id, 'codigo': devolucion.codigo, 'estado': devolucion.estado}
                if devolucion else None
            )

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
                            # cliente — matchear por la exacta, nunca tomar la
                            # primera fila. La búsqueda vive en
                            # `services/cxc_cruce.py`: era la TERCERA copia de
                            # la misma consulta en el repo, y una de las tres
                            # buscaba por la clave equivocada.
                            from app.services import cxc_cruce as _cx3
                            fila_cxc = _cx3.fila_de_la_factura(
                                cxc_data, tipo_docto, consec_docto,
                                _tipo_fe, _consec_fe)
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
        for tipo_ret, datos_ret in CATALOGO_RETENCIONES.items():
            tasa = datos_ret['tasa']
            base_calculo = base_de_retencion(tipo_ret, base_gravable, total_iva)
            monto_estimado = (monto_de_retencion(tipo_ret, base_gravable, total_iva)
                              if datos_disponibles else 0)
            retenciones_disponibles.append({
                'tipo': tipo_ret,
                'nombre': datos_ret['nombre'],
                'puc': datos_ret['puc'],
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
        #
        # `ENTREGADO_SIN_PAGO` no genera NINGÚN documento y se declara acá de
        # forma explícita. Sin esta línea el caso quedaba fuera «por
        # casualidad» —su `forma_pago` es `None`, que cae en la lista de
        # exclusión de abajo— y una parada de ese estado a la que alguien le
        # pusiera forma de pago habría propuesto un recibo de caja por plata
        # que nadie recibió.
        acciones_pendientes = []
        if estado != EstadoEntrega.ENTREGADO_SIN_PAGO:
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
            # Lo que el conductor declaró haber recibido. La pantalla lo
            # compara contra el neto de Siesa y, en PARCIAL, lo usa como
            # monto por defecto del RC (ver `registrar_cobro_recaudo`).
            # Faltaba en el payload: el front leía `preview.monto_cobrado`
            # y siempre recibía `undefined` → "Conductor: $0".
            'monto_cobrado': float(recaudo.monto_cobrado or 0),
            'datos_factura': {
                'base_gravable': base_gravable,
                'total_iva': total_iva,
                'total_neto': total_neto,
                'co_factura': co_factura,
                'cuenta_cxc': cuenta_cxc,
                'datos_disponibles': datos_disponibles,
            },
            'retenciones_disponibles': retenciones_disponibles,
            # Motivo que el conductor eligió en campo (Pago Parcial, pantalla
            # de última milla) — el admin lo ve premarcado abajo pero decide:
            # puede quitarlo o cambiarlo antes de confirmar el cobro.
            'motivo_descuento_sugerido': recaudo.motivo_descuento or '',
            # Cuánto se descontó. Con esto la pantalla calcula lo que el
            # cliente DEBÍA pagar si el descuento resulta improcedente
            # (`monto_cobrado + monto_descuento`) — la misma referencia que
            # usa el guard de `registrar_cobro_recaudo`, no el neto de la
            # factura, que en un PARCIAL incluye lo devuelto.
            'monto_descuento': float(recaudo.monto_descuento or 0),
            # None = pendiente de decisión (bloquea el RC), True = confirmada,
            # False = rechazada (bloquea el RC hasta pagar el valor completo).
            'retencion_confirmada': recaudo.retencion_confirmada,
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
    def confirmar_retencion(recaudo_id: int, admin_id: int, confirmar: bool) -> dict:
        """Decisión explícita del admin sobre el `motivo_descuento` que el
        conductor declaró en campo — ver el campo en el modelo y el guard en
        `registrar_cobro_recaudo`. `confirmar=True` deja seguir el flujo
        normal (RC neto + DC de retención); `False` bloquea el RC hasta que
        el monto usado alcance el valor completo de la factura.
        """
        recaudo = db.session.query(RecaudoEntrega).with_for_update().get(recaudo_id)
        if not recaudo:
            raise LookupError(f'RecaudoEntrega {recaudo_id} no encontrado')
        if not recaudo.motivo_descuento:
            raise ValueError(
                f'Recaudo {recaudo_id} no tiene motivo de retención declarado '
                'por el conductor — no hay nada que confirmar')
        if recaudo.siesa_rc_triggered:
            raise ValueError(
                f'RC ya fue disparado para recaudo {recaudo_id} — la decisión '
                'ya no aplica')

        recaudo.retencion_confirmada = bool(confirmar)
        recaudo.retencion_confirmada_por = admin_id
        recaudo.retencion_confirmada_en = datetime.utcnow()
        db.session.commit()
        logger.info(
            '[LIQUIDACION] Retención %s para recaudo %d (motivo %s) por admin %s',
            'CONFIRMADA' if confirmar else 'RECHAZADA', recaudo_id,
            recaudo.motivo_descuento, admin_id
        )
        return recaudo.to_dict()

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

        # El ESTADO manda, no solo la forma de pago.
        #
        # Se validaba `forma_pago` y no el estado, así que una petición directa
        # registraba un cobro —y disparaba un RC real a Siesa— sobre una parada
        # RECHAZADA (no se entregó nada) o ENTREGADO_SIN_PAGO (se entregó y el
        # cliente no pagó). Plata que no existe, en un documento financiero.
        #
        # La validación vive acá y no en la ruta porque el endpoint no es la
        # única puerta: lo mismo que acaba de costar el guard de packing.
        if estado not in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL):
            raise ValueError(
                f'No se puede registrar cobro sobre una parada {estado}: '
                f'solo ENTREGADO o PARCIAL representan dinero recibido')

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

        # `siesa_rc_triggered` se enciende recién cuando el DLQ PROCESA el
        # job (pre-flag, justo antes del POST) — no cuando se encola. Entre
        # un primer clic en "Registrar Cobro" y ese momento (el DLQ corre
        # cada 1 min salvo que `disparar_dlq_inmediato` ya haya alcanzado a
        # correr) el guard de arriba no ve nada y un segundo clic encola un
        # segundo RECIBO_CAJA para el mismo recaudo. Mirar la cola, no solo
        # lo enviado — mismo patrón que ya costó un duplicado real en
        # DOCUMENTO_CONTABLE_RET (`_pucs_en_cola`).
        if _hay_rc_en_cola(recaudo_id):
            raise ValueError(
                f'Ya hay un Recibo de Caja en cola para el recaudo {recaudo_id} '
                '— espera a que se procese antes de volver a intentar '
                '(evita duplicar el RC)'
            )

        # PARCIAL sin NC disparada: NO bloquea la creación del RC (2026-08-19).
        #
        # Antes esto era un ValueError duro — el admin no podía ni encolar el
        # RC mientras recepción no confirmara físicamente la devolución
        # (horas o días). El RC no necesita esperar: es por lo que el
        # conductor SÍ entregó (ver el cálculo de `monto` más abajo), un
        # documento distinto de la NC, que es por lo que volvió.
        #
        # La Regla 7 («NC → RC, nunca en paralelo contra la misma factura —
        # deadlock T353 en SQL Server») sigue intacta: `depende_de_nc` más
        # abajo encola el job igual, y es el DLQ (`DependenciaPendiente`,
        # sin gastar reintento) el que espera a que la NC dispare antes de
        # postear el RC a Siesa — el mismo patrón que ya usa
        # `_procesar_recaudo` (el botón masivo "Enviar a Siesa" de Rutas)
        # desde antes. Esta vía nunca lo había adoptado.

        # Retención declarada en campo — decisión del admin obligatoria.
        #
        # `motivo_descuento` es lo que el CONDUCTOR anotó (lo que el cliente
        # dijo, sin verificar). Antes, en Liquidación eso era solo una
        # casilla premarcada "sugerida" — nada impedía crear el RC sin que
        # nadie se pronunciara sobre si el cliente de verdad tenía derecho al
        # descuento. Ver `confirmar_retencion()`.
        if recaudo.motivo_descuento and recaudo.retencion_confirmada is None:
            raise ValueError(
                f'El conductor declaró un motivo de retención '
                f'({recaudo.motivo_descuento}) — confírmalo o recházalo '
                'antes de registrar el cobro'
            )
        if recaudo.motivo_descuento and recaudo.retencion_confirmada is False and any(
                (r.get('tipo') if isinstance(r, dict) else r) == recaudo.motivo_descuento
                for r in (retenciones or [])):
            raise ValueError(
                f'La retención {recaudo.motivo_descuento} fue rechazada — '
                'no se puede aplicar en este cobro'
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
        un_cxc = ''
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

            # Pedido cabecera: CO + NIT. El PEDIDO, no la FE — mismo defecto
            # que el comentario de arriba, reaparecido acá: `get_pedido_cabecera`
            # busca por *_pedido_siesa, y mandarle el tipo/consec de la FE
            # ('FEW'-1416, por ejemplo) no encuentra ningún pedido con ese
            # tipo de documento. Siesa responde vacío, `co_factura` se queda
            # en '' y el RC nunca se puede encolar — "co_factura vacío" no es
            # un dato que falte en Siesa, es la pregunta mal hecha.
            cabecera = connekta.get_pedido_cabecera(
                tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa)
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
                        # casi siempre referencia el PEDIDO — `f353_*_docto_cruce`
                        # trae 'PD'/consec del pedido, verificado en vivo el
                        # 2026-08-11. PD1411/FE-1416 (2026-08-18) probó que no
                        # es universal: esa cartera venía indexada por la FE.
                        # `fila_de_la_factura` prueba pedido primero y cae a
                        # FE si no matchea, en vez de quedar en el fallback
                        # `SIESA_CXC_AUXILIAR` (regla 11 al revés).
                        # La búsqueda vive en `services/cxc_cruce.py`. Estaba
                        # escrita acá y otra vez en `siesa_job_service`, con
                        # claves DISTINTAS y las dos citando esta misma
                        # verificación en vivo — y la de allá decidía si un
                        # recibo de caja se reenviaba.
                        # `f353_id_un_cruce` — Unidad de Negocio REAL de esa
                        # fila, no `SIESA_UNIDAD_NEGOCIO` (global). PD1411/
                        # FE-1416 (2026-08-18): la UN real era 99, el env var
                        # mandaba 001 en todo — Siesa rechazó el RC dos veces
                        # por el mismo motivo («UN diferente a la del
                        # documento» y «documento de cruce no existe», la
                        # clave compuesta no podía matchear con la UN mala).
                        # `gestor-cartera-pame` ya lo resuelve así.
                        from app.services import cxc_cruce as _cx
                        fila_cxc = _cx.fila_de_la_factura(
                            cxc_data, tarea.tipo_docto_pedido_siesa,
                            tarea.consec_docto_pedido_siesa,
                            tipo_docto_fe, consec_fe)
                        if fila_cxc:
                            cuenta_cxc = fila_cxc.get('f253_id', '')
                            un_cxc = fila_cxc.get('f353_id_un_cruce', '')
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
        #
        # PARCIAL: el RC es por lo que el conductor SÍ entregó al admin
        # (`monto_cobrado`) — NUNCA `total_neto`, que es el valor COMPLETO de
        # la factura en Siesa sin descontar lo devuelto. La factura misma no
        # se toca (ni el RC ni la NC la editan): son documentos de cruce
        # aparte. Usar `total_neto` acá facturaría de más el cobro; la
        # diferencia (lo devuelto) la cierra la NC contra la factura, no el
        # RC. ENTREGADO sí prefiere `total_neto` — sin devolución de por
        # medio, es el dato verificado contra Siesa, más confiable que lo que
        # el conductor tecleó.
        if monto_override is not None:
            monto = float(monto_override)
        elif estado == EstadoEntrega.PARCIAL:
            monto = float(recaudo.monto_cobrado or 0)
        elif datos_siesa_ok and total_neto > 0:
            monto = total_neto
        else:
            monto = float(recaudo.monto_cobrado or 0)

        # Retención rechazada: el cliente debe pagar lo que le correspondía.
        # No hay un segundo estado de "ya pagó el resto" en el WMS a propósito
        # (ver el campo en el modelo): el admin corrige el monto cuando el
        # dinero llegó y el bloqueo se resuelve solo.
        #
        # La referencia NO es `total_neto`. En una entrega PARCIAL el cliente
        # devolvió mercancía, así que nunca va a pagar el neto completo de la
        # factura — exigirlo dejaba esos pedidos trabados para siempre, sin
        # ninguna salida en la pantalla. Lo que sí debía pagar es la parte que
        # se quedó: `monto_cobrado + monto_descuento`, o sea el "valor a
        # cobrar" que el conductor tenía en la puerta antes de restarle el
        # descuento que ahora el admin declaró improcedente. En un ENTREGADO
        # esa suma es el neto de la factura, así que la regla es una sola para
        # ambos estados.
        #
        # `monto_cobrado` no se reescribe con el override (el override solo
        # decide `monto`, arriba), así que la referencia no se mueve por
        # editar el monto: es exactamente lo que se busca.
        if recaudo.motivo_descuento and recaudo.retencion_confirmada is False:
            descuento_declarado = float(recaudo.monto_descuento or 0)
            if descuento_declarado > 0:
                esperado = float(recaudo.monto_cobrado or 0) + descuento_declarado
            elif datos_siesa_ok and total_neto > 0:
                # Sin monto declarado (el motivo pudo entrar por la liquidación
                # masiva, que no guarda cuánto se descontó) queda el neto de la
                # factura como única referencia disponible.
                esperado = total_neto
            else:
                raise ValueError(
                    'Retención rechazada y sin monto de descuento declarado ni '
                    'datos de Siesa para verificar cuánto debía pagar el '
                    'cliente — reintenta cuando Siesa esté disponible'
                )
            from app.services.cxc_cruce import TOLERANCIA as _TOL
            if monto < esperado - _TOL:
                raise ValueError(
                    f'Retención rechazada — el cliente debe pagar el valor '
                    f'completo (${esperado:,.2f}). Monto actual: '
                    f'${monto:,.2f}. Ajusta el monto cuando el cliente pague '
                    'la diferencia.'
                )

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

                base_ret = base_de_retencion(tipo_ret, base_gravable, total_iva)
                monto_ret = monto_de_retencion(tipo_ret, base_gravable, total_iva)
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
                        # Misma fila de cartera que ya resuelve el RC (`un_cxc`,
                        # `f353_id_un_cruce`) — sin esto el DC caía al fallback
                        # global de `connekta.trigger_documento_contable`
                        # (`SIESA_UNIDAD_NEGOCIO`), el mismo defecto que ya
                        # rechazó el RC hermano (142888) el 2026-08-18. Job 470
                        # (recaudo 19, PD1421, ruta 22, 2026-08-20) es la
                        # primera liquidación con retención real: quedó
                        # FALLIDO 5/5 con rechazo estructural de Siesa.
                        'unidad_negocio': un_cxc,
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
            unidad_negocio=un_cxc,
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

    @staticmethod
    def crear_devoluciones_pendientes_ruta(ruta_id: int) -> dict:
        """
        Arma la DevolucionCliente pendiente (ver _crear_devolucion_pendiente)
        para cada recaudo PARCIAL/RECHAZADO de la ruta. Llamada automáticamente
        desde RutaService.liquidar_ruta/forzar_cierre_ruta — así la devolución
        cae sola al módulo de Devoluciones al liquidar, sin que el admin tenga
        que dispararla a mano recaudo por recaudo (ese paso manual se perdió
        al eliminar el botón "Enviar Nota Crédito (NC)" del módulo Liquidación
        sin dejar reemplazo — bug real detectado en producción, ruta #16 /
        PD1350: 0 SiesaJobs, 0 devoluciones creadas).

        A propósito NO toca RC/DC — esos siguen siendo manuales en el módulo
        Liquidación (ahí el admin revisa retenciones y el monto Siesa vs. lo
        que declaró el conductor antes de confirmar el cobro; automatizarlos
        quitaría esa revisión).

        Idempotente (_crear_devolucion_pendiente no duplica si ya existe) y
        nunca lanza — un recaudo con error (p.ej. FE no resuelta) queda para
        el flujo manual de respaldo (Rutas → "Enviar a Siesa") en vez de
        tumbar la liquidación completa.
        """
        from app.services.fe_resolver import FENoEncontrada, resolver_fe

        recaudos = (RecaudoEntrega.query
                    .filter_by(ruta_id=ruta_id)
                    .filter(RecaudoEntrega.estado_entrega.in_(
                        [EstadoEntrega.RECHAZADO, EstadoEntrega.PARCIAL]))
                    .all())
        resumen = {'creadas': 0, 'errores': []}

        for recaudo in recaudos:
            if recaudo.siesa_nc_triggered:
                continue
            tarea = recaudo.tarea
            if not tarea:
                continue
            try:
                tipo_docto_fe, consec_fe = resolver_fe(tarea)
            except FENoEncontrada:
                tipo_docto_fe = consec_fe = ''
            if not tipo_docto_fe or not consec_fe:
                logger.warning(
                    '[LIQUIDACION] crear_devoluciones_pendientes_ruta: recaudo %d '
                    'sin FE resuelta — queda para el flujo manual', recaudo.id
                )
                continue

            items = (None if recaudo.estado_entrega == EstadoEntrega.RECHAZADO
                      else recaudo.items_entregados)
            try:
                if _crear_devolucion_pendiente(
                    recaudo, tarea, tipo_docto_fe, consec_fe,
                    items_devueltos=items,
                    notas=f'WMS Ruta #{ruta_id} | Liquidar en WMS | {recaudo.estado_entrega}',
                ):
                    resumen['creadas'] += 1
            except Exception as e:
                logger.error(
                    '[LIQUIDACION] crear_devoluciones_pendientes_ruta: recaudo %d: %s',
                    recaudo.id, e
                )
                resumen['errores'].append({'recaudo_id': recaudo.id, 'error': str(e)})

        db.session.commit()
        logger.info(
            '[LIQUIDACION] crear_devoluciones_pendientes_ruta: ruta %d — '
            '%d devolución(es) creada(s), %d error(es)',
            ruta_id, resumen['creadas'], len(resumen['errores'])
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
      CONTADO + PARCIAL                   → devolución pendiente → RC espera esa NC
      CRÉDITO + ENTREGADO                 → noop (queda en cartera para Gestor)
      CRÉDITO + PARCIAL                   → devolución pendiente solamente
      RECHAZADO                           → devolución pendiente total

    "Devolución pendiente" (ver _crear_devolucion_pendiente): PARCIAL/RECHAZADO
    ya no disparan la NC (250696) directo desde acá — arman una
    DevolucionCliente ABIERTA que la recepcionista confirma físicamente en
    Devoluciones. Esa confirmación dispara la NC real (251126, con cruce
    automático de cartera) y marca recaudo.siesa_nc_triggered=True (bridge en
    siesa_job_service.py), que es lo que destraba el RC dependiente.
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
    resultado = {'rc': 0, 'nc': 0, 'dc': 0, 'credito': 0, 'ya_procesado': 0,
                 # Cuenta aparte y no dentro de `credito`: un crédito lo
                 # autorizó alguien; esto no lo autorizó nadie.
                 'sin_pago': 0}

    # ── ENTREGADO_SIN_PAGO: la excepción. No se automatiza nada. ─────────
    #
    # La mercancía se entregó y el cliente no pagó. No hay nota crédito —nada
    # volvió que devolver— ni recibo de caja —no entró plata—. **La factura ya
    # existe y queda abierta en cartera**, que es exactamente donde el Gestor
    # la ve.
    #
    # Lo que no debe pasar automáticamente es que esto se trate como crédito
    # otorgado: nadie evaluó a ese cliente. La parada llega marcada, quien
    # liquida la ve y decide si escala (BK-OPS-01 §3.5).
    if estado == EstadoEntrega.ENTREGADO_SIN_PAGO:
        resultado['sin_pago'] = 1
        return resultado

    # ── RECHAZADO: devolución pendiente total (recepción confirma → NC) ──
    if estado == EstadoEntrega.RECHAZADO:
        if recaudo.siesa_nc_triggered:
            resultado['ya_procesado'] = 1
            return resultado
        if _crear_devolucion_pendiente(
            recaudo, tarea, tipo_docto_fe, consec_fe,
            items_devueltos=None,  # None = devolución total
            notas=f'{notas_base} | RECHAZADO total',
        ):
            resultado['nc'] = 1
        else:
            resultado['ya_procesado'] = 1
        return resultado

    # ── CRÉDITO + ENTREGADO: noop ────────────────────────────────
    if es_credito and estado == EstadoEntrega.ENTREGADO:
        resultado['credito'] = 1
        return resultado

    # ── CRÉDITO + PARCIAL: devolución pendiente (recepción confirma → NC) ──
    if es_credito and estado == EstadoEntrega.PARCIAL:
        if recaudo.siesa_nc_triggered:
            resultado['ya_procesado'] = 1
            return resultado
        if _crear_devolucion_pendiente(
            recaudo, tarea, tipo_docto_fe, consec_fe,
            items_devueltos=recaudo.items_entregados,
            notas=f'{notas_base} | PARCIAL crédito — devolución',
        ):
            resultado['nc'] = 1
        else:
            resultado['ya_procesado'] = 1
        return resultado

    # ── CONTADO + PARCIAL: devolución pendiente → RC espera esa NC ──────
    if not es_credito and estado == EstadoEntrega.PARCIAL:
        if not recaudo.siesa_nc_triggered:
            _crear_devolucion_pendiente(
                recaudo, tarea, tipo_docto_fe, consec_fe,
                items_devueltos=recaudo.items_entregados,
                notas=f'{notas_base} | PARCIAL contado — devolución',
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


def _crear_devolucion_pendiente(recaudo: RecaudoEntrega, tarea, tipo_docto_fe: str,
                                 consec_fe, items_devueltos, notas: str) -> bool:
    """
    "Liquidar en WMS" ya no crea la NC directo (250696, sin cruce automático
    de cartera) — arma una DevolucionCliente ABIERTA con recaudo_entrega_id
    apuntando a este recaudo. La recepcionista la confirma en el módulo de
    Devoluciones (251126, con cruce automático de cartera) — eso dispara la
    NC real, y el bridge en siesa_job_service.py (job
    NOTA_CREDITO_DEVOLUCION_CLIENTE) marca recaudo.siesa_nc_triggered=True
    al terminar, destrabando el RECIBO_CAJA que dependa de esa NC.

    Quién decide qué se devolvió pasa de "lo que declaró el conductor en la
    calle" a "lo que la recepcionista contó físicamente" — la razón real de
    este cambio, no solo evitar la NC duplicada.

    items_devueltos=None → devolución total (Rechazado): todas las líneas
    reales de la factura, cantidad completa. Si no, solo las líneas con
    cantidad_devuelta > 0 que declaró el conductor (ver RecaudoEntrega.
    items_entregados) — la recepcionista puede ajustarlas antes de confirmar.

    Devuelve False sin hacer nada si ya existe una devolución (ABIERTA o
    CONFIRMADA) para este recaudo — evita duplicar el pendiente si
    "Liquidar en WMS" se vuelve a apretar antes de que la recepcionista
    confirme la primera vez.
    """
    from app.models.devolucion_cliente import DevolucionCliente
    from app.models.producto import Producto
    from app.services.connekta_gateway import connekta
    from app.services.devolucion_cliente_service import DevolucionClienteService

    ya_existe = (DevolucionCliente.query
                 .filter_by(recaudo_entrega_id=recaudo.id)
                 .filter(DevolucionCliente.estado != 'CANCELADA')
                 .first())
    if ya_existe:
        logger.info(
            '[LIQUIDACION] recaudo %d ya tiene devolución pendiente %s — no se duplica',
            recaudo.id, ya_existe.codigo
        )
        return False

    rowids_data = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
    if not rowids_data:
        raise ValueError(
            f'No se obtuvieron líneas de FE {tipo_docto_fe}-{consec_fe} — '
            'no se puede armar la devolución pendiente'
        )

    declarado_por_codigo = None  # None = total, dict = parcial
    if items_devueltos:
        declarado_por_codigo = {}
        for it in items_devueltos:
            cant = it.get('cantidad_devuelta') or it.get('devuelto', 0)
            if float(cant or 0) > 0:
                declarado_por_codigo[it.get('codigo')] = float(cant)

    lineas = []
    for row in rowids_data:
        ref = (row.get('f120_referencia') or '').strip()
        if not ref:
            continue
        producto = Producto.query.filter_by(codigo_siesa=ref).first()
        if not producto:
            logger.warning(
                '[LIQUIDACION] devolución pendiente recaudo %d: referencia Siesa %r '
                'sin producto WMS — omitida', recaudo.id, ref
            )
            continue
        cant_facturada = float(row.get('f470_cant_base') or 0)
        if declarado_por_codigo is None:
            cant_devuelta = cant_facturada
        else:
            cant_devuelta = declarado_por_codigo.get(producto.codigo, 0)
        if cant_devuelta <= 0:
            continue
        lineas.append({
            'producto_id': producto.id,
            'codigo_siesa': ref,
            'cantidad_facturada': cant_facturada,
            'cantidad_devuelta': min(cant_devuelta, cant_facturada),
            'f470_id_unidad_medida': (row.get('f470_id_unidad_medida') or '').strip(),
            'f150_id_bodega': (row.get('f150_id') or '').strip(),
            'f470_rowid': str(row.get('f470_rowid') or ''),
        })

    if not lineas:
        raise ValueError(
            f'Sin líneas para armar la devolución pendiente del recaudo {recaudo.id} — '
            'revisar códigos declarados por el conductor vs factura real en Siesa'
        )

    DevolucionClienteService.crear_devolucion(
        tarea_packing_id=tarea.id,
        tipo_docto_fe=tipo_docto_fe,
        consec_fe=consec_fe,
        almacen_id=tarea.almacen_id,
        recepcionista_id=None,
        lineas=lineas,
        es_total=(declarado_por_codigo is None),
        observaciones=notas,
        recaudo_entrega_id=recaudo.id,
        commit=False,
    )
    logger.info(
        '[LIQUIDACION] Devolución pendiente creada para recaudo %d (FE %s-%s, %d línea(s))',
        recaudo.id, tipo_docto_fe, consec_fe, len(lineas)
    )
    return True


def _hay_rc_en_cola(recaudo_id: int) -> bool:
    """¿Ya hay un RECIBO_CAJA en cola (o completado) para este recaudo? — no
    solo ENVIADO (`siesa_rc_triggered`, que se enciende recién cuando el DLQ
    llega al pre-flag, justo antes del POST, no al encolar). Mismo patrón que
    `_pucs_en_cola` para las retenciones.
    """
    return SiesaJob.query.filter(
        SiesaJob.tipo == 'RECIBO_CAJA',
        SiesaJob.referencia_tipo == 'RecaudoEntrega',
        SiesaJob.referencia_id == recaudo_id,
        SiesaJob.estado.notin_(['FALLIDO', 'DESCARTADO']),
    ).first() is not None


def _encolar_recibo_caja(recaudo: RecaudoEntrega, tipo_docto_fe: str,
                          consec_fe, tercero_nit: str, sucursal: str,
                          monto: float, forma_pago: str, notas: str,
                          admin_id: int = None, depende_de_nc: bool = False,
                          co_factura: str = '', cuenta_cxc: str = '',
                          unidad_negocio: str = ''):
    """Encola job RECIBO_CAJA en la DLQ.

    Guarda contra la cola, no solo contra `siesa_rc_triggered` — protege
    también al barrido masivo (`_procesar_recaudo` / `liquidar_ruta_siesa`)
    de encolar un segundo RC si `registrar_cobro_recaudo` ya encoló uno para
    este recaudo y el DLQ todavía no lo procesó.
    """
    if _hay_rc_en_cola(recaudo.id):
        logger.info(
            '[LIQUIDACION] recaudo %d: ya hay un RECIBO_CAJA en cola — no se duplica',
            recaudo.id
        )
        return
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
            'unidad_negocio': unidad_negocio,
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


def _pucs_en_cola(recaudo_id: int) -> set:
    """Cuentas PUC con un DOCUMENTO_CONTABLE_RET ya en cola para este recaudo
    — no solo ya ENVIADO (`RecaudoEntrega.pucs_enviadas()`, que mira la
    bandera de lo enviado). Un job PENDIENTE ya reserva esa cuenta: sin mirar
    la cola, dos encoladores del mismo recaudo (`/liquidar-completo` y el
    barrido de `_procesar_recaudo` que corre a continuación) duplican el job.
    Compartida con `rutas.py` — era la misma consulta escrita dos veces.
    """
    pucs = set()
    for j in SiesaJob.query.filter_by(
            tipo='DOCUMENTO_CONTABLE_RET', referencia_tipo='RecaudoEntrega',
            referencia_id=recaudo_id).all():
        if j.estado in ('FALLIDO', 'DESCARTADO'):
            continue
        try:
            pucs.add((j.get_payload() or {}).get('cuenta_puc'))
        except Exception:
            pass
    return pucs


def _encolar_documento_contable(recaudo: RecaudoEntrega, tipo_docto_fe: str,
                                  consec_fe, tercero_nit: str, sucursal: str,
                                  notas: str, admin_id: int = None,
                                  co_factura: str = '', cuenta_cxc: str = '',
                                  unidad_negocio: str = ''):
    """Encola job DOCUMENTO_CONTABLE_RET en la DLQ.

    El monto sale de `recaudo.monto_descuento` si ya viene declarado (lo que
    el cliente retuvo de verdad en la puerta, o lo que `/liquidar-completo`
    ya calculó con `monto_de_retencion()`). Si no viene declarado —el motivo
    entró sin monto, p.ej. datos históricos de antes del 2026-08-13—, se
    calcula acá con la MISMA fórmula de una sola fuente
    (`base_de_retencion()`/`monto_de_retencion()`, base gravable/IVA reales
    de Siesa).

    Antes esta rama calculaba `monto_cobrado * tasa` para cualquier tipo de
    retención. Para RETEIVA eso es la fórmula equivocada —RETEIVA va sobre el
    IVA, no sobre lo cobrado (que incluye IVA + subtotal)— y sobreestimaba el
    monto retenido hasta ~6x en una factura típica. Sin datos reales de Siesa
    disponibles, Regla 0: no se inventa el monto, el DC no se encola.
    """
    motivo = recaudo.motivo_descuento or ''
    cuenta_puc = RETENCION_PUC.get(motivo, '')
    if not cuenta_puc:
        logger.error(
            '[LIQUIDACION] motivo_descuento=%s sin cuenta PUC mapeada — DC no encolado',
            motivo
        )
        return

    if cuenta_puc in _pucs_en_cola(recaudo.id):
        logger.info(
            '[LIQUIDACION] recaudo %d: ya hay un DOCUMENTO_CONTABLE_RET en cola '
            'para la cuenta %s — no se duplica', recaudo.id, cuenta_puc
        )
        return

    monto_descuento = float(recaudo.monto_descuento or 0)
    base_gravable_payload = float(recaudo.monto_cobrado or 0)

    if monto_descuento <= 0:
        from app.services.connekta_gateway import connekta
        try:
            lineas_raw = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
        except Exception as e:
            logger.warning(
                '[LIQUIDACION] recaudo %d: no se pudo leer la factura en Siesa '
                'para calcular la retención %s — DC no encolado: %s',
                recaudo.id, motivo, e
            )
            return
        if not lineas_raw:
            logger.warning(
                '[LIQUIDACION] recaudo %d: factura sin líneas en Siesa — '
                'DC no encolado (retención %s)', recaudo.id, motivo
            )
            return

        base_gravable = sum(float(ln.get('f470_vlr_bruto', 0)) for ln in lineas_raw)
        total_iva = sum(float(ln.get('f470_vlr_imp', 0)) for ln in lineas_raw)
        monto_descuento = monto_de_retencion(motivo, base_gravable, total_iva)
        base_gravable_payload = base_de_retencion(motivo, base_gravable, total_iva)
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
            'base_gravable': base_gravable_payload,
            'co_factura': co_factura,
            'cuenta_cxc': cuenta_cxc,
            'unidad_negocio': unidad_negocio,
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


def _nombre_retencion(tipo: str) -> str:
    entrada = CATALOGO_RETENCIONES.get(tipo)
    return entrada['nombre'] if entrada else tipo
