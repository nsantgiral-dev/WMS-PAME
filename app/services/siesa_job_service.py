"""
Dead Letter Queue — procesador de jobs asíncronos hacia Siesa.

El scheduler llama a procesar_jobs_pendientes() cada 1 minuto.
Si Connekta rechaza (periodo cerrado, ítem bloqueado, timeout):
  - Reintento 1 → espera 5 min
  - Reintento 2 → espera 15 min
  - Reintento 3 → espera 45 min
  - Tras 3 fallos → estado=FALLIDO → alerta roja en dashboard admin

Tipos de job implementados:
  (extensible: agregar nuevo tipo + handler en _ejecutar_job)

  TRANSFERENCIA_UBICACIONES (conector 173066, RESERVA→PICKING) se retiró
  2026-09-07: ambas ubicaciones son la misma bodega Siesa (NB1 no tiene
  sub-bodegas para picking/reserva, eso es organización interna del WMS),
  así que no había ningún documento real que declarar — y encima 173066
  no es idempotente en Siesa. reposicion_service.confirmar_reposicion() ya
  no encola nada, queda 100% en el WMS.
"""

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from app.extensions import db
from app.models.siesa_job import SiesaJob, EstadoSiesaJob
from app.models.packing import EstadoPacking
from app.models.conteo import EstadoConteo
from app.utils.fecha import fecha_hoy_bogota

logger = logging.getLogger(__name__)


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


from app.services.connekta_gateway import (
    ConnektaNoEnviado,
    ConnektaResultadoDesconocido as _ResultadoDesconocido,
)

#: Primera palabra del error de un envío que quedó sin verificar: el panel de
#: recuperación y el aviso la reconocen sin parsear prosa (la condición real
#: es `preflag_sin_verificar`: el objeto con su pre-flag puesto y el job FALLIDO).
MARCA_SIN_VERIFICAR = 'SIN_VERIFICAR'

#: Pre-flag del traslado a averías sobre su ancla (`MovimientoInventario.siesa_sync`).
SIESA_SYNC_ENVIANDO = 'ENVIANDO'

#: Los tres desenlaces de un envío con pre-flag (`estado_del_preflag`).
PREFLAG_LIBRE = 'LIBRE'                  # la bandera no está puesta: se puede enviar
PREFLAG_ENVIADO = 'ENVIADO'              # puesta y con respuesta: ya entró
PREFLAG_SIN_DESENLACE = 'SIN_DESENLACE'  # puesta sin respuesta: «no sé»


def estado_del_preflag(obj) -> str:
    """¿Qué se sabe del envío a Siesa de `obj`? **Una política para todos los
    tipos con pre-flag** (2026-09-26): ENTRADA_OC (`RecepcionMercancia`),
    TRASLADO_AVERIAS (`TareaDevolucion` o su `MovimientoInventario` ancla) y
    AJUSTE_CONTEO (`SesionConteo`).

    La bandera se pone ANTES del POST (Regla 6), así que puesta sola no dice
    «enviado»: dice «se intentó». Solo es ENVIADO con la respuesta guardada
    (`siesa_response`, o `siesa_sync = 'ENVIADO'` en el movimiento). Puesta
    sin respuesta —un crash entre el pre-flag y el POST, un job PROCESANDO
    reseteado— es SIN_DESENLACE: FALLIDO sin reintento y «¿Está en Siesa?».
    Hasta hoy la guarda de los tres tipos booleanos lo daba por hecho y
    cerraba el job COMPLETADO sin que el POST hubiera salido."""
    if obj is None:
        return PREFLAG_LIBRE
    if hasattr(obj, 'siesa_sync') and not hasattr(obj, 'siesa_triggered'):
        if obj.siesa_sync == 'ENVIADO':
            return PREFLAG_ENVIADO
        if obj.siesa_sync == SIESA_SYNC_ENVIANDO:
            return PREFLAG_SIN_DESENLACE
        return PREFLAG_LIBRE
    if not getattr(obj, 'siesa_triggered', False):
        return PREFLAG_LIBRE
    return PREFLAG_ENVIADO if getattr(obj, 'siesa_response', None) else PREFLAG_SIN_DESENLACE


#: Quién resuelve un envío sin verificar y dónde (lo dicen las pantallas).
QUIEN_RESUELVE_SIN_VERIFICAR = ('el administrador, en Siesa → Recuperación '
                                '(«¿Está en Siesa?»)')


def envio_siesa_publico(obj) -> dict:
    """Lo que una pantalla muestra del envío a Siesa de `obj`, sacado de la
    política (P2-8, 2026-09-26): «✓ Sincronizada» solo con desenlace; con la
    bandera sin desenlace, «Sin verificar en Siesa» y quién lo resuelve."""
    estado = estado_del_preflag(obj)
    if estado == PREFLAG_ENVIADO:
        return {'estado': estado, 'texto': 'Sincronizada con Siesa', 'quien_resuelve': None}
    if estado == PREFLAG_SIN_DESENLACE:
        return {'estado': estado, 'texto': 'Sin verificar en Siesa: pudo haber entrado',
                'quien_resuelve': QUIEN_RESUELVE_SIN_VERIFICAR}
    return {'estado': estado, 'texto': 'Pendiente en Siesa', 'quien_resuelve': None}


def _exigir_preflag_con_desenlace(obj, job, que: str) -> bool:
    """`True` si ya se envió (el job se cierra idempotente); levanta «no sé»
    si la bandera quedó sin desenlace; `False` si está libre."""
    return _decidir_por_desenlace(estado_del_preflag(obj), job, que)


def _exigir_envio_con_desenlace(job, que: str) -> bool:
    """Lo mismo, preguntado por el job: los tipos de la plata (NC y retención)
    guardan su desenlace en otro sitio que los de inventario, y lo sabe
    `_preflag_de` (integración v2, 2026-09-27: una sola decisión de «no sé»
    para los seis tipos de `TIPOS_CON_PREFLAG`)."""
    return _decidir_por_desenlace(estado_del_envio(job), job, que)


