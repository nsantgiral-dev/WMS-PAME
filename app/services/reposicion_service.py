"""
Motor de Reabastecimiento RESERVA → PICKING.

Responsabilidades:
  1. verificar_stock_picking()  — el cron llama esto tras cada picking completado
                                   y también el scheduler nocturno
  2. asignar_tarea()            — el abastecedor pide trabajo, el sistema asigna
  3. confirmar_reposicion()     — Abastecedor escanea LPN + confirma → "rompe la paca"
                                   → LPN CONSUMIDO + stock PICKING actualizado
                                   → job Siesa conector 173076 (tránsito entre ubicaciones)

Reglas:
  - Solo ubicaciones tipo_zona='PICKING' disparan alertas (RESERVA y GENERAL nunca)
  - Solo LPNs ACTIVO en la ubicacion_reserva del mismo almacen son candidatos
  - Si hay TareaReposicion PENDIENTE/EN_PROCESO para esa (ubicacion_picking, producto)
    no se crea duplicada
"""

import logging
from datetime import datetime
from app.extensions import db
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.lpn import LPN
from app.models.tarea_reposicion import TareaReposicion
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


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
            LPN.estado == 'ACTIVO',
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
    siguiente = TareaReposicion.query.filter_by(
        estado='PENDIENTE',
        abastecedor_id=None,
    ).order_by(TareaReposicion.fecha_creacion.asc()).first()

    if not siguiente:
        return None

    siguiente.abastecedor_id = abastecedor_id
    siguiente.estado = 'EN_PROCESO'
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
    if not lpn or lpn.estado != 'ACTIVO':
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
    tarea.estado = 'COMPLETADA'
    tarea.unidades_movidas = unidades
    tarea.fecha_completada = datetime.utcnow()

    db.session.commit()

    # f) Encolar job Siesa en la DLQ — reintentos automáticos, alerta si falla 3 veces
    _encolar_siesa_job(tarea, lpn, unidades)

    # g) Re-evaluar stock (puede haber otra tarea necesaria)
    try:
        verificar_stock_picking(almacen_id=tarea.almacen_id)
    except Exception as e:
        logger.error(f'[REPOSICION] Error re-evaluando stock post-reposición: {e}')

    return {
        'ok': True,
        'mensaje': f'Reposición completada — {unidades} UNDs de {lpn.codigo} ahora en {tarea.ubicacion_picking.codigo}',
        'tarea': tarea.to_dict(),
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

    ub_reserva = _Ub.query.get(tarea.ubicacion_reserva_id)
    ub_picking = _Ub.query.get(tarea.ubicacion_picking_id)
    producto = tarea.producto

    if not ub_reserva or not ub_picking or not producto:
        logger.error(f'[REPOSICION DLQ] Datos incompletos para job — no se encola')
        return

    job = encolar_transferencia_ubicaciones(
        bodega_id=connekta.bodega,
        ubicacion_origen=ub_reserva.codigo,
        ubicacion_destino=ub_picking.codigo,
        referencia_item=getattr(producto, 'codigo_siesa', None) or producto.codigo,
        cantidad=unidades,
        nota=f'Reposición WMS {tarea.codigo} — LPN {lpn.codigo}',
        referencia_tipo='TareaReposicion',
        referencia_id=tarea.id,
    )
    db.session.commit()
    logger.info(f'[REPOSICION DLQ] Job {job.id} encolado para {tarea.codigo}')
