"""
Servicio de Logística Inversa (Devoluciones físicas).

REGLA DE ORO: Este servicio NO toca dinero, IVA ni contabilidad.
Siesa ya procesó la nota crédito. Nosotros solo ubicamos cajas.

Flujo:
  Reconciliación detecta SIESA_MAYOR
  → crear_tareas_desde_discrepancias()
  → recepcionista ve tarea en PWA
  → escanea producto, elige ubicación
  → confirmar_ubicacion() actualiza stock WMS
  → diferencial queda en 0
"""
import logging
from datetime import datetime
from app.extensions import db
from app.models.devolucion import TareaDevolucion
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.ubicacion import Ubicacion
from app.models.almacen import Almacen
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

_UBICACION_AVERIADOS = 'AVERIADOS'


def crear_tareas_desde_discrepancias(discrepancias: list, almacen_id: int, timestamp_reconciliacion: str) -> dict:
    """
    Recibe la lista de discrepancias del reconciliador.
    Para cada SIESA_MAYOR crea una TareaDevolucion si no existe ya una PENDIENTE.
    Idempotente: se puede llamar en cada reconciliación sin duplicar tareas.

    Retorna dict con conteos.
    """
    creadas = 0
    ya_existian = 0
    errores = 0

    siesa_mayor = [d for d in discrepancias if d.get('estado') == 'SIESA_MAYOR']

    for disc in siesa_mayor:
        producto_id = disc['producto_id']
        cantidad = abs(disc['diferencia'])  # siempre positivo

        # Idempotencia: clave única producto + estado PENDIENTE
        ikey = f'DEV-{producto_id}-PENDIENTE'

        # Savepoint por item — el rollback solo deshace este item, no los anteriores
        savepoint = db.session.begin_nested()
        try:
            existente = TareaDevolucion.query.filter_by(
                idempotency_key=ikey
            ).first()

            if existente:
                # Si existe pero la cantidad cambió, actualizarla
                if existente.cantidad_diferencia != cantidad:
                    existente.cantidad_diferencia = cantidad
                    db.session.flush()
                ya_existian += 1
                savepoint.commit()
                continue

            codigo = f'DEV-{producto_id}-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}'
            tarea = TareaDevolucion(
                codigo=codigo,
                producto_id=producto_id,
                almacen_id=almacen_id,
                cantidad_diferencia=cantidad,
                estado='PENDIENTE',
                origen_reconciliacion=timestamp_reconciliacion,
                idempotency_key=ikey
            )
            db.session.add(tarea)
            db.session.flush()
            savepoint.commit()
            creadas += 1
            logger.info(f'[DEV] Tarea creada: {codigo} · prod {producto_id} · {cantidad} uds')

        except Exception as e:
            logger.warning(f'[DEV] Error creando tarea para producto {producto_id}: {e}')
            savepoint.rollback()  # solo revierte este item, no los anteriores
            errores += 1

    try:
        db.session.commit()
    except Exception as e:
        logger.error(f'[DEV] Error en commit: {e}')
        db.session.rollback()

    return {'creadas': creadas, 'ya_existian': ya_existian, 'errores': errores}


def listar_pendientes(almacen_id: int = None) -> list:
    """Lista tareas PENDIENTE o EN_PROCESO para el recepcionista."""
    q = TareaDevolucion.query.filter(
        TareaDevolucion.estado.in_(['PENDIENTE', 'EN_PROCESO'])
    )
    if almacen_id:
        q = q.filter_by(almacen_id=almacen_id)
    return [t.to_dict() for t in q.order_by(TareaDevolucion.fecha_creacion).all()]