def _decidir_por_desenlace(estado: str, job, que: str) -> bool:
    if estado == PREFLAG_ENVIADO:
        return True
    if estado == PREFLAG_SIN_DESENLACE:
        raise _ResultadoDesconocido(
            f'{MARCA_SIN_VERIFICAR}: {job.tipo} job={job.id}: {que} ya se intentó '
            f'enviar y no hay constancia de que haya entrado (el proceso pudo morir '
            f'entre la marca y el envío). No se reenvía ni se da por hecho: búsquelo '
            f'en Siesa y resuélvalo en Siesa → Recuperación («¿Está en Siesa?»).')
    return False


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

    `espera_minutos` (default 30, el caso de arriba — recepción física,
    horas) es la excepción, no la regla: DC esperando su RC, o una RIT
    esperando el consecutivo que 174646 le va a poner, resuelven en el mismo
    ciclo del DLQ (segundos, la propia liquidación los encadena). 30 minutos
    ahí no evita nada — solo hace que una NI ya destrabada tarde media hora
    en salir sin ninguna razón real detrás.
    """

    def __init__(self, mensaje: str, espera_minutos: int = 30):
        super().__init__(mensaje)
        self.espera_minutos = espera_minutos




def dlq_puede_postear(momento=None) -> bool:
    """¿La DLQ puede mandar documentos a Siesa ahora? **La única que lo dice
    para la cola.**

    La ventana es UNA: `ventana_siesa.ventana_abierta` (Regla 14). Esta
    función solo agrega la excepción de la cola: **en simulación no hay
    Siesa** —ni POST ni GET reales— y la ventana no protege nada.

    En **ensayo** sí aplica, a propósito: los POST se bloquean pero los GET
    son reales (el RC consulta la cartera antes de postear, el despacho las
    facturas), y de noche contra un Siesa que no contesta gastan reintentos
    igual. El frente de liquidación la eximía también en ensayo; se integró
    sin eso (2026-09-25). Trinquetes: `tests/test_dlq_ventana_siesa.py`,
    `tests/test_ventana_siesa.py`."""
    from app.services.connekta_gateway import connekta
    if getattr(connekta, 'modo_simulacion', False):
        return True
    from app.services.ventana_siesa import ventana_abierta
    return ventana_abierta(momento)


#: Orden de la cola (2026-09-26). Hasta hoy `q.limit(20)` sin ORDER BY: en
#: temporada, con cientos de jobs, cuáles salían era cosa de la base. Primero
#: la emisión fiscal (el muelle espera la factura para cargar el camión);
#: después la cadena de la plata en su orden de dependencia (NC → RC → DC,
#: Regla 7), que así sale en un ciclo y no en tres. Lo que no está en la
#: lista va después, en orden de llegada.
PRIORIDAD_DLQ = ('DESPACHO_F470', 'NOTA_CREDITO_FACTURA', 'NOTA_CREDITO_DEVOLUCION_CLIENTE',
                 'MOTIVO_DIAN_NC', 'RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET')

#: Un job de un tipo que NO es el primero de la lista y que lleva esperando su
#: turno más que esto pasa adelante: con un flujo continuo de despachos, un
#: recibo de caja no puede quedar al final de la cola todo el día. No aplica
#: dentro del primer tipo: ahí adelantar lo más viejo es empezar cajas nuevas
#: en vez de terminar las empezadas (medido: 86 cajas/h en vez de 150).
ESPERA_MAXIMA_SIN_TURNO = 30   # minutos


def orden_de_la_cola(ahora):
    """El ORDER BY de la cola. **La única** que ordena la DLQ:

    1. un job de otro tipo que lleva > `ESPERA_MAXIMA_SIN_TURNO` esperando;
    2. la prioridad del tipo (`PRIORIDAD_DLQ`);
    3. FIFO (creación, id). Dentro de la emisión esto ya termina lo empezado
       antes de empezar otra caja: la que espera su RM es más vieja que las
       que no arrancaron. Lo que lo rompía era el envejecimiento aplicado a
       las cajas sin empezar (medido: 86 cajas/h en vez de 176).
    """
    from sqlalchemy import and_, case
    listo_desde = db.func.coalesce(SiesaJob.proximo_intento, SiesaJob.fecha_creacion)
    envejecido = case((and_(SiesaJob.tipo != PRIORIDAD_DLQ[0],
                            listo_desde <= ahora - timedelta(minutes=ESPERA_MAXIMA_SIN_TURNO)), 0),
                      else_=1)
    prioridad = case({t: i for i, t in enumerate(PRIORIDAD_DLQ)}, value=SiesaJob.tipo,
                     else_=len(PRIORIDAD_DLQ))
    return (envejecido, prioridad, SiesaJob.fecha_creacion, SiesaJob.id)


def _procesar_jobs_pendientes_interno(_app):
    """Lógica interna de procesamiento — separada para permitir captura de errores DB externos."""
    with _app.app_context():
        # Circuit breaker: si Connekta está caído, no gastar reintentos
        from app.services.connekta_gateway import connekta
        # Solo mientras no toque probar: vencido el intervalo, el primer job
        # es el probe (`circuito_admite_intento`, H1). Con `_cb_state ==
        # 'OPEN'` la DLQ de un proceso sin otra charla con Siesa no volvía a
        # intentar nunca.
        if not connekta.circuito_admite_intento():
            logger.info('[DLQ] Circuit breaker OPEN — pausando DLQ hasta que Siesa responda')
            return 0

        # Advisory lock: solo un worker procesa la DLQ a la vez.
        # Sin esto, cuando Siesa se recupera y hay 100 jobs acumulados, N workers
        # los atacan simultáneamente saturando la API de Connekta.
        #
        # De SESIÓN y en conexión propia, no `pg_try_advisory_xact_lock`: un
        # lock de transacción se suelta en el primer commit, y `_run_dlq_jobs`
        # comitea antes y después de cada job. Desde el segundo job en adelante
        # la exclusión no existía — y con ella caían también los FOR UPDATE
        # SKIP LOCKED de los jobs que quedaban en la lista, así que otro worker
        # podía tomar los mismos jobs PENDIENTE que este iba a procesar.
        from app.utils.lock import LOCK_DLQ, advisory_lock
        with advisory_lock(LOCK_DLQ, 'dlq') as tomado:
            if not tomado:
                logger.info('[DLQ] Otro worker ya procesa jobs — omitido')
                return 0
            return _run_dlq_jobs()


def _run_dlq_jobs():
    """Procesa hasta 20 jobs elegibles. Llamado solo cuando el advisory lock está tomado."""
    from datetime import timedelta
    # P0-9: una base sellada para otro ambiente (una copia de producción
    # restaurada en QA) no se procesa. Ni el barrido de atascados: los jobs
    # quedan tal cual llegaron, PENDIENTE, para que alguien decida.
    from app.services import sello_ambiente
    _ok_sello, _motivo_sello = sello_ambiente.puede_postear()
    if not _ok_sello:
        logger.error('[DLQ] Ciclo omitido: %s', _motivo_sello)
        return 0
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
    )
    # Fuera de la ventana de Siesa (Regla 14) no sale ningún POST: solo lo que
    # no va a Siesa (el correo). Un RC encolado a las 20:30 no gasta sus
    # reintentos contra un Siesa que no contesta y amanece FALLIDO: espera
    # PENDIENTE, sin gastar nada, a que abra la ventana (P1-10). Decisión del
    # dueño: Siesa caído = se para todo, sin caminos alternativos.
    from app.services.ventana_siesa import TIPOS_SIN_SIESA
    if not dlq_puede_postear():
        q = q.filter(SiesaJob.tipo.in_(TIPOS_SIN_SIESA))
    q = q.order_by(*orden_de_la_cola(ahora)).limit(20)
    # skip_locked solo disponible en PostgreSQL — en SQLite lo ignoramos
    try:
        jobs = q.with_for_update(skip_locked=True).all()
    except Exception as _e_skip:
        logger.warning(f'[DLQ] skip_locked no soportado — fallback sin lock: {_e_skip}')
        jobs = q.all()

    if not jobs:
        return 0

    procesados = 0
    # FM_SIESA_UNREACHABLE: tras una caída, la cola acumulada sale de golpe;
    # una pausa entre jobs no inunda a Siesa. **Solo en los 10 min después de
    # que el circuito se cerró** (2026-09-26): antes bastaban 10 pendientes,
    # que en temporada es siempre — 1 s por job sin razón (medido en
    # `tests/test_dlq_temporada.py`: 168 cajas/h con la pausa, 176 sin ella).
    from app.services.connekta_gateway import connekta as _cx
    _total_pendientes = SiesaJob.query.filter(SiesaJob.estado == EstadoSiesaJob.PENDIENTE).count()
    _inter_job_delay = (1.0 if _total_pendientes > 10 and _cx._circuit.recien_recuperado()
                        else 0.0)
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
                job.proximo_intento = datetime.utcnow() + timedelta(
                    minutes=getattr(e, 'espera_minutos', 30))
                job.error_ultimo = str(e)[:2000]
                db.session.commit()
                logger.info('[DLQ] Job %s en espera: %s', job.id, e)
                continue
            if isinstance(e, _ResultadoDesconocido):
                # El POST salió y no sabemos si Siesa lo procesó. **Reintentar
                # es la única acción prohibida**: si el documento existe, el
                # reintento crea el segundo.
                #
                # Va directo a FALLIDO —sin gastar los reintentos que quedan y
                # sin reprogramarse— porque lo que desbloquea esto no es
                # esperar: es que alguien mire Siesa. Un job que reintenta
                # solo terminaría duplicando mientras nadie mira.
                job.estado = EstadoSiesaJob.FALLIDO
                job.proximo_intento = None
                job.error_ultimo = str(e)[:2000]
                db.session.commit()
                logger.error(
                    '[DLQ] Job %s (%s) con RESULTADO DESCONOCIDO — el '
                    'documento puede existir en Siesa. No se reintenta: '
                    'verificar en Auditoría de documentos antes de nada.',
                    job.id, job.tipo)
                _anotar_en_recaudo(job, 'SIN_VERIFICAR')
                _crear_alerta_admin(job)
                continue
            if isinstance(e, ErrorDeterminista):
                # **Determinista**: los mismos datos la producen siempre. El
                # payload del job no cambia entre intentos y la factura
                # tampoco, así que los 5 reintentos dan el mismo resultado —
                # lo único que agregan son 185 minutos (5+15+45+120) de silencio
                # mientras el operario cree que la nota crédito va en camino.
                #
                # Va a FALLIDO sin gastar reintentos, igual que
                # `_ResultadoDesconocido`, por la misma razón: lo que lo
                # desbloquea no es esperar, es que una persona mire. Acá esa
                # persona rehace la devolución desde Recepción con el
                # `f470_rowid` por línea.
                #
                # `intentos` mide fallos que pueden salir distinto la próxima
                # vez. Éste no puede. Confundirlos es la misma clase de «un
                # contador, dos significados» que ya costó las banderas de
                # idempotencia.
                job.estado = EstadoSiesaJob.FALLIDO
                job.proximo_intento = None
                job.error_ultimo = str(e)[:2000]
                db.session.commit()
                logger.error(
                    '[DLQ] Job %s (%s) AMBIGUO — no se reintenta (el reintento '
                    'daría lo mismo): %s', job.id, job.tipo, e)
                _anotar_en_recaudo(job, 'FALLIDO')
                _crear_alerta_admin(job)
                continue
            from app.services.connekta_gateway import ConnektaRateLimit
            if isinstance(e, ConnektaRateLimit):
                # 429: no entró y se reintenta (Regla 3), pero esperar el
                # Retry-After no es un fallo. En temporada una ráfaga de 429
                # agotaba los cinco intentos de cajas sanas (P3, 2026-09-26).
                job.estado = EstadoSiesaJob.PENDIENTE
                job.proximo_intento = datetime.utcnow() + timedelta(
                    seconds=getattr(e, 'retry_after_s', 300))
                job.error_ultimo = str(e)[:2000]
                db.session.commit()
                logger.info('[DLQ] Job %s (%s) con 429 — espera %ss sin gastar intento',
                            job.id, job.tipo, getattr(e, 'retry_after_s', 300))
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
                _anotar_en_recaudo(job, 'FALLIDO')
                _crear_alerta_admin(job)
            else:
                logger.warning(
                    f'[DLQ] Job {job.id} ({job.tipo}) falló (intento {job.intentos}/'
                    f'{job.max_intentos}) — reintento en '
                    f'{_espera_de(job)}: {error_msg}'
                )

    return procesados


def _espera_de(job) -> str:
    """La espera real del próximo intento, leída de la misma tabla que la
    programa (`siesa_job._BACKOFF_MINUTOS`). Había una copia de tres etiquetas
    para cuatro esperas: del 4.º fallo en adelante el log decía «45 min» y el
    job esperaba 120."""
    from app.models.siesa_job import _BACKOFF_MINUTOS
    return f'{_BACKOFF_MINUTOS[min(job.intentos - 1, len(_BACKOFF_MINUTOS) - 1)]} min'


def encolar_traslado_averias(movimiento, codigo_siesa: str, cantidad: int,
                             bodega_del_almacen: str, referencia: str) -> bool:
    """Encola UN traslado NB1→AV1 anclado en el movimiento que registró la avería.

    Núcleo único del encolado: recepción y auditoría de picking entran por acá.
    Dos copias del guard de bodega divergirían, y la que divergiera emitiría
    documentos con la bodega equivocada — que Siesa acepta y nadie ve hasta que
    el saldo no cuadra, sin causa a la vista.

    Devuelve True si encoló. NO hace commit — lo hace el caller.

    ## El guard de bodega

    `transferir_a_averias` emite con la bodega de salida **cableada** a
    `CONNEKTA_BODEGA` y CO fijo. Para un almacén de otra bodega el documento
    diría que la mercancía salió de donde nunca estuvo. Se declara en el log y
    no se emite: Regla 0, fallar conservador Y decirlo.
    """
    from app.services.connekta_gateway import connekta

    if not codigo_siesa:
        logger.warning(
            '[AVERIAS] %s: sin codigo_siesa — no se puede mover a %s. Queda en '
            'la zona de averías del WMS y Siesa lo sigue contando como vendible.',
            referencia, connekta.bodega_averias)
        return False

    if bodega_del_almacen != connekta.bodega:
        logger.warning(
            '[AVERIAS] %s: %s unidad(es) de %s en la bodega %s, pero el conector '
            '142951 solo sabe emitir %s→%s. NO se encola — un documento con la '
            'bodega equivocada descuadra sin dejar causa. Resolver a mano en Siesa.',
            referencia, cantidad, codigo_siesa, bodega_del_almacen,
            connekta.bodega, connekta.bodega_averias)
        return False

    if movimiento is None or movimiento.siesa_sync in ('ENVIADO', SIESA_SYNC_ENVIANDO):
        return False

    # Dedup: un job vivo para el mismo movimiento ya cubre este traslado.
    ya = SiesaJob.query.filter(
        SiesaJob.tipo == 'TRASLADO_AVERIAS',
        SiesaJob.referencia_tipo == 'movimiento_averia',
        SiesaJob.referencia_id == movimiento.id,
        SiesaJob.estado.in_(EstadoSiesaJob.ACTIVOS),
    ).first()
    if ya:
        return False

    SiesaJob.encolar(
        'TRASLADO_AVERIAS',
        {
            'movimiento_id': movimiento.id,
            'item_codigo': codigo_siesa,
            'cantidad': int(cantidad),
            'referencia': referencia[:200],
        },
        referencia_tipo='movimiento_averia',
        referencia_id=movimiento.id,
    )
    return True


def _encolar_averias_de_recepcion(rec) -> int:
    """Encola un traslado NB1→AV1 por cada ítem de la recepción que llegó roto.

    Devuelve cuántos encoló. NO hace commit — lo hace el caller.

    ## El guard de bodega, y por qué no se puede saltar

    `transferir_a_averias` emite con la bodega de salida **cableada** a
    `CONNEKTA_BODEGA` (NB1) y CO fijo 003. Para una recepción de otro almacén,
    el documento diría que la mercancía salió de NB1 — una bodega donde nunca
    estuvo. Siesa lo aceptaría y el descuadre aparecería después, sin causa
    visible.

    Así que se encola SOLO para el almacén cuya bodega Siesa es la del
    conector, y para los demás se declara en el log en vez de emitir un
    documento falso. Regla 0: fallar hacia el lado conservador Y decirlo.
    """
    from app.models.inventario import MovimientoInventario as _MovInv
    # `connekta` se importa acá adentro, igual que en `_ejecutar_job`: el módulo
    # no lo tiene a nivel superior y tomarlo de otro lado abriría una segunda
    # referencia al mismo singleton.
    from app.services.connekta_gateway import connekta

    almacen = getattr(rec, 'almacen', None)
    bodega_rec = getattr(almacen, 'bodega_siesa_id', None) if almacen else None
    encolados = 0

    for item in rec.items:
        averiadas = getattr(item, 'cantidad_averiada', 0) or 0
        if averiadas <= 0:
            continue

        # El chequeo de `codigo_siesa` NO se repite acá: lo hace
        # `encolar_traslado_averias`, que es el núcleo. Estuvo duplicado un rato
        # y lo delató el conteo de anclas de una mutación — dos copias del mismo
        # guard son dos cosas que pueden divergir.
        codigo_siesa = (getattr(item.producto, 'codigo_siesa', '') or '').strip()

        mov = _MovInv.query.filter_by(
            idempotency_key=f'REC-AVE-{rec.id}-{item.producto_id}').first()
        if mov is None:
            logger.warning(
                '[AVERIAS] recepción %s ítem %s: no se encontró el movimiento de '
                'avería — sin ancla no se encola, para no mover stock en Siesa '
                'sin respaldo en el kardex del WMS.', rec.id, item.producto_id)
            continue

        if encolar_traslado_averias(
                movimiento=mov, codigo_siesa=codigo_siesa, cantidad=averiadas,
                bodega_del_almacen=bodega_rec,
                referencia=f'Avería en recepción {rec.codigo} · OC {rec.numero_oc_siesa}'):
            encolados += 1

    return encolados


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


class ErrorDeterminista(Exception):
    """Un error que da lo mismo en cada reintento: los datos del job no cambian
    solos. Va a FALLIDO sin gastar reintentos y con alerta (ver el clasificador
    en `_run_dlq_jobs`): lo que lo desbloquea es que una persona mire."""


class DatoQueFalta(ErrorDeterminista):
    """Un envío de la liquidación al que le falta un dato que pone una
    persona —la factura no aparece en la cartera pasado el tope, la fila no
    trae su unidad de negocio, lo cobrado no cuadra con la factura—. El texto
    dice qué falta, de qué pedido y quién lo pone; la tarjeta NO ofrece
    «Reintentar» (daría lo mismo): la salida es poner el dato y volver a
    «Enviar todo a Siesa» (`envio_liquidacion`, 2026-09-27)."""

    MARCA = 'DATO_QUE_FALTA'

    def __init__(self, mensaje: str):
        super().__init__(f'{self.MARCA}: {mensaje}')


class TrasladoNoEnviadoEnEnsayo(ErrorDeterminista):
    """MODO_ENSAYO bloqueó el STS de un traslado (2026-09-26, solo QA).

    Antes el job quedaba COMPLETADO y la solicitud seguía EN_PACKING sin
    documento ni botón: «hecho» sin nada hecho, y un traslado que no avanzaba
    nunca. Ahora el job queda FALLIDO con este motivo y la solicitud lo
    declara (`siesa_error`): con el ensayo apagado, «Reintentar» el job envía
    el STS."""


class NotaCreditoSinLineas(ErrorDeterminista):
    """La NC de una devolución contada no encontró NINGUNA de sus líneas en la
    factura.

    Antes el job devolvía `{'sin_lineas': True}` y el DLQ lo marcaba
    COMPLETADO: sin nota crédito, con el inventario ya reingresado en el WMS y
    —si la devolución venía de ruta— con el recibo de caja esperando para
    siempre una NC que nunca iba a salir. La falla se leía como un éxito.
    """


class LineaDevueltaAmbigua(ErrorDeterminista):
    """No se puede decir A QUÉ LÍNEA de la factura corresponde lo devuelto.

    Se levanta desde `_construir_lineas_nc` (rama parcial) cuando un item
    declarado llega **sin `f470_rowid`** y su referencia aparece en más de una
    línea de la factura — el producto de doble unidad (PQ + UND).

    Es la Regla 0 aplicada a este caso: adivinar por referencia mete las DOS
    líneas en la nota crédito con la cantidad completa, cruza de más contra la
    cartera (Regla 21: Siesa no aprueba un documento cuya cartera no cuadre) y
    reingresa a la bodega mercancía que nadie devolvió. Que el job falle y
    alguien mire es reversible; una NC de más es un documento fiscal que hay
    que reversar a mano.

    **No gasta reintentos** (ver el clasificador en `_run_dlq_jobs`). Es
    determinista: el payload del job no cambia entre intentos y la factura
    tampoco, así que los cinco intentos dan el mismo resultado. Lo único que
    agregan es el backoff —185 minutos— antes de avisarle a la única persona
    que puede resolverla, mientras el operario cree que la nota crédito va en
    camino. El precedente es `_ResultadoDesconocido`: a FALLIDO directo con
    alerta, porque lo que desbloquea esto no es esperar.

    Por eso los handlers la re-lanzan **con su tipo intacto**, agregándole el
    contexto (job, devolución/recaudo, factura) dentro del mensaje. Envuelta en
    una `Exception` genérica el clasificador no la puede distinguir de un
    rechazo de Siesa, que sí se reintenta sin riesgo — la misma confusión que
    `ConnektaPaginacionError` documenta del otro lado del flujo.
    """


def _cant_declarada(valor) -> Decimal:
    """Cantidad devuelta como `Decimal`, tolerando `None` y basura.

    Un `None` en `cantidad_devuelta` hacía reventar `int()` con un `TypeError`
    sin tipo propio: el clasificador del DLQ lo trataba como transitorio y
    quemaba los cinco reintentos (185 min) sobre un fallo determinista — justo
    lo que `LineaDevueltaAmbigua` se introdujo para evitar. Acá vale 0, que cae
    por el filtro de «no se devolvió nada» y no crea línea.
    """
    try:
        return Decimal(str(valor if valor is not None else 0))
    except (InvalidOperation, ValueError):
        logger.warning(
            '[DLQ] cantidad_devuelta ilegible (%r) — se trata como 0, la línea '
            'no entra a la NC', valor)
        return Decimal(0)


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
    'f470_rowid', ver abajo), con su 'cantidad_devuelta'. Si un item trae
    'es_averiado': True y se pasó bodega_averias, esa línea usa bodega_averias
    en vez de f150_id.

    ## La clave del match es `f470_rowid`, no la referencia

    Este repo tiene **productos de doble unidad**: la misma referencia se
    factura en DOS líneas, una en PQ y otra en UND, con `f470_rowid` y valores
    distintos (PAPELSP6741 — el incidente ya documentado en
    `despacho_parcial_service.py:102-106`, donde un dict indexado solo por
    referencia *«guardaba la ÚLTIMA línea (UND) causando el error 244328»*).

    Indexando por referencia, las **dos** filas de la factura matchean el mismo
    item devuelto y las dos entran a la NC con la cantidad completa. Reproducido
    sobre una devolución de 1 paquete: NC con 2 líneas y `F353_VLR_CRUCE=26675`
    cuando debía ser 24250 — se cruza de más contra la factura y se reingresan
    2 unidades de inventario por 1 devuelta.

    **No `(referencia, uom)`:** el rowid identifica la línea sin ambigüedad, es
    lo que Siesa exige en el payload (`f470_rowid_movto`), y el par
    referencia+unidad seguiría siendo ambiguo si una factura repitiera la misma
    referencia y unidad en dos renglones.

    El dato ya existe de punta a punta: `LineaDevolucionCliente.f470_rowid` lo
    puebla `devolucion_cliente_service.buscar_pedido` (y
    `liquidacion_service._crear_devolucion_pendiente` para las de ruta), y el
    PWA lo devuelve por línea. Solo faltaba propagarlo al payload del job.

    ## Ítem sin `f470_rowid` (Regla 0)

    Un payload encolado antes de este arreglo no lleva rowid. La política:

    · **referencia con UNA sola línea en la factura** → se usa esa línea. No es
      adivinar: la correspondencia es única, y es exactamente el caso de todas
      las devoluciones históricas (verificado: 29 de 29 líneas en producción ya
      tienen rowid, y ningún job NC quedó pendiente con payload viejo).
    · **referencia con DOS o más líneas** —el producto de doble unidad, el único
      caso donde la ambigüedad existe— → `LineaDevueltaAmbigua`. El job falla,
      el DLQ alerta y alguien mira. Fallar es reversible; cruzar de más no.
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

    # devueltos_map: f470_rowid (normalizado a str) → item declarado.
    # devueltos_sin_rowid: referencia → items sin rowid (payloads viejos).
    devueltos_map = {}
    devueltos_sin_rowid = {}
    for it in items_devueltos:
        # `Decimal`, no `int`. `LineaDevolucionCliente.cantidad_devuelta` es
        # `Numeric(14, 4)`: las fracciones son representables y llegan hasta acá.
        # Con `int()`, una devolución de 0,5 unidades daba 0, el ítem se caía del
        # mapa, el job devolvía `{'sin_lineas': True}` y el DLQ lo marcaba
        # COMPLETADO — sin nota crédito, con la factura abierta en cartera y el
        # inventario ya reingresado en el WMS. La falla devolvía algo
        # indistinguible del éxito.
        #
        # El gemelo de esta misma pregunta (`liquidacion_service.
        # _cantidades_por_linea_de_factura`) siempre usó float y por eso los dos
        # divergían: 2,5 → 2 acá y 2,5 allá.
        if _cant_declarada(it.get('cantidad_devuelta')) <= 0:
            continue
        rowid_item = str(it.get('f470_rowid') or '').strip()
        if rowid_item:
            if rowid_item in devueltos_map:
                # Ningún productor de hoy arma esto (una línea de devolución por
                # línea de factura, en Recepción y en Liquidación). Si algún día
                # lo arma, que no se pierda en silencio como se perdía la línea
                # PQ con el dict indexado por referencia.
                logger.warning(
                    '[DLQ] _construir_lineas_nc: dos items declaran el mismo '
                    'f470_rowid %s (%r) — se usa el último, la NC puede quedar '
                    'corta', rowid_item, it.get('codigo'))
            devueltos_map[rowid_item] = it
        else:
            devueltos_sin_rowid.setdefault(it.get('codigo', ''), []).append(it)

    # Cuántas líneas de la factura trae cada referencia. >1 es el producto de
    # doble unidad: ahí la referencia sola no alcanza para decidir.
    lineas_por_ref = {}
    for row in rowids_data:
        _r = row.get('f120_referencia', '')
        lineas_por_ref[_r] = lineas_por_ref.get(_r, 0) + 1

    consumidos = set()
    for row in rowids_data:
        ref = row.get('f120_referencia', '')
        rowid_fila = str(row.get('f470_rowid') or '').strip()
        item = devueltos_map.get(rowid_fila)
        if item is not None:
            consumidos.add(id(item))
        else:
            candidatos = devueltos_sin_rowid.get(ref) or []
            if candidatos:
                if len(candidatos) > 1 or lineas_por_ref.get(ref, 0) > 1:
                    _rowids_fac = [str(r.get('f470_rowid') or '') for r in rowids_data
                                   if r.get('f120_referencia', '') == ref]
                    raise LineaDevueltaAmbigua(
                        f'la referencia {ref!r} se devolvió sin f470_rowid y la '
                        f'factura tiene {len(_rowids_fac)} línea(s) con esa '
                        f'referencia (rowids {_rowids_fac}, '
                        f'{len(candidatos)} item(s) declarado(s)) — es un producto '
                        'de doble unidad: no se puede saber a cuál corresponde lo '
                        'devuelto. No se construye la NC: adivinar cruzaría de más '
                        'contra la factura y reingresaría inventario que nadie '
                        'devolvió. Rehacer la devolución desde Recepción (el PWA '
                        'ya manda el rowid por línea).'
                    )
                item = candidatos[0]
                consumidos.add(id(item))
        if not item:
            continue
        cant_dev = _cant_declarada(item['cantidad_devuelta'])
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

    # Lo declarado que no encontró su línea en la factura. Antes desaparecía sin
    # ruido: la NC salía corta y el cruce quedaba por debajo del saldo, que es
    # el lado conservador pero también el silencioso. Se declara (no se levanta:
    # una NC corta se completa; una de más hay que reversarla en el ERP).
    _declarados = list(devueltos_map.values()) + [
        it for lista in devueltos_sin_rowid.values() for it in lista
    ]
    _huerfanos = [it for it in _declarados if id(it) not in consumidos]
    if _huerfanos:
        logger.warning(
            '[DLQ] _construir_lineas_nc: %d item(s) devuelto(s) sin línea en la '
            'factura — NO entran a la NC: %s',
            len(_huerfanos),
            [(it.get('codigo'), it.get('f470_rowid')) for it in _huerfanos],
        )
    return lineas_nc


# «¿La factura está saldada?» y «¿el recibo entró?» ya no se contestan acá
# leyendo la cartera entera del NIT: los contesta `envio_liquidacion` con la
# lectura EXACTA del documento (CO + tipo + consecutivo, Regla 18) —
# `leer_cruce` para el saldo de antes y `lectura_de_verificacion` para el de
# después—. La numeración de las FE se solapa entre CO (medido en producción
# el 2026-09-27), y una fila de otro CO decidía si un recibo se reenviaba.


def _verificar_rc_sin_respuesta(job, recaudo, connekta) -> dict:
    """Un recibo de caja con la bandera puesta y sin desenlace: el POST salió
    sin respuesta clara, o el proceso se cortó entre la marca y el envío.
    **Nunca se reenvía** (Regla 3). Se lee la cartera en los momentos de
    `envio_liquidacion.PLAN_VERIFICACION_MIN` después del intento (la Regla 20
    son segundos; la indexación de la cartera, minutos):

    · si el saldo bajó por el monto del recibo, entró → ENVIADO;
    · si todavía no toca la última lectura, espera (sin gastar intento);
    · en la última, «no sé» → FALLIDO sin reintento (`SIN_VERIFICAR`), con lo
      que una persona tiene que buscar en Siesa. Cada lectura queda en el job:
      «No está en Siesa» exige una que confirme el saldo intacto
      (`LiquidacionService.resolver_recibo_sin_verificar`)."""
    import math
    from app.services import envio_liquidacion as _env
    payload = job.get_payload() or {}
    if not payload.get('intento_rc_en'):
        # Un envío de antes de la verificación diferida (o sin la marca): el
        # plan se cuenta desde ahora — mejor esperar de más que decidir sin
        # haber leído.
        payload['intento_rc_en'] = datetime.utcnow().isoformat(timespec='seconds')
        job.payload = json.dumps(payload, ensure_ascii=False)
    lect = _env.lectura_de_verificacion(job, recaudo, connekta)
    ver = dict(payload.get('verificacion') or {})
    ver.setdefault('desde', payload['intento_rc_en'])
    ver['lecturas'] = list(ver.get('lecturas') or []) + [lect]
    payload['verificacion'] = ver
    job.payload = json.dumps(payload, ensure_ascii=False)
    db.session.commit()
    que = _env.que_es(recaudo.tarea, payload.get('tipo_docto_fe'), payload.get('consec_fe'),
                      payload.get('co_factura'))
    if lect['entro']:
        logger.info('[DLQ] RECIBO_CAJA job=%s: verificación diferida — %s', job.id, lect['motivo'])
        _anotar_en_recaudo(job, 'ENVIADO', recaudo=recaudo, consec=lect.get('consec'))
        return {'verificado_por': 'saldo', 'diferido': True, 'recaudo_id': recaudo.id}
    minutos = lect.get('minutos_desde_intento') or 0.0
    faltan = [m for m in _env.PLAN_VERIFICACION_MIN if m > minutos + 0.5]
    if faltan:
        raise DependenciaPendiente(
            f'{que}: verificando si el recibo de caja entró a Siesa (el envío no tuvo '
            f'respuesta clara). Última lectura: {lect.get("motivo")}. Próxima lectura en '
            f'{math.ceil(faltan[0] - minutos)} min; no se reenvía.',
            espera_minutos=max(1, math.ceil(faltan[0] - minutos)))
    buscar = _env.que_buscar_en_siesa(job, recaudo)
    raise _ResultadoDesconocido(
        f'{MARCA_SIN_VERIFICAR}: {que}: el recibo de caja de '
        f'${float(buscar.get("valor") or 0):,.0f} quedó sin verificar — el envío no tuvo '
        f'respuesta clara y la cartera, leída a los {_env.PLAN_VERIFICACION_MIN[0]} min y a '
        f'las {_env.PLAN_VERIFICACION_MIN[-1] // 60} h, no lo confirma ({lect.get("motivo")}). '
        f'No se reenvía: un recibo duplicado se reversa a mano. Búsquelo en Siesa '
        f'(Auditoría de documentos): {buscar["documento"]} del CO {buscar.get("co") or "—"}, '
        f'fecha {buscar.get("fecha") or "—"}, tercero {buscar.get("tercero_nit") or "—"}, '
        f'valor ${float(buscar.get("valor") or 0):,.0f}, que cruza la factura '
        f'{buscar.get("factura") or "—"}; y dígalo en Liquidación («Sí está» / «No está»).')


def _tipo_de_retencion_por_puc(cuenta_puc):
    """El tipo de retención de una cuenta PUC (payloads anteriores a
    `tipo_retencion`). `None` si la cuenta no está en el catálogo."""
    from app.services.liquidacion_service import RETENCION_PUC
    return next((t for t, puc in RETENCION_PUC.items() if puc == cuenta_puc), None)


def _ejecutar_con_preflag(obj, post_fn):
    """
    Ejecuta un POST a Siesa con el patrón pre-flag correcto (Regla 6 + Regla 3),
    compartido por ENTRADA_OC, TRASLADO_AVERIAS y AJUSTE_CONTEO — los tres
    usan los mismos nombres de atributo (`siesa_triggered`/`siesa_triggered_at`)
    y la misma lógica exacta, que antes vivía copiada tres veces y divergía en
    silencio (ver hallazgo de auditoría 2026-09-16: los tres marcaban el flag
    DESPUÉS del POST, con una ventana de duplicado real si el proceso muere
    entre el POST exitoso y ese commit — un job atascado en PROCESANDO se
    resetea a PENDIENTE a los 10 min y se reintenta desde cero).

    `obj` es el modelo con el flag (RecepcionMercancia, TareaDevolucion,
    SesionConteo). `post_fn` es un callable sin argumentos que hace el POST
    real contra Connekta y devuelve su resultado.

    1. Marca el flag ANTES del POST — si el proceso muere a mitad de camino,
       el guard de idempotencia que cada job ya tiene al principio (`if
       obj.siesa_triggered: return {'idempotente': True}`) atrapa el reintento
       sin volver a llamar a Siesa. No hace falta ninguna consulta de
       verificación contra Siesa para esto — el orden solo ya cierra la
       ventana.
    2. **Solo `ConnektaNoEnviado` baja el flag** (4xx, 429, `codigo != 0`,
       payload que no se pudo armar, circuito abierto, ambiente que no
       coincide): es la prueba positiva de que el documento no entró, y el
       backoff normal reintenta de verdad.
    3. Todo lo demás es «no sé» (tanda 2, 2026-09-25): un timeout de lectura
       (`ConnektaResultadoDesconocido`), un 5xx, una conexión cortada, un JSON
       ilegible. El flag **queda** y se levanta `ConnektaResultadoDesconocido`:
       el dispatcher lo manda a FALLIDO sin reintento automático (Regla 3).
       Hasta esta tanda un 502 revertía el flag y la cola reenviaba: un ajuste
       o una entrada por OC duplicados, que se reversan a mano en el ERP. La
       salida es humana: `resolver_preflag_sin_verificar` («¿está en Siesa?»),
       desde Siesa → Recuperación.
    4. Si el POST fue en `modo_ensayo`, se revierte: no se creó nada real.

    Levanta la excepción en los casos 2 y 3, después de dejar el flag en el
    estado correcto — el caller no necesita manejar el error, solo lo que
    pasa cuando el POST sale bien.
    """
    obj.siesa_triggered = True
    obj.siesa_triggered_at = datetime.utcnow()
    db.session.commit()

    try:
        resultado = post_fn()
    except _ResultadoDesconocido:
        raise
    except ConnektaNoEnviado:
        obj.siesa_triggered = False
        db.session.commit()
        raise
    except Exception as e_post:
        raise _ResultadoDesconocido(
            f'{MARCA_SIN_VERIFICAR}: el envío a Siesa falló sin respuesta clara '
            f'({str(e_post)[:300]}) y el documento pudo haber entrado. No se reenvía '
            f'solo (Regla 3): búsquelo en Siesa y resuélvalo en Siesa → Recuperación '
            f'(«¿Está en Siesa?»).') from e_post

    if resultado.get('modo_ensayo'):
        obj.siesa_triggered = False
        db.session.commit()
    elif not getattr(obj, 'siesa_response', None):
        # El desenlace: con la respuesta guardada la bandera dice «entró»
        # (`estado_del_preflag`). Sin esto, bandera sola = «no sé».
        obj.siesa_response = json.dumps(resultado, default=str)[:20000]
        db.session.commit()

    return resultado


def _emitir_despacho(job: SiesaJob, tarea, payload: dict) -> dict:
    """El cuerpo del DESPACHO_F470, con el candado de emisión del pedido ya
    tomado (`emision_exclusiva`)."""
    # Reconciliación automática: Siesa puede tener la factura aunque WMS no lo sepa
    # (respuesta HTTP perdida por restart/timeout). Si ya existe → corregir WMS sin reenviar.
    # **Tres respuestas**: «no se pudo preguntar» no es «no hay factura» —
    # sin saberlo no se manda nada.
    if not tarea.siesa_triggered:
        from app.services.reconciliacion_service import ReconciliacionService
        rec = ReconciliacionService.reconciliar_despacho(
            tarea,
            tipo_docto=payload.get('tipo_docto_pedido', ''),
            consec_docto=payload.get('consec_docto_pedido', ''),
        )
        if rec.get('reconciliado'):
            return rec
        if rec.get('no_se'):
            raise DependenciaPendiente(
                f'No se pudo verificar en Siesa si el pedido '
                f'{payload.get("numero_pedido_siesa")} ya tiene factura '
                f'({rec.get("motivo")}). No se envía nada hasta saberlo.',
                espera_minutos=10)
        if rec.get('anulado'):
            raise ErrorDeterminista(
                f'El pedido {payload.get("numero_pedido_siesa")} está ANULADO en '
                f'Siesa: no se remisiona ni se factura. Cancele la caja.')

    # 142945→142943: RemisionPedido → FacturaRemision.
    # La RM descarga inventario cuenta 14 directamente — sin dependencia de automatización Siesa.
    # DespachoParialService maneja idempotencia (rm_tipo/rm_consec en BD), cabecera del pedido
    # y el encadenamiento completo incluyendo _persistir_resultado (siesa_triggered, DESPACHADO).
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


def _ejecutar_job(job: SiesaJob) -> dict:
    """Despacha el job al handler correcto según su tipo."""
    from app.services.connekta_gateway import connekta
    payload = job.get_payload()

    if job.tipo == 'DESPACHO_F470':
        # Idempotencia: si un intento anterior llegó a Siesa (siesa_triggered=True),
        # no volver a llamar — evita crear remisión duplicada.
        from app.models.packing import TareaPacking
        tarea = TareaPacking.query.get(payload.get('tarea_id'))
        if tarea and tarea.siesa_triggered:
            logger.info(
                f'[DLQ] DESPACHO_F470 job={job.id}: tarea {tarea.id} ya tiene '
                f'siesa_triggered=True — omitiendo llamada a Siesa (idempotencia)'
            )
            return {'idempotente': True, 'tarea_id': tarea.id}
        if not tarea:
            raise ValueError(f'DESPACHO_F470 job={job.id}: tarea_id={payload.get("tarea_id")} no encontrada')

        # Fuera de la ventana de Siesa (Regla 14), con el circuito abierto u
        # otro carril emitiendo este mismo pedido (facturar-remision, RM
        # manual: H3) no se intenta. Esperar no gasta reintento.
        from app.services.documento_fiscal import emision_exclusiva
        with emision_exclusiva(tarea, 'dlq') as permiso:
            if not permiso:
                raise DependenciaPendiente(
                    permiso.motivo, espera_minutos=(2 if permiso.status == 409 else 15))
            return _emitir_despacho(job, tarea, payload)

    if job.tipo == 'ENTRADA_OC':
        # Idempotencia: si un intento anterior llegó a Siesa (siesa_triggered=True),
        # no volver a llamar — evita crear entrada contable duplicada.
        from app.models.recepcion import RecepcionMercancia
        import json as _json
        rec = RecepcionMercancia.query.get(payload.get('recepcion_id'))
        if _exigir_preflag_con_desenlace(rec, job, f'la entrada de la recepción {getattr(rec, "id", "?")}'):
            logger.info(
                f'[DLQ] ENTRADA_OC job={job.id}: recepción {rec.id} ya entró a Siesa '
                f'— omitiendo llamada (idempotencia)'
            )
            return {'idempotente': True, 'recepcion_id': rec.id}

        def _post_entrada_oc():
            return connekta.confirmar_entrada_compras(
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

        # Pre-flag ANTES del POST (Regla 6) vía el helper compartido — si no
        # hay `rec` (recepcion_id no encontrado en el payload) no hay nada
        # sobre lo que marcar el flag; se llama a Siesa igual, sin idempotencia.
        if rec:
            resultado = _ejecutar_con_preflag(rec, _post_entrada_oc)
            if not resultado.get('modo_ensayo'):
                rec.siesa_response = _json.dumps(resultado)
                db.session.commit()
        else:
            resultado = _post_entrada_oc()

        # ── Avisarle a Siesa de lo que llegó roto ────────────────────────
        #
        # Se encola ACÁ y no al confirmar la recepción, por orden: el traslado
        # NB1→AV1 descuenta de NB1, y esas unidades solo existen en NB1 después
        # de que esta entrada por OC aterrizó. Encolarlo antes sería pedirle a
        # Siesa que mueva stock que todavía no tiene. Mismo patrón que
        # MOTIVO_DIAN_NC, que se encola desde adentro del job que lo habilita.
        if rec and not resultado.get('modo_ensayo'):
            try:
                _encolar_averias_de_recepcion(rec)
                db.session.commit()
            except Exception as _e_ave:
                db.session.rollback()
                logger.error(
                    '[DLQ] ENTRADA_OC job=%s: la entrada quedó BIEN en Siesa pero '
                    'no se pudo encolar el traslado a averías de la recepción %s: '
                    '%s. La mercancía rota sigue contada como vendible en Siesa.',
                    job.id, rec.id, _e_ave)
        return resultado

    if job.tipo == 'TRASLADO_AVERIAS':
        # ── Ancla de idempotencia: el MOVIMIENTO que registró la avería ──
        #
        # La idempotencia de este handler colgaba SOLO de `TareaDevolucion`, y
        # esa tabla está DEPRECATED desde el 2026-07-28 sin ningún escritor
        # vivo. Consecuencia: un job encolado desde cualquier otro camino caía
        # en «tarea no existe» y **devolvía éxito sin llamar a Siesa** — el
        # traslado a AV1 nunca ocurría y nada lo decía.
        #
        # El ancla nueva es `MovimientoInventario.siesa_sync`, un campo que
        # existía desde el principio con default 'PENDIENTE' y que NADIE leía
        # jamás. Es el hecho durable: si el movimiento está, la avería ocurrió;
        # si dice ENVIADO, Siesa ya se enteró.
        _mov_id = payload.get('movimiento_id')
        if _mov_id:
            from app.models.inventario import MovimientoInventario as _MovInv
            _mov = db.session.get(_MovInv, _mov_id)
            if _mov is None:
                logger.warning(
                    '[DLQ] TRASLADO_AVERIAS job=%s: movimiento_id=%s no existe — '
                    'no se llama a Siesa para no mover stock sin respaldo.',
                    job.id, _mov_id)
                return {'idempotente': True, 'sin_movimiento': True}
            # El pre-flag de un intento anterior sin desenlace (crash o «no
            # sé») levanta: reenviar puede duplicar el traslado.
            if _exigir_preflag_con_desenlace(_mov, job, f'el traslado del movimiento {_mov.id}'):
                logger.info(
                    '[DLQ] TRASLADO_AVERIAS job=%s: movimiento %s ya marcado '
                    'ENVIADO — omitido.', job.id, _mov.id)
                return {'idempotente': True, 'movimiento_id': _mov.id}

            _item_codigo = payload.get('item_codigo')
            if not _item_codigo:
                raise ValueError(
                    f'TRASLADO_AVERIAS job={job.id}: item_codigo faltante — '
                    f'Siesa no acepta el documento sin referencia del ítem.')

            # Pre-flag (Regla 6) sobre el ancla: ENVIANDO antes del POST. Solo
            # `ConnektaNoEnviado` lo devuelve a su valor; un 5xx o una conexión
            # cortada es «no sé» (tanda 2): queda ENVIANDO y se declara.
            _sync_antes = _mov.siesa_sync
            _mov.siesa_sync = SIESA_SYNC_ENVIANDO
            db.session.commit()
            try:
                _res = connekta.transferir_a_averias(
                    item_codigo=_item_codigo,
                    cantidad=payload['cantidad'],
                    referencia=payload.get('referencia', ''),
                )
            except _ResultadoDesconocido:
                raise
            except ConnektaNoEnviado:
                _mov.siesa_sync = _sync_antes or 'PENDIENTE'
                db.session.commit()
                raise
            except Exception as _e_av:
                raise _ResultadoDesconocido(
                    f'{MARCA_SIN_VERIFICAR}: TRASLADO_AVERIAS job={job.id}: el envío '
                    f'falló sin respuesta clara ({str(_e_av)[:300]}) y el traslado pudo '
                    f'haber entrado. No se reenvía: resuélvalo en Siesa → Recuperación.'
                ) from _e_av
            # En modo ensayo el POST se bloquea del lado del servidor: no hubo
            # traslado real, así que el movimiento vuelve a como estaba — o el
            # reintento quedaría bloqueado por una idempotencia que no respalda nada.
            if _res.get('modo_ensayo'):
                _mov.siesa_sync = _sync_antes or 'PENDIENTE'
                db.session.commit()
            else:
                try:
                    _mov.siesa_sync = 'ENVIADO'
                    db.session.commit()
                except Exception as _e:
                    db.session.rollback()
                    logger.critical(
                        '[DLQ] TRASLADO_AVERIAS job=%s: Siesa OK pero no se pudo '
                        'marcar el movimiento %s — riesgo de traslado duplicado '
                        'NB1→AV1 (el saldo de NB1 puede quedar negativo): %s',
                        job.id, _mov.id, _e)
            return _res

        from app.models.devolucion import TareaDevolucion as _TareaDev
        tarea_dev = _TareaDev.query.get(payload.get('tarea_id'))

        # P4: idempotencia — si tarea_dev no existe o ya tiene triggered, no reenviar
        if tarea_dev is None:
            logger.warning(
                f'[DLQ] TRASLADO_AVERIAS job={job.id}: tarea_id={payload.get("tarea_id")} '
                f'no existe en DB — omitiendo llamada a Siesa para evitar duplicado sin clave'
            )
            return {'idempotente': True, 'sin_tarea': True}

        if _exigir_preflag_con_desenlace(tarea_dev, job, f'el traslado a averías de la tarea {tarea_dev.id}'):
            logger.info(
                f'[DLQ] TRASLADO_AVERIAS job={job.id}: tarea {tarea_dev.id} ya entró a '
                f'Siesa — omitiendo llamada (idempotencia P4)'
            )
            return {'idempotente': True, 'tarea_id': tarea_dev.id}

        item_codigo = payload.get('item_codigo')
        if not item_codigo:
            raise ValueError(
                f'TRASLADO_AVERIAS job={job.id}: item_codigo faltante en payload — '
                f'no se puede enviar a Siesa sin referencia del ítem'
            )

        # Pre-flag ANTES del POST (Regla 6) vía el helper compartido — cierra
        # la ventana en la que un proceso muerto a mitad de camino dejaba
        # `siesa_triggered=False` y el reset automático de jobs atascados
        # reintentaba desde cero, duplicando el traslado a averías (el saldo
        # de la bodega origen podía quedar negativo en Siesa).
        return _ejecutar_con_preflag(tarea_dev, lambda: connekta.transferir_a_averias(
            item_codigo=item_codigo,
            cantidad=payload['cantidad'],
            referencia=payload.get('referencia', ''),
        ))

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
                # Con su desenlace: AJUSTADO es la constancia (estado_del_preflag).
                sesion_cteo.siesa_response = sesion_cteo.siesa_response or json.dumps(
                    {'estado_corregido': True, 'job_id': job.id})
                db.session.commit()
            except Exception as _e_fix:
                db.session.rollback()
                logger.error(f'[DLQ] No se pudo corregir siesa_triggered para sesion {sesion_id}: {_e_fix}')
            return {'idempotente': True, 'sesion_id': sesion_id, 'estado_corregido': True}

        # P4: idempotencia — si ya entró, no reenviar; bandera sin desenlace
        # (crash entre la marca y el POST) = «no sé», levanta.
        if _exigir_preflag_con_desenlace(sesion_cteo, job, f'el ajuste de la sesión {sesion_id}'):
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
                    # Reaplicar cambio de inventario WMS — la misma política que
                    # el camino normal (`aplicar_ajuste_al_wms`, VAL-2): el SKU
                    # en el almacén queda en lo contado, no un hueco con piso 0.
                    _mc = payload.get('motivo_codigo')
                    _cant = payload.get('cantidad', 0)
                    if payload.get('tarea_picking_id'):
                        _inv = (_UbicProd.query
                                .filter_by(
                                    ubicacion_id=payload.get('ubicacion_id'),
                                    producto_id=payload.get('producto_id')
                                ).with_for_update().first())
                        if _inv:
                            _inv.bloqueado = max(0, _inv.bloqueado - _cant)
                    if _mc and _cant:
                        from app.services.conteo_service import ConteoService as _CSR
                        _CSR.aplicar_ajuste_al_wms(sesion_cteo, _mc, _cant,
                                                   documento=payload.get('referencia'))
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
            # Sin `diferencia` se reconstruye contra la MISMA base del conteo
            # (el teórico de su foto, `existencia − POS`), nunca contra la
            # existencia cruda: esa resta es el doble descuento del POS.
            from app.services.conteo_service import ConteoService as _CS
            _dif_r = (
                sesion_cteo.diferencia
                if sesion_cteo.diferencia is not None
                else (sesion_cteo.cantidad_fisica or 0) - (_CS.base_de_comparacion(sesion_cteo) or 0)
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

        # Pre-flag ANTES del POST (Regla 6) vía el helper compartido — antes
        # el flag se marcaba en un mini-commit DESPUÉS del POST, y si ESE
        # commit fallaba el código hacía `raise`, lo que gastaba un reintento
        # normal con `siesa_triggered` todavía en `False`: el siguiente intento
        # reenviaba el ajuste, duplicándolo — exactamente lo que el mini-commit
        # existía para evitar. Con el flag ya persistido antes de llamar a
        # Siesa, ese escenario deja de ser posible.
        resultado = _ejecutar_con_preflag(sesion_cteo, lambda: connekta.enviar_ajuste_inventario(
            motivo_codigo=payload['motivo_codigo'],
            item_codigo=item_codigo,
            item_id_siesa=_item_id_siesa,
            cantidad=payload['cantidad'],
            referencia=payload.get('referencia', ''),
            bodega=payload.get('bodega'),
            centro_op=payload.get('centro_op'),
            # El costo que se fijó al aprobar (solo una entrada sin promedio
            # en la bodega lo trae): el DLQ no lo recalcula.
            costo_unitario=payload.get('costo_unitario'),
        ))
        _now = datetime.utcnow()

        # Commit completo: estado + respuesta + inventario. `siesa_triggered`
        # ya quedó persistido por el helper — si esto falla no hay riesgo de
        # doble ajuste, solo estado/respuesta/inventario pendientes de
        # reconciliar (la rama de recuperación de arriba, sesión atascada en
        # AJUSTANDO, ya sabe recuperarse de esto).
        try:
            sesion_cteo = _SesionConteo.query.get(sesion_id)
            sesion_cteo.siesa_response = json.dumps(resultado)
            sesion_cteo.estado = EstadoConteo.AJUSTADO
            sesion_cteo.fecha_cierre = _now

            # Actualizar stock WMS local — mantiene sincronía sin esperar sync
            # nocturna. El SKU en el ALMACÉN queda en lo contado, repartido
            # entre sus huecos (`ConteoService.aplicar_ajuste_al_wms`, VAL-2):
            # antes el delta del total iba a un solo hueco con piso 0.
            motivo_codigo = payload['motivo_codigo']
            cantidad_ajuste = payload['cantidad']
            tarea_picking_id = payload.get('tarea_picking_id')
            if tarea_picking_id:
                # Desbloquear solo si vino de excepción de picking: el bloqueo
                # vive en el hueco de la tarea (el de la sesión de auditoría).
                inv = (_UbicProd.query
                       .filter_by(
                           ubicacion_id=payload.get('ubicacion_id'),
                           producto_id=payload.get('producto_id')
                       ).with_for_update().first())
                if inv:
                    inv.bloqueado = max(0, inv.bloqueado - cantidad_ajuste)
            from app.services.conteo_service import ConteoService as _CSW
            _CSW.aplicar_ajuste_al_wms(sesion_cteo, motivo_codigo, cantidad_ajuste,
                                       documento=payload.get('referencia'))

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
        from app.services.traslado_service import TrasladoService, traslado_usa_rit

        # Con TRASLADO_USA_RIT apagada se ignora también el consecutivo que traiga
        # el payload de un job encolado antes: el STS sale por 173076 directo.
        consec_rit = (
            (payload.get('consec_rit') or solicitud.siesa_requisicion_consec)
            if traslado_usa_rit() else None
        )
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

        if res.get('modo_ensayo'):
            from app.extensions import db as _db
            solicitud.siesa_error = (
                'ENSAYO (MODO_ENSAYO): el documento de salida (STS) NO se envió a '
                f'Siesa; el traslado sigue en {solicitud.estado}. Con el ensayo '
                'apagado, reintente el envío desde Siesa → Recuperación.')
            _db.session.commit()
            raise TrasladoNoEnviadoEnEnsayo(
                f'DESPACHO_TRASLADO job={job.id}: MODO_ENSAYO bloqueó el STS de '
                f'{solicitud.codigo}; no se envió nada. El traslado sigue en '
                f'{solicitud.estado}. Reintente con el ensayo apagado.')
        if not res.get('simulado'):
            consec = TrasladoService._extraer_consec(res)
            if consec:
                solicitud.siesa_salida_consec = consec
            else:
                # Tres respuestas (P0-6): «no sé» no se confunde con «no está».
                from app.services.connekta_gateway import RecuperacionNoDisponible
                try:
                    consec_rec = _st.recuperar_consec_salida(solicitud.codigo)
                except RecuperacionNoDisponible as _e_rec:
                    logger.error('[DLQ] DESPACHO_TRASLADO %s: recovery sin respuesta: %s',
                                 solicitud.codigo, _e_rec)
                    consec_rec = None
                solicitud.siesa_salida_consec = consec_rec
            solicitud.estado = EstadoTraslado.EN_TRANSITO
            # `fecha_despacho` se escribía SOLO en `TrasladoService.despachar`
            # (el botón del admin). Este camino —cierre de packing → DLQ— la
            # dejaba en NULL, y `traslado_monitor_service` filtra por
            # `fecha_despacho <= limite`: un NULL nunca entra, así que el
            # monitor de traslados estancados era CIEGO a todo lo despachado
            # por el empacador. TRA-30 los reportaba como «en tránsito sin
            # fecha de despacho» sin que nadie supiera por qué.
            if solicitud.fecha_despacho is None:
                solicitud.fecha_despacho = datetime.utcnow()
            # Sin consecutivo el STS salió (el POST fue aceptado) pero el WMS
            # no lo sabe leer: borrar el error dejaba el traslado «en verde»
            # con el ETS imposible de emitir. Se declara.
            solicitud.siesa_error = None if solicitud.siesa_salida_consec else (
                'AVISO: el 173076 fue aceptado por Siesa pero el WMS no pudo leer '
                'su consecutivo. Use Reintentar despacho para recuperarlo — el '
                'botón verifica antes de reenviar.')
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

        # Bandera con su NC confirmada: idempotente; bandera sola: «no sé»
        # (la misma decisión que los otros cinco tipos con pre-flag).
        if recaudo and _exigir_envio_con_desenlace(
                job, f'la nota crédito del recaudo {recaudo.id}'):
            logger.info(
                '[DLQ] NOTA_CREDITO_FACTURA job=%s: recaudo %s ya tiene su NC en '
                'Siesa — idempotente', job.id, recaudo.id)
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

        try:
            lineas_nc = _construir_lineas_nc(
                rowids_data, es_total, items_devueltos, causal,
                motivo=connekta.motivo_ventas, uom_default=connekta.uom_default,
                bodega_default=connekta.bodega,
            )
        except LineaDevueltaAmbigua as _e_amb:
            # Regla 0: el job falla nombrando el recaudo y la referencia. Ocurre
            # ANTES del pre-flag, así que no queda ninguna bandera encendida.
            #
            # Se re-lanza **la misma clase** con el contexto adentro del
            # mensaje. El envoltorio en `Exception` genérica que había acá le
            # borraba el tipo al clasificador del DLQ, que entonces no la podía
            # distinguir de un rechazo de Siesa —reintentable— y le gastaba los
            # 5 intentos: 185 minutos de backoff antes de avisarle a la única
            # persona que puede resolverla. La ambigüedad es determinista: los
            # mismos datos la producen siempre. Es la convención que ya siguen
            # `ConnektaPaginacionError` y `_ResultadoDesconocido`: el contexto
            # se agrega, el tipo no se toca.
            raise LineaDevueltaAmbigua(
                f'NOTA_CREDITO_FACTURA job={job.id} recaudo='
                f'{payload.get("recaudo_id")} FE {tipo_docto_fe}-{consec_fe}: '
                f'{_e_amb}'
            ) from _e_amb

        if not lineas_nc:
            logger.warning(
                '[DLQ] NOTA_CREDITO_FACTURA job=%s: sin líneas para devolver — '
                'posible mismatch de códigos entre items_devueltos y factura Siesa',
                job.id
            )
            _anotar_en_recaudo(job, 'SIN_LINEAS', recaudo=recaudo)
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
        except _ResultadoDesconocido:
            # **El POST salió y no sabemos si entró.** NO se revierte el
            # pre-flag: con la bandera abajo el DLQ reintenta, y si el
            # documento sí se había creado eso es una SEGUNDA nota crédito
            # —un documento fiscal que alguien tiene que reversar a mano.
            #
            # El `except Exception` que había acá decía «fallo explícito: no
            # se creó nada» y atrapaba también el timeout, que es justo el
            # caso donde sí se creó. Su hermana `RECIBO_CAJA` ya verificaba
            # antes de revertir; esta no, y nada explicaba la diferencia.
            logger.error(
                '[DLQ] NOTA_CREDITO_FACTURA job=%s: timeout. La NC PUEDE '
                'existir en Siesa. No se revierte el pre-flag ni se '
                'reintenta — verificar en Auditoría de documentos.', job.id)
            raise
        except ConnektaNoEnviado:
            # Prueba positiva de que no entró: Siesa contestó que no (4xx,
            # `codigo != 0`, 429) o el POST no salió. Acá sí no se creó nada,
            # y se revierte para que el DLQ reintente. La otra mitad del
            # pre-flag.
            if recaudo:
                try:
                    recaudo.siesa_nc_triggered = False
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            raise
        except Exception as _e_post:
            # Un 5xx, una conexión cortada, un JSON ilegible sobre un 200: la
            # NC PUEDE existir. No se revierte (Regla 3) y se declara.
            raise _ResultadoDesconocido(
                f'NOTA_CREDITO_FACTURA job={job.id}: el envío falló sin respuesta '
                f'clara de Siesa ({_e_post}). La nota crédito PUEDE existir: '
                f'verificar en Siesa antes de reintentar.') from _e_post

        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if recaudo and _es_ensayo:
            # Modo ensayo: el POST se bloqueó, no hay documento que proteger.
            try:
                recaudo.siesa_nc_triggered = False
                db.session.commit()
            except Exception:
                db.session.rollback()
        elif recaudo:
            _anotar_en_recaudo(job, 'ENVIADO', respuesta=resultado, recaudo=recaudo)
        return resultado

    if job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE':
        # 142946 — Nota crédito de devolución de cliente atada al pedido/factura
        # (módulo de Recepción, reemplaza TareaDevolucion). tipo_docto_fe/consec_fe
        # ya vienen resueltos como el tipo/consec REAL de la factura electrónica
        # (connekta.get_detalle_factura, ver devolucion_cliente_service.py) —
        # nunca los del pedido.
        from app.models.devolucion_cliente import DevolucionCliente as _DC
        devolucion = db.session.get(_DC, payload.get('devolucion_id'))

        if devolucion and _exigir_envio_con_desenlace(
                job, f'la nota crédito de la devolución {devolucion.id}'):
            logger.info(
                '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: devolución %s ya tiene '
                'su NC en Siesa — idempotente', job.id, devolucion.id)
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
        try:
            lineas_nc = _construir_lineas_nc(
                rowids_data, es_total=False, items_devueltos=items_devueltos,
                causal=causal, motivo=connekta.motivo_ventas, uom_default=connekta.uom_default,
                bodega_default=connekta.bodega, bodega_averias=connekta.bodega_averias,
            )
        except LineaDevueltaAmbigua as _e_amb:
            # Regla 0: el job falla nombrando la devolución y la referencia, en
            # vez de emitir una NC que cruza de más. Ocurre ANTES del pre-flag
            # (`siesa_nc_triggered` sigue en False), así que la devolución se
            # puede rehacer desde Recepción sin destrabar nada a mano.
            #
            # Misma clase, mensaje enriquecido — ver la gemela en
            # NOTA_CREDITO_FACTURA. Envolverla en `Exception` la volvía
            # indistinguible de un rechazo reintentable y le costaba 3 horas de
            # backoff a un error que da lo mismo las cinco veces. Y éste es el
            # handler **vivo**: lo encola `devolucion_cliente_service.py`.
            raise LineaDevueltaAmbigua(
                f'NOTA_CREDITO_DEVOLUCION_CLIENTE job={job.id} devolución='
                f'{devolucion.codigo if devolucion else payload.get("devolucion_id")} '
                f'FE {tipo_docto_fe}-{consec_fe}: {_e_amb}'
            ) from _e_amb

        if not lineas_nc:
            # Antes: `{'sin_lineas': True}` → COMPLETADO sin NC. Ahora falla y
            # avisa (determinista: el payload no cambia). Ocurre ANTES del
            # pre-flag, así que nada queda marcado como enviado.
            raise NotaCreditoSinLineas(
                f'NOTA_CREDITO_DEVOLUCION_CLIENTE job={job.id} devolución='
                f'{devolucion.codigo if devolucion else payload.get("devolucion_id")} '
                f'FE {tipo_docto_fe}-{consec_fe}: ninguna línea devuelta coincide con la '
                f'factura en Siesa ({[(it.get("codigo"), it.get("f470_rowid")) for it in items_devueltos]}). '
                f'El inventario ya reingresó (zona de devoluciones); la NC no sale hasta '
                f'revisar las líneas.')

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

        # ── Pre-flag (Regla 6) ──────────────────────────────────────────────
        # Este handler marcaba `siesa_nc_triggered` **después** del POST. Un
        # crash o un timeout entre el POST y el commit dejaba la bandera en
        # False, el DLQ reintentaba, y Siesa recibía una SEGUNDA nota crédito
        # —documento fiscal, con cruce de cartera automático (251126).
        #
        # CLAUDE.md declara este defecto como corregido y lista este job como
        # «pre-flag». Lo estaba en `NOTA_CREDITO_FACTURA`, que **no tiene
        # productor**: el arreglo se aplicó al gemelo muerto. Este es el vivo
        # —lo encola `devolucion_cliente_service.py:335`— y seguía marcando
        # después.
        if devolucion:
            devolucion.siesa_nc_triggered = True
            devolucion.siesa_nc_triggered_at = datetime.utcnow()
            db.session.commit()

        try:
            resultado = connekta.trigger_nota_factura_crear_cruzar(
                tipo_docto_fe=tipo_docto_fe,
                consec_fe=consec_fe,
                lineas=lineas_nc,
                valor_cruce=float(valor_cruce),
                notas=payload.get('notas', ''),
            )
        except _ResultadoDesconocido:
            # El POST salió y no sabemos si entró. **No se revierte**: con la
            # bandera abajo el DLQ reintenta, y si la NC ya existía eso es la
            # segunda. Verificar en Auditoría de documentos.
            logger.error(
                '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: timeout. La NC '
                'PUEDE existir en Siesa. No se revierte el pre-flag ni se '
                'reintenta — verificar en Auditoría de documentos.', job.id)
            raise
        except ConnektaNoEnviado:
            # Rechazo explícito de Siesa (o el POST no salió): acá sí no se
            # creó nada, y revertir deja que el DLQ reintente. La otra mitad
            # del pre-flag.
            if devolucion:
                try:
                    devolucion.siesa_nc_triggered = False
                    devolucion.siesa_nc_triggered_at = None
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            raise
        except Exception as _e_post:
            # Sin respuesta clara la NC PUEDE existir: no se revierte y se
            # declara (Regla 3). Antes un 5xx revertía y el DLQ reenviaba.
            raise _ResultadoDesconocido(
                f'NOTA_CREDITO_DEVOLUCION_CLIENTE job={job.id}: el envío falló sin '
                f'respuesta clara de Siesa ({_e_post}). La nota crédito PUEDE '
                f'existir: verificar en Siesa antes de reintentar.') from _e_post

        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if devolucion and _es_ensayo:
            # Modo ensayo: el POST se bloqueó, no hay documento que proteger.
            try:
                devolucion.siesa_nc_triggered = False
                devolucion.siesa_nc_triggered_at = None
                db.session.commit()
            except Exception:
                db.session.rollback()
        if devolucion and not _es_ensayo:
            # La bandera ya se encendió ANTES del POST (pre-flag, arriba).
            # Acá solo queda guardar la respuesta: si este commit falla, la
            # protección anti-duplicado sigue en pie —que era justo lo que el
            # `logger.critical` de la versión anterior tenía que gritar,
            # porque entonces la bandera y el documento no estaban atados.
            try:
                devolucion.siesa_nc_response = json.dumps(resultado, ensure_ascii=False)
                db.session.commit()
            except Exception as _e:
                db.session.rollback()
                logger.error(
                    '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: la NC entró '
                    'pero no se pudo guardar la respuesta de Siesa en la '
                    'devolución %s: %s. El pre-flag sigue arriba, así que no '
                    'hay riesgo de duplicado.', job.id, devolucion.id, _e)

        # Bridge Liquidación de ruta → Devoluciones: si esta devolución se
        # originó en una entrega Parcial/Rechazada ("Liquidar en WMS", ver
        # liquidacion_service._crear_devolucion_pendiente), avisarle al
        # RecaudoEntrega que su NC ya salió — es la señal que el RECIBO_CAJA
        # dependiente (depende_de_nc) está esperando para poder dispararse.
        if devolucion and not _es_ensayo and devolucion.recaudo_entrega_id:
            try:
                # El desenlace también, en la entidad (m036fotos). El
                # consecutivo llega después, con el motivo DIAN. Si este puente
                # falla, el RC dependiente lo reconstruye (`nc_ya_salio`).
                from app.services import devolucion_ruta as _dr_puente
                if _dr_puente.puentear_nc_al_recaudo(devolucion, respuesta=resultado):
                    db.session.commit()
                    logger.info(
                        '[DLQ] NOTA_CREDITO_DEVOLUCION_CLIENTE job=%s: recaudo %s '
                        'marcado siesa_nc_triggered=True — RC dependiente puede dispararse',
                        job.id, devolucion.recaudo_entrega_id
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
        # Si la devolución vino de una ruta, su recaudo recibe el consecutivo
        # real de la NC (m036fotos). Anotar no puede romper el motivo.
        if devolucion.recaudo_entrega_id:
            try:
                from app.models.recaudo_entrega import RecaudoEntrega as _REM
                _rec = db.session.get(_REM, devolucion.recaudo_entrega_id)
                if _rec is not None and _rec.siesa_nc_consec != str(consec_nc):
                    _rec.anotar_documento_siesa('NC', 'ENVIADO', consec=consec_nc)
                    db.session.commit()
            except Exception as _e_rec:
                db.session.rollback()
                logger.warning('[DLQ] MOTIVO_DIAN_NC job=%s: no se pudo anotar el '
                               'consecutivo en el recaudo: %s', job.id, _e_rec)

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
        from app.models.recaudo_entrega import EstadoEntrega as _EE, RecaudoEntrega as _RE
        from app.services import envio_liquidacion as _env
        from app.services import politica_cobro as _pc
        recaudo = _RE.query.get(payload.get('recaudo_id'))
        if recaudo is None or recaudo.tarea is None:
            raise DatoQueFalta(
                f'RECIBO_CAJA job={job.id}: la parada {payload.get("recaudo_id")} (o su pedido) ya '
                f'no existe en el WMS — no hay de qué emitir un recibo. Descártelo (administrador).')

        if recaudo.siesa_rc_triggered:
            # La bandera es de PRE-envío (Regla 6): encendida dice «se intentó»,
            # no «entró». Solo con el desenlace confirmado (`rc_llego_a_siesa`)
            # esto es idempotente. Si no, un intento anterior quedó sin
            # respuesta clara (o se cortó): la verificación diferida (P1-3)
            # lee la cartera a los +15 min y a las +2 h; **nunca reenvía**.
            if _pc.rc_llego_a_siesa(recaudo):
                logger.info(
                    '[DLQ] RECIBO_CAJA job=%s: recaudo %s ya tiene su RC en Siesa '
                    '— idempotente', job.id, recaudo.id)
                return {'idempotente': True, 'recaudo_id': recaudo.id}
            return _verificar_rc_sin_respuesta(job, recaudo, connekta)

        # El recibo solo sale si la parada sigue siendo la que se encoló (P1-3).
        # El monto no se re-deriva de lo declarado (job 483: pisar el neto
        # correcto con lo que tecleó el conductor ya costó $10.151,97); se
        # comprueba que nadie reescribió la parada por un camino que no pasó
        # por `puede_editar_cobro`.
        _que = _env.que_es(recaudo.tarea)
        if recaudo.estado_entrega not in (_EE.ENTREGADO, _EE.PARCIAL):
            raise DatoQueFalta(
                f'{_que}: la parada quedó {recaudo.estado_entrega} después de encolar el '
                f'recibo — no representa plata recibida. No se envía; si la parada está '
                f'bien así, no hay recibo que emitir.')
        _cambios = _pc.difiere_de_instantanea(recaudo, payload.get('instantanea'))
        if _cambios:
            raise DatoQueFalta(
                f'{_que}: la parada cambió después de encolar el recibo '
                f'({"; ".join(_cambios)}). No se envía una cifra que ya no es la de la '
                f'parada: quien liquida vuelve a pulsar «Enviar todo a Siesa» y sale con '
                f'la parada como está ahora.')

        # Secuencialidad: si depende de NC, verificar que NC ya pasó.
        # `DependenciaPendiente` y no `Exception`: esperar no gasta reintento.
        if payload.get('depende_de_nc') and not recaudo.siesa_nc_triggered:
            from app.services import devolucion_ruta as _dr_rc
            if _dr_rc.nc_ya_salio(recaudo):
                # La NC salió y el puente al recaudo falló en su job (P1-6b):
                # se reconstruye acá, con la misma función.
                _d_nc = _dr_rc.devolucion_vigente(recaudo.id)
                _dr_rc.puentear_nc_al_recaudo(_d_nc)
                db.session.commit()
                logger.warning(
                    '[DLQ] RECIBO_CAJA job=%s: la NC de la devolución %s ya había '
                    'salido y el recaudo %s no lo sabía — puente reconstruido',
                    job.id, _d_nc.codigo if _d_nc else '?', recaudo.id)
            elif _dr_rc.nc_no_llegara(recaudo):
                # La NC ya no va a salir si la devolución terminó sin ella
                # —contada en cero (FALTANTE_TOTAL) o cancelada—: el RC sale por
                # lo cobrado y la factura queda con el saldo de lo que no
                # volvió, en cartera. NC → RC → DC no se rompe: no hay NC que
                # esperar, y el DC sigue esperando a este RC.
                logger.warning(
                    '[DLQ] RECIBO_CAJA job=%s: la devolución del recaudo %s terminó sin '
                    'nota crédito (faltante total o cancelada) — el RC sale por lo cobrado',
                    job.id, recaudo.id)
            else:
                # Sin tope que la destrabe (NC → RC, Regla 7): la espera se
                # hace VISIBLE (P1-4). Pasadas `RC_ESPERA_NC_HORAS` el mensaje
                # dice cuánto lleva, qué devolución y quién la destraba, y el
                # resumen diario lo lista (`devolucion_ruta.avisos`).
                _esp = _dr_rc.rc_esperando_nc(job)
                _que_rc = _env.que_es(recaudo.tarea)
                if _esp is not None and _esp['vencido']:
                    raise DependenciaPendiente(
                        f'{_que_rc}: el recibo de caja lleva {_esp["horas"]:g} h esperando la '
                        f'nota crédito de la devolución {_esp["devolucion"] or "(sin devolución)"} '
                        f'(el tope es {_esp["tope"]:g} h): la plata ya entregada no entra a la caja '
                        f'de Siesa. La destraba {_esp["responsable"]}.')
                raise DependenciaPendiente(
                    f'{_que_rc}: el recibo de caja espera la nota crédito de la devolución, '
                    f'que se dispara cuando bodega la cuenta. Sigue pendiente.'
                )

        # Un pago bancario sale a Siesa cuando alguien lo vio en el banco
        # (m051liqcaja, integración de liquidación 2026-09-27): por verificar
        # espera sin gastar intento; no encontrado no sale (`ErrorDeterminista`).
        # Antes de leer la cartera: esperar al banco no le cuesta una lectura
        # a Siesa por ciclo.
        from app.services import verificacion_banco as _vb
        _vb.exigir_para_rc(recaudo)

        # Lo que el POST necesita de la cartera, resuelto AHORA de la fila del
        # documento exacto (P1-2): tercero, sucursal, cuenta, UN, CO. Encolar
        # ya no le pregunta nada a Siesa (P1-1). Sin fila, espera o se declara
        # (`envio_liquidacion`): nunca sale con un dato de respaldo.
        # El monto: si lo encoló «Registrar cobro» ya viene calculado (con la
        # decisión de quien liquidó); si lo encoló el botón masivo, se calcula
        # acá con la misma política (`politica_cobro.monto_rc`).
        payload, _cruce = _env.resolver_envio(job, recaudo, connekta)
        monto_payload = float(payload['monto'])

        # Los argumentos del POST se arman ANTES del pre-flag: un dato que falta
        # revienta acá, sin bandera puesta y sin POST.
        #
        # `fecha_recaudo`: el día (Bogotá) en que el conductor cobró, no el del
        # envío del DLQ (Regla 5; una ruta liquidada al día siguiente llevaba
        # la fecha del envío). Sin fecha de confirmación, la del envío.
        from app.utils.fecha import fecha_bogota_de
        kwargs_rc = dict(
            tercero_nit=payload['tercero_nit'],
            sucursal=payload['sucursal'],
            monto=monto_payload,
            forma_pago=payload.get('forma_pago', 'EFECTIVO'),
            tipo_docto_fe=payload['tipo_docto_fe'],
            consec_fe=payload['consec_fe'],
            co_factura=payload['co_factura'],
            cuenta_cxc=payload['cuenta_cxc'],
            unidad_negocio=payload['unidad_negocio'],
            notas=payload.get('notas', ''),
            ajuste_valor=float(payload.get('ajuste_valor') or 0),
            ajuste_es_sobrante=bool(payload.get('ajuste_es_sobrante', False)),
            # Del recaudo y no del payload: el comprobante se puede haber
            # anotado después de encolar el job (edición de la parada).
            referencia_pago=(getattr(recaudo, 'referencia_pago', None) or ''),
            # El momento real del cobro (m050plata), no la hora de la última
            # re-confirmación (validación de la plata, 2026-09-26).
            fecha_recaudo=fecha_bogota_de(_pc.momento_del_cobro(recaudo)),
        )

        # Pre-flight: ¿la factura que vamos a pagar ya quedó sin saldo?
        # (cross-flow WMS↔Cartera — alguien más ya la cruzó por otra vía). El
        # saldo de la MISMA fila que se acaba de leer queda como el de ANTES
        # del POST: es la vara con que, si el POST falla sin decir que no, se
        # decide si el recibo entró (P0-4).
        saldo_antes = _cruce.saldo
        from app.services.cxc_cruce import TOLERANCIA as _TOL_RC
        # `saldo_antes is not None` y no truthy: `None` significa «no pude
        # verificar» (simulación), y saltarse el envío por no saber dejaría la
        # factura sin recibo para siempre.
        if saldo_antes is not None and saldo_antes <= _TOL_RC:
            logger.info(
                '[DLQ] RECIBO_CAJA job=%s: factura %s-%s ya sin saldo pendiente '
                '(pre-flight) — marcando completado sin enviar',
                job.id, payload.get('tipo_docto_fe', ''), payload.get('consec_fe', ''),
            )
            job.payload = json.dumps(payload, ensure_ascii=False)
            recaudo.siesa_rc_triggered = True
            db.session.commit()
            _anotar_en_recaudo(job, 'YA_SALDADA', recaudo=recaudo)
            return {'ya_existente': True, 'recaudo_id': payload.get('recaudo_id')}

        # Pre-flag: marcar ANTES del POST para cerrar el crash window. El saldo
        # de antes y lo resuelto quedan escritos en el job, en la misma
        # transacción. Y la marca de un cobro de otro mes (tanda 2): el recibo
        # se fecha en el mes del envío y el aviso de rutas que cruzan de mes lo
        # lista (`rezago_liquidacion.recibos_de_otro_mes`). La decide la misma
        # política que arma las fechas del payload.
        recaudo.siesa_rc_triggered = True
        from app.services.politica_cobro import fechas_del_recibo
        from app.utils import fecha as _fecha_rc
        _hoy_rc = _fecha_rc.fecha_hoy_bogota()
        _f_rc = fechas_del_recibo(kwargs_rc['fecha_recaudo'], _hoy_rc)
        recaudo.rc_cobro_otro_mes = (
            datetime.strptime(_f_rc['fecha_cobro'], '%Y%m%d').date()
            if _f_rc['cruza_mes'] else None)
        payload['saldo_antes_rc'] = saldo_antes
        # El día del documento: lo que una persona busca en Siesa si el envío
        # queda sin verificar (`envio_liquidacion.que_buscar_en_siesa`).
        payload['fecha_documento_rc'] = _hoy_rc
        # Cuándo salió el POST: la vara de la verificación diferida (P1-3).
        payload['intento_rc_en'] = datetime.utcnow().isoformat(timespec='seconds')
        job.payload = json.dumps(payload, ensure_ascii=False)
        db.session.commit()

        try:
            resultado = connekta.trigger_recibo_caja(**kwargs_rc)
        except ConnektaNoEnviado:
            # **Prueba positiva de que no entró**: Siesa contestó que no (4xx,
            # `codigo != 0`, 429), el POST no salió (circuito abierto) o el
            # documento no se pudo armar. Acá, y solo acá, se revierte la
            # bandera para que la cola reintente.
            try:
                recaudo.siesa_rc_triggered = False
                recaudo.rc_cobro_otro_mes = None
                db.session.commit()
            except Exception:
                db.session.rollback()
            raise
        except Exception as _e_post:
            # «¿Entró?» se contesta por el DOCUMENTO, no por «saldo ≤ 0,5»: un
            # RC de contado con retención sale neto y el DC va después, así que
            # tras un recibo que SÍ entró la factura conserva el saldo de la
            # retención. Se compara el saldo de antes (escrito arriba) contra
            # el de ahora (`lectura_de_verificacion`, la única que decide): si
            # bajó por el monto del recibo, entró. Si no, «no sé» — y ante «no
            # sé» no se reintenta (Regla 3): la bandera queda puesta y la
            # verificación sigue sola a los +15 min y a las +2 h (P1-3).
            _lect = _env.lectura_de_verificacion(job, recaudo, connekta)
            if _lect['entro']:
                logger.info(
                    '[DLQ] RECIBO_CAJA job=%s: el POST falló pero el saldo de la '
                    'factura bajó por el monto del recibo — el RC sí entró', job.id)
                _anotar_en_recaudo(job, 'ENVIADO', recaudo=recaudo)
                return {'timeout_pero_exitoso': True, 'verificado_por': 'saldo'}
            logger.error(
                '[DLQ] RECIBO_CAJA job=%s: el POST falló sin que Siesa dijera que no '
                'y el saldo todavía no confirma si el recibo del recaudo %s entró. '
                'No se reintenta (Regla 3): se vuelve a leer la cartera en %s min.',
                job.id, recaudo.id, _env.PLAN_VERIFICACION_MIN[0])
            payload['verificacion'] = {
                'desde': datetime.utcnow().isoformat(timespec='seconds'),
                'error': str(_e_post)[:300],
                'lecturas': [_lect],
            }
            job.payload = json.dumps(payload, ensure_ascii=False)
            db.session.commit()
            raise DependenciaPendiente(
                f'{_env.que_es(recaudo.tarea, payload.get("tipo_docto_fe"), payload.get("consec_fe"))}: '
                f'el envío del recibo de caja falló sin respuesta clara de Siesa ({_e_post}). '
                f'No se reenvía: el WMS vuelve a leer la cartera a los '
                f'{_env.PLAN_VERIFICACION_MIN[0]} min y a las '
                f'{_env.PLAN_VERIFICACION_MIN[-1] // 60} h para saber si entró.',
                espera_minutos=_env.PLAN_VERIFICACION_MIN[0]) from _e_post

        # Si modo ensayo, revertir flag (no se creó nada en Siesa)
        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if _es_ensayo:
            try:
                recaudo.siesa_rc_triggered = False
                recaudo.rc_cobro_otro_mes = None
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            _anotar_en_recaudo(job, 'ENVIADO', respuesta=resultado, recaudo=recaudo)
        return resultado

    if job.tipo == 'DOCUMENTO_CONTABLE_RET':
        # 142882 — Documento contable para retenciones
        from app.models.recaudo_entrega import RecaudoEntrega as _RE
        from app.services import politica_cobro as _pc
        recaudo = _RE.query.get(payload.get('recaudo_id'))

        # La guarda es POR CUENTA PUC, no por recaudo.
        #
        # Las retenciones son N: retefuente + reteIVA + ICA son tres documentos
        # distintos. Con la guarda sobre el booleano, el primer job la encendía
        # y **los otros dos se declaraban idempotentes sin enviar nada** — tres
        # jobs completados, un documento en Siesa.
        _puc = payload.get('cuenta_puc')
        # La marca es de PRE-envío (como la del RC): solo con el desenlace
        # confirmado es idempotente. Sin él, un intento anterior se cortó o
        # no se pudo verificar: completar acá decía «hecho» sin saberlo y
        # reenviar puede duplicar la NI. Se declara (validación 2026-09-26).
        if recaudo and _puc and _exigir_envio_con_desenlace(
                job, f'la retención {_puc} del recaudo {recaudo.id}'):
            logger.info(
                '[DLQ] DOCUMENTO_CONTABLE_RET job=%s: recaudo %s ya tiene la '
                'cuenta %s en Siesa — idempotente', job.id, recaudo.id, _puc)
            return {'idempotente': True, 'recaudo_id': recaudo.id, 'cuenta_puc': _puc}

        # La política de retención se revalida antes del POST (P0-5): un job
        # encolado antes de que alguien la rechazara —o reintentado a mano—
        # no emite una NI por plata que el cliente no tenía derecho a
        # descontar.
        if recaudo is not None:
            _tipo_ret = payload.get('tipo_retencion') or _tipo_de_retencion_por_puc(_puc)
            try:
                _pc.exigir_retencion_aplicable(recaudo, _tipo_ret)
            except _pc.RetencionNoAplicable as _e_pol:
                raise ErrorDeterminista(
                    f'DOCUMENTO_CONTABLE_RET job={job.id}: {_e_pol}. No se envía.'
                ) from _e_pol

        # Secuencialidad: NI de retenciones DEBE ir DESPUÉS del RC.
        # Si el RC no pasó aún, el cruce CxC del NI puede fallar porque
        # Siesa no ha reducido el saldo por el cash todavía.
        if recaudo and not recaudo.siesa_rc_triggered:
            _rc_vivo = SiesaJob.query.filter(
                SiesaJob.tipo == 'RECIBO_CAJA',
                SiesaJob.referencia_tipo == 'RecaudoEntrega',
                SiesaJob.referencia_id == recaudo.id,
                SiesaJob.estado.in_(list(EstadoSiesaJob.ACTIVOS)),
            ).first()
            if _rc_vivo is None:
                # El RC no está en la cola: FALLIDO, DESCARTADO o nunca
                # encolado. Esperar era para siempre (P1-6a) — reintentar sin
                # espera cada 2 minutos, sin que nadie lo viera. Se declara.
                raise ErrorDeterminista(
                    f'DOCUMENTO_CONTABLE_RET job={job.id}: espera el recibo de caja '
                    f'del recaudo {recaudo.id} y ese recibo no está en la cola '
                    f'(falló, se descartó o nunca se encoló). Resolver el recibo y '
                    f'reintentar esta retención.')
            # Espera corta: el RC de este mismo recaudo suele resolverse en
            # el mismo ciclo del DLQ (segundos) — no es la recepción física
            # de horas/días que sí justifica el default de 30 min. Salvo que
            # el RC esté a su vez esperando la NC de una devolución (lo
            # desbloquea bodega, horas o días): entonces la misma espera del
            # RC, y no un reintento cada 2 minutos por días (validación
            # 2026-09-26).
            _rc_espera_nc = bool((_rc_vivo.get_payload() or {}).get('depende_de_nc')
                                 and not recaudo.siesa_nc_triggered)
            # Ni cada 2 min por días mientras el RC espera al banco (validación
            # 2026-09-29, P3): al verificarla, `_despertar_recibo` lo acelera.
            from app.services import verificacion_banco as _vb_dc
            _rc_espera_banco = (_vb_dc.estado(recaudo) == _vb_dc.POR_VERIFICAR
                                and _vb_dc.exige_verificacion())
            raise DependenciaPendiente(
                f'DOCUMENTO_CONTABLE_RET job={job.id}: DC espera el RC del '
                f'recaudo {recaudo.id}'
                + (', que espera la nota crédito de la devolución' if _rc_espera_nc else '')
                + (', que espera que alguien vea la transferencia en el banco'
                   if _rc_espera_banco else '')
                + '. Sigue pendiente.',
                espera_minutos=30 if (_rc_espera_nc or _rc_espera_banco) else 2,
            )

        # Lo que el POST necesita de la cartera, resuelto AHORA de la fila del
        # documento exacto (P1-2) — la misma resolución que el recibo. Sin
        # fila, espera o se declara: la retención nunca sale con la UN de
        # respaldo (las dos de QA quedaron FALLIDO 38 días por eso).
        from app.services import envio_liquidacion as _env
        if recaudo is None or recaudo.tarea is None:
            raise DatoQueFalta(
                f'DOCUMENTO_CONTABLE_RET job={job.id}: la parada '
                f'{payload.get("recaudo_id")} (o su pedido) ya no existe en el WMS. '
                f'Descártelo (administrador).')
        # El monto y la base: si los encoló «Registrar cobro» ya vienen; si fue
        # el botón masivo, se calculan acá sobre lo que el cliente se quedó.
        payload, _cruce = _env.resolver_envio(job, recaudo, connekta)

        # Argumentos ANTES del pre-flag (un dato ausente no deja la cuenta
        # marcada sin POST).
        kwargs_dc = dict(
            tercero_nit=payload['tercero_nit'],
            sucursal=payload['sucursal'],
            cuenta_puc=payload['cuenta_puc'],
            monto=float(payload['monto']),
            base_gravable=float(payload.get('base_gravable') or 0),
            tipo_docto_fe=payload['tipo_docto_fe'],
            consec_fe=payload['consec_fe'],
            co_factura=payload['co_factura'],
            cuenta_cxc=payload['cuenta_cxc'],
            unidad_negocio=payload['unidad_negocio'],
            notas=payload.get('notas', ''),
            ajuste_valor=float(payload.get('ajuste_valor') or 0),
            ajuste_razon=payload.get('ajuste_razon', ''),
        )

        # Pre-flag: cerrar crash window (misma lógica que RC), por cuenta. Lo
        # resuelto queda escrito en el job en la misma transacción.
        job.payload = json.dumps(payload, ensure_ascii=False)
        if _puc:
            recaudo.marcar_puc_enviada(_puc)
        db.session.commit()

        try:
            resultado = connekta.trigger_documento_contable(**kwargs_dc)
        except ConnektaNoEnviado:
            # Prueba positiva de que no entró (Siesa dijo que no, o el POST no
            # salió): la única revertible.
            if recaudo and _puc:
                try:
                    recaudo.desmarcar_puc(_puc)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            raise
        except Exception as _e_post:
            # Regla 3: sin respuesta clara el documento PUEDE existir. La
            # cuenta queda marcada y el job va a FALLIDO sin reintento: un NI
            # de retención duplicado se reversa a mano en Siesa.
            raise _ResultadoDesconocido(
                f'DOCUMENTO_CONTABLE_RET job={job.id}: el envío falló sin respuesta '
                f'clara de Siesa ({_e_post}). El documento PUEDE existir: verificar '
                f'en Siesa antes de reintentar.') from _e_post

        _es_ensayo = bool(resultado.get('modo_ensayo'))
        if recaudo and _puc and _es_ensayo:
            try:
                recaudo.desmarcar_puc(_puc)
                db.session.commit()
            except Exception:
                db.session.rollback()
        elif recaudo:
            _anotar_en_recaudo(job, 'ENVIADO', respuesta=resultado, recaudo=recaudo)
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
            # Espera corta: 174646 corre en el mismo ciclo del DLQ, no es
            # una espera de horas como la recepción física.
            raise DependenciaPendiente(
                f'{s.codigo}: la RIT todavía no tiene consecutivo — '
                f'esperar a que 174646 lo resuelva',
                espera_minutos=2,
            )

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


#: Qué documento del recaudo produce cada tipo de job. Los tres de la
#: liquidación, y ningún otro: los demás no cuelgan de un recaudo.
_DOCUMENTO_DEL_RECAUDO = {
    'NOTA_CREDITO_FACTURA': 'NC',
    'RECIBO_CAJA': 'RC',
    'DOCUMENTO_CONTABLE_RET': 'DC',
}


def _anotar_en_recaudo(job: SiesaJob, resultado: str, respuesta=None,
                       recaudo=None, consec=None):
    """Deja el desenlace del documento en el recaudo (m036fotos).

    **Anotar no puede romper lo anotado**: el documento ya salió (o ya se dio
    por perdido) y eso lo decide la cola, no esta nota. Cualquier fallo acá se
    registra y se traga — igual que `_post_completado`.
    """
    doc = _DOCUMENTO_DEL_RECAUDO.get(job.tipo)
    if not doc:
        return
    try:
        if recaudo is None:
            from app.models.recaudo_entrega import RecaudoEntrega as _RE
            rid = (job.get_payload() or {}).get('recaudo_id')
            recaudo = db.session.get(_RE, rid) if rid else None
        if recaudo is None:
            return
        recaudo.anotar_documento_siesa(
            doc, resultado, respuesta=respuesta, consec=consec,
            cuenta_puc=(job.get_payload() or {}).get('cuenta_puc') if doc == 'DC' else None)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning('[DLQ] job %s (%s): no se pudo anotar %s en el recaudo: %s',
                       job.id, job.tipo, resultado, e)


def _post_completado(job: SiesaJob):
    """Actualiza la entidad referenciada al completarse el job."""
    if not job.referencia_tipo or not job.referencia_id:
        return
    if job.referencia_tipo == 'TareaReposicion':
        # RAMA MUERTA (documentada 2026-09-09, no eliminada): reposición
        # RESERVA→PICKING es 100% WMS desde el commit 9447464 y ya no crea
        # ningún SiesaJob con este referencia_tipo (confirmado por grep en
        # todo el repo) — este bloque nunca se ejecuta hoy. Se deja sin
        # borrar porque no hace daño y documenta de dónde salían
        # `siesa_job_id`/`siesa_enviado` en `TareaReposicion`, por si algún
        # día se retoma ese flujo.
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
    """Todos los FALLIDO, para ACTUAR sobre ellos (reintentar, descartar). Para
    CONTAR cuántos documentos siguen trabados: `fallidos_vigentes`."""
    return SiesaJob.query.filter_by(estado=EstadoSiesaJob.FALLIDO).order_by(SiesaJob.fecha_creacion.desc()).all()


#: Un FALLIDO de hace más de esto se muestra con su edad y no pone CRÍTICO: el
#: rojo es para lo que falló esta semana, que todavía tiene arreglo barato.
DIAS_FALLIDO_RECIENTE = 7

#: Jobs que no son documentos de Siesa: no cuentan como «documento trabado».
#: Es la misma lista que la ventana deja pasar de noche: una, no dos.
from app.services.ventana_siesa import TIPOS_SIN_SIESA as TIPOS_SIN_DOCUMENTO  # noqa: E402


def momento_del_fallo(job):
    """Cuándo falló, lo mejor que se sabe: el último intento
    (`fecha_procesando`) o, si nunca entró a PROCESANDO, su creación.
    `siesa_jobs` no guarda «falló el…» — se declara en `fallidos_vigentes`."""
    return job.fecha_procesando or job.fecha_creacion


def fallidos_vigentes(desde=None, hasta=None, *, tipos=None,
                      excluir_tipos=TIPOS_SIN_DOCUMENTO, referencia_tipo=None,
                      referencia_ids=None, ahora=None) -> dict:
    """Los jobs FALLIDO que **siguen** trabados. **La única** que los cuenta.

    Un FALLIDO deja de estar trabado cuando:

    · **lo superó un COMPLETADO posterior** del mismo tipo y referencia (un
      reintento que se encoló como job nuevo y entró): el documento existe;
    · **la reconciliación lo resolvió** — `ReconciliacionService` lo cierra
      como COMPLETADO con `resultado.reconciliado` al encontrar la factura en
      Siesa, así que ya no llega acá como FALLIDO.

    `desde`/`hasta` (UTC naive) acotan por **creación** del job (la cohorte):
    no hay columna «falló el…». Lo que queda se separa en **recientes**
    (último intento dentro de `DIAS_FALLIDO_RECIENTE`) y **viejos**, con su
    edad en días: solo los recientes ponen CRÍTICO en la salud.

    Un FALLIDO sin referencia no puede estar superado: sigue contando
    (`sin_referencia` lo dice). Devuelve `{'jobs', 'recientes', 'viejos',
    'superados', 'sin_referencia', 'por_tipo', 'ventana_reciente_dias',
    'edad_de'}`; `jobs` = recientes + viejos, más nuevos primero.

    Trinquete: `tests/test_analitica_solo_lo_actual.py` — ningún otro sitio
    consulta `estado == FALLIDO` para contar.
    """
    ahora = ahora or datetime.utcnow()
    q = SiesaJob.query.filter(SiesaJob.estado == EstadoSiesaJob.FALLIDO)
    if tipos:
        q = q.filter(SiesaJob.tipo.in_(list(tipos)))
    if excluir_tipos:
        q = q.filter(SiesaJob.tipo.notin_(list(excluir_tipos)))
    if referencia_tipo is not None:
        q = q.filter(SiesaJob.referencia_tipo == referencia_tipo)
    if referencia_ids is not None:
        ids = list(referencia_ids)
        if not ids:
            q = q.filter(db.false())
        else:
            q = q.filter(SiesaJob.referencia_id.in_(ids))
    if desde is not None:
        q = q.filter(SiesaJob.fecha_creacion >= desde)
    if hasta is not None:
        q = q.filter(SiesaJob.fecha_creacion < hasta)
    fallidos = q.order_by(SiesaJob.fecha_creacion.desc(), SiesaJob.id.desc()).all()

    # El COMPLETADO más reciente (por id: la secuencia dice qué se escribió
    # después) de cada tipo + referencia de los fallidos.
    claves = {(j.tipo, j.referencia_tipo, j.referencia_id)
              for j in fallidos if j.referencia_id is not None}
    ultimo_ok = {}
    if claves:
        for tipo, rtipo, rid, jid in (
                db.session.query(SiesaJob.tipo, SiesaJob.referencia_tipo,
                                 SiesaJob.referencia_id, SiesaJob.id)
                .filter(SiesaJob.estado == EstadoSiesaJob.COMPLETADO,
                        SiesaJob.tipo.in_(sorted({c[0] for c in claves})),
                        SiesaJob.referencia_id.in_(sorted({c[2] for c in claves})))
                .all()):
            k = (tipo, rtipo, rid)
            if k in claves and jid > ultimo_ok.get(k, 0):
                ultimo_ok[k] = jid

    frontera = ahora - timedelta(days=DIAS_FALLIDO_RECIENTE)
    recientes, viejos, superados, sin_ref = [], [], 0, 0
    por_tipo = {}
    for j in fallidos:
        if j.referencia_id is None:
            sin_ref += 1
        elif ultimo_ok.get((j.tipo, j.referencia_tipo, j.referencia_id), 0) > j.id:
            superados += 1
            continue
        cuando = momento_del_fallo(j)
        reciente = cuando is None or cuando >= frontera   # sin fecha: reciente (Regla 0)
        (recientes if reciente else viejos).append(j)
        t = por_tipo.setdefault(j.tipo, {'tipo': j.tipo, 'n': 0, 'recientes': 0,
                                         'viejos': 0, 'desde_utc': None,
                                         'edad_dias_max': None})
        t['n'] += 1
        t['recientes' if reciente else 'viejos'] += 1
        if cuando is not None:
            if t['desde_utc'] is None or cuando.isoformat() < t['desde_utc']:
                t['desde_utc'] = cuando.isoformat()
            edad = (ahora - cuando).days
            t['edad_dias_max'] = max(edad, t['edad_dias_max'] or 0)
    return {
        'jobs': recientes + viejos,
        'recientes': recientes,
        'viejos': viejos,
        'superados': superados,
        'sin_referencia': sin_ref,
        'por_tipo': sorted(por_tipo.values(), key=lambda x: (-x['recientes'], -x['n'], x['tipo'])),
        'ventana_reciente_dias': DIAS_FALLIDO_RECIENTE,
        'edad_de': ('último intento (fecha_procesando) o, si nunca se procesó, la '
                    'creación: siesa_jobs no guarda cuándo falló'),
    }


def cerrar_despachos_resueltos(tarea, resultado: dict, incluir_pendientes: bool = False) -> int:
    """Los DESPACHO_F470 de una caja que **ya tiene su documento** dejan de
    contar como trabados: FALLIDO (y, si lo resolvió una persona,
    PENDIENTE/REINTENTANDO) → COMPLETADO con `resultado` y el error que
    tenían. **La única que lo hace** (la reconciliación y la emisión —DLQ o
    carril manual— la llaman). No toca PROCESANDO: ése es el que está
    emitiendo. Sin commit.

    Hasta el 2026-09-26 solo la reconciliación cerraba el FALLIDO: tras un
    «Facturar RM manual» exitoso el job seguía en `fallidos_vigentes`
    (Salud CRÍTICO, «Documentos trabados») hasta que alguien le daba
    «Reintentar» a un documento que ya existía (H6)."""
    estados = [EstadoSiesaJob.FALLIDO]
    if incluir_pendientes:
        estados += [EstadoSiesaJob.PENDIENTE, EstadoSiesaJob.REINTENTANDO]
    jobs = SiesaJob.query.filter(
        SiesaJob.tipo == 'DESPACHO_F470',
        SiesaJob.referencia_tipo == 'TareaPacking',
        SiesaJob.referencia_id == tarea.id,
        SiesaJob.estado.in_(estados),
    ).all()
    for job in jobs:
        job.marcar_completado({**resultado, 'estado_que_tenia': job.estado,
                               'error_que_tenia': (job.error_ultimo or '')[:500]})
        job.proximo_intento = None
    return len(jobs)


def _remision_sin_factura(job):
    """La caja de un DESPACHO_F470 con remisión (o un 142945 sin confirmar)
    y sin factura confirmada — o `None`. Descartar ese job deja mercancía
    remisionada (descargada del inventario de Siesa) sin factura, fuera de
    todo contador."""
    if job.tipo != 'DESPACHO_F470' or job.referencia_tipo != 'TareaPacking':
        return None
    from app.models.packing import TareaPacking
    from app.services.documento_fiscal import fe_confirmada
    tarea = db.session.get(TareaPacking, job.referencia_id) if job.referencia_id else None
    if tarea is None or fe_confirmada(tarea):
        return None
    if tarea.rm_consec or tarea.rm_enviada_at:
        return tarea
    return None


def descartar_job(job_id: int, *, usuario_id: int, motivo: str, origen: str = None,
                  reconoce_remision_sin_factura: bool = False) -> SiesaJob:
    """Un humano decide que un job FALLIDO no se intenta más → DESCARTADO.

    **Regla 3: descartar NO reenvía nada.** No toca el documento ni las
    banderas de su referencia: si el POST pudo haber entrado a Siesa, sigue
    pudiendo haber entrado — descartar solo dice «el WMS deja de contarlo como
    trabado». Motivo obligatorio; queda en la bitácora (DESCARTAR) con el
    error que se deja de mirar. No hace commit.

    **Un DESPACHO_F470 con remisión y sin factura no se descarta** salvo que
    quien descarta lo reconozca explícitamente (`reconoce_remision_sin_factura`,
    queda en la bitácora): es la única huella de una mercancía remisionada sin
    factura, y descartarlo la saca de todo contador. La salida normal es
    «Facturar remisión».
    """
    from app.services.bitacora import foto, motivo_obligatorio, registrar_accion
    motivo = motivo_obligatorio(motivo, 'descartar un envío a Siesa')
    job = db.session.get(SiesaJob, job_id)
    if job is None:
        raise LookupError(f'Job {job_id} no encontrado')
    if job.estado != EstadoSiesaJob.FALLIDO:
        raise ValueError(f'Solo se descarta un job FALLIDO — el {job_id} está {job.estado}')
    tarea_rm = _remision_sin_factura(job)
    if tarea_rm is not None and not reconoce_remision_sin_factura:
        rm = (f'{tarea_rm.rm_tipo or "RM"}-{tarea_rm.rm_consec}' if tarea_rm.rm_consec
              else 'una remisión enviada sin confirmar')
        raise ValueError(
            f'La caja {tarea_rm.codigo} ({tarea_rm.numero_pedido_siesa}) tiene {rm} y no '
            f'tiene factura: descartar este envío dejaría mercancía remisionada sin '
            f'factura fuera de todo contador. Complete la factura con «Facturar '
            f'remisión»; si de verdad hay que descartarlo, confírmelo explícitamente.')
    registrar_accion(
        'DESCARTAR', 'SiesaJob', job.id,
        entidad_codigo=f'{job.tipo}#{job.id}',
        usuario_id=usuario_id, motivo=motivo, origen=origen,
        antes=foto(job, ['estado', 'intentos', 'error_ultimo',
                         'referencia_tipo', 'referencia_id']),
        despues={'estado': EstadoSiesaJob.DESCARTADO,
                 **({'remision_sin_factura_reconocida': True} if tarea_rm is not None else {})},
    )
    job.estado = EstadoSiesaJob.DESCARTADO
    return job


def descartar_job_retenido(job: SiesaJob, *, usuario_id: int, motivo: str) -> SiesaJob:
    """El DESPACHO_F470 que espera a cartera deja de esperar: su caja se
    canceló. **Solo PENDIENTE** (no se ejecuta ahora) y sin documento en Siesa
    —lo garantiza `PackingService._retencion_que_frena`, el único llamador—.
    Bitácora DESCARTAR. No hace commit."""
    from app.services.bitacora import foto, motivo_obligatorio, registrar_accion
    motivo = motivo_obligatorio(motivo, 'descartar un envío a Siesa')
    if job.estado != EstadoSiesaJob.PENDIENTE:
        raise ValueError(f'El envío {job.id} está {job.estado}: no se descarta.')
    registrar_accion(
        'DESCARTAR', 'SiesaJob', job.id,
        entidad_codigo=f'{job.tipo}#{job.id}',
        usuario_id=usuario_id, motivo=motivo,
        antes=foto(job, ['estado', 'intentos', 'error_ultimo', 'proximo_intento',
                         'referencia_tipo', 'referencia_id']),
        despues={'estado': EstadoSiesaJob.DESCARTADO, 'retenido_por_cartera': True},
    )
    job.estado = EstadoSiesaJob.DESCARTADO
    job.proximo_intento = None
    return job


def reencolar_job_fallido(job: SiesaJob, *, usuario_id: int = None,
                          motivo: str = None, origen: str = None,
                          payload: str = None) -> SiesaJob:
    """Un job FALLIDO vuelve a PENDIENTE — **el fallo que se borra queda escrito**.

    Reintentar pone `intentos` en 0 y `error_ultimo` en None: sin esto, un job
    que falló tres veces con «el documento de cruce no existe» y después pasó
    se veía igual que uno que pasó a la primera. El historial del fallo va a
    la bitácora (REINTENTAR) antes de limpiarlo, en la misma transacción.

    Única función que limpia `error_ultimo`/`intentos` de un job — el
    trinquete de `tests/test_bitacora_acciones.py` lo exige (con una excepción
    declarada en conteo). No hace commit.
    """
    from app.services.bitacora import registrar_accion, foto
    registrar_accion(
        'REINTENTAR', 'SiesaJob', job.id,
        entidad_codigo=f'{job.tipo}#{job.id}',
        usuario_id=usuario_id, motivo=motivo, origen=origen,
        antes=foto(job, ['estado', 'intentos', 'error_ultimo', 'proximo_intento',
                         'referencia_tipo', 'referencia_id']),
        despues={'estado': EstadoSiesaJob.PENDIENTE, 'intentos': 0},
    )
    job.estado = EstadoSiesaJob.PENDIENTE
    job.intentos = 0
    job.proximo_intento = None
    job.error_ultimo = None
    if payload is not None:
        job.payload = payload
    return job


def reintentar_job(job_id: int, usuario_id: int = None, motivo: str = None) -> dict:
    """Admin fuerza un reintento de un job FALLIDO. Un envío que quedó sin
    verificar no se reintenta así: se resuelve (`resolver_preflag_sin_verificar`)."""
    job = SiesaJob.query.get(job_id)
    if not job:
        raise ValueError(f'Job {job_id} no encontrado')
    if job.estado != EstadoSiesaJob.FALLIDO:
        raise ValueError(f'Job {job_id} no está en estado FALLIDO — está {job.estado}')
    exigir_no_sin_verificar(job)
    reencolar_job_fallido(job, usuario_id=usuario_id, motivo=motivo)
    db.session.commit()
    return job.to_dict()


# ═════════════════════════════════════════════════════════════════════════════
# Envíos con pre-flag que quedaron sin verificar — la salida humana
# ═════════════════════════════════════════════════════════════════════════════
#
# ENTRADA_OC, TRASLADO_AVERIAS y AJUSTE_CONTEO ponen su pre-flag antes del POST
# (`_ejecutar_con_preflag`, y el ancla del movimiento de avería). Ante un «no
# sé» la bandera queda y el job va a FALLIDO sin reintento (tanda 2). Con la
# bandera puesta, «Reintentar» leería «ya enviado» y cerraría el job sin
# preguntarle a nadie si el documento entró: por eso reintentar lo rechaza y la
# salida es una persona que busca en Siesa y dice qué vio (como el recibo de
# caja: `LiquidacionService.resolver_recibo_sin_verificar`).

TIPOS_CON_PREFLAG = ('ENTRADA_OC', 'TRASLADO_AVERIAS', 'AJUSTE_CONTEO',
                     # Validación de la plata (2026-09-26): la retención y las
                     # dos NC tienen la misma regla que el RC — bandera sola
                     # no es hecho (el RC la resuelve en Liquidación con
                     # `resolver_recibo_sin_verificar`).
                     'DOCUMENTO_CONTABLE_RET', 'NOTA_CREDITO_FACTURA',
                     'NOTA_CREDITO_DEVOLUCION_CLIENTE')


class _Preflag:
    """El pre-flag de un job: dónde está, si quedó **sin desenlace** (puesto y
    sin constancia de que entró: «no sé»), cómo se baja y cómo se confirma.
    Uno por tipo; la retención es por cuenta PUC.

    `estado()` es la misma pregunta para todos los tipos (integración v2,
    2026-09-27) y contesta LIBRE · ENVIADO · SIN_DESENLACE: los tres de
    inventario con `estado_del_preflag` —bandera sin respuesta—; las NC y la
    retención, con la señal positiva de su política de cobro. Una bandera
    puesta con su respuesta guardada ya no es «sin verificar»: entró. La
    usan la guarda del job (`_exigir_envio_con_desenlace`), el panel y
    reintentar (`preflag_sin_verificar`)."""

    def __init__(self, obj, estado, bajar, confirmar, foto):
        self.obj, self._estado, self._bajar, self._confirmar, self._foto = (
            obj, estado, bajar, confirmar, foto)

    def estado(self) -> str:
        return self._estado()

    def sin_desenlace(self) -> bool:
        return self.estado() == PREFLAG_SIN_DESENLACE

    def bajar(self):
        self._bajar()

    def confirmar(self, motivo: str = None):
        """Deja escrito el desenlace «sí entró», dicho por una persona."""
        self._confirmar(motivo)

    def foto(self) -> dict:
        return self._foto()


def _preflag_inventario(obj, attr, libre, job_id):
    """ENTRADA_OC, AJUSTE_CONTEO y TRASLADO_AVERIAS (tarea o movimiento ancla):
    el «no sé» lo decide `estado_del_preflag` —la política de inventario—, no
    la bandera sola. Confirmar escribe el desenlace (`siesa_response`, o el
    movimiento a ENVIADO) para que la guarda del job lo cierre sin POST."""
    def _confirmar(motivo):
        if attr == 'siesa_sync':
            obj.siesa_sync = 'ENVIADO'
        else:
            obj.siesa_response = json.dumps({'resuelto_a_mano': True, 'job_id': job_id,
                                             'motivo': motivo}, ensure_ascii=False)

    def _foto():
        f = {attr: getattr(obj, attr, None)}
        if attr != 'siesa_sync':
            f['siesa_response'] = bool(getattr(obj, 'siesa_response', None))
        return f
    return _Preflag(obj, lambda: estado_del_preflag(obj),
                    lambda: setattr(obj, attr, libre), _confirmar, _foto)


def _estado_por_bandera(puesta: bool, llego: bool) -> str:
    """Bandera y señal positiva → uno de los tres desenlaces."""
    if not puesta:
        return PREFLAG_LIBRE
    return PREFLAG_ENVIADO if llego else PREFLAG_SIN_DESENLACE


def _preflag_de(job):
    """El `_Preflag` de un job, o `None` si el job no tiene (o su objeto ya no
    existe)."""
    if job is None or job.tipo not in TIPOS_CON_PREFLAG:
        return None
    p = job.get_payload() or {}
    if job.tipo == 'ENTRADA_OC':
        from app.models.recepcion import RecepcionMercancia
        obj = db.session.get(RecepcionMercancia, p.get('recepcion_id')) if p.get('recepcion_id') else None
        return _preflag_inventario(obj, 'siesa_triggered', False, job.id) if obj is not None else None
    if job.tipo == 'TRASLADO_AVERIAS':
        if p.get('movimiento_id'):
            from app.models.inventario import MovimientoInventario
            obj = db.session.get(MovimientoInventario, p['movimiento_id'])
            if obj is None:
                return None
            return _preflag_inventario(obj, 'siesa_sync', 'PENDIENTE', job.id)
        from app.models.devolucion import TareaDevolucion
        obj = db.session.get(TareaDevolucion, p.get('tarea_id')) if p.get('tarea_id') else None
        return _preflag_inventario(obj, 'siesa_triggered', False, job.id) if obj is not None else None
    if job.tipo == 'AJUSTE_CONTEO':
        from app.models.conteo import SesionConteo
        obj = db.session.get(SesionConteo, p.get('sesion_id')) if p.get('sesion_id') else None
        return _preflag_inventario(obj, 'siesa_triggered', False, job.id) if obj is not None else None
    if job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE':
        from app.models.devolucion_cliente import DevolucionCliente
        obj = db.session.get(DevolucionCliente, p.get('devolucion_id')) if p.get('devolucion_id') else None
        if obj is None:
            return None

        def _bajar_dev():
            obj.siesa_nc_triggered = False
            obj.siesa_nc_triggered_at = None

        def _confirmar_dev(_motivo=None):
            obj.siesa_nc_response = obj.siesa_nc_response or json.dumps(
                {'resuelto_a_mano': True}, ensure_ascii=False)
        # Sin desenlace = la bandera sin la respuesta de Siesa.
        return _Preflag(obj, lambda: _estado_por_bandera(bool(obj.siesa_nc_triggered),
                                                         bool(obj.siesa_nc_response)),
                        _bajar_dev, _confirmar_dev,
                        lambda: {'siesa_nc_triggered': obj.siesa_nc_triggered,
                                 'siesa_nc_response': bool(obj.siesa_nc_response)})
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services import politica_cobro as _pc
    rec = db.session.get(RecaudoEntrega, p.get('recaudo_id')) if p.get('recaudo_id') else None
    if rec is None:
        return None
    if job.tipo == 'NOTA_CREDITO_FACTURA':
        def _bajar_nc():
            rec.siesa_nc_triggered = False
            rec.anotar_documento_siesa('NC', 'FALLIDO')
        return _Preflag(rec, lambda: _estado_por_bandera(bool(rec.siesa_nc_triggered),
                                                         bool(_pc.nc_llego(rec))),
                        _bajar_nc, lambda _motivo=None: rec.anotar_documento_siesa('NC', 'ENVIADO'),
                        lambda: {'siesa_nc_triggered': rec.siesa_nc_triggered,
                                 'siesa_nc_resultado': rec.siesa_nc_resultado})
    puc = p.get('cuenta_puc')
    if not puc:
        return None

    def _bajar_dc():
        rec.desmarcar_puc(puc)
        rec.anotar_documento_siesa('DC', 'FALLIDO', cuenta_puc=puc)
    return _Preflag(rec, lambda: _estado_por_bandera(puc in rec.pucs_enviadas(),
                                                     bool(_pc.dc_llego(rec, puc))),
                    _bajar_dc, lambda _motivo=None: rec.anotar_documento_siesa('DC', 'ENVIADO', cuenta_puc=puc),
                    lambda: {'cuenta_puc': puc, 'enviada': puc in rec.pucs_enviadas(),
                             'resultado': _pc.resultado_dc(rec, puc)})


def estado_del_envio(job) -> str:
    """LIBRE · ENVIADO · SIN_DESENLACE del envío de un job con pre-flag
    (`_preflag_de`); LIBRE si el tipo no tiene o su objeto ya no existe."""
    pf = _preflag_de(job)
    return pf.estado() if pf is not None else PREFLAG_LIBRE


def preflag_sin_verificar(job) -> bool:
    """¿Este job FALLIDO dejó su pre-flag puesto sin desenlace? **La única**
    que lo contesta (panel, reintentar, reintentos en lote)."""
    if job is None or job.estado != EstadoSiesaJob.FALLIDO:
        return False
    pf = _preflag_de(job)
    return pf is not None and pf.sin_desenlace()


def exigir_no_sin_verificar(job) -> None:
    """Levanta `ValueError` si el job quedó sin verificar: reintentarlo
    cerraría el envío como hecho sin que nadie haya mirado Siesa."""
    if preflag_sin_verificar(job):
        raise ValueError(
            f'El envío {job.id} ({job.tipo}) quedó sin verificar: pudo haber entrado a '
            f'Siesa. No se reintenta así — búsquelo en Siesa y diga si está o no '
            f'(Siesa → Recuperación, «¿Está en Siesa?»).')


def resolver_preflag_sin_verificar(job_id: int, *, usuario_id: int, entro: bool,
                                   motivo: str, origen: str = None) -> SiesaJob:
    """Una persona dice cómo terminó un envío que quedó sin verificar.

    · `entro=True`: lo encontró en Siesa. El pre-flag queda (o el movimiento
      pasa a ENVIADO) y el job vuelve a la cola: su guarda lo cierra sin
      POST (el conteo en AJUSTANDO termina de ajustar el WMS ahí mismo).
    · `entro=False`: no está. Se baja el pre-flag y el job vuelve a la cola:
      se envía de nuevo.

    Motivo obligatorio; FORZAR en la bitácora. **No hace POST.** No hace commit.
    """
    from app.services.bitacora import (FORZADO_PREFLAG_RESUELTO_A_MANO, foto,
                                       motivo_obligatorio, registrar_accion)
    texto = motivo_obligatorio(motivo, 'resolver a mano un envío a Siesa sin verificar')
    job = db.session.get(SiesaJob, job_id)
    if job is None:
        raise LookupError(f'Job {job_id} no encontrado')
    if not preflag_sin_verificar(job):
        raise ValueError(f'El envío {job_id} no está pendiente de verificar: no hay nada '
                         f'que resolver.')
    pf = _preflag_de(job)
    antes = pf.foto()
    if entro:
        pf.confirmar(motivo=texto)
    else:
        pf.bajar()
    registrar_accion(
        'FORZAR', pf.obj, usuario_id=usuario_id, motivo=texto, origen=origen,
        entidad_codigo=f'{job.tipo}#{job.id}',
        antes=antes,
        despues={'forzado': FORZADO_PREFLAG_RESUELTO_A_MANO, 'job_id': job.id,
                 'tipo': job.tipo, 'entro': bool(entro), **pf.foto()})
    reencolar_job_fallido(job, usuario_id=usuario_id, origen=origen,
                          motivo=f'Resuelto a mano: {"está" if entro else "no está"} en '
                                 f'Siesa — {texto}')
    return job


def disparar_dlq_inmediato(app=None):
    """
    Lanza procesar_jobs_pendientes() en un hilo daemon para procesar jobs recién encolados
    sin bloquear el worker de Gunicorn. El advisory lock (`advisory_lock(LOCK_DLQ)`) garantiza que
    a lo sumo un hilo corre la DLQ simultáneamente — si el scheduler ya está corriendo, el hilo
    sale en <1ms sin hacer nada.

    Uso: llamar inmediatamente después de commit() que encola un SiesaJob PENDIENTE.
    El hilo procesa el job en segundos en vez de esperar el cron de 5 minutos.
    """
    import threading
    from flask import current_app

    _app = app or current_app._get_current_object()
    # Candado anti-producción local (tanda 2 · E): el hilo automático de la
    # DLQ tampoco arranca fuera de Railway contra una base de Railway.
    if _app.config.get('CANDADO_PRODUCCION_LOCAL'):
        logger.warning('[DLQ] disparar_dlq_inmediato omitido: %s',
                       _app.config['CANDADO_PRODUCCION_LOCAL'])
        return

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
    from app.services.cron_latido import con_latido  # P1-11
    scheduler.add_job(
        func=con_latido('dlq_siesa_jobs', procesar_jobs_pendientes),
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
