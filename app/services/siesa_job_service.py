"""
Dead Letter Queue — procesador de jobs asíncronos hacia Siesa.

El scheduler llama a procesar_jobs_pendientes() cada 1 minuto.
Si Connekta rechaza (periodo cerrado, ítem bloqueado, timeout):
  - Reintento 1 → espera 5 min
  - Reintento 2 → espera 15 min
  - Reintento 3 → espera 45 min
  - Tras 3 fallos → estado=FALLIDO → alerta roja en dashboard admin

Tipos de job implementados:
  TRANSFERENCIA_UBICACIONES → conector 173076 (RESERVA → PICKING)
  (extensible: agregar nuevo tipo + handler en _ejecutar_job)
"""

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from app.extensions import db
from app.models.siesa_job import SiesaJob, EstadoSiesaJob
from app.models.packing import EstadoPacking
from app.models.conteo import EstadoConteo
from app.utils.fecha import fecha_hoy_bogota

logger = logging.getLogger(__name__)


def encolar_transferencia_ubicaciones(
    bodega_id: str,
    ubicacion_origen: str,
    ubicacion_destino: str,
    referencia_item: str,
    cantidad: int,
    nota: str = '',
    centro_op: str = None,
    referencia_tipo: str = None,
    referencia_id: int = None,
) -> SiesaJob:
    """
    Encola una transferencia entre ubicaciones (conector 173066).
    El caller hace commit.
    """
    return SiesaJob.encolar(
        tipo='TRANSFERENCIA_UBICACIONES',
        payload={
            'bodega_id': bodega_id,
            'ubicacion_origen': ubicacion_origen,
            'ubicacion_destino': ubicacion_destino,
            'referencia_item': referencia_item,
            'cantidad': cantidad,
            'nota': nota,
            'centro_op': centro_op,
        },
        referencia_tipo=referencia_tipo,
        referencia_id=referencia_id,
    )


def procesar_jobs_pendientes(app=None):
    """
    Procesa todos los jobs PENDIENTE cuyo proximo_intento <= ahora.
    El scheduler lo llama cada 5 minutos.
    """
    from flask import current_app
    _app = app or current_app._get_current_object()

    # [46] Envolver todo el cuerpo en try/except para que un fallo de DB no paralice la DLQ
    try:
        return _procesar_jobs_pendientes_interno(_app)
    except Exception as e:
        logger.error(f'[DLQ] Error inesperado en procesar_jobs_pendientes: {e}', exc_info=True)
        return 0


class DependenciaPendiente(Exception):
    """El job no puede correr todavía porque otro paso no ha ocurrido.

    **No es un fallo, y por eso no gasta reintento.**

    El recibo de caja de una parada PARCIAL espera a que salga su nota
    crédito, y esa NC la desbloquea una **recepción física en bodega**: alguien
    que confirma la devolución. Con el backoff `[5,15,45,120,180]` y 5
    intentos, la espera total es de ~6 horas.

    Una ruta municipal liquidada por la tarde espera a una persona que puede
    llegar mañana. El job se quedaba FALLIDO y **el cobro nunca entraba a
    Siesa** — la cartera fantasma otra vez, por un reloj.

    `intentos` mide fallos. Esperar no es uno. Confundirlos es la misma clase
    de «un contador, dos significados» que ya costó las banderas de
    idempotencia. El precedente estaba al lado: `ConnektaCircuitOpenError`
    tampoco gasta reintento.
    """


_ADVISORY_LOCK_DLQ = 2007  # evita thundering herd cuando Siesa se recupera y hay N workers


def _procesar_jobs_pendientes_interno(_app):
    """Lógica interna de procesamiento — separada para permitir captura de errores DB externos."""
    with _app.app_context():
        # Circuit breaker: si Connekta está caído, no gastar reintentos
        from app.services.connekta_gateway import connekta
        if connekta._cb_state == 'OPEN':
            logger.info('[DLQ] Circuit breaker OPEN — pausando DLQ hasta que Siesa responda')
            return 0

        # Advisory lock: solo un worker procesa la DLQ a la vez.
        # Sin esto, cuando Siesa se recupera y hay 100 jobs acumulados, N workers
        # los atacan simultáneamente saturando la API de Connekta.
        from sqlalchemy import text as _text
        lock = db.session.execute(_text('SELECT pg_try_advisory_xact_lock(:k)'), {'k': _ADVISORY_LOCK_DLQ}).scalar()
        if not lock:
            logger.info('[DLQ] Otro worker ya procesa jobs — omitido')
            return 0

        return _run_dlq_jobs()


def _run_dlq_jobs():
    """Procesa hasta 20 jobs elegibles. Llamado solo cuando el advisory lock está tomado."""
    from datetime import timedelta
    ahora = datetime.utcnow()

    # Recuperar jobs atascados en PROCESANDO por más de 10 min (worker colgado / crash).
    # Usamos fecha_procesando (cuándo entró a PROCESANDO) — fecha_creacion puede ser de horas
    # antes si el job esperó en backoff, lo que causaría falsos reinicios y duplicados en Siesa.
    # Fallback a fecha_creacion para jobs pre-migración sin fecha_procesando.
    _stuck_cutoff = ahora - timedelta(minutes=10)
    _stuck = SiesaJob.query.filter(
        SiesaJob.estado == EstadoSiesaJob.PROCESANDO,
        db.or_(
            db.and_(SiesaJob.fecha_procesando.isnot(None), SiesaJob.fecha_procesando <= _stuck_cutoff),
            db.and_(SiesaJob.fecha_procesando.is_(None),   SiesaJob.fecha_creacion   <= _stuck_cutoff),
        ),
    ).all()
    if _stuck:
        for j in _stuck:
            j.estado = EstadoSiesaJob.PENDIENTE
            j.intentos = (j.intentos or 0) + 1
            logger.warning(f'[DLQ] Job {j.id} stuck PROCESANDO >10min — reset a PENDIENTE (intento {j.intentos})')
        db.session.commit()

    # Sweep: SesionConteo stuck en AJUSTANDO >15min sin job activo → re-encolar
    try:
        from app.models.conteo import SesionConteo as _SC
        _ajustando_cutoff = ahora - timedelta(minutes=15)
        _stuck_sesiones = _SC.query.filter(
            _SC.estado == 'AJUSTANDO',
            _SC.siesa_triggered == False,
            db.or_(
                _SC.fecha_cierre <= _ajustando_cutoff,
                db.and_(_SC.fecha_cierre.is_(None), _SC.fecha_creacion <= _ajustando_cutoff),
            ),
        ).all()
        for _ss in _stuck_sesiones:
            _tiene_job = SiesaJob.query.filter_by(
                referencia_tipo='SesionConteo', referencia_id=_ss.id,
                tipo='AJUSTE_CONTEO',
            ).filter(SiesaJob.estado.in_(
                list(EstadoSiesaJob.ACTIVOS) + [EstadoSiesaJob.FALLIDO]
            )).first()
            if not _tiene_job:
                logger.warning(f'[DLQ] SesionConteo {_ss.id} stuck AJUSTANDO >15min sin job — re-encolando')
                SiesaJob.encolar(
                    tipo='AJUSTE_CONTEO',
                    payload={'sesion_id': _ss.id},
                    referencia_tipo='SesionConteo',
                    referencia_id=_ss.id,
                )
                db.session.commit()
    except Exception as _e_sweep:
        logger.warning(f'[DLQ] Sweep AJUSTANDO falló: {_e_sweep}')

    q = SiesaJob.query.filter(
        SiesaJob.estado == EstadoSiesaJob.PENDIENTE,
        db.or_(
            SiesaJob.proximo_intento.is_(None),
            SiesaJob.proximo_intento <= ahora,
        )
    ).limit(20)
    # skip_locked solo disponible en PostgreSQL — en SQLite lo ignoramos
    try:
        jobs = q.with_for_update(skip_locked=True).all()
    except Exception as _e_skip:
        logger.warning(f'[DLQ] skip_locked no soportado — fallback sin lock: {_e_skip}')
        jobs = q.all()

    if not jobs:
        return 0

    procesados = 0
    # FM_SIESA_UNREACHABLE: si hay muchos jobs pendientes (recuperación tras outage),
    # añadir pausa entre ejecuciones para no inundar Siesa con ráfaga de llamadas.
    _total_pendientes = SiesaJob.query.filter(SiesaJob.estado == EstadoSiesaJob.PENDIENTE).count()
    _inter_job_delay = 1.0 if _total_pendientes > 10 else 0.0
    if _inter_job_delay:
        logger.info(
            f'[DLQ] {_total_pendientes} jobs pendientes — aplicando delay {_inter_job_delay}s '
            f'entre jobs para evitar ráfaga sobre Siesa (thundering herd)'
        )

    # [M8] Time-box: break after 4 min to avoid blocking the scheduler slot
    _dlq_start = datetime.utcnow()
    _DLQ_MAX_SECONDS = 50  # ~50s — deja margen antes del próximo ciclo de 1 min

    for job in jobs:
        # Check elapsed time before starting a new job
        if (datetime.utcnow() - _dlq_start).total_seconds() > _DLQ_MAX_SECONDS:
            logger.warning(
                f'[DLQ] Ciclo excedió {_DLQ_MAX_SECONDS}s — cortando con {procesados} procesados, '
                f'{len(jobs) - procesados} restantes se procesan en el próximo ciclo'
            )
            break

        job.estado = EstadoSiesaJob.PROCESANDO
        job.fecha_procesando = ahora  # registrar cuándo entró a PROCESANDO para stuck-job detection
        db.session.commit()  # lock en el registro

        if _inter_job_delay and procesados > 0:
            import time
            time.sleep(_inter_job_delay)

        try:
            resultado = _ejecutar_job(job)
            job.marcar_completado(resultado)
            db.session.commit()
            logger.info(f'[DLQ] Job {job.id} ({job.tipo}) completado — intento {job.intentos + 1}')
            procesados += 1

            # Actualizar referencia si aplica — aislado para que un fallo aquí
            # no marque el job como FALLIDO (Siesa ya procesó el trabajo)
            try:
                _post_completado(job)
            except Exception as post_err:
                logger.warning(
                    f'[DLQ] Job {job.id} ({job.tipo}) completado en Siesa pero '
                    f'_post_completado falló: {post_err} — revisar referencia manualmente'
                )

        except Exception as e:
            from app.services.connekta_gateway import ConnektaCircuitOpenError
            if isinstance(e, DependenciaPendiente):
                # Esperar no es fallar. Se reprograma sin gastar reintento —
                # si no, una ruta liquidada por la tarde agota los 5 intentos
                # en 6 horas esperando una recepción que ocurre mañana, y el
                # cobro no entra nunca.
                job.estado = EstadoSiesaJob.PENDIENTE
                job.proximo_intento = datetime.utcnow() + timedelta(minutes=30)
                job.error_ultimo = str(e)[:2000]
                db.session.commit()
                logger.info('[DLQ] Job %s en espera: %s', job.id, e)
                continue
            if isinstance(e, ConnektaCircuitOpenError):
                # Circuit breaker abierto — NO gastar reintento.
                # El job se queda en PROCESANDO/PENDIENTE y se reintenta
                # cuando el circuit cierre.
                job.estado = EstadoSiesaJob.PENDIENTE
                job.proximo_intento = None  # será tomado en el próximo ciclo post-recovery
                db.session.commit()
                logger.info(
                    '[DLQ] Job %s (%s) pausado por circuit breaker — no gasta reintento',
                    job.id, job.tipo
                )
                continue

            error_msg = str(e)
            job.marcar_fallo(error_msg)
            db.session.commit()

            if job.estado == EstadoSiesaJob.FALLIDO:
                logger.error(
                    f'[DLQ] Job {job.id} ({job.tipo}) FALLIDO tras {job.intentos} intentos: {error_msg}'
                )
                _marcar_motivo_dian_manual(job, error_msg)
                _crear_alerta_admin(job)
            else:
                logger.warning(
                    f'[DLQ] Job {job.id} ({job.tipo}) falló (intento {job.intentos}/'
                    f'{job.max_intentos}) — reintento en '
                    f'{_BACKOFF_LABELS[min(job.intentos - 1, 2)]}: {error_msg}'
                )

    return procesados


