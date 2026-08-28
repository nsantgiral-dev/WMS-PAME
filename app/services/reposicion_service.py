"""
Motor de Reabastecimiento RESERVA → PICKING.

Responsabilidades:
  1. verificar_stock_picking()  — corre tras cada picking confirmado, tras cada
                                   reposición confirmada, por el botón manual
                                   "Verificar stock ahora", y cada 30 min por
                                   init_scheduler() (§5 más abajo)
  2. asignar_tarea()            — el abastecedor pide trabajo, el sistema asigna
  3. confirmar_reposicion()     — Abastecedor escanea LPN + confirma → "rompe la paca"
                                   → LPN CONSUMIDO + stock PICKING actualizado
                                   → job Siesa conector 173076 (tránsito entre ubicaciones)
  4. configurar_umbral()        — única función que valida y escribe
                                   stock_minimo/stock_maximo/secuencia_ruteo de
                                   una ubicación (la usan esta ruta y Layout)

Reglas:
  - Solo ubicaciones tipo_zona='PICKING' disparan alertas (RESERVA y GENERAL nunca)
  - Solo LPNs ACTIVO en la ubicacion_reserva del mismo almacen son candidatos
  - Si hay TareaReposicion PENDIENTE/EN_PROCESO para esa (ubicacion_picking, producto)
    no se crea duplicada
"""

import logging
from datetime import datetime
from app.extensions import db
from app.models.lpn import EstadoLPN
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.lpn import LPN
from app.models.tarea_reposicion import TareaReposicion, EstadoReposicion
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 0. Configuración de umbrales — única función que valida y escribe
#    stock_minimo/stock_maximo/secuencia_ruteo de una ubicación.
# ──────────────────────────────────────────────────────────────────────────────

_NOTSET = object()  # distingue "no vino en la llamada" de "vino None (limpiar el campo)"


def configurar_umbral(ubicacion_id: int, stock_minimo=_NOTSET, stock_maximo=_NOTSET,
                       secuencia_ruteo=_NOTSET, capacidad_referencia: int = None):
    """
    Configura los umbrales de reabastecimiento (stock_minimo/stock_maximo) que
    verificar_stock_picking() lee para decidir cuándo generar una TareaReposicion.

    Es la única función que valida y escribe estos campos — antes la ruta
    (`PATCH /ubicacion/<id>/limites`) los escribía inline sin validar zona ni
    signo, y Layout (al asignar un SKU) los habría escrito por su cuenta con
    otra copia del mismo criterio. Layout es dueño de "cuánto cabe en el
    hueco" (capacidad_maxima); Reposición es dueño de "cuándo avisar" — esta
    función es el único puente entre los dos, para que Layout no tenga que
    conocer las reglas de Reposición.

    capacidad_referencia (opcional): si no se manda stock_maximo explícito y
    la ubicación todavía no tiene uno propio, se usa como techo por defecto
    — "cuánto cabe" es un límite razonable para "hasta cuánto reponer" cuando
    nadie configuró explícitamente el segundo. Nunca pisa un stock_maximo ya
    configurado a mano.

    No hace commit — el caller decide la transacción (Layout la agrupa con el
    movimiento de inventario; la ruta de Reposición commitea sola).
    """
    ubicacion = Ubicacion.query.get(ubicacion_id)
    if not ubicacion:
        raise ValueError(f'Ubicación {ubicacion_id} no encontrada')

    quiere_minimo = stock_minimo is not _NOTSET and stock_minimo is not None
    quiere_maximo = stock_maximo is not _NOTSET and stock_maximo is not None

    if (quiere_minimo or quiere_maximo) and ubicacion.tipo_zona not in Ubicacion.ZONAS_SLOT_UNICO:
        raise ValueError(
            f'stock_minimo/stock_maximo solo aplican a huecos '
            f'{"/".join(Ubicacion.ZONAS_SLOT_UNICO)} — en RESERVA/AVERIAS el '
            f'hueco puede compartirse entre varios SKUs y un umbral por hueco '
            f'no representa nada'
        )
    if quiere_minimo and stock_minimo < 0:
        raise ValueError('stock_minimo no puede ser negativo')
    if quiere_maximo and stock_maximo < 0:
        raise ValueError('stock_maximo no puede ser negativo')

    efectivo_minimo = stock_minimo if stock_minimo is not _NOTSET else ubicacion.stock_minimo
    efectivo_maximo = stock_maximo if stock_maximo is not _NOTSET else ubicacion.stock_maximo
    if efectivo_minimo is not None and efectivo_maximo is not None and efectivo_minimo > efectivo_maximo:
        raise ValueError(
            f'stock_minimo ({efectivo_minimo}) no puede ser mayor a stock_maximo ({efectivo_maximo})'
        )

    if stock_minimo is not _NOTSET:
        ubicacion.stock_minimo = stock_minimo
    if stock_maximo is not _NOTSET:
        ubicacion.stock_maximo = stock_maximo
    elif capacidad_referencia is not None and ubicacion.stock_maximo is None:
        ubicacion.stock_maximo = capacidad_referencia
    if secuencia_ruteo is not _NOTSET:
        ubicacion.secuencia_ruteo = secuencia_ruteo

    return ubicacion


