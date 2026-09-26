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
from collections import namedtuple
import os
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


def tope_diferencia_recaudo() -> float:
    """Cuánto puede absorberse en silencio entre lo que declaró el conductor
    (`monto_cobrado`) y el neto de la factura, sin que un admin lo confirme
    a mano.

    Mismo patrón que `gestor-cartera-pame` (`recaudo/modelo.py::tope_ajuste`,
    RECAUDO_AJUSTE_TOPE) para el mismo problema: un Recibo de Caja que sale
    por el neto de Siesa en vez de por lo que el conductor dijo que cobró es
    correcto cuando la diferencia es residuo de redondeo, y es un faltante
    real sin explicar cuando no lo es (caso real: PD1426, $10.304 de
    diferencia, sin devolución ni retención tributaria de por medio — el RC
    salió por el neto completo igual, sin que nadie lo confirmara).

    A diferencia del Gestor, acá no hay medición empírica propia todavía —
    $100 es el mismo valor por defecto que allá, no uno recalculado contra
    datos reales de WMS-PAME. Ajustar vía env var si la operación real dice
    otra cosa.
    """
    try:
        return float(os.environ.get('RECAUDO_DIFERENCIA_TOPE', '100'))
    except (TypeError, ValueError):
        return 100.0


def tope_ajuste_faltante() -> float:
    """Cuánto faltante al peso puede declararse CON razón explícita, más allá
    del residuo silencioso de `tope_diferencia_recaudo` ($100).

    Dos topes, no uno — mismo principio que `gestor-cartera-pame`
    (`recaudo/modelo.py::tope_ajuste_faltante/_sobrante`, RECAUDO_AJUSTE_
    TOPE_FALTANTE) contra el mismo Siesa: el faltante es cartera que se da
    por cobrada sin que la plata haya entrado — «eso se borra sin cobrarla»
    — así que el techo es más estricto que el del sobrante. Mismo default
    ($1.000) que allá; acá tampoco hay medición propia todavía.
    """
    try:
        return float(os.environ.get('RECAUDO_AJUSTE_TOPE_FALTANTE', '1000'))
    except (TypeError, ValueError):
        return 1000.0


def tope_ajuste_sobrante() -> float:
    """Cuánto sobrante al peso puede declararse CON razón explícita.

    El sobrante es plata que SÍ entró — el riesgo no es perder cartera sino
    cobrarle al cliente algo que no debía —, por eso el tope es más laxo que
    el del faltante. Mismo default ($5.000) que gestor-cartera-pame.
    """
    try:
        return float(os.environ.get('RECAUDO_AJUSTE_TOPE_SOBRANTE', '5000'))
    except (TypeError, ValueError):
        return 5000.0