_BACKOFF_LABELS = ['5 min', '15 min', '45 min']


def _marcar_motivo_dian_manual(job: SiesaJob, error_msg: str):
    """Un MOTIVO_DIAN_NC agotado devuelve el paso a contabilidad, por escrito.

    Sin esto el tri-estado se queda en NULL para siempre y la devolución
    aparece como "sin intentar" cuando en realidad se intentó y no se pudo —
    la clase de silencio que hace que un paso manual se salte porque nadie
    recuerda si aplica.
    """
    if job.tipo != 'MOTIVO_DIAN_NC':
        return
    try:
        from app.models.devolucion_cliente import DevolucionCliente as _DC
        devolucion = db.session.get(_DC, job.get_payload().get('devolucion_id'))
        if not devolucion or devolucion.siesa_motivo_dian == 'AUTOMATICO':
            return
        devolucion.siesa_motivo_dian = 'MANUAL'
        devolucion.siesa_motivo_dian_at = datetime.utcnow()
        devolucion.siesa_motivo_dian_detalle = error_msg[:500]
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error('[DLQ] no se pudo marcar motivo DIAN manual (job=%s): %s', job.id, e)


def _construir_lineas_nc(rowids_data: list, es_total: bool, items_devueltos: list,
                          causal: str, motivo: str, uom_default: str, bodega_default: str,
                          bodega_averias: str = None) -> list:
    """
    Construye las líneas del payload de 142946 (trigger_nota_factura) a partir
    de las filas reales de la factura (get_rowids_factura) y lo que se declaró
    devuelto. Función pura, sin I/O — extraída del job NOTA_CREDITO_FACTURA para
    que NOTA_CREDITO_DEVOLUCION_CLIENTE (devoluciones de cliente en Recepción)
    la reutilice sin duplicar esta lógica (ver Regla 0 del CLAUDE.md).

    motivo/uom_default/bodega_default: mismos fallbacks que usaba el bloque
    original (connekta.motivo_ventas/uom_default/bodega) — se pasan explícitos
    para mantener la función pura (sin depender del singleton connekta).

    es_total=True: todas las líneas de la factura, cantidad completa (usado por
    Liquidación cuando no hay items_devueltos explícito). No soporta bodega_averias
    por línea — si se necesita ese flag, usar es_total=False con items_devueltos.
    es_total=False: solo las líneas presentes en items_devueltos (match por
    'codigo'), con su 'cantidad_devuelta'. Si un item trae 'es_averiado': True
    y se pasó bodega_averias, esa línea usa bodega_averias en vez de f150_id.
    """
    lineas_nc = []
    if es_total:
        for row in rowids_data:
            lineas_nc.append({
                'f470_rowid_movto': row['f470_rowid'],
                'f470_cant_base': row.get('f470_cant_base', 1),
                'f470_id_bodega': row.get('f150_id') or bodega_default,
                'f470_id_motivo': motivo,
                'f470_id_causal_devol': causal,
                'f470_id_unidad_medida': row.get('f470_id_unidad_medida') or uom_default,
                'f120_referencia': row.get('f120_referencia', ''),
                # Devolución total: el prorrateo es la línea entera (factor 1).
                # Se calcula igual que en la rama parcial para que
                # `valor_cruce` salga del mismo campo en los dos casos — si
                # solo una rama lo produjera, el cruce de una NC total valdría
                # cero y la cartera quedaría abierta sin que nada avisara.
                'f470_vlr_neto_prorrateado': Decimal(str(row.get('f470_vlr_neto') or 0)),
            })
        return lineas_nc

    devueltos_map = {
        it['codigo']: it
        for it in items_devueltos
        if int(it.get('cantidad_devuelta', 0)) > 0
    }
    for row in rowids_data:
        ref = row.get('f120_referencia', '')
        item = devueltos_map.get(ref)
        if not item:
            continue
        cant_dev = int(item['cantidad_devuelta'])
        bodega = row.get('f150_id') or bodega_default
        if item.get('es_averiado') and bodega_averias:
            bodega = bodega_averias
        # Prorrateo del valor neto (CON IVA) de la línea — get_rowids_factura
        # trae el valor de la línea FACTURADA completa, no por unidad. Usado
        # por trigger_nota_factura_crear_cruzar (251126) para F353_VLR_CRUCE.
        # NUNCA usar f470_vlr_bruto aquí — bug confirmado en vivo 2026-07-30
        # (ver CLAUDE.md): el cruce queda creado pero sin aplicar si el valor
        # no coincide con el saldo real (que incluye IVA) de la factura.
        cant_facturada = row.get('f470_cant_base') or 0
        vlr_neto_linea = row.get('f470_vlr_neto') or 0
        if cant_facturada:
            vlr_prorrateado = (Decimal(str(vlr_neto_linea)) * Decimal(cant_dev)
                                / Decimal(str(cant_facturada)))
        else:
            vlr_prorrateado = Decimal(0)
        lineas_nc.append({
            'f470_rowid_movto': row['f470_rowid'],
            'f470_cant_base': cant_dev,
            'f470_id_bodega': bodega,
            'f470_id_motivo': motivo,
            'f470_id_causal_devol': causal,
            'f470_id_unidad_medida': row.get('f470_id_unidad_medida') or uom_default,
            'f120_referencia': ref,
            'f470_vlr_neto_prorrateado': vlr_prorrateado,
        })
    return lineas_nc