# ──────────────────────────────────────────────────────────────────────────────
# 1. Verificación de stock y generación de tareas
# ──────────────────────────────────────────────────────────────────────────────

def verificar_stock_picking(almacen_id: int = None):
    """
    Escanea todas las ubicaciones PICKING con stock_minimo definido.
    Para cada una donde stock_actual < stock_minimo, crea una TareaReposicion
    si no existe ya una activa.

    Llamar: después de confirmar picking + en el scheduler nocturno.
    """
    q = UbicacionProducto.query.join(
        Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id
    ).filter(
        Ubicacion.tipo_zona == 'PICKING',
        Ubicacion.stock_minimo.isnot(None),
        Ubicacion.activo == True,
    )
    if almacen_id:
        q = q.filter(Ubicacion.almacen_id == almacen_id)

    # with_for_update(skip_locked=True): evita crear tareas duplicadas cuando
    # dos workers ejecutan verificar_stock_picking concurrentemente para el mismo inv.
    try:
        registros = q.with_for_update(skip_locked=True).all()
    except Exception:
        registros = q.all()
    generadas = 0

    for inv in registros:
        ub_picking = inv.ubicacion
        stock_actual = inv.cantidad - (inv.reservado or 0)

        if stock_actual >= ub_picking.stock_minimo:
            continue  # bien — no hace falta reponer

        # ¿Ya existe tarea activa para este (ubicacion_picking, producto)?
        ya_existe = TareaReposicion.query.filter(
            TareaReposicion.ubicacion_picking_id == ub_picking.id,
            TareaReposicion.producto_id == inv.producto_id,
            TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO']),
        ).first()
        if ya_existe:
            continue

        # ¿Hay LPN disponible en alguna ubicacion RESERVA del mismo almacén?
        lpn_candidato = LPN.query.join(
            Ubicacion, Ubicacion.id == LPN.ubicacion_id
        ).filter(
            LPN.producto_id == inv.producto_id,
            LPN.almacen_id == ub_picking.almacen_id,
            LPN.estado == EstadoLPN.ACTIVO,
            Ubicacion.tipo_zona == 'RESERVA',
        ).order_by(LPN.fecha_creacion.asc()).first()  # FIFO

        if not lpn_candidato:
            logger.warning(
                f'[REPOSICION] Sin LPN disponible para producto {inv.producto_id} '
                f'en RESERVA almacén {ub_picking.almacen_id} — picking {ub_picking.codigo} bajo mínimo'
            )
            continue

        # Cuántas unidades reponer: llenar hasta stock_maximo si está definido
        stock_maximo = ub_picking.stock_maximo or (ub_picking.stock_minimo * 3)
        cantidad_a_reponer = stock_maximo - stock_actual
        cantidad_a_reponer = min(cantidad_a_reponer, lpn_candidato.cantidad_actual)

        tarea = TareaReposicion(
            codigo=TareaReposicion.generar_codigo(),
            producto_id=inv.producto_id,
            almacen_id=ub_picking.almacen_id,
            cantidad_unidades=cantidad_a_reponer,
            ubicacion_reserva_id=lpn_candidato.ubicacion_id,
            ubicacion_picking_id=ub_picking.id,
            lpn_id=lpn_candidato.id,
            estado='PENDIENTE',
        )
        db.session.add(tarea)
        generadas += 1
        logger.info(
            f'[REPOSICION] TareaReposicion {tarea.codigo} generada — '
            f'producto {inv.producto_id} | {ub_picking.codigo} | LPN {lpn_candidato.codigo} | '
            f'{cantidad_a_reponer} UNDs'
        )

    if generadas:
        db.session.commit()

    return generadas