def _validar_diferencia_declarada(monto_declarado: float, total_neto: float,
                                   contexto: str = '') -> None:
    """Levanta `ValueError` si `monto_declarado` (lo que el conductor dijo
    que cobró) difiere del neto de Siesa por encima del residuo de redondeo
    tolerado — ver `tope_diferencia_recaudo`.

    **Una sola función, dos llamadores** (`registrar_cobro_recaudo` y
    `_procesar_recaudo`): antes de esto, el segundo mandaba `monto_cobrado`
    directo a Siesa sin comparar nunca contra el neto real — la misma
    política de "de dónde sale el monto del RC" escrita distinto en dos
    sitios, y solo uno la validaba. Regla 0 corolario, ya documentado varias
    veces en este repo para el mismo tipo de duplicación.

    Sin efecto si `monto_declarado <= 0` (nunca se declaró — no hay nada que
    comparar; Regla 0, ausencia de dato no es evidencia de faltante).
    """
    if monto_declarado <= 0:
        return
    diferencia = abs(total_neto - monto_declarado)
    tope = tope_diferencia_recaudo()
    if diferencia > tope:
        raise ValueError(
            f'El conductor declaró ${monto_declarado:,.2f} y la factura en Siesa '
            f'dice ${total_neto:,.2f}{contexto} — una diferencia de '
            f'${diferencia:,.2f}, por encima del residuo de redondeo tolerado '
            f'(${tope:,.2f}). No es una devolución ni hay retención que la '
            f'explique — decida el monto explícitamente (monto_override) antes '
            f'de continuar.'
        )


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
        from app.services import parada_tardia as _pt_det

        retenciones_disponibles = [
            {'tipo': k, 'nombre': v['nombre'], 'puc': v['puc'], 'tasa': v['tasa']}
            for k, v in CATALOGO_RETENCIONES.items()
        ]

        resultado_recaudos = []
        warnings = []
        from app.services import politica_cobro as _pc_lote
        _rc_llegaron = _pc_lote.rc_llegaron(recaudos)
        from app.services import senales_ruta as _sr
        _faltantes = _sr.faltantes_de_retorno_de_recaudos([r.id for r in recaudos])

        for recaudo in recaudos:
            tarea = recaudo.tarea
            rd = recaudo.to_dict()
            # Lo que quien liquida tiene que mirar antes de firmar. Señales, no
            # veredictos (`senales_ruta.senales_de_recaudo`).
            rd['senales'] = _sr.senales_de_recaudo(recaudo, ruta, _faltantes.get(recaudo.id))
            rd['faltante_retorno'] = _faltantes.get(recaudo.id)
            rd['hora_dispositivo'] = _sr.clasificar_hora(
                recaudo.ts_dispositivo, recaudo.ts_desfase_s,
                recaudo.fecha_confirmacion, ruta.fecha_cierre)
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
            # Contado contraentrega vs crédito real: cómo trata la liquidación
            # esta parada (la política, no la forma de pago) y si es un
            # crédito que nadie autorizó.
            from app.services import cond_pago as _cp_det
            _cobro_det = _cp_det.cobro_de_recaudo(recaudo, tarea)
            rd['cobro'] = {k: _cobro_det.get(k) for k in
                           ('cobrar', 'dias', 'codigo', 'origen', 'congelado')}
            rd['trato_cobro'] = (_cp_det.trato_de_cobro(recaudo, tarea)
                                 if recaudo.estado_entrega in (EstadoEntrega.ENTREGADO,
                                                               EstadoEntrega.PARCIAL)
                                 else None)
            rd['credito_no_autorizado'] = rd['trato_cobro'] == _cp_det.TRATO_NO_AUTORIZADO

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
            # devolucion_ruta). El admin ve acá si ya se envió.
            from app.models.devolucion_cliente import EstadoDevolucionCliente as _EDC
            from app.services import devolucion_ruta as _dr_det
            devolucion = _dr_det.devolucion_vigente(recaudo.id)
            # El estado de la devolución viaja con lo que significa: una
            # contada en cero (FALTANTE_TOTAL) no va a tener nota crédito, y la
            # pantalla decía «pendiente de que recepción confirme» y «NCE por
            # $X» sobre ella (e2e 2026-09-25). La pantalla no lo deduce.
            rd['devolucion_pendiente'] = (
                {'id': devolucion.id, 'codigo': devolucion.codigo, 'estado': devolucion.estado,
                 'contada': devolucion.estado in _EDC.CONTADAS,
                 'sin_nc': _dr_det.nc_no_llegara(recaudo)}
                if devolucion else None
            )
            # Lo que «Enviar a Siesa» produciría hoy para esta parada, de la
            # política (`politica_cobro.documentos_pendientes`).
            from app.services import politica_cobro as _pc_det
            rd['documentos_pendientes'] = _pc_det.documentos_pendientes(recaudo, tarea)
            # «Llegó» con señal positiva, no con la bandera de pre-envío.
            rd['rc_llego'] = recaudo.id in _rc_llegaron
            rd['rc_sin_verificar'] = bool(recaudo.siesa_rc_triggered and not rd['rc_llego']
                                          and not rd['rc_en_cola'])
            rd['cobro_editable'] = _pc_det.puede_editar_cobro(recaudo)
            rd['decision_retencion'] = _pc_det.decision_retencion(recaudo)

            resultado_recaudos.append(rd)

        result = {
            'ruta': ruta.to_dict(),
            'recaudos': resultado_recaudos,
            # Tanda 2 · B: lo que nadie gestionó (cliente, valor, hace cuánto,
            # referencias). La liquidación no pasa hasta resolverlas
            # (`RutaService.liquidar_ruta`); el formulario de la oficina sale de acá.
            'paradas_sin_gestionar': _pt_det.sin_gestionar(ruta),
            'formulario_oficina': _pt_det.formulario_oficina(),
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
        lineas_raw = []

        if _tipo_fe and _consec_fe:
            from app.services.connekta_gateway import connekta
            try:
                # Factura lines: base_gravable, IVA, neto
                lineas_raw = connekta.get_rowids_factura(_tipo_fe, _consec_fe) or []
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
        # Sobre lo que el cliente se quedó (`base_retencion_entregada`), la
        # misma base con la que se va a emitir el DC. En una PARCIAL sin
        # devolución amarrada no hay base: se dice, no se estima sobre la
        # factura entera.
        from app.services import politica_cobro as _pc_pv
        _base_ent = (_pc_pv.base_retencion_entregada(recaudo, lineas_raw)
                     if datos_disponibles else None)
        base_retencion_disponible = _base_ent is not None
        if _base_ent is not None:
            base_gravable_ret, total_iva_ret = _base_ent
        else:
            base_gravable_ret, total_iva_ret = base_gravable, total_iva
        retenciones_disponibles = []
        for tipo_ret, datos_ret in CATALOGO_RETENCIONES.items():
            tasa = datos_ret['tasa']
            base_calculo = base_de_retencion(tipo_ret, base_gravable_ret, total_iva_ret)
            monto_estimado = (monto_de_retencion(tipo_ret, base_gravable_ret, total_iva_ret)
                              if base_retencion_disponible else 0)
            retenciones_disponibles.append({
                'tipo': tipo_ret,
                'nombre': datos_ret['nombre'],
                'puc': datos_ret['puc'],
                'tasa': tasa,
                'base': base_calculo,
                'monto_estimado': monto_estimado,
            })

        # ── Siesa horario check — la ventana es UNA (Regla 14) ──────
        from app.services.ventana_siesa import ventana_abierta
        siesa_horario_ok = ventana_abierta()

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
        from app.services import cond_pago as _cp_pv
        _trato = (_cp_pv.trato_de_cobro(recaudo, tarea)
                  if estado in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL) else None)
        if estado != EstadoEntrega.ENTREGADO_SIN_PAGO:
            if estado in (EstadoEntrega.PARCIAL, EstadoEntrega.RECHAZADO):
                if not recaudo.siesa_nc_triggered:
                    acciones_pendientes.append('NOTA_CREDITO_FACTURA')
            # Por el TRATO (la política), no por `forma_pago`: un CREDITO sobre
            # contado no propone recibo, pero tampoco pasa como crédito.
            if _trato == _cp_pv.TRATO_CONTADO and forma_pago:
                if not recaudo.siesa_rc_triggered:
                    acciones_pendientes.append('RECIBO_CAJA')
                if not recaudo.siesa_dc_triggered:
                    acciones_pendientes.append('DOCUMENTO_CONTABLE_RET')
        _cobro_pv = _cp_pv.cobro_de_recaudo(recaudo, tarea)

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
            # Si la base de retención (lo que el cliente se quedó) se pudo
            # calcular. En una PARCIAL sin devolución amarrada no se puede.
            'base_retencion_disponible': base_retencion_disponible,
            # Lo que decide `politica_cobro.monto_rc`: en una PARCIAL el RC NO
            # sale neto de la retención (lo cobrado ya viene neto). La vista
            # previa no recalcula por su cuenta.
            'rc_resta_retencion': _pc_pv.rc_resta_retencion(recaudo),
            'decision_retencion': _pc_pv.decision_retencion(recaudo),
            'cobro_editable': _pc_pv.puede_editar_cobro(recaudo),
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
            # La clasificación de cobro con la que se juzga esta parada, y si
            # quedó como crédito que nadie autorizó.
            'cobro': {k: _cobro_pv.get(k) for k in
                      ('cobrar', 'dias', 'codigo', 'origen', 'congelado')},
            'credito_no_autorizado': _trato == _cp_pv.TRATO_NO_AUTORIZADO,
            'credito_autorizado': _cp_pv.credito_autorizado(recaudo),
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
        # Congelada al ENCOLAR el recibo, no al enviarlo (P1-3): el monto del
        # RC ya se armó con esta decisión.
        from app.services import politica_cobro as _pc
        if not _pc.puede_editar_cobro(recaudo):
            raise ValueError(
                f'La decisión sobre la retención ya no aplica: '
                f'{_pc.motivo_cobro_congelado(recaudo)}')

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
    def autorizar_credito(recaudo_id: int, admin_id: int, razon: str) -> dict:
        """La oficina decide que una parada de contado que no trajo plata queda
        como CRÉDITO. **Razón obligatoria**; autor, fecha y razón quedan en el
        recaudo y en la bitácora (EDITAR). Es la única salida del estado
        `credito_no_autorizado` que no pasa por cobrar.

        No toca la forma de pago ni el monto (lo que declaró el conductor se
        conserva): cambia cómo lo trata la liquidación. Solo sobre una parada
        que HOY es crédito no autorizado — autorizar algo que no lo necesita
        sería una marca sin sentido que después confunde a quien la lea.
        """
        from app.services import cond_pago as _cp
        from app.services.bitacora import registrar_accion, motivo_obligatorio
        recaudo = db.session.query(RecaudoEntrega).with_for_update().get(recaudo_id)
        if not recaudo:
            raise LookupError(f'RecaudoEntrega {recaudo_id} no encontrado')
        texto = motivo_obligatorio(razon)
        if _cp.credito_autorizado(recaudo):
            raise ValueError(f'El crédito del recaudo {recaudo_id} ya fue autorizado')
        if not _cp.credito_no_autorizado(recaudo, recaudo.tarea):
            raise ValueError(
                f'El recaudo {recaudo_id} no es un crédito sin autorizar: no hay nada '
                'que autorizar (o es crédito real, o trajo plata)')
        antes = {'credito_autorizado_por': None, 'credito_autorizado_en': None,
                 'credito_autorizado_razon': None}
        recaudo.credito_autorizado_por = admin_id
        recaudo.credito_autorizado_en = datetime.utcnow()
        recaudo.credito_autorizado_razon = texto
        tarea = recaudo.tarea
        registrar_accion(
            'EDITAR', recaudo, usuario_id=admin_id, motivo=texto,
            entidad_codigo=getattr(tarea, 'numero_pedido_siesa', None),
            antes=antes,
            despues={'credito_autorizado_por': admin_id,
                     'credito_autorizado_en': recaudo.credito_autorizado_en.isoformat(),
                     'credito_autorizado_razon': texto,
                     'forma_pago': recaudo.forma_pago,
                     'monto_cobrado': float(recaudo.monto_cobrado or 0),
                     'cobro': _cp.cobro_de_recaudo(recaudo, tarea)})
        db.session.commit()
        logger.info('[LIQUIDACION] Crédito autorizado recaudo %d por %s: %s',
                    recaudo_id, admin_id, texto)
        return recaudo.to_dict()

    @staticmethod
    def registrar_cobro_recaudo(recaudo_id: int, admin_id: int = None,
                                retenciones: list = None,
                                monto_override: float = None,
                                ajuste_valor: float = 0,
                                ajuste_es_sobrante: bool = False,
                                ajuste_razon: str = '') -> dict:
        """
        Enqueues RC + individual DCs for a single recaudo.

        Uses with_for_update() for concurrency protection.
        Validates sequencing (NC before RC for PARCIAL).
        Calculates retentions with correct bases (RETEIVA on IVA, others on base_gravable).

        ajuste_valor / ajuste_es_sobrante / ajuste_razon: decisión EXPLÍCITA
        del admin sobre una diferencia al peso que `tope_diferencia_recaudo`
        ($100) no absorbe sola. Mismo mecanismo que `gestor-cartera-pame`
        contra el mismo Siesa (`AjustePorDiferencia`) — el faltante/sobrante
        se declara en Siesa (cuenta 53959503/42958101), en vez de dejar el
        saldo de la factura abierto para siempre sin que nadie lo explique.
        Requiere razón (no se acepta en silencio) y tiene que EXPLICAR la
        diferencia real entre lo declarado y el neto de Siesa — no es un
        valor libre. Solo aplica a ENTREGADO: PARCIAL ya tiene su propio
        mecanismo (`monto_descuento`/retención rechazada, arriba) para una
        pregunta distinta (cuánto debía pagar el cliente, no un residuo de
        redondeo del cobro).
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

        # Una parada que declara «no entró plata» no produce recibo. Si además
        # era de contado, el mensaje lo dice: no es crédito, es un cobro que
        # falta (o un crédito que hay que autorizar).
        from app.services import cond_pago as _cp_rc
        if _cp_rc.forma_no_cobra(forma_pago):
            if _cp_rc.credito_no_autorizado(recaudo, tarea):
                raise ValueError(mensaje_credito_no_autorizado(recaudo, tarea))
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
                '— espere a que se procese antes de volver a intentar '
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
        #
        # Una sola política para las dos puertas (`politica_cobro`): la misma
        # que lee `_procesar_recaudo` y la que revalida el ejecutor del DLQ.
        from app.services import politica_cobro as _pc
        _pc.exigir_cobro_decidido(recaudo)
        for _r in (retenciones or []):
            _pc.exigir_retencion_aplicable(
                recaudo, _r.get('tipo') if isinstance(_r, dict) else _r)

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

        lineas_raw = []
        try:
            # Factura lines
            lineas_raw = connekta.get_rowids_factura(tipo_docto_fe, consec_fe) or []
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

        # ── Calculate retentions (sin encolar todavía) ────────────────
        # Se calcula la retención real ANTES de tocar la cola: el guard de
        # diferencia (más abajo) necesita el neto final, y si bloquea no
        # puede quedar un DC huérfano ya encolado para un RC que nunca sale.
        #
        # La base es lo que el cliente SE QUEDÓ (`base_retencion_entregada`):
        # en una PARCIAL la mercancía que volvió no se retiene. Antes era la
        # factura entera.
        import json
        dc_jobs_info = []
        retenciones_detalle = []
        retenciones_validas = []
        total_retenciones = 0

        if retenciones:
            _base_ent = (_pc.base_retencion_entregada(recaudo, lineas_raw)
                         if datos_siesa_ok else None)
            if _base_ent is None:
                raise ValueError(
                    'Base gravable no disponible — no se pueden calcular retenciones'
                    + (' (la entrega fue parcial y la devolución todavía no está '
                       'amarrada a la factura: sin saber qué se quedó el cliente '
                       'no hay base)' if datos_siesa_ok else '')
                )
            base_gravable, total_iva = _base_ent

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
                retenciones_validas.append(
                    (tipo_ret, cuenta_puc, tasa, base_ret, monto_ret))

        # ── El monto del RC: UNA función para las dos puertas ─────────────
        #
        # `politica_cobro.monto_rc`. PARCIAL: lo que entró (`monto_cobrado`, o
        # el override de quien liquida), que YA viene neto de la retención que
        # el cliente aplicó en la puerta — antes se le restaba otra vez acá y
        # el RC salía corto por el valor de la retención. ENTREGADO: el neto
        # real de Siesa (o el override) menos la retención que procede.
        #
        # La validación de diferencia va DESPUÉS (ver más abajo): la
        # Liquidación real SIEMPRE manda `monto_override` (precargado con el
        # neto de Siesa por el propio frontend), así que lo que hay que
        # comparar contra lo que declaró el conductor es el resultado FINAL.
        monto_neto_rc = _pc.monto_rc(
            recaudo,
            total_neto_siesa=(total_neto if datos_siesa_ok else None),
            retencion=total_retenciones,
            monto_override=monto_override,
        )
        # Lo que el cobro cubre de la factura (RC + retenciones que SÍ salen).
        monto = monto_neto_rc + (total_retenciones if _pc.rc_resta_retencion(recaudo) else 0)

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
        if _pc.decision_retencion(recaudo) == _pc.RECHAZADA:
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
                    'cliente — reintente cuando Siesa esté disponible'
                )
            from app.services.cxc_cruce import TOLERANCIA as _TOL
            if monto < esperado - _TOL:
                raise ValueError(
                    f'Retención rechazada — el cliente debe pagar el valor '
                    f'completo (${esperado:,.2f}). Monto actual: '
                    f'${monto:,.2f}. Ajusta el monto cuando el cliente pague '
                    'la diferencia.'
                )

        # ── El ajuste al peso: decisión explícita, no un valor libre ──────
        # Se valida ANTES del guard de diferencia — si explica la diferencia
        # real, el guard de abajo ni se llama para ese gap. Si no la explica
        # (o no trae razón, o excede su propio tope), revienta acá con su
        # propio mensaje en vez de caer en el genérico de "diferencia sin
        # explicar".
        ajuste_abs = round(abs(float(ajuste_valor or 0)), 2)
        ajuste_aplicado = False
        if ajuste_abs > 0:
            if estado != EstadoEntrega.ENTREGADO:
                raise ValueError(
                    'El ajuste al peso solo aplica a cobros ENTREGADO — un '
                    'PARCIAL ya tiene su propio mecanismo (monto_descuento / '
                    'retención rechazada) para la misma pregunta.'
                )
            if not ajuste_razon.strip():
                raise ValueError(
                    'Un ajuste al peso necesita razón. Es el único movimiento '
                    'en que la liquidación toca una cuenta de resultados '
                    '(53959503/42958101); sin razón, en tres meses nadie sabe '
                    'qué pasó.'
                )
            if ajuste_es_sobrante and total_retenciones > 0:
                raise ValueError(
                    'Este recaudo tiene retención Y un SOBRANTE al mismo '
                    'tiempo — esa combinación no está probada contra Siesa '
                    '(el faltante sí: se absorbe en el documento contable de '
                    'la retención). Registre el cobro por el neto exacto, sin '
                    'el sobrante, y abónelo aparte.'
                )
            tope = tope_ajuste_sobrante() if ajuste_es_sobrante else tope_ajuste_faltante()
            if ajuste_abs > tope:
                lado = 'sobrante' if ajuste_es_sobrante else 'faltante'
                raise ValueError(
                    f'El {lado} declarado (${ajuste_abs:,.2f}) supera el tope '
                    f'para este caso (${tope:,.2f}). Una diferencia mayor no es '
                    'redondeo: es un error de digitación o algo que hay que '
                    'resolver antes de cerrar el cobro, no taparlo en una '
                    'cuenta de resultados.'
                )
            monto_cobrado_ajuste = float(recaudo.monto_cobrado or 0)
            if monto_cobrado_ajuste <= 0:
                raise ValueError(
                    'No hay monto declarado por el conductor para comparar — '
                    'un ajuste al peso explica una diferencia contra ALGO '
                    'declarado, no se puede justificar contra nada (Regla 0).'
                )
            diferencia_real = round(monto_neto_rc - monto_cobrado_ajuste, 2)
            es_sobrante_real = diferencia_real < 0
            from app.services.cxc_cruce import TOLERANCIA as _TOL
            if (
                es_sobrante_real != ajuste_es_sobrante
                or abs(ajuste_abs - abs(diferencia_real)) > _TOL
            ):
                raise ValueError(
                    f'El ajuste declarado (${ajuste_abs:,.2f}, '
                    f'{"sobrante" if ajuste_es_sobrante else "faltante"}) no '
                    f'explica la diferencia real entre lo que dijo el '
                    f'conductor (${monto_cobrado_ajuste:,.2f}) y el neto de '
                    f'Siesa (${monto_neto_rc:,.2f}{" tras la retención" if total_retenciones else ""}'
                    f'): la diferencia real es '
                    f'${abs(diferencia_real):,.2f} '
                    f'{"sobrante" if es_sobrante_real else "faltante"}.'
                )
            ajuste_aplicado = True

        # ── Guard: diferencia declarada vs. lo que el RC va a cobrar ──
        # Comparar acá — contra `monto_neto_rc`, YA con la retención real
        # descontada — es lo que hace que un pago parcial legítimo por
        # retención (el cliente pagó neto de RETEFUENTE/RETEIVA/ICA) pase sin
        # bloquear, en vez de comparar contra el bruto de la factura como en
        # el primer intento de este guard (ver comentario en "Determine
        # monto" arriba — ese punto nunca corría: el frontend siempre manda
        # `monto_override`). Solo aplica a ENTREGADO — PARCIAL ya tiene su
        # propia verificación arriba («Retención rechazada»), con su propia
        # referencia (lo que el cliente se quedó, no el neto completo).
        #
        # Se salta cuando el ajuste al peso YA validó y va a explicar esta
        # misma diferencia en Siesa — no tiene sentido bloquear un gap que el
        # admin acaba de justificar con razón y que va a quedar declarado.
        if estado == EstadoEntrega.ENTREGADO and not ajuste_aplicado:
            _validar_diferencia_declarada(
                float(recaudo.monto_cobrado or 0),
                monto_neto_rc,
                contexto=(
                    ' (neto, después de descontar la retención)'
                    if total_retenciones else ''
                ),
            )

        # ── Enqueue retentions (ahora sí, con el guard ya superado) ───
        for tipo_ret, cuenta_puc, tasa, base_ret, monto_ret in retenciones_validas:
            dc_notas = (
                f'Liquidación per-recaudo | DC recaudo #{recaudo_id} | '
                f'Retención {tipo_ret} | Admin: {admin_id}'
            )
            # El único encolador de retenciones (`_encolar_retencion`): la
            # misma política y la misma forma de payload que el botón masivo.
            dc_job = _encolar_retencion(
                recaudo, tipo_ret=tipo_ret, monto=monto_ret, base_gravable=base_ret,
                tipo_docto_fe=tipo_docto_fe, consec_fe=consec_fe,
                tercero_nit=nit, sucursal=sucursal, co_factura=co_factura,
                cuenta_cxc=cuenta_cxc, unidad_negocio=un_cxc, notas=dc_notas,
                admin_id=admin_id, accion_origen='liquidacion_per_recaudo',
            )
            if dc_job is None:
                logger.info('[LIQUIDACION] recaudo %d: la retención %s ya estaba en '
                            'cola — no se duplica', recaudo_id, tipo_ret)
                continue
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

        # ── El ajuste al peso viaja en UN solo documento ──────────────────
        # Sin retención: el RC lo lleva (142888 puede declararlo solo). Con
        # retención: el RC NO puede — no tiene base gravable para justificar
        # la retención junto con un ajuste (ver `trigger_recibo_caja`) — así
        # que va en el ÚLTIMO DocumentoContable encolado. "Último" y no
        # "todos": declararlo en más de uno duplicaría el crédito de cartera
        # por la misma plata.
        if ajuste_aplicado:
            if dc_jobs_info:
                _ultimo_dc = SiesaJob.query.get(dc_jobs_info[-1]['job_id'])
                if _ultimo_dc:
                    payload_dc = json.loads(_ultimo_dc.payload)
                    payload_dc['ajuste_valor'] = ajuste_abs
                    payload_dc['ajuste_razon'] = ajuste_razon
                    _ultimo_dc.payload = json.dumps(payload_dc, ensure_ascii=False)
                    logger.info(
                        '[LIQUIDACION] Ajuste al peso ($%.2f) declarado en el '
                        'DC %s de recaudo %d (lleva retención)',
                        ajuste_abs, _ultimo_dc.id, recaudo_id,
                    )
            elif rc_job:
                payload_rc = json.loads(rc_job.payload)
                payload_rc['ajuste_valor'] = ajuste_abs
                payload_rc['ajuste_es_sobrante'] = bool(ajuste_es_sobrante)
                rc_job.payload = json.dumps(payload_rc, ensure_ascii=False)
                logger.info(
                    '[LIQUIDACION] Ajuste al peso ($%.2f, %s) declarado en el '
                    'RC %s de recaudo %d',
                    ajuste_abs,
                    'sobrante' if ajuste_es_sobrante else 'faltante',
                    rc_job.id, recaudo_id,
                )

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
    def resolver_recibo_sin_verificar(recaudo_id: int, usuario_id: int, entro: bool,
                                      motivo: str, consecutivo=None) -> dict:
        """Una persona dice cómo terminó un recibo de caja que el WMS no pudo
        verificar. **La salida que no existía** (P0-4): un RC cuyo POST falló
        sin que Siesa dijera que no queda con la bandera puesta y el job
        FALLIDO — no se reenvía solo (Regla 3) —, y hasta ahora nada lo
        destrababa.

        · `entro=True`: quien buscó en Siesa encontró el recibo. Queda
          ENVIADO (con el consecutivo si lo dio) y la retención puede seguir.
        · `entro=False`: no está en Siesa. Se baja la bandera y el cobro se
          puede registrar de nuevo (o reintentar el job).

        Motivo obligatorio; FORZAR en la bitácora. No hace POST. No sobre un
        recibo con envío en curso, ni sobre uno que ya consta como llegado.
        """
        from app.services import politica_cobro as _pc
        from app.services.bitacora import (FORZADO_RC_RESUELTO_A_MANO, foto,
                                           motivo_obligatorio, registrar_accion)
        texto = motivo_obligatorio(motivo, 'resolver a mano un recibo de caja')
        recaudo = db.session.query(RecaudoEntrega).with_for_update().get(recaudo_id)
        if not recaudo:
            raise LookupError(f'RecaudoEntrega {recaudo_id} no encontrado')
        if not recaudo.siesa_rc_triggered or _pc.rc_llego_a_siesa(recaudo):
            raise ValueError('Este recibo de caja no está pendiente de verificar: no hay '
                             'nada que resolver')
        from app.models.siesa_job import EstadoSiesaJob
        vivo = SiesaJob.query.filter(
            SiesaJob.tipo == 'RECIBO_CAJA', SiesaJob.referencia_tipo == 'RecaudoEntrega',
            SiesaJob.referencia_id == recaudo.id,
            SiesaJob.estado.in_(list(EstadoSiesaJob.ACTIVOS))).first()
        if vivo is not None:
            raise ValueError(f'Hay un envío de este recibo en curso (job {vivo.id}): '
                             'espere a que termine')
        antes = foto(recaudo, ['siesa_rc_triggered', 'siesa_rc_resultado', 'siesa_rc_consec'])
        if entro:
            recaudo.anotar_documento_siesa('RC', 'ENVIADO', consec=(consecutivo or None))
        else:
            recaudo.siesa_rc_triggered = False
            recaudo.anotar_documento_siesa('RC', 'FALLIDO')
        tarea = recaudo.tarea
        registrar_accion(
            'FORZAR', recaudo, usuario_id=usuario_id, motivo=texto,
            entidad_codigo=getattr(tarea, 'numero_pedido_siesa', None),
            antes=antes,
            despues={'forzado': FORZADO_RC_RESUELTO_A_MANO, 'entro': bool(entro),
                     **foto(recaudo, ['siesa_rc_triggered', 'siesa_rc_resultado',
                                      'siesa_rc_consec'])})
        db.session.commit()
        logger.info('[LIQUIDACION] RC del recaudo %d resuelto a mano por %s: %s',
                    recaudo_id, usuario_id, 'entró' if entro else 'no entró')
        return recaudo.to_dict()

    @staticmethod
    def corregir_monto_declarado(recaudo_id: int, nuevo_monto: float, razon: str,
                                  admin_id: int = None) -> dict:
        """
        Corrige `monto_cobrado` cuando lo que el conductor declaró en la calle
        resultó estar mal — no porque falte plata, sino porque el número en sí
        era incorrecto (el cliente pagó de menos por un descuento que no le
        correspondía del todo y luego pagó la diferencia, un error de
        digitación, etc.).

        Deliberadamente NO es lo mismo que el ajuste al peso
        (`ajuste_valor`/`ajuste_razon` en `registrar_cobro_recaudo`): ese
        declara una pérdida real contra una cuenta de resultados de Siesa y
        por eso tiene tope. Esto no le dice nada nuevo a Siesa — solo corrige
        el dato de origen del WMS para que el RC (y, si aplica, el DC) salgan
        con el número real cuando por fin se registre el cobro. Sin tope: un
        error de digitación de $50.000 sigue siendo un error de digitación,
        no un riesgo financiero de $50.000.

        `RutaService.confirmar_parada` ya permite corregir este mismo campo,
        pero SOLO mientras `ruta.estado == EN_TRANSITO` — el caso real que
        motivó esto es exactamente el que llega tarde: la ruta ya está
        ENTREGADA (o más allá) cuando Liquidación descubre el número mal.
        Ese guard no se toca; este es un segundo camino, angosto a propósito
        (solo el monto, nunca estado_entrega/items_entregados/bultos), para
        el momento en que el primero ya no aplica.
        """
        recaudo = db.session.query(RecaudoEntrega).with_for_update().get(recaudo_id)
        if not recaudo:
            raise LookupError(f'RecaudoEntrega {recaudo_id} no encontrado')

        if recaudo.estado_entrega not in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL):
            raise ValueError(
                f'No se puede corregir el monto de una parada {recaudo.estado_entrega}: '
                'solo ENTREGADO o PARCIAL representan dinero recibido')

        # Mismo momento de congelamiento que `confirmar_parada` — y por la
        # misma razón (ver ese comentario): el RC ya se arma desde este
        # campo; cambiarlo después de que salió deja al WMS y a Siesa
        # diciendo cifras distintas sin que ningún reintento lo reconcilie.
        from app.services import politica_cobro as _pc
        _congelado = _pc.motivo_cobro_congelado(recaudo)
        if not _pc.puede_editar_cobro(recaudo):
            raise ValueError(
                f'El monto ya no se puede corregir desde acá: {_congelado}. '
                'Una corrección de un valor ya contabilizado se hace con nota '
                'crédito; si el envío falla, se puede corregir de nuevo.'
            )

        nuevo_monto = round(float(nuevo_monto or 0), 2)
        if nuevo_monto <= 0:
            raise ValueError('El monto corregido debe ser mayor a 0')
        razon = (razon or '').strip()
        if not razon:
            raise ValueError(
                'La corrección necesita una razón. No es un ajuste contable '
                '—no toca Siesa—, pero sigue siendo dinero: sin razón, en '
                'tres meses nadie sabe por qué cambió el número.'
            )

        monto_anterior = float(recaudo.monto_cobrado or 0)
        if abs(nuevo_monto - monto_anterior) < 0.01:
            raise ValueError(
                f'El monto corregido (${nuevo_monto:,.2f}) es igual al ya '
                'declarado — no hay nada que corregir')

        ahora = _ahora_bogota()
        nota = (
            f'[CORRECCIÓN MONTO {ahora.strftime("%Y-%m-%d %H:%M")} · '
            f'admin {admin_id}] ${monto_anterior:,.2f} → ${nuevo_monto:,.2f}: {razon}'
        )
        recaudo.observaciones = (
            f'{recaudo.observaciones}\n{nota}' if recaudo.observaciones else nota
        )
        recaudo.monto_cobrado = nuevo_monto
        recaudo.editado_por = admin_id
        recaudo.editado_en = ahora
        db.session.commit()

        logger.info(
            '[LIQUIDACION] Recaudo %d: monto_cobrado corregido $%.2f → $%.2f '
            'por admin %s — %s',
            recaudo_id, monto_anterior, nuevo_monto, admin_id, razon,
        )
        return recaudo.to_dict()

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
                    'credito_omitidos': 0, 'ya_procesados': 0,
                    'credito_no_autorizado': 0, 'errores': []}

        for recaudo in recaudos:
            try:
                r = _procesar_recaudo(recaudo, notas_base, admin_id)
                resumen['rc_encolados'] += r.get('rc', 0)
                resumen['nc_encolados'] += r.get('nc', 0)
                resumen['dc_encolados'] += r.get('dc', 0)
                resumen['credito_omitidos'] += r.get('credito', 0)
                resumen['ya_procesados'] += r.get('ya_procesado', 0)
                resumen['credito_no_autorizado'] += r.get('credito_no_autorizado', 0)
                # Un documento que no se encoló y no levantó (el DC sin base
                # gravable) llega por acá. Antes solo dejaba un WARNING en el
                # log mientras el contador subía: la pantalla decía «1 DC» sin
                # documento. `errores` es lo que el PWA pinta en rojo
                # (`rutas.js:2893`), mismo canal que usa el caller gemelo de
                # `/liquidar-completo`.
                for _msg in r.get('errores', []):
                    resumen['errores'].append({
                        'recaudo_id': recaudo.id,
                        'tarea_id': recaudo.tarea_id,
                        'error': _msg,
                        'codigo': (CREDITO_NO_AUTORIZADO
                                   if str(_msg).startswith(CREDITO_NO_AUTORIZADO) else None),
                    })
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
        Asegura que cada recaudo RECHAZADO/PARCIAL de la ruta tenga su
        devolución (m045devol: normalmente ya nació al confirmar la parada; acá
        se ENCUENTRA). Solo crea —con la misma función única,
        `devolucion_ruta.asegurar_devolucion`— la de una parada confirmada
        antes de este cambio o por un camino que no pasó por la parada. Y la
        intenta amarrar a su factura (con red; si Siesa no responde, se hace al
        contar).

        Nunca lanza: lo que impide una devolución (referencia sin producto,
        factura no localizada) queda escrito en ella (`problema_factura`) y en
        `errores`, visible — no en un log.
        """
        from app.services import devolucion_ruta as _dr

        recaudos = (RecaudoEntrega.query
                    .filter_by(ruta_id=ruta_id)
                    .filter(RecaudoEntrega.estado_entrega.in_(
                        [EstadoEntrega.RECHAZADO, EstadoEntrega.PARCIAL]))
                    .all())
        resumen = {'creadas': 0, 'encontradas': 0, 'errores': []}

        for recaudo in recaudos:
            try:
                dev, creada = _dr.asegurar_devolucion(recaudo)
                if dev is None:
                    continue
                resumen['creadas' if creada else 'encontradas'] += 1
                problema = _vincular_sin_romper(dev)
                if problema:
                    resumen['errores'].append({'recaudo_id': recaudo.id,
                                               'devolucion': dev.codigo,
                                               'error': problema})
            except Exception as e:
                logger.error(
                    '[LIQUIDACION] crear_devoluciones_pendientes_ruta: recaudo %d: %s',
                    recaudo.id, e
                )
                resumen['errores'].append({'recaudo_id': recaudo.id, 'error': str(e)})

        db.session.commit()
        logger.info(
            '[LIQUIDACION] crear_devoluciones_pendientes_ruta: ruta %d — '
            '%d creada(s), %d encontrada(s), %d error(es)',
            ruta_id, resumen['creadas'], resumen['encontradas'], len(resumen['errores'])
        )
        return resumen