def _factura_saldada_en_siesa(connekta, nit: str, recaudo) -> bool | None:
    """¿La factura ya no tiene saldo en Siesa? `True` | `False` | **`None`**.

    Usada por RECIBO_CAJA en dos momentos: (a) pre-flight, para no duplicar un
    RC ya aplicado por otra vía; (b) tras un POST que lanzó excepción, para
    distinguir un timeout que SÍ entró (Regla 3) de uno que no aplicó nada.

    ## Dos cosas que estaban mal

    **Buscaba por la FACTURA.** Los campos de cruce traen el **PEDIDO**
    —verificado en vivo el 2026-08-11 y escrito con esas palabras en
    `liquidacion_service`, que sí lo hacía bien—. Con la clave equivocada no
    encontraba nunca ninguna fila.

    **Y devolvía `False` cuando no encontraba.** Así que tras un timeout
    respondía «el RC no entró», el job revertía la bandera y la cola reenviaba:
    **segundo recibo de caja**. Exactamente el incidente que la Regla 3 existe
    para prevenir.

    Ahora la búsqueda vive en `services/cxc_cruce.py` —una sola— y el «no sé»
    tiene su propio valor. El caller decide, y ante `None` la Regla 3 manda:
    no reintentar.
    """
    from app.services import cxc_cruce as _cx
    try:
        tarea = getattr(recaudo, 'tarea', None) if recaudo else None
        tipo_pedido = getattr(tarea, 'tipo_docto_pedido_siesa', None)
        consec_pedido = getattr(tarea, 'consec_docto_pedido_siesa', None)
        if not (nit and tipo_pedido and consec_pedido):
            return None
        return _cx.esta_saldada(connekta.get_cxc_general(nit), tipo_pedido, consec_pedido)
    except Exception as e:                       # noqa: BLE001
        logger.warning('[DLQ] no se pudo verificar el saldo en Siesa: %s', e)
        return None