# ──────────────────────────────────────────────────────────────────────────────
# 2. Asignación al abastecedor
# ──────────────────────────────────────────────────────────────────────────────

def get_tarea_abastecedor(abastecedor_id: int):
    """
    Devuelve la tarea activa del abastecedor, o asigna la siguiente PENDIENTE.
    """
    # ¿Ya tiene tarea en proceso?
    activa = TareaReposicion.query.filter_by(
        abastecedor_id=abastecedor_id,
        estado='EN_PROCESO',
    ).first()
    if activa:
        return activa.to_dict()

    # Tomar la más antigua PENDIENTE con LPN asignado (las más urgentes primero)
    # with_for_update(skip_locked=True): si dos abastecedores piden al mismo tiempo,
    # cada uno toma una tarea diferente — sin esto ambos tomarían la misma.
    siguiente = (TareaReposicion.query
        .filter_by(estado='PENDIENTE', abastecedor_id=None)
        .order_by(TareaReposicion.fecha_creacion.asc())
        .with_for_update(skip_locked=True)
        .first())

    if not siguiente:
        return None

    siguiente.abastecedor_id = abastecedor_id
    siguiente.estado = EstadoReposicion.EN_PROCESO
    siguiente.fecha_inicio = datetime.utcnow()
    db.session.commit()

    logger.info(f'[REPOSICION] {siguiente.codigo} asignada a abastecedor {abastecedor_id}')
    return siguiente.to_dict()


def get_tareas_abastecedor(abastecedor_id: int):
    """Lista todas las tareas activas del abastecedor."""
    tareas = TareaReposicion.query.filter(
        TareaReposicion.abastecedor_id == abastecedor_id,
        TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO']),
    ).order_by(TareaReposicion.fecha_creacion.asc()).all()
    return [t.to_dict() for t in tareas]


# ──────────────────────────────────────────────────────────────────────────────
# 3. Confirmación — "Romper la paca"
# ──────────────────────────────────────────────────────────────────────────────