def _vincular_sin_romper(devolucion) -> str | None:
    """Amarra la devolución a su factura si está activa y no lo estaba.
    Devuelve el problema que impide contarla (o None). Un fallo de red no es un
    problema de la devolución: se reintenta al contar."""
    from app.services import devolucion_ruta as _dr
    from app.services.connekta_gateway import connekta
    if devolucion.estado not in ('EN_CAMION', 'ABIERTA'):
        return None
    try:
        with db.session.begin_nested():
            _dr.vincular_a_factura(devolucion, gateway=connekta)
    except Exception as e:
        logger.warning('[LIQUIDACION] no se pudo vincular %s a su factura (se reintenta al '
                       'contar): %s', devolucion.codigo, e)
        return None
    return devolucion.problema_factura


def _devolucion_de_ruta(recaudo, resultado: dict) -> bool:
    """La devolución del recaudo: la ENCUENTRA (nació al confirmar la parada)
    o, si es una parada vieja, la crea con la función única. Lo que impida
    contarla va a `resultado['errores']`.

    Devuelve True si hay una nota crédito EN CAMINO para este recaudo —la
    devolución está sin contar, o contada con la NC todavía sin salir—, que es
    lo que el contador `nc` de la liquidación siempre quiso decir. False si ya
    terminó (NC enviada, contada en cero, cancelada): «ya procesado»."""
    from app.services import devolucion_ruta as _dr
    dev, _creada = _dr.asegurar_devolucion(recaudo)
    if dev is None:
        return False
    problema = _vincular_sin_romper(dev)
    if problema:
        resultado.setdefault('errores', []).append(
            f'devolución {dev.codigo}: {problema}')
    if dev.estado in ('EN_CAMION', 'ABIERTA'):
        return True
    # CONFIRMADA y no `CONTADAS`: acá la pregunta es «¿hay NC en camino?», y una
    # contada en cero (FALTANTE_TOTAL) no la va a tener.
    from app.models.devolucion_cliente import EstadoDevolucionCliente as _EDC
    return dev.estado == _EDC.CONFIRMADA and not dev.siesa_nc_triggered