def _ejecutar_job(job: SiesaJob) -> dict:
    """Despacha el job al handler correcto según su tipo."""
    from app.services.connekta_gateway import connekta
    payload = job.get_payload()

    if job.tipo == 'TRANSFERENCIA_UBICACIONES':
        # P4: transferencias no son idempotentes en Siesa. Si el primer intento llegó
        # (timeout de red) y reintentamos, creamos un doble movimiento de inventario.
        # Solución conservadora: abortar el reintento y dejar que la reconciliación nocturna
        # detecte la discrepancia, en lugar de arriesgar duplicar el traslado en Siesa.
        # NOTA: usamos error_ultimo (no intentos) porque el stuck-sweep incrementa intentos
        # sin llamar a Siesa — un job interrumpido por Railway tendría intentos>0 pero
        # error_ultimo vacío (nunca se ejecutó realmente).
        if job.intentos > 0 and job.error_ultimo:
            logger.warning(
                f'[DLQ] TRANSFERENCIA_UBICACIONES job={job.id} intento={job.intentos + 1} '
                f'abortado por riesgo de duplicado — la reconciliación nocturna detectará '
                f'la discrepancia si el primer intento falló realmente.'
            )
            job.max_intentos = job.intentos  # fuerza FALLIDO en el ciclo siguiente
            raise Exception(
                'Reintento abortado: transferencia no idempotente — revisar manualmente en Siesa'
            )
        return connekta.transferir_entre_ubicaciones(
            bodega_id=payload['bodega_id'],
            ubicacion_origen=payload['ubicacion_origen'],
            ubicacion_destino=payload['ubicacion_destino'],
            referencia_item=payload['referencia_item'],
            cantidad=payload['cantidad'],
            nota=payload.get('nota', ''),
            centro_op=payload.get('centro_op'),
        )

    if job.tipo == 'DESPACHO_F470':
        # Idempotencia: si un intento anterior llegó a Siesa (siesa_triggered=True),
        # no volver a llamar — evita crear remisión duplicada.
        from app.models.packing import TareaPacking
        import json as _json
        tarea = TareaPacking.query.get(payload.get('tarea_id'))
        if tarea and tarea.siesa_triggered:
            logger.info(
                f'[DLQ] DESPACHO_F470 job={job.id}: tarea {tarea.id} ya tiene '
                f'siesa_triggered=True — omitiendo llamada a Siesa (idempotencia)'
            )
            return {'idempotente': True, 'tarea_id': tarea.id}

        # Reconciliación automática: Siesa puede tener la factura aunque WMS no lo sepa
        # (respuesta HTTP perdida por restart/timeout). Si ya existe → corregir WMS sin reenviar.
        if tarea and not tarea.siesa_triggered:
            from app.services.reconciliacion_service import ReconciliacionService
            rec = ReconciliacionService.reconciliar_despacho(
                tarea,
                tipo_docto=payload.get('tipo_docto_pedido', ''),
                consec_docto=payload.get('consec_docto_pedido', ''),
            )
            if rec.get('reconciliado'):
                return rec

        # 142945→142943: RemisionPedido → FacturaRemision.
        # La RM descarga inventario cuenta 14 directamente — sin dependencia de automatización Siesa.
        # DespachoParialService maneja idempotencia (rm_tipo/rm_consec en BD), cabecera del pedido
        # y el encadenamiento completo incluyendo _persistir_resultado (siesa_triggered, DESPACHADO).
        if not tarea:
            raise ValueError(f'DESPACHO_F470 job={job.id}: tarea_id={payload.get("tarea_id")} no encontrada')
        from app.services.despacho_parcial_service import DespachoParialService
        items = payload.get('items', [])
        if items and all(float(i.get('cantidad_empacada') or 0) <= 0 for i in items):
            raise ValueError(
                f'DESPACHO_F470 job={job.id}: pedido {payload.get("numero_pedido_siesa")} — '
                'todos los ítems tienen cantidad_empacada=0. Sin stock para remisionar. '
                'Cancelar el packing o ajustar cantidades antes de reintentar.'
            )
        cantidades = {
            i['producto_codigo']: float(i.get('cantidad_empacada') or 0)
            for i in items
            if float(i.get('cantidad_empacada') or 0) > 0
        }
        resultado = DespachoParialService.despachar_parcial(tarea, cantidades)
        logger.info('[DLQ] DESPACHO_F470 job=%s tarea=%s → DESPACHADO (142945→142943)', job.id, tarea.id)
        return resultado

    if job.tipo == 'ENTRADA_OC':
        # Idempotencia: si un intento anterior llegó a Siesa (siesa_triggered=True),
        # no volver a llamar — evita crear entrada contable duplicada.
        from app.models.recepcion import RecepcionMercancia
        import json as _json
        rec = RecepcionMercancia.query.get(payload.get('recepcion_id'))
        if rec and rec.siesa_triggered:
            logger.info(
                f'[DLQ] ENTRADA_OC job={job.id}: recepción {rec.id} ya tiene '
                f'siesa_triggered=True — omitiendo llamada a Siesa (idempotencia)'
            )
            return {'idempotente': True, 'recepcion_id': rec.id}
        resultado = connekta.confirmar_entrada_compras(
            id_co_oc=payload.get('id_co_oc', connekta.centro_op),
            tipo_docto_oc=payload.get('tipo_docto_oc', ''),
            consec_docto_oc=payload.get('consec_docto_oc', ''),
            items=payload.get('items', []),
            es_parcial=payload.get('es_parcial', False),
            proveedor_id=payload.get('proveedor_id', ''),
            sucursal_prov=payload.get('sucursal_prov', ''),
            tercero_comprador=payload.get('tercero_comprador'),
            moneda_docto=payload.get('moneda_docto'),
            moneda_conv=payload.get('moneda_conv'),
            moneda_local=payload.get('moneda_local'),
            tasa_conv=payload.get('tasa_conv', 0.0),
            tasa_local=payload.get('tasa_local', 0.0),
            num_docto_referencia=payload.get('num_docto_referencia'),
            cond_pago=payload.get('cond_pago', ''),
        )
        # Persistir flag — misma protección que DESPACHO_F470 (emergency block)
        # No marcar triggered en modo ensayo: el POST fue bloqueado, no hay entrada real en Siesa.
        if rec and not rec.siesa_triggered and not resultado.get('modo_ensayo'):
            try:
                rec.siesa_triggered = True
                rec.siesa_response = _json.dumps(resultado)
                rec.siesa_triggered_at = datetime.utcnow()
                db.session.commit()
            except Exception as _e:
                logger.critical(
                    f'[DLQ] ENTRADA_OC job={job.id}: Siesa OK pero fallo al guardar '
                    f'siesa_triggered — revisar manualmente recepción {rec.id}. Error: {_e}'
                )
                db.session.rollback()
                # Emergency: persistir SOLO el flag de idempotencia para bloquear
                # el reintento de la DLQ — sin esto la próxima ejecución llamará a
                # Siesa de nuevo y creará una entrada contable duplicada (cuenta 1435).
                try:
                    rec.siesa_triggered = True
                    rec.siesa_triggered_at = datetime.utcnow()
                    db.session.commit()
                except Exception as _e2:
                    db.session.rollback()
                    logger.critical(
                        f'[DLQ] ENTRADA_OC job={job.id}: DOBLE FALLO — '
                        f'siesa_triggered no persiste: {_e2}. '
                        f'Recepción {rec.id} en riesgo de duplicado contable.'
                    )
        return resultado

    if job.tipo == 'TRASLADO_AVERIAS':
        from app.models.devolucion import TareaDevolucion as _TareaDev
        tarea_dev = _TareaDev.query.get(payload.get('tarea_id'))

        # P4: idempotencia — si tarea_dev no existe o ya tiene triggered, no reenviar
        if tarea_dev is None:
            logger.warning(
                f'[DLQ] TRASLADO_AVERIAS job={job.id}: tarea_id={payload.get("tarea_id")} '
                f'no existe en DB — omitiendo llamada a Siesa para evitar duplicado sin clave'
            )
            return {'idempotente': True, 'sin_tarea': True}

        if tarea_dev.siesa_triggered:
            logger.info(
                f'[DLQ] TRASLADO_AVERIAS job={job.id}: tarea {tarea_dev.id} ya tiene '
                f'siesa_triggered=True — omitiendo llamada (idempotencia P4)'
            )
            return {'idempotente': True, 'tarea_id': tarea_dev.id}

        item_codigo = payload.get('item_codigo')
        if not item_codigo:
            raise ValueError(
                f'TRASLADO_AVERIAS job={job.id}: item_codigo faltante en payload — '
                f'no se puede enviar a Siesa sin referencia del ítem'
            )

        resultado = connekta.transferir_a_averias(
            item_codigo=item_codigo,
            cantidad=payload['cantidad'],
            referencia=payload.get('referencia', ''),
        )
        # Marcar triggered — emergency block para bloquear reintento duplicado
        # No marcar triggered en modo ensayo: el POST fue bloqueado, no hay traslado real en Siesa.
        if tarea_dev and not resultado.get('modo_ensayo'):
            try:
                tarea_dev.siesa_triggered = True
                tarea_dev.siesa_triggered_at = datetime.utcnow()
                db.session.commit()
            except Exception as _e:
                logger.critical(
                    f'[DLQ] TRASLADO_AVERIAS job={job.id}: Siesa OK pero fallo al guardar '
                    f'siesa_triggered — revisar manualmente tarea_dev {tarea_dev.id}. Error: {_e}'
                )
                db.session.rollback()
                # Emergency: sin este commit la DLQ reintentará y el traslado NB1→AV1
                # se duplicará — el saldo de NB1 puede quedar negativo en Siesa.
                try:
                    tarea_dev.siesa_triggered = True
                    tarea_dev.siesa_triggered_at = datetime.utcnow()
                    db.session.commit()
                except Exception as _e2:
                    db.session.rollback()
                    logger.critical(
                        f'[DLQ] TRASLADO_AVERIAS job={job.id}: DOBLE FALLO — '
                        f'siesa_triggered no persiste: {_e2}. '
                        f'Tarea_dev {tarea_dev.id} en riesgo de traslado duplicado (NB1 puede quedar negativo).'
                    )
        return resultado

    if job.tipo == 'AJUSTE_CONTEO':
        from app.models.conteo import SesionConteo as _SesionConteo
        from app.models.inventario import UbicacionProducto as _UbicProd
        sesion_id = payload.get('sesion_id')
        sesion_cteo = _SesionConteo.query.get(sesion_id)

        if sesion_cteo is None:
            logger.warning(
                f'[DLQ] AJUSTE_CONTEO job={job.id}: sesion_id={sesion_id} '
                f'no existe en DB — omitiendo para evitar ajuste sin clave'
            )
            return {'idempotente': True, 'sin_sesion': True}

        # Guard: detectar estado inconsistente (AJUSTADO + siesa_triggered=False).
        # Siesa ya registró el ajuste pero el mini-commit de siesa_triggered falló.
        # Corregir siesa_triggered para que auditorías no muestren "sin Siesa".
        if sesion_cteo.estado == EstadoConteo.AJUSTADO and not sesion_cteo.siesa_triggered:
            logger.critical(
                f'[DLQ] AJUSTE_CONTEO job={job.id}: sesion {sesion_id} AJUSTADO '
                f'pero siesa_triggered=False — inconsistencia detectada. '
                f'Siesa pudo haberlo procesado. Corrigiendo flag.'
            )
            try:
                sesion_cteo.siesa_triggered = True
                sesion_cteo.siesa_triggered_at = sesion_cteo.fecha_cierre or datetime.utcnow()
                db.session.commit()
            except Exception as _e_fix:
                db.session.rollback()
                logger.error(f'[DLQ] No se pudo corregir siesa_triggered para sesion {sesion_id}: {_e_fix}')
            return {'idempotente': True, 'sesion_id': sesion_id, 'estado_corregido': True}

        # P4: idempotencia — si siesa_triggered, no reenviar
        if sesion_cteo.siesa_triggered:
            # Si la sesión quedó atascada en AJUSTANDO (crash entre mini-commit y full-commit),
            # recuperar el estado final sin volver a llamar a Siesa.
            if sesion_cteo.estado == EstadoConteo.AJUSTANDO:
                logger.warning(
                    f'[DLQ] AJUSTE_CONTEO job={job.id}: sesion {sesion_id} atascada en '
                    f'AJUSTANDO con siesa_triggered=True — recuperando estado AJUSTADO'
                )
                try:
                    _now_rec = datetime.utcnow()
                    sesion_cteo.estado = EstadoConteo.AJUSTADO
                    sesion_cteo.fecha_cierre = sesion_cteo.fecha_cierre or _now_rec
                    if not sesion_cteo.siesa_response:
                        sesion_cteo.siesa_response = json.dumps({'recuperado_dlq': True, 'job_id': job.id})
                    # Reaplicar cambio de inventario WMS
                    _mc = payload.get('motivo_codigo')
                    _cant = payload.get('cantidad', 0)
                    _inv = (_UbicProd.query
                            .filter_by(
                                ubicacion_id=payload.get('ubicacion_id'),
                                producto_id=payload.get('producto_id')
                            ).with_for_update().first())
                    if _inv:
                        _tpid = payload.get('tarea_picking_id')
                        if _tpid:
                            _inv.bloqueado = max(0, _inv.bloqueado - _cant)
                        if _mc == 'AJ-SAL':
                            _inv.cantidad = max(0, _inv.cantidad - _cant)
                        else:
                            _inv.cantidad += _cant
                    db.session.commit()
                    logger.info(
                        f'[DLQ] AJUSTE_CONTEO job={job.id}: sesion {sesion_id} recuperada → AJUSTADO'
                    )
                except Exception as _e_rec:
                    db.session.rollback()
                    logger.error(
                        f'[DLQ] AJUSTE_CONTEO job={job.id}: fallo al recuperar sesion {sesion_id}: {_e_rec}'
                    )
            else:
                logger.info(
                    f'[DLQ] AJUSTE_CONTEO job={job.id}: sesion {sesion_id} ya tiene '
                    f'siesa_triggered=True — omitiendo llamada (idempotencia P4)'
                )
            return {'idempotente': True, 'sesion_id': sesion_id}

        item_codigo = payload.get('item_codigo')
        if not item_codigo:
            # Payload mínimo (jobs creados antes del formato extendido).
            # Reconstruir todos los campos desde la SesionConteo.
            from app.models.almacen import Almacen as _AlmRec
            item_codigo = sesion_cteo.producto_codigo_siesa
            if not item_codigo:
                raise ValueError(
                    f'AJUSTE_CONTEO job={job.id}: item_codigo faltante en payload '
                    f'y sesion {sesion_id} sin producto_codigo_siesa'
                )
            _alm_r = _AlmRec.query.get(sesion_cteo.almacen_id)
            _dif_r = (
                sesion_cteo.diferencia
                if sesion_cteo.diferencia is not None
                else (sesion_cteo.cantidad_fisica or 0) - (sesion_cteo.existencia_siesa or 0)
            )
            if _dif_r == 0:
                logger.info(
                    f'[DLQ] AJUSTE_CONTEO job={job.id}: diferencia=0 para sesion {sesion_id} '
                    f'— sin ajuste que enviar a Siesa'
                )
                return {'idempotente': True, 'diferencia_cero': True}
            payload = {
                **payload,
                'item_codigo': item_codigo,
                'motivo_codigo': sesion_cteo.motivo_codigo or ('AJ-ENT' if _dif_r > 0 else 'AJ-SAL'),
                'cantidad': abs(_dif_r),
                'referencia': sesion_cteo.codigo or f'CC-{sesion_id}',
                'bodega': _alm_r.bodega_siesa_id if _alm_r else None,
                'centro_op': _alm_r.centro_op_siesa if _alm_r else None,
                'ubicacion_id': sesion_cteo.ubicacion_id,
                'producto_id': sesion_cteo.producto_id,
                'tarea_picking_id': sesion_cteo.tarea_picking_id,
            }
            logger.warning(
                f'[DLQ] AJUSTE_CONTEO job={job.id}: payload mínimo — reconstruido desde '
                f'sesion {sesion_id}: {payload["motivo_codigo"]} {item_codigo} {payload["cantidad"]} uds'
            )

        from app.models.pedido_siesa import PedidoSiesa as _PedidoSiesa
        _ps = _PedidoSiesa.query.filter(
            _PedidoSiesa.item_codigo == item_codigo,
            _PedidoSiesa.item_id_siesa.isnot(None),
        ).first()
        _item_id_siesa = _ps.item_id_siesa if _ps else None
        logger.info(f'[DLQ] AJUSTE_CONTEO item_id_siesa lookup: item={item_codigo} → {_item_id_siesa!r}')

        resultado = connekta.enviar_ajuste_inventario(
            motivo_codigo=payload['motivo_codigo'],
            item_codigo=item_codigo,
            item_id_siesa=_item_id_siesa,
            cantidad=payload['cantidad'],
            referencia=payload.get('referencia', ''),
            bodega=payload.get('bodega'),
            centro_op=payload.get('centro_op'),
        )

        # CRÍTICO: commit mínimo de siesa_triggered=True ANTES del commit completo.
        # Si Railway mata el proceso después de este commit, el retry verá
        # siesa_triggered=True en el guard de idempotencia y no llamará Siesa de nuevo.
        # Sin esto, un crash entre el HTTP 200 de Siesa y el commit completo
        # deja siesa_triggered=False → el retry genera un doble ajuste de inventario.
        _now = datetime.utcnow()
        _es_ensayo = bool(resultado.get('modo_ensayo'))
        try:
            sesion_cteo = _SesionConteo.query.get(sesion_id)
            if not _es_ensayo:
                sesion_cteo.siesa_triggered = True
                sesion_cteo.siesa_triggered_at = _now
                db.session.commit()
        except Exception as _e_flag:
            db.session.rollback()
            logger.critical(
                f'[DLQ] AJUSTE_CONTEO job={job.id}: no se pudo persistir siesa_triggered '
                f'para sesion {sesion_id}: {_e_flag} — abortando para no dejar estado ambiguo'
            )
            raise

        # Commit completo: estado + respuesta + inventario
        try:
            sesion_cteo = _SesionConteo.query.get(sesion_id)
            sesion_cteo.siesa_response = json.dumps(resultado)
            sesion_cteo.estado = EstadoConteo.AJUSTADO
            sesion_cteo.fecha_cierre = _now

            # Actualizar stock WMS local — mantiene sincronía sin esperar sync nocturna
            motivo_codigo = payload['motivo_codigo']
            cantidad_ajuste = payload['cantidad']
            inv = (_UbicProd.query
                   .filter_by(
                       ubicacion_id=payload.get('ubicacion_id'),
                       producto_id=payload.get('producto_id')
                   ).with_for_update().first())
            if inv:
                # Desbloquear solo si vino de excepción de picking
                tarea_picking_id = payload.get('tarea_picking_id')
                if tarea_picking_id:
                    inv.bloqueado = max(0, inv.bloqueado - cantidad_ajuste)
                # Ajustar stock WMS (conteos cíclicos + excepciones)
                if motivo_codigo == 'AJ-SAL':
                    inv.cantidad = max(0, inv.cantidad - cantidad_ajuste)
                else:
                    inv.cantidad += cantidad_ajuste

            db.session.commit()
        except Exception as _e:
            db.session.rollback()
            # siesa_triggered=True ya persistido — no hay riesgo de doble ajuste.
            # Solo logueamos que el estado/respuesta no se guardaron (no crítico).
            logger.error(
                f'[DLQ] AJUSTE_CONTEO job={job.id}: siesa_triggered OK pero '
                f'fallo guardando estado/respuesta para sesion {sesion_id}: {_e}'
            )
        return resultado

    if job.tipo == 'ALERTA_EMAIL':
        tipo_alerta = payload.get('tipo_alerta', 'desconocido')
        asunto = payload.get('asunto', 'sin asunto')
        error_original = payload.get('error', '')
        # [A17] Actually retry sending the email — the DLQ backoff may have given
        # Resend time to recover. Only fall back to CRITICAL log if retry also fails.
        try:
            from app.services.alertas_service import enviar_email as _enviar
            _enviado = _enviar(
                asunto=f'[RETRY] {asunto}',
                cuerpo_html=f'<p>Alerta original falló: {error_original}</p>',
                cuerpo_texto=f'Alerta original falló: {error_original}',
            )
            if _enviado:
                logger.info(f'[ALERTA_EMAIL] Retry exitoso para "{tipo_alerta}" ({asunto})')
                return {'procesado': True, 'tipo_alerta': tipo_alerta, 'email_reenviado': True}
        except Exception as _e_retry:
            logger.warning(f'[ALERTA_EMAIL] Retry de email también falló: {_e_retry}')
        logger.critical(
            f'[ALERTA_EMAIL] Email de alerta "{tipo_alerta}" ({asunto}) no fue enviado: '
            f'{error_original}. Verificar RESEND_API_KEY y ALERTA_EMAIL_DEST en Railway.'
        )
        return {'procesado': True, 'tipo_alerta': tipo_alerta, 'nota': 'ver logs CRITICAL'}

    if job.tipo == 'DESPACHO_TRASLADO':
        # 174930 — Transfer desde RIT → Salida en Tránsito (STS)
        # Idempotencia: siesa_salida_consec ya guardado → no reenviar
        from app.models.traslado import SolicitudTraslado, EstadoTraslado
        solicitud_id = payload.get('solicitud_id')
        solicitud = SolicitudTraslado.query.get(solicitud_id) if solicitud_id else None
        if not solicitud:
            raise ValueError(f'DESPACHO_TRASLADO job={job.id}: solicitud_id={solicitud_id} no encontrada')
        if solicitud.siesa_salida_consec:
            logger.info(
                '[DLQ] DESPACHO_TRASLADO job=%s: solicitud %s ya tiene siesa_salida_consec=%s — '
                'idempotente, omitiendo', job.id, solicitud.codigo, solicitud.siesa_salida_consec
            )
            return {'idempotente': True, 'solicitud_id': solicitud_id}

        from app.services.siesa_traslado_adapter import siesa_traslado as _st
        from app.services.traslado_service import TrasladoService

        consec_rit = payload.get('consec_rit') or solicitud.siesa_requisicion_consec
        if consec_rit:
            res = _st.despachar_desde_rit(consec_rit=consec_rit, codigo=solicitud.codigo)
        else:
            bodega_transito = solicitud.bodega_transito_siesa or _st.bodega_transito
            if not bodega_transito:
                raise ValueError(
                    f'DESPACHO_TRASLADO job={job.id}: sin RIT ni bodega_transito configurada'
                )
            items = payload.get('items', [])
            res = _st.registrar_salida_transito(
                bodega_origen=solicitud.bodega_origen_siesa,
                bodega_transito=bodega_transito,
                items=items,
                codigo=solicitud.codigo,
                consec_requisicion=None,
                bodega_destino=solicitud.bodega_destino_siesa,
            )

        if not res.get('simulado') and not res.get('modo_ensayo'):
            consec = TrasladoService._extraer_consec(res)
            if consec:
                solicitud.siesa_salida_consec = consec
            else:
                consec_rec = _st.recuperar_consec_salida(solicitud.codigo)
                solicitud.siesa_salida_consec = consec_rec
            solicitud.estado = EstadoTraslado.EN_TRANSITO
            solicitud.siesa_error = None
            from app.extensions import db as _db
            _db.session.commit()
        logger.info('[DLQ] DESPACHO_TRASLADO job=%s solicitud=%s → EN_TRANSITO',
                    job.id, solicitud.codigo)
        return {'solicitud_id': solicitud_id, 'consec': solicitud.siesa_salida_consec}

    # ── Liquidación de ruta: conectores financieros ─────────────

    if job.tipo == 'NOTA_CREDITO_FACTURA':
        # 142946 — Nota crédito amarrada a factura (devolución parcial/total)
        from app.models.recaudo_entrega import RecaudoEntrega as _RE
        recaudo = _RE.query.get(payload.get('recaudo_id'))

        if recaudo and recaudo.siesa_nc_triggered:
            logger.info(
                '[DLQ] NOTA_CREDITO_FACTURA job=%s: recaudo %s ya tiene '
                'siesa_nc_triggered=True — idempotente', job.id, recaudo.id
            )
            return {'idempotente': True, 'recaudo_id': recaudo.id}

        tipo_docto_fe = payload['tipo_docto_fe']
        consec_fe = payload['consec_fe']

        # Paso 1: GET f470_rowid de cada línea de la factura
        rowids_data = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
        if not rowids_data:
            raise Exception(
                f'No se obtuvieron rowids para FE {tipo_docto_fe}-{consec_fe} — '
                'la factura puede no existir en Siesa'
            )

        # Paso 2: construir líneas para la NC
        items_devueltos = payload.get('items_devueltos', [])
        es_total = payload.get('es_total', False)
        causal = payload.get('causal_devolucion') or connekta.causal_devolucion_default

        lineas_nc = _construir_lineas_nc(
            rowids_data, es_total, items_devueltos, causal,
            motivo=connekta.motivo_ventas, uom_default=connekta.uom_default,
            bodega_default=connekta.bodega,
        )

        if not lineas_nc:
            logger.warning(
                '[DLQ] NOTA_CREDITO_FACTURA job=%s: sin líneas para devolver — '
                'posible mismatch de códigos entre items_devueltos y factura Siesa',
                job.id
            )
            return {'sin_lineas': True, 'recaudo_id': payload.get('recaudo_id')}

        # Paso 3: POST 251126 — crea Y cruza la cartera en un solo POST.
        #
        # Antes iba por `trigger_nota_factura` (250696), que SOLO CREA: dejaba
        # la NC sin cruzar y alguien tenía que apretar «Automático» en el tab
        # CxC de Siesa, a mano, por cada devolución de ruta.
        #
        # Y 250696 no está en uso en Connekta (confirmado 2026-08-13). Este
        # camino nunca llegó a hacer POST —el job 440 fallaba antes, al
        # resolver la factura— así que cambiarlo no puede romper nada que
        # funcionara: apuntaba a un conector que no está registrado.
        #
        # 251126 es el que ya se verificó en vivo (FEW-1466, 2026-07-31) y el
        # que usa Devolución de Cliente. Un solo conector para las dos notas
        # crédito — Regla 0.
        valor_cruce = sum(
            (lin.get('f470_vlr_neto_prorrateado', Decimal(0)) for lin in lineas_nc),
            Decimal(0),
        )
        # PRE-flag, no post-flag (Regla 6). Era la única de las tres que
        # marcaba DESPUÉS del POST: un crash entre el POST y el commit dejaba
        # la bandera en False, el DLQ reintentaba y Siesa recibía una **segunda
        # nota crédito**. RC y DC ya marcaban antes; ésta no, y nada explicaba
        # por qué.
        if recaudo:
            recaudo.siesa_nc_triggered = True
            db.session.commit()

        try:
            resultado = connekta.trigger_nota_factura_crear_cruzar(
                tipo_docto_fe=tipo_docto_fe,
                consec_fe=consec_fe,
                lineas=lineas_nc,
                valor_cruce=float(valor_cruce),
                notas=payload.get('notas', ''),
            )
        except Exception as _e_post:
            # Fallo explícito: no se creó nada, se revierte para que el DLQ
            # reintente. La otra mitad del pre-flag.
            if recaudo:
                try:
                    recaudo.siesa_nc_triggered = False
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            raise _e_post

        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if recaudo and _es_ensayo:
            # Modo ensayo: el POST se bloqueó, no hay documento que proteger.
            try:
                recaudo.siesa_nc_triggered = False
                db.session.commit()
            except Exception:
                db.session.rollback()
        return resultado

    if job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE':
        # 142946 — Nota crédito de devolución de cliente atada al pedido/factura
        # (módulo de Recepción, reemplaza TareaDevolucion). tipo_docto_fe/consec_fe
        # ya vienen resueltos como el tipo/consec REAL de la factura electrónica
        # (connekta.get_detalle_factura, ver devolucion_cliente_service.py) —
        # nunca los del pedido.
        from app.models.devolucion_cliente import DevolucionCliente as _DC
        devolucion = db.session.get(_DC, payload.get('devolucion_id'))

        if devolucion and devolucion.siesa_nc_triggered:
            logger.info(
                '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: devolución %s ya tiene '
                'siesa_nc_triggered=True — idempotente', job.id, devolucion.id
            )
            return {'idempotente': True, 'devolucion_id': devolucion.id}

        tipo_docto_fe = payload['tipo_docto_fe']
        consec_fe = payload['consec_fe']

        rowids_data = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
        if not rowids_data:
            raise Exception(
                f'No se obtuvieron rowids para FE {tipo_docto_fe}-{consec_fe} — '
                'la factura puede no existir en Siesa'
            )

        items_devueltos = payload.get('items_devueltos', [])
        causal = connekta.causal_devolucion_default
        # Siempre por match de items (nunca la rama es_total="todas las líneas
        # completas"): items_devueltos ya trae la cantidad exacta contada por
        # línea y el flag es_averiado por línea — la rama es_total del conector
        # de Liquidación no soporta ese flag por línea.
        lineas_nc = _construir_lineas_nc(
            rowids_data, es_total=False, items_devueltos=items_devueltos,
            causal=causal, motivo=connekta.motivo_ventas, uom_default=connekta.uom_default,
            bodega_default=connekta.bodega, bodega_averias=connekta.bodega_averias,
        )

        if not lineas_nc:
            logger.warning(
                '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: sin líneas para devolver — '
                'posible mismatch de códigos entre items_devueltos y factura Siesa',
                job.id
            )
            return {'sin_lineas': True, 'devolucion_id': payload.get('devolucion_id')}

        # 251126 crea la NC Y cruza la cartera en el mismo POST (ver CLAUDE.md
        # "Cruce de cartera SÍ se pudo automatizar") — reemplaza a
        # trigger_nota_factura (250696), que solo creaba. valor_cruce es la
        # suma del valor neto prorrateado por línea, calculado en
        # _construir_lineas_nc — NUNCA el bruto (bug confirmado 2026-07-30).
        valor_cruce = sum(
            (lin['f470_vlr_neto_prorrateado'] for lin in lineas_nc), Decimal(0)
        )
        # Marca de agua ANTES de crear: distingue nuestra NC de otra idéntica
        # creada hoy por otra devolución del mismo valor. Nunca bloqueante —
        # `get_max_rowid_nc` se traga sus propios errores; sin marca la
        # identificación posterior es más estricta, no más laxa.
        _puede_dian = connekta.puede_fijar_motivo_dian
        _rowid_antes = connekta.get_max_rowid_nc() if _puede_dian else None
        _fecha_nc = fecha_hoy_bogota()
        resultado = connekta.trigger_nota_factura_crear_cruzar(
            tipo_docto_fe=tipo_docto_fe,
            consec_fe=consec_fe,
            lineas=lineas_nc,
            valor_cruce=float(valor_cruce),
            notas=payload.get('notas', ''),
        )

        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if devolucion and not _es_ensayo:
            try:
                devolucion.siesa_nc_triggered = True
                devolucion.siesa_nc_triggered_at = datetime.utcnow()
                devolucion.siesa_nc_response = json.dumps(resultado, ensure_ascii=False)
                db.session.commit()
            except Exception as _e:
                db.session.rollback()
                logger.critical(
                    '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: Siesa OK pero fallo '
                    'siesa_nc_triggered — devolución %s en riesgo de NC duplicada: %s',
                    job.id, devolucion.id, _e
                )
                try:
                    devolucion.siesa_nc_triggered = True
                    db.session.commit()
                except Exception:
                    db.session.rollback()

        # Bridge Liquidación de ruta → Devoluciones: si esta devolución se
        # originó en una entrega Parcial/Rechazada ("Liquidar en WMS", ver
        # liquidacion_service._crear_devolucion_pendiente), avisarle al
        # RecaudoEntrega que su NC ya salió — es la señal que el RECIBO_CAJA
        # dependiente (depende_de_nc) está esperando para poder dispararse.
        if devolucion and not _es_ensayo and devolucion.recaudo_entrega_id:
            try:
                from app.models.recaudo_entrega import RecaudoEntrega as _REC
                recaudo_origen = db.session.get(_REC, devolucion.recaudo_entrega_id)
                if recaudo_origen and not recaudo_origen.siesa_nc_triggered:
                    recaudo_origen.siesa_nc_triggered = True
                    db.session.commit()
                    logger.info(
                        '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: recaudo %s '
                        'marcado siesa_nc_triggered=True — RC dependiente puede dispararse',
                        job.id, recaudo_origen.id
                    )
            except Exception as _e:
                db.session.rollback()
                logger.critical(
                    '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: NC de devolución %s OK pero '
                    'fallo el bridge a recaudo %s — el RC dependiente quedará bloqueado hasta '
                    'revisión manual: %s',
                    job.id, devolucion.id, devolucion.recaudo_entrega_id, _e
                )

        # Paso 3 del procedimiento manual (motivo DIAN) — job aparte, nunca
        # inline: si fallara acá, el reintento del DLQ entraría por la guarda
        # de idempotencia de arriba y nunca volvería a intentarlo. Y nada de lo
        # que pase con el motivo puede tocar la NC, que ya existe en Siesa.
        if devolucion and not _es_ensayo:
            try:
                if _puede_dian:
                    SiesaJob.encolar(
                        'MOTIVO_DIAN_NC',
                        {
                            'devolucion_id': devolucion.id,
                            'valor_cruce': float(valor_cruce),
                            'fecha': _fecha_nc,
                            'rowid_antes': _rowid_antes,
                        },
                        referencia_tipo='devolucion_cliente',
                        referencia_id=devolucion.id,
                    )
                else:
                    devolucion.siesa_motivo_dian = 'MANUAL'
                    devolucion.siesa_motivo_dian_at = datetime.utcnow()
                    devolucion.siesa_motivo_dian_detalle = (
                        'automatización no configurada '
                        '(falta CONNEKTA_CONSULTA_NC_CONSECUTIVO)'
                        if not connekta.modo_simulacion else 'modo simulación'
                    )
                db.session.commit()
            except Exception as _e:
                db.session.rollback()
                logger.error(
                    '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: NC OK pero no se '
                    'pudo encadenar el motivo DIAN — queda manual: %s', job.id, _e
                )
        return resultado

    if job.tipo == 'MOTIVO_DIAN_NC':
        # 251546 — le pone el concepto DIAN a una NC que ya existe en
        # Elaboración (paso 3 del "Procedimiento Manual" del CLAUDE.md).
        # Aprobar (paso 4) sigue sin solución de API.
        from app.models.devolucion_cliente import DevolucionCliente as _DC
        devolucion = db.session.get(_DC, payload.get('devolucion_id'))
        if not devolucion:
            return {'sin_devolucion': True, 'devolucion_id': payload.get('devolucion_id')}

        if devolucion.siesa_motivo_dian == 'AUTOMATICO':
            logger.info(
                '[DLQ] MOTIVO_DIAN_NC job=%s: devolución %s ya tiene motivo — idempotente',
                job.id, devolucion.id
            )
            return {'idempotente': True, 'devolucion_id': devolucion.id}

        consec_nc = int(devolucion.siesa_nc_consec) if devolucion.siesa_nc_consec \
            else connekta.get_consec_nc_creada(
                valor_cruce=payload['valor_cruce'],
                fecha=payload['fecha'],
                rowid_minimo=payload.get('rowid_antes'),
            )
        # Se guarda ANTES del POST y por separado: saber qué NCE es ya vale por
        # sí solo (contabilidad la tenía que buscar a mano en Auditoría), y si
        # el motivo falla no hay razón para volver a resolver el consecutivo.
        if not devolucion.siesa_nc_consec:
            devolucion.siesa_nc_consec = str(consec_nc)
            db.session.commit()

        resultado = connekta.trigger_motivo_dian_nc(consec_nc)
        if not resultado.get('modo_ensayo'):
            devolucion.siesa_motivo_dian = 'AUTOMATICO'
            devolucion.siesa_motivo_dian_at = datetime.utcnow()
            devolucion.siesa_motivo_dian_detalle = (
                f'{connekta.tipo_docto_nota_credito}-{consec_nc} '
                f'concepto={connekta.concepto_dian_nc}'
            )
            db.session.commit()
        return {**resultado, 'consec_nc': consec_nc}

    if job.tipo == 'RECIBO_CAJA':
        # 142888 — Recibo de caja (cobro del conductor)
        from app.models.recaudo_entrega import RecaudoEntrega as _RE
        recaudo = _RE.query.get(payload.get('recaudo_id'))

        if recaudo and recaudo.siesa_rc_triggered:
            logger.info(
                '[DLQ] RECIBO_CAJA job=%s: recaudo %s ya tiene '
                'siesa_rc_triggered=True — idempotente', job.id, recaudo.id
            )
            return {'idempotente': True, 'recaudo_id': recaudo.id}

        # Secuencialidad: si depende de NC, verificar que NC ya pasó.
        # `DependenciaPendiente` y no `Exception`: esperar no gasta reintento.
        if payload.get('depende_de_nc') and recaudo and not recaudo.siesa_nc_triggered:
            raise DependenciaPendiente(
                f'RECIBO_CAJA job={job.id}: RC espera la NC del recaudo '
                f'{recaudo.id}, que la desbloquea la recepción física de la '
                f'devolución. Sigue pendiente.'
            )

        # Re-read monto: si el recaudo fue editado post-enqueue, usar el valor actual
        monto_payload = float(payload['monto'])
        if recaudo:
            monto_actual = float(recaudo.monto_cobrado or 0)
            if abs(monto_actual - monto_payload) > 1 and monto_actual > 0:
                logger.warning(
                    '[DLQ] RECIBO_CAJA job=%s: monto payload=%.2f difiere de '
                    'monto_cobrado actual=%.2f — usando actual',
                    job.id, monto_payload, monto_actual
                )
                monto_payload = monto_actual

        # Pre-flight: ¿la factura que vamos a pagar ya quedó sin saldo?
        # (cross-flow WMS↔Cartera — alguien más ya la cruzó por otra vía)
        nit_rc = payload.get('tercero_nit', '')
        # `is True` y no truthy: `None` significa «no pude verificar», y saltarse
        # el envío por no saber dejaría la factura sin recibo para siempre.
        if _factura_saldada_en_siesa(connekta, nit_rc, recaudo) is True:
            logger.info(
                '[DLQ] RECIBO_CAJA job=%s: factura %s-%s ya sin saldo pendiente '
                '(pre-flight) — marcando completado sin enviar',
                job.id, payload.get('tipo_docto_fe', ''), payload.get('consec_fe', ''),
            )
            if recaudo:
                recaudo.siesa_rc_triggered = True
                db.session.commit()
            return {'ya_existente': True, 'recaudo_id': payload.get('recaudo_id')}

        # Pre-flag: marcar ANTES del POST para cerrar el crash window.
        if recaudo:
            recaudo.siesa_rc_triggered = True
            db.session.commit()

        try:
            resultado = connekta.trigger_recibo_caja(
                tercero_nit=payload['tercero_nit'],
                sucursal=payload.get('sucursal', '001'),
                monto=monto_payload,
                forma_pago=payload.get('forma_pago', 'EFECTIVO'),
                tipo_docto_fe=payload['tipo_docto_fe'],
                consec_fe=payload['consec_fe'],
                co_factura=payload.get('co_factura', ''),
                cuenta_cxc=payload.get('cuenta_cxc', ''),
                notas=payload.get('notas', ''),
            )
        except Exception as _e_post:
            # POST falló — verificar el saldo real antes de revertir (Regla #3:
            # un timeout no significa que falló, puede que sí haya entrado)
            _rc_entro = _factura_saldada_en_siesa(
                connekta, payload.get('tercero_nit', ''), recaudo)
            if _rc_entro is True:
                logger.info(
                    '[DLQ] RECIBO_CAJA job=%s: POST falló pero API 21 confirma '
                    'que RC sí entró a Siesa — manteniendo flag', job.id
                )
                return {'timeout_pero_exitoso': True}
            if _rc_entro is None:
                # NO SE SABE. Regla 3: un timeout no significa que falló, y un
                # recibo de caja duplicado es un documento financiero que
                # alguien tiene que reversar a mano. Se conserva la bandera —el
                # lado que NO reintenta— y se declara, porque la factura puede
                # quedar sin recibo y eso lo ve el desglose como saldo abierto.
                logger.error(
                    '[DLQ] RECIBO_CAJA job=%s: POST falló y NO se pudo verificar '
                    'el saldo del recaudo %s. No se reintenta (Regla 3) — revisar '
                    'en Siesa si el RC entró.',
                    job.id, getattr(recaudo, 'id', '?')
                )
                return {'verificacion_imposible': True,
                        'recaudo_id': getattr(recaudo, 'id', None)}
            # Confirmado que NO entró — revertir pre-flag y reintentar
            if recaudo:
                try:
                    recaudo.siesa_rc_triggered = False
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            raise _e_post

        # Si modo ensayo, revertir flag (no se creó nada en Siesa)
        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if recaudo and _es_ensayo:
            try:
                recaudo.siesa_rc_triggered = False
                db.session.commit()
            except Exception:
                db.session.rollback()
        return resultado

    if job.tipo == 'DOCUMENTO_CONTABLE_RET':
        # 142882 — Documento contable para retenciones
        from app.models.recaudo_entrega import RecaudoEntrega as _RE
        recaudo = _RE.query.get(payload.get('recaudo_id'))

        # La guarda es POR CUENTA PUC, no por recaudo.
        #
        # Las retenciones son N: retefuente + reteIVA + ICA son tres documentos
        # distintos. Con la guarda sobre el booleano, el primer job la encendía
        # y **los otros dos se declaraban idempotentes sin enviar nada** — tres
        # jobs completados, un documento en Siesa.
        _puc = payload.get('cuenta_puc')
        if recaudo and _puc and _puc in recaudo.pucs_enviadas():
            logger.info(
                '[DLQ] DOCUMENTO_CONTABLE_RET job=%s: recaudo %s ya envió la '
                'cuenta %s — idempotente', job.id, recaudo.id, _puc
            )
            return {'idempotente': True, 'recaudo_id': recaudo.id, 'cuenta_puc': _puc}

        # Secuencialidad: NI de retenciones DEBE ir DESPUÉS del RC.
        # Si el RC no pasó aún, el cruce CxC del NI puede fallar porque
        # Siesa no ha reducido el saldo por el cash todavía.
        if recaudo and not recaudo.siesa_rc_triggered:
            raise DependenciaPendiente(
                f'DOCUMENTO_CONTABLE_RET job={job.id}: DC espera el RC del '
                f'recaudo {recaudo.id}. Sigue pendiente.'
            )

        # Pre-flag: cerrar crash window (misma lógica que RC), por cuenta.
        if recaudo and _puc:
            recaudo.marcar_puc_enviada(_puc)
            db.session.commit()

        try:
            resultado = connekta.trigger_documento_contable(
                tercero_nit=payload['tercero_nit'],
                sucursal=payload.get('sucursal', '001'),
                cuenta_puc=payload['cuenta_puc'],
                monto=float(payload['monto']),
                base_gravable=float(payload.get('base_gravable', 0)),
                tipo_docto_fe=payload['tipo_docto_fe'],
                consec_fe=payload['consec_fe'],
                co_factura=payload.get('co_factura', ''),
                cuenta_cxc=payload.get('cuenta_cxc', ''),
                notas=payload.get('notas', ''),
            )
        except Exception as _e_post:
            if recaudo and _puc:
                try:
                    recaudo.desmarcar_puc(_puc)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            raise _e_post

        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if recaudo and _puc and _es_ensayo:
            try:
                recaudo.desmarcar_puc(_puc)
                db.session.commit()
            except Exception:
                db.session.rollback()
        return resultado

    if job.tipo == 'COMPROMISOS_RIT':
        # 174720 — registra sobre la RIT las cantidades que packing confirmó.
        #
        # Se encolaba desde `traslado_service` y **no tenía rama acá**: caía en
        # el `raise` de abajo, quemaba los 5 intentos en ~6 h y moría FALLIDO.
        # El reintento que el comentario del encolado promete nunca funcionó.
        #
        # No es cosmético: sin compromisos, `despachar()` no puede usar el
        # 174930 —que toma las cantidades de la RIT— y cae al 173076 dejando
        # una RIT suelta. Que este job entre a tiempo es lo que evita esa
        # RIT suelta; que no entre ya no descuadra el inventario.
        from app.models.traslado import SolicitudTraslado
        from app.services.siesa_traslado_adapter import siesa_traslado

        payload = job.get_payload()
        s = db.session.get(SolicitudTraslado, payload.get('solicitud_id'))
        if not s:
            return {'omitido': True, 'motivo': 'la solicitud ya no existe'}
        if s.siesa_compromisos_ok:
            return {'idempotente': True, 'motivo': 'compromisos ya registrados'}
        if s.siesa_salida_consec:
            # Ya se despachó por la vía de respaldo con las cantidades reales.
            # Registrar compromisos ahora no cambiaría ese STS y sí dejaría la
            # RIT diciendo algo distinto de lo que se movió.
            return {'omitido': True,
                    'motivo': f'ya despachado (STS {s.siesa_salida_consec}) — '
                              f'la RIT queda suelta, no se toca'}
        if not s.siesa_requisicion_consec:
            raise DependenciaPendiente(
                f'{s.codigo}: la RIT todavía no tiene consecutivo — '
                f'esperar a que 174646 lo resuelva')

        siesa_traslado.registrar_compromisos(
            consec_rit=s.siesa_requisicion_consec,
            bodega_origen=s.bodega_origen_siesa,
            bodega_destino=s.bodega_destino_siesa,
            items=payload.get('items') or [],
            codigo=s.codigo,
        )
        # Después del POST, igual que en el camino en línea: esta bandera abre
        # la compuerta del 174930, no evita un duplicado.
        s.siesa_compromisos_ok = True
        s.siesa_error = None
        db.session.commit()
        logger.info('[DLQ] %s compromisos 174720 registrados en reintento', s.codigo)
        return {'compromisos_registrados': True, 'codigo': s.codigo}

    raise ValueError(f'Tipo de job no reconocido: {job.tipo}')


