"""
DEPRECATED (2026-07-28) — reemplazado por devolucion_cliente_service.py.
Su único caller real (inventario_siesa_service.py::_run_reconciliacion) ya no
invoca crear_tareas_desde_discrepancias(); este servicio queda sin uso en
producción, conservado solo por sus tests existentes
(tests/test_servicios_coverage.py) y por el histórico de TareaDevolucion ya
persistido. No agregar funcionalidad nueva aquí — el reemplazo real es
buscar_pedido/crear_devolucion/confirmar_entrada_fisica en
devolucion_cliente_service.py.

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
from app.models.devolucion import TareaDevolucion, EstadoDevolucion
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.ubicacion import Ubicacion
from app.models.almacen import Almacen
from app.services.connekta_gateway import connekta
from sqlalchemy.exc import IntegrityError as _IntegrityError

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

    # [M3] Bulk pre-load: fetch all existing TareaDevolucion by idempotency_key in one query
    ikeys_all = [f'DEV-{d["producto_id"]}-PENDIENTE' for d in siesa_mayor]
    existentes_map = {}
    if ikeys_all:
        for td in TareaDevolucion.query.filter(
            TareaDevolucion.idempotency_key.in_(ikeys_all)
        ).all():
            existentes_map[td.idempotency_key] = td

    for disc in siesa_mayor:
        producto_id = disc['producto_id']
        cantidad = abs(disc['diferencia'])  # siempre positivo

        # Idempotencia: clave única producto + estado PENDIENTE
        ikey = f'DEV-{producto_id}-PENDIENTE'

        # Savepoint por item — el rollback solo deshace este item, no los anteriores
        savepoint = db.session.begin_nested()
        try:
            existente = existentes_map.get(ikey)

            if existente:
                if existente.estado == 'COMPLETADO':
                    if not existente.es_averiado:
                        # Devolución normal: no necesita Siesa — ya resuelta físicamente
                        ya_existian += 1
                        savepoint.commit()
                        continue
                    # Averiado: verificar si existe SiesaJob activo o ya completado.
                    # NO usamos siesa_triggered aquí porque ese flag sólo lo pone el DLQ
                    # handler después del HTTP 200 de Siesa — consultamos directamente la tabla.
                    from app.models.siesa_job import SiesaJob as _SiesaJob
                    job_activo = _SiesaJob.query.filter_by(
                        referencia_tipo='TareaDevolucion',
                        referencia_id=existente.id,
                    ).filter(_SiesaJob.estado.in_(
                        ['PENDIENTE', 'REINTENTANDO', 'PROCESANDO', 'COMPLETADO']
                    )).first()
                    if job_activo:
                        # Job en vuelo o Siesa ya confirmó — no recrear
                        ya_existian += 1
                        savepoint.commit()
                        continue
                    # Sin SiesaJob confirmado: averiado completado pero Siesa nunca fue notificada
                    logger.warning(
                        f'[DEV] TareaDevolucion {existente.id} COMPLETADO sin SiesaJob (averiado) '
                        f'para prod {producto_id} — creando nueva tarea'
                    )
                    existente.idempotency_key = f'DEV-{producto_id}-SIN-SIESA-{existente.id}'
                    db.session.flush()
                    # Caer al bloque de creación abajo (no hacer continue)
                elif existente.estado in ('PENDIENTE', 'EN_PROCESO'):
                    # Ya está siendo atendida
                    if existente.cantidad_diferencia != cantidad:
                        existente.cantidad_diferencia = cantidad
                        db.session.flush()
                    ya_existian += 1
                    savepoint.commit()
                    continue
                # CANCELADO u otro: recrear la tarea

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
            # ERROR (no warning): fallo aquí implica que una discrepancia queda sin tarea.
            # El operario no verá el ítem para devolver → diferencia permanece invisible.
            logger.error(
                f'[DEV] Error creando tarea para producto {producto_id}: {e}',
                exc_info=True
            )
            savepoint.rollback()  # solo revierte este item, no los anteriores
            errores += 1

    try:
        db.session.commit()
    except Exception as e:
        logger.error('[DEV] Error en commit outer — todas las TareaDevolucion del batch se perdieron', exc_info=True)
        db.session.rollback()
        # El outer commit falló: ninguna tarea fue persistida aunque creadas > 0
        errores += creadas
        creadas = 0
        try:
            from app.services.alertas_service import enviar_email, _config_resend
            if _config_resend():
                enviar_email(
                    asunto='[WMS ALERTA] Batch devoluciones perdido — commit fallido',
                    cuerpo_texto=(
                        f'El commit final del reconciliador falló con error:\n{e}\n\n'
                        f'Todas las TareaDevolucion del batch ({errores} ítems) fueron revertidas. '
                        'Se reintentará en el próximo ciclo de reconciliación (~5 min).'
                    ),
                    cuerpo_html=None,
                )
        except Exception as _e_alert:
            logger.critical('[DEV] Email de alerta de commit fallido también falló: %s', _e_alert)

    # [A26] Si hubo errores, notificar proactivamente — las discrepancias sin tarea
    # quedan invisibles indefinidamente (el operario nunca ve esos ítems para devolver).
    if errores > 0:
        logger.error(
            f'[DEV] {errores} discrepancia(s) SIESA_MAYOR sin tarea de devolución creada — '
            f'el diferencial de inventario permanece activo. '
            f'Creadas={creadas}, ya_existian={ya_existian}'
        )
        try:
            from app.services.alertas_service import enviar_email, _config_resend
            if _config_resend():
                enviar_email(
                    asunto=f'[WMS ALERTA] {errores} devolución(es) sin tarea — diferencial activo',
                    cuerpo_texto=(
                        f'El reconciliador detectó discrepancias SIESA_MAYOR pero '
                        f'{errores} tarea(s) de devolución no pudieron crearse.\n\n'
                        f'Creadas: {creadas} | Ya existían: {ya_existian} | Errores: {errores}\n'
                        'Revisar logs del servidor para el detalle de cada fallo.\n'
                        'Timestamp reconciliación: ' + str(timestamp_reconciliacion)
                    ),
                    cuerpo_html=None,
                )
        except Exception as _e_alert:
            logger.critical(f'[DEV] Email de alerta de errores de devolución también falló: {_e_alert}')

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
    from sqlalchemy.orm import selectinload as _sl
    tarea = (TareaDevolucion.query
             .options(_sl(TareaDevolucion.producto))
             .filter_by(id=tarea_id)
             .with_for_update()
             .first())
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
        try:
            db.session.flush()
        except _IntegrityError:
            # Race condition: otro worker creó la misma Ubicacion entre el SELECT y este INSERT.
            # Ubicacion.codigo tiene unique=True — recuperar la fila que ganó la carrera.
            db.session.rollback()
            ub = Ubicacion.query.filter_by(codigo=codigo_ub, almacen_id=tarea.almacen_id).first()
            if not ub:
                raise  # fallo por otra razón — propagar

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
        try:
            db.session.flush()
        except _IntegrityError:
            # Race condition: otro worker insertó la fila entre el SELECT y el INSERT.
            # [M1] db.session.rollback() libera el row-lock de tarea adquirido al inicio.
            # Re-adquirir lock de tarea y re-verificar estado antes de continuar.
            db.session.rollback()
            tarea = (TareaDevolucion.query
                     .options(_sl(TareaDevolucion.producto))
                     .filter_by(id=tarea_id)
                     .with_for_update()
                     .first())
            if not tarea or tarea.estado == 'COMPLETADO':
                raise ValueError('Tarea ya completada por otro proceso — operación idempotente')
            # Re-resolver ub: si era nueva fue revertida por el rollback
            ub = Ubicacion.query.filter_by(codigo=codigo_ub, almacen_id=tarea.almacen_id).first()
            if not ub:
                raise ValueError(f'Ubicación {codigo_ub} no encontrada tras rollback de race condition')
            reg = (UbicacionProducto.query.filter_by(
                ubicacion_id=ub.id,
                producto_id=tarea.producto_id,
                lote=None
            ).with_for_update().first())
            if reg:
                saldo_antes = reg.cantidad
                reg.cantidad += tarea.cantidad_diferencia
                reg.row_version += 1
            else:
                raise ValueError(
                    f'No se pudo obtener/crear UbicacionProducto para ubicación {ub.codigo}'
                )

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
    tarea.estado = EstadoDevolucion.COMPLETADO
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
        # Capturar antes del commit (expire_on_commit haría lazy load ineficiente después)
        # [C6] Solo codigo_siesa — Siesa no conoce códigos WMS. Si se usara tarea.producto.codigo
        # (WMS), el job quedaría en FALLIDO permanente: Siesa rechaza referencias desconocidas.
        # [CRÍTICO] Si no hay codigo_siesa, bloquear la confirmación en vez de marcar COMPLETADO
        # silenciosamente sin Siesa — la avería quedaría sin registrar en el ERP y el inventario
        # de Siesa divergiría permanentemente del WMS.
        _item_codigo = tarea.producto.codigo_siesa if tarea.producto else None
        if not _item_codigo:
            raise ValueError(
                f'El producto (id={tarea.producto_id}) no tiene código Siesa configurado. '
                'El traslado a bodega de averías no puede enviarse a Siesa sin ese código. '
                'Configura el campo codigo_siesa en el catálogo de productos antes de confirmar.'
            )
        job_dlq = SiesaJob.encolar(
                tipo='TRASLADO_AVERIAS',
                payload={
                    'tarea_id': tarea.id,
                    'item_codigo': _item_codigo,
                    'cantidad': tarea.cantidad_diferencia,
                    'referencia': tarea.codigo,
                },
                referencia_tipo='TareaDevolucion',
                referencia_id=tarea.id,
            )
        # NO ponemos siesa_triggered=True aquí — el DLQ handler lo pondrá
        # sólo cuando Siesa responda HTTP 200 (semántica exacta de idempotencia).
        # El loop de reconciliación se suprime consultando SiesaJob activo (ver abajo).

    try:
        db.session.commit()
    except Exception as e_commit:
        db.session.rollback()
        logger.error(f'[DEV] Error al confirmar ubicación tarea {tarea_id}: {e_commit}')
        raise ValueError(f'Error al guardar confirmación de devolución: {e_commit}') from e_commit
    logger.info(f'[DEV] Tarea {tarea.codigo} completada · ubicación {codigo_ub} · averiado={es_averiado}')
    # Disparar DLQ inmediato para reducir gap WMS↔Siesa de ~5 min a ~segundos
    if job_dlq:
        try:
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
        except Exception as _e_dlq:
            logger.warning(f'[DEV] disparar_dlq_inmediato falló (DLQ scheduler lo recogerá): {_e_dlq}')

    return tarea


def descartar(tarea_id: int, recepcionista_id: int, motivo: str = None) -> TareaDevolucion:
    """
    Descarta una tarea (p.ej. el diferencial era error de conteo en Siesa).
    NO modifica stock — solo cierra la tarea.
    """
    tarea = TareaDevolucion.query.get(tarea_id)
    if not tarea:
        raise ValueError(f'Tarea {tarea_id} no existe')
    tarea.estado = EstadoDevolucion.DESCARTADO
    tarea.recepcionista_id = recepcionista_id
    tarea.observaciones = motivo
    tarea.fecha_completado = datetime.utcnow()
    tarea.idempotency_key = f'DEV-DESCARTADO-{tarea.id}'
    try:
        db.session.commit()
    except Exception as e_commit:
        db.session.rollback()
        logger.error(f'[DEV] Error al descartar tarea {tarea_id}: {e_commit}')
        raise ValueError(f'Error al descartar tarea: {e_commit}') from e_commit
    return tarea