def _contar_dc(resultado: dict, r_dc) -> None:
    """Traduce el desenlace del documento contable al resultado del recaudo.

    **Una función, un lugar**: las dos ramas que encolan retención (CONTADO
    PARCIAL y CONTADO ENTREGADO) hacían `resultado['dc'] = 1` cada una por su
    lado, y las dos mentían igual. Si mañana aparece una tercera rama, que use
    ésta y no una tercera copia de la regla.

    `dc` cuenta **documentos de retención en la cola** tras esta corrida: 1
    cuando se encoló acá y 1 cuando ya había uno encolado para esa cuenta PUC
    (existe y se va a enviar). 0 cuando no hay ninguno — y ahí el motivo se
    declara en `errores`, que es lo que la pantalla pinta en rojo.
    """
    if r_dc.estado in (DC_ENCOLADO, DC_YA_EN_COLA):
        resultado['dc'] = 1
        return
    resultado.setdefault('errores', []).append(r_dc.motivo)


#: Código estable del error, para que la pantalla lo reconozca sin parsear prosa.
CREDITO_NO_AUTORIZADO = 'credito_no_autorizado'


def mensaje_credito_no_autorizado(recaudo, tarea=None) -> str:
    """El texto que ve quien liquida. Dice qué pasó y qué puede hacer."""
    from app.services import cond_pago as _cp
    tarea = tarea if tarea is not None else recaudo.tarea
    cobro = _cp.cobro_de_recaudo(recaudo, tarea)
    cond = cobro.get('codigo') or 'sin condición'
    dias = f' ({cobro["dias"]} días)' if cobro.get('dias') is not None else ''
    que = (recaudo.forma_pago or 'sin forma de pago')
    monto = float(recaudo.monto_cobrado or 0)
    pedido = getattr(tarea, 'numero_pedido_siesa', None) or f'tarea {recaudo.tarea_id}'
    return (f'{CREDITO_NO_AUTORIZADO}: el pedido {pedido} es de contado contraentrega '
            f'({cond}{dias}) y se registró {que} con ${monto:,.0f} cobrados. No se '
            f'documenta como crédito: cóbrelo, o autorícelo como crédito con una razón '
            f'(queda en la bitácora).')


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

    "Devolución pendiente" (`devolucion_ruta`): desde m045devol la devolución
    NACE al confirmar la parada (EN_CAMION) y acá se ENCUENTRA; solo una parada
    vieja la crea, con la misma función. La recepcionista la cuenta; eso
    dispara la NC real (251126, con cruce automático de cartera) y marca
    recaudo.siesa_nc_triggered=True (bridge en siesa_job_service.py), que es lo
    que destraba el RC dependiente. Si la devolución termina SIN NC (contada en
    cero o cancelada), el RC sale por lo cobrado (`nc_no_llegara`).

    El dict devuelto trae además `errores`: los documentos que NO se encolaron
    sin levantar excepción (hoy, el DC sin base gravable — ver `_contar_dc`).
    Cada contador cuenta documentos que existen; lo que falta viaja por
    `errores`, no por el silencio del log.
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
    co_factura, cuenta_cxc, un_cxc = _resolver_cuenta_cxc(tarea, tipo_docto_fe, consec_fe)

    estado = recaudo.estado_entrega
    forma_pago = (recaudo.forma_pago or '').upper()
    # Contado contraentrega vs crédito real (2026-09-24): se ramifica por la
    # CLASIFICACIÓN de la parada (`cond_pago.trato_de_cobro`, el snapshot
    # congelado al confirmar), no por la forma de pago que marcó el conductor.
    # Un CREDITO/EXENTO sobre una factura de contado —o un ENTREGADO de contado
    # con $0— no es crédito: es plata que nadie cobró, y no va al contador
    # `credito` sino a `errores` hasta que la oficina lo autorice con razón.
    from app.services import cond_pago as _cp_liq
    trato = (_cp_liq.trato_de_cobro(recaudo, tarea)
             if estado in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL) else None)
    es_credito = trato == _cp_liq.TRATO_CREDITO
    monto = float(recaudo.monto_cobrado or 0)
    resultado = {'rc': 0, 'nc': 0, 'dc': 0, 'credito': 0, 'ya_procesado': 0,
                 # Contado que salió sin plata y sin autorización. Bloquea la
                 # liquidación de la ruta (`RutaService.liquidar_ruta`).
                 'credito_no_autorizado': 0,
                 # Cuenta aparte y no dentro de `credito`: un crédito lo
                 # autorizó alguien; esto no lo autorizó nadie.
                 'sin_pago': 0,
                 # Lo que NO se encoló y no levantó excepción. Sin este canal,
                 # un documento financiero abortado solo dejaba un WARNING en
                 # el log y el contador subía igual — ver
                 # `_encolar_documento_contable`.
                 'errores': []}

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
        if _devolucion_de_ruta(recaudo, resultado):
            resultado['nc'] = 1
        else:
            resultado['ya_procesado'] = 1
        return resultado

    # ── CONTADO sin plata y sin autorización: no se documenta como crédito ──
    #
    # Lo físico sigue su camino: si hubo devolución (PARCIAL), la mercancía
    # volvió y recepción tiene que poder recibirla — la devolución pendiente se
    # arma igual. Lo que NO se hace es tratar lo no cobrado como crédito: se
    # declara en `errores` con su código y la oficina decide (cobrarlo, o
    # «autorizar como crédito» con razón, que queda en la bitácora).
    if trato == _cp_liq.TRATO_NO_AUTORIZADO:
        resultado['credito_no_autorizado'] = 1
        if estado == EstadoEntrega.PARCIAL and not recaudo.siesa_nc_triggered:
            if _devolucion_de_ruta(recaudo, resultado):
                resultado['nc'] = 1
        resultado['errores'].append(mensaje_credito_no_autorizado(recaudo, tarea))
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
        if _devolucion_de_ruta(recaudo, resultado):
            resultado['nc'] = 1
        else:
            resultado['ya_procesado'] = 1
        return resultado

    # ── La retención declarada tiene que estar decidida ──────────────────
    #
    # La misma política que «Registrar cobro» (`politica_cobro`): con una
    # retención declarada y sin decidir no sale ni el RC (su monto depende de
    # si procede) ni el DC. Antes el botón masivo encolaba la NI con solo mirar
    # `motivo_descuento` — una NI por plata que nadie verificó que el cliente
    # pudiera descontar (P0-5). Lo físico (la devolución) sigue su camino.
    from app.services import politica_cobro as _pc
    try:
        _pc.exigir_cobro_decidido(recaudo)
        _decidido = True
    except _pc.RetencionNoAplicable as _e_pend:
        _decidido = False
        resultado['errores'].append(f'Recaudo {recaudo.id}: {_e_pend}')

    # ── CONTADO + PARCIAL: devolución pendiente → RC espera esa NC ──────
    if not es_credito and estado == EstadoEntrega.PARCIAL:
        if not recaudo.siesa_nc_triggered:
            _devolucion_de_ruta(recaudo, resultado)
            resultado['nc'] = 1
        if not _decidido:
            return resultado

        if not recaudo.siesa_rc_triggered and monto > 0:
            # `monto_rc`: lo que entró, ya neto de la retención de la puerta.
            monto_rc_parcial = _pc.monto_rc(recaudo)
            # Retención rechazada: el cliente debía pagar lo que se quedó
            # completo (`monto_cobrado + monto_descuento`). La misma regla que
            # «Registrar cobro»; antes el botón masivo la ignoraba.
            if (_pc.decision_retencion(recaudo) == _pc.RECHAZADA
                    and float(recaudo.monto_descuento or 0) > 0.5):
                resultado['errores'].append(
                    f'Recaudo {recaudo.id}: la retención {recaudo.motivo_descuento} '
                    f'fue rechazada y el cliente pagó '
                    f'${float(recaudo.monto_cobrado or 0):,.0f} de '
                    f'${float(recaudo.monto_cobrado or 0) + float(recaudo.monto_descuento or 0):,.0f}. '
                    f'El recibo de caja no sale hasta que se corrija el monto.')
            else:
                # Espera la NC —salvo que la devolución ya haya terminado SIN NC
                # (contada en cero o cancelada): entonces sale por lo cobrado y la
                # factura queda con el saldo de lo que no volvió
                # (`devolucion_ruta.nc_no_llegara`; el ejecutor lo vuelve a mirar).
                from app.services import devolucion_ruta as _dr_rc
                _encolar_recibo_caja(
                    recaudo, tipo_docto_fe, consec_fe,
                    tercero_nit, sucursal, monto_rc_parcial, forma_pago,
                    notas=f'{notas_base} | PARCIAL contado — cobro',
                    admin_id=admin_id,
                    depende_de_nc=not _dr_rc.nc_no_llegara(recaudo),
                    co_factura=co_factura,
                    cuenta_cxc=cuenta_cxc,
                    unidad_negocio=un_cxc,
                )
                resultado['rc'] = 1

        # Retención (si aplica y está CONFIRMADA) — el contador refleja lo que
        # quedó en la cola, no la intención de encolar. Ver
        # `_encolar_documento_contable`.
        if (not recaudo.siesa_dc_triggered
                and _pc.decision_retencion(recaudo) == _pc.CONFIRMADA):
            _contar_dc(resultado, _encolar_documento_contable(
                recaudo, tipo_docto_fe, consec_fe,
                tercero_nit, sucursal,
                notas=f'{notas_base} | Retención {recaudo.motivo_descuento}',
                admin_id=admin_id,
                co_factura=co_factura,
                cuenta_cxc=cuenta_cxc,
                unidad_negocio=un_cxc,
            ))

        return resultado

    # ── CONTADO + ENTREGADO: RC (+ DC si retención) ──────────────
    if not es_credito and estado == EstadoEntrega.ENTREGADO:
        if not _decidido:
            return resultado
        _retencion_procede = _pc.decision_retencion(recaudo) == _pc.CONFIRMADA
        if not recaudo.siesa_rc_triggered and monto > 0:
            # Mismo criterio que `registrar_cobro_recaudo` (`monto_rc`):
            # preferir el neto real de Siesa sobre lo declarado, validando
            # antes que no difieran más que el residuo de redondeo
            # (`_validar_diferencia_declarada`).
            #
            # Un fallo de red al consultar la factura NO bloquea el cobro
            # —cae al monto declarado, igual que `registrar_cobro_recaudo`
            # cuando `datos_siesa_ok` es False—; lo que sí bloquea es una
            # diferencia real ya verificada contra Siesa.
            #
            # La retención SIEMPRE se recalcula contra Siesa sobre lo que el
            # cliente se quedó (`base_retencion_entregada`) — nunca desde
            # `recaudo.monto_descuento` (ver `_encolar_documento_contable`) —
            # y solo si está CONFIRMADA: una rechazada no se descuenta del RC.
            total_neto_rc = None
            retencion_preview = 0.0
            try:
                from app.services.connekta_gateway import connekta as _connekta_rc
                lineas_rc = _connekta_rc.get_rowids_factura(tipo_docto_fe, consec_fe)
                if lineas_rc:
                    _neto = round(
                        sum(float(ln.get('f470_vlr_neto', 0)) for ln in lineas_rc), 2)
                    if _neto > 0:
                        total_neto_rc = _neto
                        if _retencion_procede:
                            _base_rc = _pc.base_retencion_entregada(recaudo, lineas_rc)
                            if _base_rc is not None:
                                retencion_preview = monto_de_retencion(
                                    recaudo.motivo_descuento, *_base_rc)
            except Exception as e:
                logger.warning(
                    '[LIQUIDACION] _procesar_recaudo: no se pudo verificar neto '
                    'Siesa para recaudo %d — usando monto declarado: %s',
                    recaudo.id, e,
                )

            monto_rc = _pc.monto_rc(
                recaudo, total_neto_siesa=total_neto_rc, retencion=retencion_preview)
            if total_neto_rc is not None:
                _validar_diferencia_declarada(
                    monto, monto_rc,
                    contexto=(
                        ' (neto, después de descontar la retención)'
                        if retencion_preview else ''
                    ),
                )

            _encolar_recibo_caja(
                recaudo, tipo_docto_fe, consec_fe,
                tercero_nit, sucursal, monto_rc, forma_pago,
                notas=f'{notas_base} | ENTREGADO contado',
                admin_id=admin_id,
                co_factura=co_factura,
                cuenta_cxc=cuenta_cxc,
                unidad_negocio=un_cxc,
            )
            resultado['rc'] = 1

        if not recaudo.siesa_dc_triggered and _retencion_procede:
            _contar_dc(resultado, _encolar_documento_contable(
                recaudo, tipo_docto_fe, consec_fe,
                tercero_nit, sucursal,
                notas=f'{notas_base} | Retención {recaudo.motivo_descuento}',
                admin_id=admin_id,
                co_factura=co_factura,
                cuenta_cxc=cuenta_cxc,
                unidad_negocio=un_cxc,
            ))

        if recaudo.siesa_rc_triggered and not _retencion_procede:
            resultado['ya_procesado'] = 1

        return resultado

    # Caso no contemplado
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