def confirmar_ubicacion(tarea_id: int, ubicacion_codigo: str, recepcionista_id: int,
                        es_averiado: bool = False, observaciones: str = None) -> TareaDevolucion:
    """
    El recepcionista escaneó el producto y eligió dónde ponerlo.
    1. Busca o crea la ubicación en WMS.
    2. Suma stock en esa ubicación.
    3. Registra MovimientoInventario.
    4. Marca tarea como COMPLETADO.
    5. Elimina la idempotency_key para que si vuelve a haber diferencia se cree nueva tarea.
    """
    tarea = TareaDevolucion.query.get(tarea_id)
    if not tarea:
        raise ValueError(f'Tarea {tarea_id} no existe')
    if tarea.estado == 'COMPLETADO':
        raise ValueError('Tarea ya completada')

    # Resolver ubicación destino
    codigo_ub = _UBICACION_AVERIADOS if es_averiado else ubicacion_codigo
    ub = Ubicacion.query.filter_by(codigo=codigo_ub, almacen_id=tarea.almacen_id).first()

    if not ub:
        if es_averiado:
            ub = Ubicacion(
                codigo=_UBICACION_AVERIADOS,
                almacen_id=tarea.almacen_id,
                zona='CUARENTENA',
                tipo='cuarentena',  # el picking filtra tipo != cuarentena
                activo=True
            )
        else:
            ub = Ubicacion(
                codigo=codigo_ub,
                almacen_id=tarea.almacen_id,
                zona='GENERAL',
                tipo='estanteria',
                activo=True
            )
        db.session.add(ub)
        db.session.flush()

    # [36] Actualizar stock con SELECT FOR UPDATE para evitar race condition
    reg = UbicacionProducto.query.filter_by(
        ubicacion_id=ub.id,
        producto_id=tarea.producto_id,
        lote=None
    ).with_for_update().first()

    saldo_antes = reg.cantidad if reg else 0

    if reg:
        reg.cantidad += tarea.cantidad_diferencia
        reg.row_version += 1
    else:
        reg = UbicacionProducto(
            ubicacion_id=ub.id,
            producto_id=tarea.producto_id,
            cantidad=tarea.cantidad_diferencia,
            fecha_ingreso=datetime.utcnow()
        )
        db.session.add(reg)
        db.session.flush()

    # Movimiento de inventario
    mov = MovimientoInventario(
        producto_id=tarea.producto_id,
        ubicacion_id=ub.id,
        almacen_id=tarea.almacen_id,
        tipo='DEVOLUCION' if not es_averiado else 'DEVOLUCION_AVERIADO',
        cantidad=tarea.cantidad_diferencia,
        saldo_antes=saldo_antes,
        saldo_despues=saldo_antes + tarea.cantidad_diferencia,
        motivo=f'Devolución física ubicada · tarea {tarea.codigo}',
        numero_documento=tarea.codigo,
        usuario_id=recepcionista_id
    )
    db.session.add(mov)

    # Cerrar tarea
    tarea.estado = 'COMPLETADO'
    tarea.ubicacion_id = ub.id
    tarea.es_averiado = es_averiado
    tarea.recepcionista_id = recepcionista_id
    tarea.observaciones = observaciones
    tarea.fecha_completado = datetime.utcnow()
    # Liberar idempotency_key para que futuras diferencias puedan generar nueva tarea
    tarea.idempotency_key = f'DEV-COMPLETADO-{tarea.id}'

    # [P8] Si es averiado, crear SiesaJob ANTES del commit — atómico con el estado del WMS.
    # Si Siesa falla, el job ya está en DB como PENDIENTE para el DLQ.
    job_dlq = None
    if es_averiado and not connekta.modo_simulacion:
        from app.models.siesa_job import SiesaJob
        item_codigo_dlq = tarea.producto.codigo_siesa or tarea.producto.codigo
        job_dlq = SiesaJob.encolar(
            tipo='TRASLADO_AVERIAS',
            payload={
                'tarea_id': tarea.id,
                'item_codigo': item_codigo_dlq,
                'cantidad': tarea.cantidad_diferencia,
                'referencia': tarea.codigo,
            },
            referencia_tipo='TareaDevolucion',
            referencia_id=tarea.id,
        )

    db.session.commit()
    logger.info(f'[DEV] Tarea {tarea.codigo} completada · ubicación {codigo_ub} · averiado={es_averiado}')

    # Disparar traslado NB1 → AV1 en Siesa para que vendedores no vean unidades dañadas
    if job_dlq is not None:
        try:
            item_codigo = tarea.producto.codigo_siesa or tarea.producto.codigo
            connekta.transferir_a_averias(
                item_codigo=item_codigo,
                cantidad=tarea.cantidad_diferencia,
                referencia=tarea.codigo
            )
            tarea.siesa_triggered = True
            job_dlq.marcar_completado({'ok': True})
            db.session.commit()
            logger.info(f'[DEV] Traslado Siesa NB1→AV1 OK para {item_codigo}')
        except Exception as e:
            # El job_dlq ya está en DB como PENDIENTE — DLQ lo reintentará en 5 min
            logger.error(f'[DEV] Error disparando traslado Siesa: {e} — DLQ reintentará')
            db.session.commit()

    return tarea


def descartar(tarea_id: int, recepcionista_id: int, motivo: str = None) -> TareaDevolucion:
    """
    Descarta una tarea (p.ej. el diferencial era error de conteo en Siesa).
    NO modifica stock — solo cierra la tarea.
    """
    tarea = TareaDevolucion.query.get(tarea_id)
    if not tarea:
        raise ValueError(f'Tarea {tarea_id} no existe')
    tarea.estado = 'DESCARTADO'
    tarea.recepcionista_id = recepcionista_id
    tarea.observaciones = motivo
    tarea.fecha_completado = datetime.utcnow()
    tarea.idempotency_key = f'DEV-DESCARTADO-{tarea.id}'
    db.session.commit()
    return tarea