def _post_completado(job: SiesaJob):
    """Actualiza la entidad referenciada al completarse el job."""
    if not job.referencia_tipo or not job.referencia_id:
        return
    if job.referencia_tipo == 'TareaReposicion':
        from app.models.tarea_reposicion import TareaReposicion
        tarea = TareaReposicion.query.get(job.referencia_id)
        if tarea:
            resultado = json.loads(job.resultado or '{}')
            # No marcar enviado en modo ensayo: el POST fue bloqueado, no hay transferencia real en Siesa.
            if not resultado.get('modo_ensayo'):
                tarea.siesa_enviado = True
                tarea.siesa_job_id = str(resultado.get('consecutivo') or resultado.get('id') or job.id)
                db.session.commit()


def _crear_alerta_admin(job: SiesaJob):
    """
    Log crítico + email inmediato cuando un job alcanza estado=FALLIDO.
    """
    logger.critical(
        f'[ALERTA ADMIN] Job Siesa FALLIDO — id={job.id} tipo={job.tipo} '
        f'ref={job.referencia_tipo}:{job.referencia_id} '
        f'error="{job.error_ultimo}" — Verificar periodo contable en Siesa.'
    )
    # Guard anti-cascade: si el job que falló es ALERTA_EMAIL, no crear otro ALERTA_EMAIL.
    # Sin este guard, un Resend caído genera una cadena infinita:
    # ALERTA_EMAIL FALLIDO → _crear_alerta_admin → alertar_job_fallido → nuevo ALERTA_EMAIL → ...
    if job.tipo == 'ALERTA_EMAIL':
        logger.critical(
            f'[ALERTA ADMIN] El job fallido es ALERTA_EMAIL (id={job.id}) — '
            f'no se crea alerta secundaria para evitar cascade. Revisar Resend manualmente.'
        )
        return
    try:
        from app.services.alertas_service import alertar_job_fallido
        alertar_job_fallido(job)
    except Exception as e:
        logger.error(f'[ALERTAS] No se pudo enviar email de job fallido: {e}')