def _resolver_cuenta_cxc(tarea, tipo_docto_fe, consec_fe) -> tuple:
    """(co_factura, cuenta_cxc, un_cxc) reales desde Siesa — o ('', '', '') si
    no se pudo resolver.

    Extraída de `registrar_cobro_recaudo` (2026-09-04). `_procesar_recaudo`
    (el botón masivo "Liquidar Ruta", el mismo que usa el administrador en
    producción) nunca llamaba esto — mandaba el Recibo de Caja con cuenta y
    Unidad de Negocio vacías, cayendo al fallback fijo del conector
    (`SIESA_CXC_AUXILIAR` + UN por defecto), casi nunca la cuenta real del
    cliente. Confirmado en vivo contra Siesa QA real (PD1125, PD1450,
    2026-09-04): rechazo con "el auxiliar de caja maneja una U.N. diferente
    a la del documento" + "El documento de cruce no existe" — el mismo par
    de mensajes que ya había costado el caso real PD1411/FE-1416
    (2026-08-18), corregido entonces solo en `registrar_cobro_recaudo`, sin
    llegar nunca al botón masivo. Ver Regla 0 del CLAUDE.md — una pregunta,
    dos sitios, diverge.
    """
    from app.services.connekta_gateway import connekta
    co_factura = cuenta_cxc = un_cxc = ''
    try:
        cabecera = connekta.get_pedido_cabecera(
            tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa)
        if not cabecera:
            return co_factura, cuenta_cxc, un_cxc
        co_factura = cabecera.get('f430_id_co', '') or ''
        nit = cabecera.get('f200_id_pedido_fact', '') or ''
        if not nit:
            return co_factura, cuenta_cxc, un_cxc
        cxc_data = connekta.get_cxc_general(nit)
        from app.services import cxc_cruce as _cx
        fila_cxc = _cx.fila_de_la_factura(
            cxc_data, tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa,
            tipo_docto_fe, consec_fe)
        if fila_cxc:
            cuenta_cxc = fila_cxc.get('f253_id', '') or ''
            un_cxc = fila_cxc.get('f353_id_un_cruce', '') or ''
    except Exception as e:
        logger.warning(
            '[LIQUIDACION] _resolver_cuenta_cxc falló para tarea %s: %s',
            tarea.id, e
        )
    return co_factura, cuenta_cxc, un_cxc


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
        return None
    from app.services import politica_cobro as _pc
    job = SiesaJob.encolar(
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
            # Lo que el RC da por cierto de la parada. El ejecutor lo compara
            # antes del POST: si cambió por un camino que no pasó por
            # `puede_editar_cobro`, el RC no sale (P1-3).
            'instantanea': _pc.instantanea_cobro(recaudo),
        },
        referencia_tipo='RecaudoEntrega',
        referencia_id=recaudo.id,
        creado_por_id=admin_id,
    )
    logger.info(
        '[LIQUIDACION] Encolado RECIBO_CAJA para recaudo %d (FE %s-%s, $%.2f)',
        recaudo.id, tipo_docto_fe, consec_fe, monto
    )
    return job


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