def confirmar_reposicion(tarea_id: int, abastecedor_id: int, lpn_codigo_escaneado: str = None):
    """
    El abastecedor confirmó la entrega del LPN en la zona PICKING.

    Flujo:
      a) Valida que el LPN escaneado coincide con el esperado
      b) LPN → CONSUMIDO
      c) Suma cantidad_actual del LPN al inventario de la ubicacion PICKING
      d) Registra MovimientoInventario tipo='REPOSICION'
      e) Dispara job async a Siesa (conector 173076 — tránsito salida entre ubicaciones)
      f) Dispara verificar_stock_picking() para detectar nueva necesidad
    """
    tarea = TareaReposicion.query.get(tarea_id)
    if not tarea:
        raise ValueError(f'Tarea reposición {tarea_id} no encontrada')
    if tarea.abastecedor_id != abastecedor_id:
        raise ValueError('Esta tarea no te pertenece')
    if tarea.estado != 'EN_PROCESO':
        raise ValueError(f'Tarea en estado {tarea.estado} — no se puede confirmar')

    lpn = tarea.lpn
    if not lpn or lpn.estado != EstadoLPN.ACTIVO:
        raise ValueError('El LPN de la tarea no está disponible')

    # Validar escaneo si se envió código
    if lpn_codigo_escaneado and lpn_codigo_escaneado.strip() != lpn.codigo:
        raise ValueError(
            f'LPN escaneado ({lpn_codigo_escaneado}) no coincide con el esperado ({lpn.codigo})'
        )

    unidades = lpn.cantidad_actual

    # a) Marcar LPN como CONSUMIDO
    lpn.consumir()

    # b) Actualizar inventario en ubicacion PICKING
    inv_picking = UbicacionProducto.query.filter_by(
        ubicacion_id=tarea.ubicacion_picking_id,
        producto_id=tarea.producto_id,
    ).with_for_update().first()

    if inv_picking:
        inv_picking.cantidad += unidades
    else:
        inv_picking = UbicacionProducto(
            ubicacion_id=tarea.ubicacion_picking_id,
            producto_id=tarea.producto_id,
            cantidad=unidades,
            reservado=0,
            bloqueado=0,
        )
        db.session.add(inv_picking)

    # c) Retirar del inventario RESERVA
    inv_reserva = UbicacionProducto.query.filter_by(
        ubicacion_id=tarea.ubicacion_reserva_id,
        producto_id=tarea.producto_id,
    ).with_for_update().first()
    if inv_reserva:
        inv_reserva.cantidad = max(0, inv_reserva.cantidad - unidades)

    # d) Movimiento inventario
    db.session.add(MovimientoInventario(
        producto_id=tarea.producto_id,
        ubicacion_id=tarea.ubicacion_picking_id,
        almacen_id=tarea.almacen_id,
        tipo='REPOSICION',
        cantidad=unidades,
        motivo=f'Reposición {tarea.codigo} — LPN {lpn.codigo} roto hacia {tarea.ubicacion_picking.codigo}',
        usuario_id=abastecedor_id,
        idempotency_key=f'REP-{tarea.id}-{lpn.id}',
    ))

    # e) Cerrar tarea
    tarea.estado = EstadoReposicion.COMPLETADA
    tarea.unidades_movidas = unidades
    tarea.fecha_completada = datetime.utcnow()

    # f) Encolar job Siesa ANTES del commit — P8: el SiesaJob debe ser atómico con el
    # cambio de estado. Si el commit falla o Railway reinicia entre dos commits separados,
    # el job queda sin crear y Siesa nunca se entera del movimiento RESERVA→PICKING.
    _encolar_siesa_job(tarea, lpn, unidades)

    # Pre-capturar datos antes del commit — expire_on_commit invalida relaciones lazy
    _ub_codigo = tarea.ubicacion_picking.codigo if tarea.ubicacion_picking else '?'
    _tarea_dict = tarea.to_dict()
    _almacen_id = tarea.almacen_id

    db.session.commit()

    # g) Re-evaluar stock (puede haber otra tarea necesaria)
    try:
        verificar_stock_picking(almacen_id=_almacen_id)
    except Exception as e:
        logger.error(f'[REPOSICION] Error re-evaluando stock post-reposición: {e}')

    return {
        'ok': True,
        'mensaje': f'Reposición completada — {unidades} UNDs de {lpn.codigo} ahora en {_ub_codigo}',
        'tarea': _tarea_dict,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 4. Job Siesa — encolar en DLQ (reintentos automáticos, alerta si falla 3 veces)
# ──────────────────────────────────────────────────────────────────────────────

def _encolar_siesa_job(tarea: TareaReposicion, lpn: LPN, unidades: int):
    """
    Encola la transferencia de ubicaciones en la Dead Letter Queue.
    El scheduler la ejecuta en los próximos 5 min.
    Si falla, reintenta con backoff exponencial (5→15→45 min).
    Tras 3 fallos: alerta roja en dashboard admin.
    """
    from app.services.siesa_job_service import encolar_transferencia_ubicaciones
    from app.models.ubicacion import Ubicacion as _Ub
    from app.models.almacen import Almacen

    ub_reserva = _Ub.query.get(tarea.ubicacion_reserva_id)
    ub_picking = _Ub.query.get(tarea.ubicacion_picking_id)
    producto = tarea.producto

    if not ub_reserva or not ub_picking or not producto:
        logger.error(f'[REPOSICION DLQ] Datos incompletos para job — no se encola')
        tarea.notas = (tarea.notas or '') + ' | SIESA: datos incompletos, job no creado'
        return

    # Validar codigo_siesa explícitamente — Siesa rechaza códigos WMS internos.
    item_codigo = getattr(producto, 'codigo_siesa', None)
    if not item_codigo:
        logger.critical(
            f'[REPOSICION DLQ] Producto {producto.id} ({producto.codigo}) sin codigo_siesa '
            f'— job NO creado. Siesa NO se enteró de este movimiento RESERVA→PICKING. '
            f'Configura codigo_siesa en el producto para activar la transferencia automática.'
        )
        tarea.notas = (tarea.notas or '') + f' | SIESA: producto {producto.codigo} sin codigo_siesa — transferencia NO enviada'
        tarea.siesa_enviado = False
        return

    # Resolver bodega dinámicamente desde el almacén de la tarea
    almacen = Almacen.query.get(tarea.almacen_id)
    bodega_siesa = almacen.bodega_siesa_id if almacen else None
    centro_op_siesa = almacen.centro_op_siesa if almacen else None

    if not bodega_siesa:
        logger.critical(
            f'[REPOSICION DLQ] Almacén {tarea.almacen_id} sin bodega_siesa_id — '
            f'job NO creado. Configura bodega_siesa_id en el almacén.'
        )
        tarea.notas = (tarea.notas or '') + f' | SIESA: almacén sin bodega_siesa_id — transferencia NO enviada'
        tarea.siesa_enviado = False
        return

    job = encolar_transferencia_ubicaciones(
        bodega_id=bodega_siesa,
        ubicacion_origen=ub_reserva.codigo,
        ubicacion_destino=ub_picking.codigo,
        referencia_item=item_codigo,
        cantidad=unidades,
        nota=f'Reposición WMS {tarea.codigo} — LPN {lpn.codigo}',
        centro_op=centro_op_siesa,
        referencia_tipo='TareaReposicion',
        referencia_id=tarea.id,
    )
    # No hacer commit aquí — el commit lo hace el caller (confirmar_reposicion) de forma atómica
    # con el cambio de estado de la tarea (P8: SiesaJob atómico con cambio de estado).
    logger.info(
        f'[REPOSICION DLQ] Job {job.id} preparado para {tarea.codigo} '
        f'— bodega={bodega_siesa} centro_op={centro_op_siesa}'
    )


def liberar_tareas_zombi(timeout_horas: int = 2):
    """
    Libera TareaReposicion EN_PROCESO que llevan más de `timeout_horas` sin
    progreso — el abastecedor escaneó mal, cerró la app o se fue de turno a
    medio camino, y la tarea quedó con abastecedor_id fijo. Sin esto,
    get_tarea_abastecedor() nunca la vuelve a ofrecer a nadie más — ni a otro
    abastecedor (solo busca abastecedor_id=None) ni al mismo, hasta que su
    cola de Pedido/Traslado se vacíe (nivel 2 de mobile_service.get_tarea_actual).

    Mismo timeout y misma forma que ConteoService.liberar_tareas_zombi() —
    no es casualidad, es el mismo problema (tarea EN_PROCESO abandonada) con
    otro modelo. lpn_id no se toca: se fijó en verificar_stock_picking() al
    crear la tarea, no al tomarla, y el LPN sigue ACTIVO — nadie lo consumió.
    """
    from datetime import timedelta
    umbral = datetime.utcnow() - timedelta(hours=timeout_horas)
    zombis = TareaReposicion.query.filter(
        TareaReposicion.estado == 'EN_PROCESO',
        TareaReposicion.fecha_inicio < umbral,
    ).all()

    liberadas = 0
    for t in zombis:
        logger.warning(
            f'[REPOSICION TIMEOUT] Tarea {t.codigo} (id={t.id}) EN_PROCESO '
            f'desde {t.fecha_inicio} — liberando (abastecedor #{t.abastecedor_id})'
        )
        t.estado = 'PENDIENTE'
        t.abastecedor_id = None
        t.fecha_inicio = None
        liberadas += 1

    if liberadas:
        db.session.commit()
        logger.info(f'[REPOSICION TIMEOUT] {liberadas} tarea(s) liberada(s)')
    return liberadas


# ──────────────────────────────────────────────────────────────────────────────
# 5. Scheduler — el barrido periódico que el módulo dice tener desde el
#    docstring del archivo, pero que nunca se registró en app/__init__.py.
#    Hasta ahora verificar_stock_picking() solo corría tras un picking
#    confirmado o al apretar "Verificar stock ahora" a mano — un hueco que
#    baja de mínimo por un ajuste de conteo, una devolución o un traslado no
#    disparaba nada hasta que alguien pickeara de ahí o entrara a revisar.
# ──────────────────────────────────────────────────────────────────────────────

def _barrido_stock_picking(app):
    from app.utils.lock import advisory_lock

    with app.app_context():
        try:
            # Lock 2016, NO 2015 — 2015 ya es de abc_service._liberar_zombis
            # (ver app/utils/lock.py). Compartir número entre dos jobs
            # DISTINTOS los vuelve mutuamente excluyentes sin que nadie lo
            # haya querido: cuando los dos caen en la misma ventana de 30 min,
            # uno de los dos se salta el ciclo en silencio — 'lock no
            # disponible' se registra igual para 'otro worker corriendo esto
            # mismo' que para 'un job completamente distinto lo tiene', y no
            # hay forma de distinguirlos desde el log.
            with advisory_lock(2016, 'reposicion_barrido') as tomado:
                if not tomado:
                    logger.info('[REPOSICION_SCHEDULER] Lock no disponible — omitiendo ejecución concurrente')
                    return
                generadas = verificar_stock_picking()
                if generadas:
                    logger.info(f'[REPOSICION_SCHEDULER] {generadas} tarea(s) de reposición generada(s)')
                liberadas = liberar_tareas_zombi()
                if liberadas:
                    logger.info(f'[REPOSICION_SCHEDULER] {liberadas} tarea(s) zombi liberada(s)')
        except Exception as e:
            logger.error(f'[REPOSICION_SCHEDULER] Error en barrido periódico: {e}')


def init_scheduler(app):
    """Cron cada 30 min — barre todas las ubicaciones PICKING con mínimo configurado
    y libera TareaReposicion zombi (EN_PROCESO >2h sin progreso).

    Complementa (no reemplaza) los disparos reactivos ya existentes tras
    picking y tras reposición: cubre el caso donde el stock bajó por otro
    camino (conteo, devolución, traslado) y nadie pickeó de ahí desde.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error('[REPOSICION_SCHEDULER] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=_barrido_stock_picking,
        trigger=IntervalTrigger(minutes=30),
        kwargs={'app': app},
        id='reposicion_barrido_stock_picking',
        name='Reposición — barrido periódico de stock PICKING bajo mínimo',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info('[REPOSICION_SCHEDULER] Scheduler iniciado — barrido cada 30 min')
    return scheduler