def get_jobs_fallidos():
    """Para el dashboard del admin."""
    return SiesaJob.query.filter_by(estado=EstadoSiesaJob.FALLIDO).order_by(SiesaJob.fecha_creacion.desc()).all()


def reintentar_job(job_id: int) -> dict:
    """Admin fuerza un reintento de un job FALLIDO."""
    job = SiesaJob.query.get(job_id)
    if not job:
        raise ValueError(f'Job {job_id} no encontrado')
    if job.estado != EstadoSiesaJob.FALLIDO:
        raise ValueError(f'Job {job_id} no está en estado FALLIDO — está {job.estado}')
    job.estado = EstadoSiesaJob.PENDIENTE
    job.intentos = 0
    job.proximo_intento = None
    job.error_ultimo = None
    db.session.commit()
    return job.to_dict()


def disparar_dlq_inmediato(app=None):
    """
    Lanza procesar_jobs_pendientes() en un hilo daemon para procesar jobs recién encolados
    sin bloquear el worker de Gunicorn. El advisory lock (pg_try_advisory_lock) garantiza que
    a lo sumo un hilo corre la DLQ simultáneamente — si el scheduler ya está corriendo, el hilo
    sale en <1ms sin hacer nada.

    Uso: llamar inmediatamente después de commit() que encola un SiesaJob PENDIENTE.
    El hilo procesa el job en segundos en vez de esperar el cron de 5 minutos.
    """
    import threading
    from flask import current_app

    _app = app or current_app._get_current_object()

    def _run():
        try:
            procesar_jobs_pendientes(app=_app)
        except Exception as _e:
            logger.error(f'[DLQ] disparar_dlq_inmediato hilo error: {_e}')

    t = threading.Thread(target=_run, daemon=True, name='dlq-inmediato')
    t.start()
    logger.debug('[DLQ] disparar_dlq_inmediato: hilo daemon lanzado')


def init_scheduler(app):
    """Cron cada 5 minutos — procesa la cola de jobs Siesa pendientes."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error('[DLQ] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=procesar_jobs_pendientes,
        trigger=IntervalTrigger(minutes=1),
        kwargs={'app': app},
        id='dlq_siesa_jobs',
        name='DLQ — procesar jobs Siesa pendientes (cada 1 min)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info('[DLQ] Scheduler iniciado — DLQ cada 1 min')
    return scheduler