#: Los tres desenlaces de `_encolar_documento_contable`. **Son tres, no dos**:
#: «lo encolé ahora», «ya había uno en la cola» y «no se pudo, no hay documento».
#: Con los cinco `return` vacíos de antes el llamador no podía distinguirlos y
#: contaba 1 en los tres casos — incluido el tercero, que es el defecto de las
#: tres banderas de idempotencia financiera otra vez: el tablero informando un
#: documento de retención que nunca se envió (CLAUDE.md, «Las tres banderas»).
DC_ENCOLADO = 'ENCOLADO'
DC_YA_EN_COLA = 'YA_EN_COLA'
DC_NO_ENCOLADO = 'NO_ENCOLADO'

#: `motivo` solo viene con DC_NO_ENCOLADO: es el texto que va a `errores` y que
#: la pantalla pinta en rojo (`rutas.js:2893`). Mismo patrón que el caller
#: gemelo de `/liquidar-completo` (`rutas.py:1259-1276`), que acumula en
#: `errores` y con eso decide el `ok` de la respuesta.
ResultadoDC = namedtuple('ResultadoDC', 'estado motivo')


def _encolar_documento_contable(recaudo: RecaudoEntrega, tipo_docto_fe: str,
                                  consec_fe, tercero_nit: str, sucursal: str,
                                  notas: str, admin_id: int = None,
                                  co_factura: str = '', cuenta_cxc: str = '',
                                  unidad_negocio: str = '') -> ResultadoDC:
    """Encola job DOCUMENTO_CONTABLE_RET en la DLQ. **Dice si encoló o no.**

    Devolvía `None` en los cinco caminos —encolado, duplicado y tres abortos— y
    el llamador hacía `resultado['dc'] = 1` igual. Con `get_rowids_factura`
    levantando (hoy puede: barrido incompleto), la retención no se encolaba,
    `SiesaJob.encolar` no se llamaba, y `dc_encolados` subía y pintaba «1 DC» en
    la pantalla. Un documento contable que el tablero da por hecho y que nadie
    va a buscar a Siesa es el mismo daño que las tres banderas.

    **No levanta.** El aborto se devuelve como dato porque este documento es el
    último paso del recaudo: el RC (y la devolución, si la hubo) ya se encolaron
    de verdad, y una excepción acá se llevaría puestos esos conteos ciertos —el
    tablero pasaría de informar de más a informar de menos. Se declara en
    `errores` y el operador ve el rojo con el motivo.

    El monto SIEMPRE se recalcula contra Siesa (`base_de_retencion()`/
    `monto_de_retencion()`, base gravable/IVA reales — misma fórmula, una
    sola fuente) — nunca sale de `recaudo.monto_descuento` directo.

    Antes: si `monto_descuento` ya venía declarado (lo que el conductor
    escribió en la puerta, calculado offline con datos que pudieron quedar
    desactualizados, o un valor histórico), se usaba tal cual sin volver a
    preguntarle a Siesa — la única rama que sí llamaba a Siesa era cuando el
    campo venía vacío. El estimado que ve el conductor en pantalla (ver
    `condActualizarPreviewDescuento` en rutas.js) es una vista previa a
    propósito: puede quedar desactualizado si algo de la factura cambia
    entre que se cargó la ruta y que se liquida. Con retención ReteIVA
    calculada mal (`monto_cobrado * tasa` en vez de `IVA * tasa`) esto ya
    costó ~6x de sobreestimación una vez — no hay razón para confiar un
    segundo estimado sin verificarlo, cuando ya se sabe que puede estar mal.

    `recaudo.monto_descuento` no desaparece: si difiere del valor real de
    Siesa más allá del residuo de redondeo, queda trazado en el log — no
    bloquea la liquidación masiva de la ruta por una diferencia menor.

    Si Siesa no responde, no se inventa el monto (Regla 0): el DC no se
    encola en este ciclo — la próxima liquidación lo reintenta.
    """
    motivo = recaudo.motivo_descuento or ''
    # La política decide antes que nada: una retención pendiente o rechazada
    # no llega ni a consultar la factura (P0-5, 2026-09-25).
    from app.services import politica_cobro as _pc
    try:
        _pc.exigir_retencion_aplicable(recaudo, motivo)
    except _pc.RetencionNoAplicable as _e_pol:
        return ResultadoDC(DC_NO_ENCOLADO, (
            f'Recaudo {recaudo.id}: {_e_pol}. El documento contable NO se encoló.'))
    cuenta_puc = RETENCION_PUC.get(motivo, '')
    if not cuenta_puc:
        logger.error(
            '[LIQUIDACION] motivo_descuento=%s sin cuenta PUC mapeada — DC no encolado',
            motivo
        )
        return ResultadoDC(DC_NO_ENCOLADO, (
            f'Recaudo {recaudo.id}: la retención {motivo!r} no tiene cuenta PUC '
            f'en CATALOGO_RETENCIONES — el documento contable NO se encoló.'))

    if cuenta_puc in _pucs_en_cola(recaudo.id):
        logger.info(
            '[LIQUIDACION] recaudo %d: ya hay un DOCUMENTO_CONTABLE_RET en cola '
            'para la cuenta %s — no se duplica', recaudo.id, cuenta_puc
        )
        # Sí hay documento para esa cuenta: lo encoló otro camino («Registrar
        # cobro» por parada) y sigue en la cola. Cuenta como documento
        # existente —el tablero no miente— y no es un error que declarar.
        return ResultadoDC(DC_YA_EN_COLA, None)

    from app.services.connekta_gateway import connekta
    try:
        lineas_raw = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
    except Exception as e:
        logger.warning(
            '[LIQUIDACION] recaudo %d: no se pudo leer la factura en Siesa '
            'para calcular la retención %s — DC no encolado: %s',
            recaudo.id, motivo, e
        )
        return ResultadoDC(DC_NO_ENCOLADO, (
            f'Recaudo {recaudo.id}: no se pudo leer la factura '
            f'{tipo_docto_fe}-{consec_fe} en Siesa para calcular la retención '
            f'{motivo} ({e}). El documento contable NO se encoló — calcularlo '
            f'sin la base real de Siesa daría un monto inventado.'))
    if not lineas_raw:
        logger.warning(
            '[LIQUIDACION] recaudo %d: factura sin líneas en Siesa — '
            'DC no encolado (retención %s)', recaudo.id, motivo
        )
        return ResultadoDC(DC_NO_ENCOLADO, (
            f'Recaudo {recaudo.id}: la factura {tipo_docto_fe}-{consec_fe} '
            f'volvió sin líneas de Siesa — sin base gravable, el documento '
            f'contable de la retención {motivo} NO se encoló.'))

    # La base es lo que el cliente SE QUEDÓ, no la factura entera: en una
    # PARCIAL lo devuelto no se retiene (`politica_cobro.base_retencion_entregada`).
    _base = _pc.base_retencion_entregada(recaudo, lineas_raw)
    if _base is None:
        return ResultadoDC(DC_NO_ENCOLADO, (
            f'Recaudo {recaudo.id}: la entrega fue parcial y la devolución todavía '
            f'no está amarrada a la factura {tipo_docto_fe}-{consec_fe} — sin saber '
            f'qué se quedó el cliente no hay base para la retención {motivo}. El '
            f'documento contable NO se encoló; sale en la próxima liquidación.'))
    base_gravable, total_iva = _base
    monto_descuento = monto_de_retencion(motivo, base_gravable, total_iva)
    base_gravable_payload = base_de_retencion(motivo, base_gravable, total_iva)
    if monto_descuento <= 0:
        # El monto sale del recálculo contra Siesa de arriba —no de lo que
        # declaró el conductor—, y esa decisión la tomó main con su razón
        # escrita: un ReteIVA mal calculado ya costó ~6× una vez. Lo declarado
        # queda como traza. El `lineas_raw` que nuestra rama releía acá ya está
        # leído arriba, así que el re-fetch sobra.
        logger.warning(
            '[LIQUIDACION] monto_descuento=0 para recaudo %d — DC no encolado',
            recaudo.id
        )
        # `ResultadoDC` y NO un `return` pelado: el llamador lo envuelve en
        # `_contar_dc`, que lee `.estado`. Un `None` acá levanta AttributeError
        # y se lleva puestos los conteos ciertos de RC y NC — el tablero pasaría
        # de informar de más a no informar nada.
        return ResultadoDC(DC_NO_ENCOLADO, (
            f'Recaudo {recaudo.id}: la retención {motivo} calculada sobre la '
            f'factura {tipo_docto_fe}-{consec_fe} dio cero — el documento '
            f'contable NO se encoló.'))

    declarado = float(recaudo.monto_descuento or 0)
    if declarado > 0:
        from app.services.cxc_cruce import TOLERANCIA as _TOL
        if abs(declarado - monto_descuento) > _TOL:
            logger.info(
                '[LIQUIDACION] recaudo %d: estimado declarado ($%.2f) difiere '
                'del valor real de Siesa ($%.2f) para %s — se envía el de '
                'Siesa, la diferencia queda solo trazada acá',
                recaudo.id, declarado, monto_descuento, motivo
            )

    _encolar_retencion(
        recaudo, tipo_ret=motivo, monto=monto_descuento,
        base_gravable=base_gravable_payload,
        tipo_docto_fe=tipo_docto_fe, consec_fe=consec_fe,
        tercero_nit=tercero_nit, sucursal=sucursal,
        co_factura=co_factura, cuenta_cxc=cuenta_cxc,
        unidad_negocio=unidad_negocio, notas=notas, admin_id=admin_id,
    )
    return ResultadoDC(DC_ENCOLADO, None)


