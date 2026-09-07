"""
Motor de Reabastecimiento RESERVA → PICKING.

Responsabilidades:
  1. verificar_stock_picking()  — corre tras cada picking confirmado, tras cada
                                   reposición confirmada, por el botón manual
                                   "Verificar stock ahora", y cada 30 min por
                                   init_scheduler() (§5 más abajo)
  2. asignar_tarea()            — el abastecedor pide trabajo, el sistema asigna
  3. confirmar_reposicion()     — Abastecedor escanea LPN + confirma → "rompe la paca"
                                   → LPN CONSUMIDO + stock PICKING actualizado.
                                   100% WMS, nunca toca Siesa (ver docstring de
                                   la función — RESERVA y PICKING son la misma
                                   bodega Siesa, no hay documento real que enviar)
  4. configurar_umbral()        — única función que valida y escribe
                                   stock_minimo/stock_maximo/secuencia_ruteo de
                                   una ubicación (la usan esta ruta y Layout)

Reglas:
  - Solo ubicaciones tipo_zona='PICKING' disparan alertas (RESERVA y GENERAL nunca)
  - Solo LPNs ACTIVO en la ubicacion_reserva del mismo almacen son candidatos,
    y solo si su cantidad_actual cabe en lo que falta para stock_maximo —
    "romper la paca" es atómico (confirmar_reposicion mueve el LPN entero,
    no existe consumo parcial en ningún caller de LPN.consumir() del repo),
    así que un LPN candidato no se "recorta" para que quepa: si ninguno cabe,
    no se genera tarea (ver 2026-09-07 más abajo).
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

def _generar_tarea_si_hace_falta(ub_picking: Ubicacion, producto_id: int, stock_actual: int) -> bool:
    """
    Núcleo compartido de generación: dado un hueco PICKING + producto +
    stock ya calculado, crea la TareaReposicion si hace falta y hay LPN
    disponible. Usado por las dos pasadas de verificar_stock_picking() —
    la que recorre inventario existente y la que cubre huecos asignados
    en Layout sin ninguna fila de inventario todavía (ver más abajo).
    """
    if stock_actual >= ub_picking.stock_minimo:
        return False  # bien — no hace falta reponer

    # ¿Ya existe tarea activa para este (ubicacion_picking, producto)?
    ya_existe = TareaReposicion.query.filter(
        TareaReposicion.ubicacion_picking_id == ub_picking.id,
        TareaReposicion.producto_id == producto_id,
        TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO']),
    ).first()
    if ya_existe:
        return False

    # Cuánto cabe todavía en el hueco — hay que saberlo ANTES de elegir el
    # LPN, no después. `or` trataría un stock_maximo=0 configurado a mano
    # como "no definido" (0 es falsy) y calcularía stock_minimo*3 en su
    # lugar — divergencia silenciosa entre lo que el modal "Configurar"
    # muestra guardado y lo que el motor realmente repone.
    stock_maximo = (ub_picking.stock_maximo if ub_picking.stock_maximo is not None
                     else ub_picking.stock_minimo * 3)
    disponible = stock_maximo - stock_actual

    # ¿Hay LPN disponible en alguna ubicacion RESERVA del mismo almacén QUE
    # QUEPA en lo que falta? "Romper la paca" es atómico — no existe (ni en
    # este modelo ni en ningún caller de LPN.consumir() del repo) un
    # consumo parcial que deje el resto del LPN activo. confirmar_reposicion()
    # mueve TODO lpn.cantidad_actual a PICKING; un LPN más grande que
    # `disponible` desbordaría capacidad_maxima del hueco sin que nada lo
    # note — se descubrió así, con un LPN real de 1240 UNDs contra un hueco
    # de capacidad 100 (2026-09-07). Filtrar acá, no capar la cantidad
    # después, es lo que evita ese desborde: un LPN que no cabe no es
    # candidato, punto — no se "recorta" su cantidad para que quepa,
    # porque eso no es lo que va a pasar físicamente.
    lpn_candidato = LPN.query.join(
        Ubicacion, Ubicacion.id == LPN.ubicacion_id
    ).filter(
        LPN.producto_id == producto_id,
        LPN.almacen_id == ub_picking.almacen_id,
        LPN.estado == EstadoLPN.ACTIVO,
        LPN.cantidad_actual <= disponible,
        Ubicacion.tipo_zona == 'RESERVA',
    ).order_by(LPN.fecha_creacion.asc()).first()  # FIFO, entre los que caben

    if not lpn_candidato:
        # Distinguir "no hay ningún LPN" de "hay, pero ninguno cabe" — son
        # diagnósticos distintos y el segundo no se resuelve esperando que
        # llegue un LPN nuevo, se resuelve con uno más chico o ampliando el
        # hueco.
        hay_alguno = LPN.query.join(
            Ubicacion, Ubicacion.id == LPN.ubicacion_id
        ).filter(
            LPN.producto_id == producto_id,
            LPN.almacen_id == ub_picking.almacen_id,
            LPN.estado == EstadoLPN.ACTIVO,
            Ubicacion.tipo_zona == 'RESERVA',
        ).first()
        if hay_alguno:
            logger.warning(
                f'[REPOSICION] Hay LPN(s) de producto {producto_id} en RESERVA '
                f'almacén {ub_picking.almacen_id}, pero ninguno cabe en los '
                f'{disponible} UNDs disponibles de {ub_picking.codigo} '
                f'(capacidad {stock_maximo}) — se necesita un LPN más chico'
            )
        else:
            logger.warning(
                f'[REPOSICION] Sin LPN disponible para producto {producto_id} '
                f'en RESERVA almacén {ub_picking.almacen_id} — picking {ub_picking.codigo} bajo mínimo'
            )
        return False

    tarea = TareaReposicion(
        codigo=TareaReposicion.generar_codigo(),
        producto_id=producto_id,
        almacen_id=ub_picking.almacen_id,
        cantidad_unidades=lpn_candidato.cantidad_actual,
        ubicacion_reserva_id=lpn_candidato.ubicacion_id,
        ubicacion_picking_id=ub_picking.id,
        lpn_id=lpn_candidato.id,
        estado='PENDIENTE',
    )
    db.session.add(tarea)
    logger.info(
        f'[REPOSICION] TareaReposicion {tarea.codigo} generada — '
        f'producto {producto_id} | {ub_picking.codigo} | LPN {lpn_candidato.codigo} | '
        f'{lpn_candidato.cantidad_actual} UNDs'
    )
    return True


def verificar_stock_picking(almacen_id: int = None):
    """
    Escanea todas las ubicaciones PICKING con stock_minimo definido.
    Para cada una donde stock_actual < stock_minimo, crea una TareaReposicion
    si no existe ya una activa.

    Dos pasadas, ambas conscientes de Ubicacion.producto_asignado_id (la
    asignación deliberada de Layout — "qué SKU va en este hueco", distinta
    de UbicacionProducto, que es el registro de qué hay físicamente ahora):

      A) Recorre el inventario (UbicacionProducto) que ya existe — el caso
         de siempre. Si el hueco SÍ tiene asignación en Layout y la fila de
         inventario es de OTRO producto, se omite: no tiene sentido reponer
         el SKU equivocado dentro de un hueco que Layout dice que es de
         otro — sería instalar ahí lo que nadie decidió que fuera. Un hueco
         sin asignación en Layout se procesa igual que antes (compatibilidad
         con ubicaciones nunca migradas al mecanismo de asignación).
      B) Huecos asignados en Layout que NO tienen ninguna fila de
         inventario para ese producto — invisibles para la pasada A porque
         no hay UbicacionProducto de la que partir. Antes esto significaba
         que un hueco recién asignado y vaciado del todo nunca disparaba
         reposición hasta que alguien lo pickeara primero.

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

    # ── Pasada A: inventario existente ──────────────────────────────────
    for inv in registros:
        ub_picking = inv.ubicacion

        if (ub_picking.producto_asignado_id is not None
                and ub_picking.producto_asignado_id != inv.producto_id):
            logger.warning(
                f'[REPOSICION] {ub_picking.codigo} asignado en Layout a producto '
                f'{ub_picking.producto_asignado_id}, pero tiene inventario de '
                f'{inv.producto_id} (cant={inv.cantidad}) — no se repone el SKU '
                f'equivocado dentro del hueco de otro'
            )
            continue

        stock_actual = inv.cantidad - (inv.reservado or 0)
        if _generar_tarea_si_hace_falta(ub_picking, inv.producto_id, stock_actual):
            generadas += 1

    # ── Pasada B: huecos asignados en Layout sin fila de inventario ────
    qb = (Ubicacion.query
          .filter(
              Ubicacion.tipo_zona == 'PICKING',
              Ubicacion.stock_minimo.isnot(None),
              Ubicacion.activo == True,
              Ubicacion.producto_asignado_id.isnot(None),
          ))
    if almacen_id:
        qb = qb.filter(Ubicacion.almacen_id == almacen_id)

    for ub_picking in qb.all():
        tiene_fila = UbicacionProducto.query.filter_by(
            ubicacion_id=ub_picking.id,
            producto_id=ub_picking.producto_asignado_id,
        ).first()
        if tiene_fila:
            continue  # ya cubierto por la pasada A
        if _generar_tarea_si_hace_falta(ub_picking, ub_picking.producto_asignado_id, 0):
            generadas += 1

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
      e) Dispara verificar_stock_picking() para detectar nueva necesidad

    100% WMS — nunca toca Siesa (decisión 2026-09-07). RESERVA y PICKING son
    zonas físicas dentro de UNA MISMA bodega Siesa (NB1 no tiene sub-bodegas
    para "picking" ni "reserva" — eso es organización interna del WMS, ver
    el subtítulo de Layout: "Las ubicaciones se crean y clasifican 100% en
    el WMS"). El total de la bodega en Siesa no cambia con este movimiento,
    así que no hay ningún documento real que declarar — antes se posteaba
    igual al conector 173066 (TransferenciaDirecta, mismo bodega origen y
    destino), que además resultó no ser idempotente en Siesa (un reintento
    duplicaba el movimiento) y, al probarlo en vivo, ni siquiera pasaba la
    validación de tamaño de registro del propio Siesa. Se retiró: menos
    superficie, menos riesgo, y refleja lo que el movimiento realmente es.
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

    # e) Cerrar tarea — sin job Siesa: ver docstring, RESERVA→PICKING es
    # 100% WMS, no existe documento real que postear.
    tarea.estado = EstadoReposicion.COMPLETADA
    tarea.unidades_movidas = unidades
    tarea.fecha_completada = datetime.utcnow()

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