def _encolar_retencion(recaudo: RecaudoEntrega, *, tipo_ret: str, monto: float,
                       base_gravable: float, tipo_docto_fe: str, consec_fe,
                       tercero_nit: str, sucursal: str, co_factura: str = '',
                       cuenta_cxc: str = '', unidad_negocio: str = '',
                       notas: str = '', admin_id: int = None,
                       accion_origen: str = None):
    """**El único sitio que encola un DOCUMENTO_CONTABLE_RET.**

    Lo llaman las dos puertas —«Registrar cobro» por parada y «Liquidar ruta»
    masivo— y los dos pasan por la misma política
    (`politica_cobro.exigir_retencion_aplicable`): hasta el 2026-09-25 el botón
    masivo encolaba la NI con solo mirar `motivo_descuento`, sin leer si quien
    liquida había confirmado o rechazado esa retención. Una NI emitida por
    plata que el cliente no tenía derecho a descontar.

    La cuenta PUC ya en cola no se duplica (`_pucs_en_cola`). Devuelve el job,
    o `None` si ya había uno para esa cuenta. Levanta `RetencionNoAplicable`.
    Trinquete AST: `tests/test_politica_cobro.py`.
    """
    from app.services import politica_cobro as _pc
    _pc.exigir_retencion_aplicable(recaudo, tipo_ret)
    cuenta_puc = RETENCION_PUC.get(tipo_ret, '')
    if not cuenta_puc:
        raise ValueError(f'La retención {tipo_ret!r} no tiene cuenta PUC en el catálogo')
    if cuenta_puc in _pucs_en_cola(recaudo.id):
        return None
    payload = {
        'recaudo_id': recaudo.id,
        'tipo_docto_fe': tipo_docto_fe,
        'consec_fe': str(consec_fe),
        'tercero_nit': tercero_nit,
        'sucursal': sucursal,
        'tipo_retencion': tipo_ret,
        'cuenta_puc': cuenta_puc,
        'monto': monto,
        'base_gravable': base_gravable,
        'co_factura': co_factura,
        'cuenta_cxc': cuenta_cxc,
        # `f353_id_un_cruce` real de la fila de cartera: sin él el DC caía al
        # fallback global y Siesa lo rechazaba (job 470, 2026-08-20).
        'unidad_negocio': unidad_negocio,
        'notas': notas,
    }
    if accion_origen:
        payload['accion_origen'] = accion_origen
    job = SiesaJob.encolar(
        tipo='DOCUMENTO_CONTABLE_RET',
        payload=payload,
        referencia_tipo='RecaudoEntrega',
        referencia_id=recaudo.id,
        creado_por_id=admin_id,
    )
    logger.info(
        '[LIQUIDACION] Encolado DOCUMENTO_CONTABLE_RET para recaudo %d (%s, PUC %s, $%.2f)',
        recaudo.id, tipo_ret, cuenta_puc, monto
    )
    return job


def _nombre_retencion(tipo: str) -> str:
    entrada = CATALOGO_RETENCIONES.get(tipo)
    return entrada['nombre'] if entrada else tipo
