"""
Servicio de Picking con lógica FEFO
(First Expired First Out — primero vence, primero sale)
"""
from datetime import datetime
import uuid
from app.extensions import db
from app.models.picking import TareaPicking, EstadoPicking
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion


class PickingService:

    @staticmethod
    def calcular_fefo(producto_id: int, cantidad_necesaria: int, almacen_id: int):
        """
        Calcula qué ubicaciones usar según FEFO.
        Prioriza lotes que vencen primero.
        Retorna lista de {ubicacion, cantidad, lote, fecha_vencimiento}
        """
        registros = (
            UbicacionProducto.query
            .join(Ubicacion)
            .filter(
                UbicacionProducto.producto_id == producto_id,
                Ubicacion.almacen_id == almacen_id,
                UbicacionProducto.cantidad > 0
            )
            .order_by(
                # Primero los que tienen fecha de vencimiento (FEFO)
                UbicacionProducto.fecha_vencimiento.asc().nullslast(),
                # Luego por fecha de ingreso (FIFO como fallback)
                UbicacionProducto.fecha_ingreso.asc()
            )
            .all()
        )

        asignaciones = []
        pendiente = cantidad_necesaria

        for reg in registros:
            if pendiente <= 0:
                break

            disponible = reg.cantidad_disponible()
            if disponible <= 0:
                continue

            tomar = min(disponible, pendiente)
            asignaciones.append({
                'ubicacion_id': reg.ubicacion_id,
                'ubicacion_codigo': reg.ubicacion.codigo,
                'producto_id': reg.producto_id,
                'cantidad': tomar,
                'lote': reg.lote,
                'fecha_vencimiento': reg.fecha_vencimiento
            })
            pendiente -= tomar

        return {
            'asignaciones': asignaciones,
            'cantidad_disponible': cantidad_necesaria - pendiente,
            'cantidad_faltante': pendiente,
            'completo': pendiente == 0
        }

    @staticmethod
    def crear_tareas(
        producto_id: int,
        cantidad: int,
        almacen_id: int,
        referencia_documento: str = None,
        tipo_documento: str = None,
        operario_id: int = None,
        prioridad: int = 1
    ):
        """
        Crea tareas de picking usando FEFO.
        Puede generar múltiples tareas si el stock está en varias ubicaciones.
        """
        producto = Producto.query.get(producto_id)
        if not producto:
            raise ValueError(f'Producto {producto_id} no encontrado')

        # ── Reposición Predictiva ──────────────────────────────────────────────
        # Antes de crear las tareas de picking, verificamos si el stock de la
        # zona PICKING es suficiente para toda la demanda. Si no, pre-disparamos
        # TareaReposicion al Abastecedor ANTES de que el picker empiece a caminar.
        try:
            from app.services.ola_predictiva_service import pre_verificar_ola
            pre_verificar_ola(
                items=[{'producto_id': producto_id, 'cantidad': cantidad}],
                almacen_id=almacen_id,
            )
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).warning(f'[PICKING] Ola predictiva falló silenciosamente: {_e}')
        # ──────────────────────────────────────────────────────────────────────

        fefo = PickingService.calcular_fefo(producto_id, cantidad, almacen_id)

        if not fefo['completo']:
            raise ValueError(
                f'Stock insuficiente. Disponible: {fefo["cantidad_disponible"]}, '
                f'Solicitado: {cantidad}'
            )

        tareas_creadas = []

        for asig in fefo['asignaciones']:
            codigo = f'PICK-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

            tarea = TareaPicking(
                codigo=codigo,
                producto_id=producto_id,
                cantidad_solicitada=asig['cantidad'],
                ubicacion_id=asig['ubicacion_id'],
                almacen_id=almacen_id,
                lote=asig['lote'],
                fecha_vencimiento=asig['fecha_vencimiento'],
                operario_id=operario_id,
                estado=EstadoPicking.PENDIENTE,
                prioridad=prioridad,
                referencia_documento=referencia_documento,
                tipo_documento=tipo_documento
            )

            # Reservar el stock — lock a nivel de fila para evitar doble reserva concurrente
            reg = UbicacionProducto.query.filter_by(
                ubicacion_id=asig['ubicacion_id'],
                producto_id=producto_id
            ).with_for_update().first()

            if not reg or reg.cantidad_disponible() < asig['cantidad']:
                db.session.rollback()
                raise ValueError(
                    f'Stock insuficiente al reservar en ubicación {asig["ubicacion_codigo"]}. '
                    f'Otro proceso puede haber tomado el stock — reintente.'
                )
            reg.reservado += asig['cantidad']

            db.session.add(tarea)
            tareas_creadas.append(tarea)

        db.session.commit()
        return tareas_creadas

    @staticmethod
    def confirmar_picking(tarea_id: int, cantidad_recogida: int, usuario_id: int):
        """
        Confirma que el operario recogió la mercancía.
        Descuenta el stock real y libera la reserva.
        """
        # [A16] Row-lock en TareaPicking — serializa confirmaciones concurrentes del mismo tarea_id.
        # Sin este lock, dos workers leen estado=EN_PROCESO simultáneamente, ambos pasan el guard
        # y ambos decrementan el stock → double-decrement silencioso.
        tarea = TareaPicking.query.filter_by(id=tarea_id).with_for_update().first()
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado == EstadoPicking.COMPLETADO:
            raise ValueError('Tarea ya completada')

        if tarea.estado == EstadoPicking.CANCELADO:
            raise ValueError('Tarea cancelada')

        # Operario solo puede confirmar tareas asignadas a él mismo
        if tarea.operario_id and tarea.operario_id != usuario_id:
            from app.models.usuario import Usuario as _U
            u = _U.query.get(usuario_id)
            es_supervision = u and u.rol in ('admin', 'supervisor', 'jefe_almacen')
            if not es_supervision:
                raise ValueError('No puedes confirmar una tarea asignada a otro operario')

        if cantidad_recogida > tarea.cantidad_solicitada:
            raise ValueError('Cantidad recogida supera la solicitada')

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()

        if not reg or reg.cantidad < cantidad_recogida:
            raise ValueError('Stock insuficiente en ubicación')

        # Descontar stock real
        saldo_antes = reg.cantidad
        reg.cantidad -= cantidad_recogida
        reg.reservado = max(0, reg.reservado - tarea.cantidad_solicitada)
        reg.row_version += 1

        # Registrar movimiento
        movimiento = MovimientoInventario(
            producto_id=tarea.producto_id,
            ubicacion_id=tarea.ubicacion_id,
            almacen_id=tarea.almacen_id,
            tipo='SALIDA',
            cantidad=cantidad_recogida,
            saldo_antes=saldo_antes,
            saldo_despues=reg.cantidad,
            motivo=f'Picking {tarea.codigo}',
            numero_documento=tarea.referencia_documento,
            usuario_id=usuario_id,
            idempotency_key=f'PICK-{tarea.id}'
        )

        # Actualizar tarea
        tarea.cantidad_recogida = cantidad_recogida
        tarea.estado = EstadoPicking.COMPLETADO
        tarea.fecha_completado = datetime.utcnow()
        if not tarea.fecha_inicio:
            tarea.fecha_inicio = datetime.utcnow()

        # Capturar referencia_documento antes del commit — expire_on_commit la invalida
        _ref_doc_cp = tarea.referencia_documento

        db.session.add(movimiento)
        db.session.commit()

        # Auto-sync packing asociado — ajusta cantidad_esperada al recogido real
        try:
            from app.models.packing import TareaPacking as _TPSync
            from app.services.packing_picking_sync_service import PackingPickingSyncService as _PPSync
            if _ref_doc_cp:
                _sync_pk = _TPSync.query.filter(
                    _TPSync.numero_pedido_siesa == _ref_doc_cp,
                    _TPSync.estado.in_(['PENDIENTE', 'EN_PROCESO'])
                ).first()
                if _sync_pk:
                    _PPSync.sincronizar(_sync_pk.id)
        except Exception as _e_sync:
            logger.warning('[PICKING] Sync packing falló — cantidad_esperada puede estar desactualizada: %s', _e_sync)

        return tarea

    @staticmethod
    def iniciar_picking(tarea_id: int, operario_id: int):
        """Marca la tarea como en proceso."""
        # [A15] Row-lock — evita que dos operarios inicien la misma tarea simultáneamente
        tarea = TareaPicking.query.filter_by(id=tarea_id).with_for_update().first()
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado != EstadoPicking.PENDIENTE:
            raise ValueError(f'No se puede iniciar una tarea en estado {tarea.estado}')

        # Impedir que un operario se apropie de una tarea asignada a otro
        if tarea.operario_id and tarea.operario_id != operario_id:
            from app.models.usuario import Usuario as _U
            u = _U.query.get(operario_id)
            es_supervision = u and u.rol in ('admin', 'supervisor', 'jefe_almacen')
            if not es_supervision:
                raise ValueError('Esta tarea ya está asignada a otro operario')

        tarea.estado = EstadoPicking.EN_PROCESO
        tarea.operario_id = operario_id
        tarea.fecha_inicio = datetime.utcnow()
        tarea.cantidad_recogida = 0
        tarea.empaques_escaneados = 0
        db.session.commit()
        return tarea

    @staticmethod
    def cancelar_picking(tarea_id: int, motivo: str = None):
        """Cancela una tarea y libera la reserva (y el bloqueado si aplica)."""
        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado == EstadoPicking.COMPLETADO:
            raise ValueError('No se puede cancelar una tarea completada')

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()
        if reg:
            reg.reservado = max(0, reg.reservado - tarea.cantidad_solicitada)
            # Si estaba bloqueada, liberar también el inventario congelado
            if tarea.estado == EstadoPicking.BLOQUEADO:
                cantidad_faltante = max(0, tarea.cantidad_solicitada - (tarea.cantidad_recogida or 0))
                reg.bloqueado = max(0, reg.bloqueado - cantidad_faltante)

        tarea.estado = EstadoPicking.CANCELADO
        db.session.commit()
        return tarea

    @staticmethod
    def reabrir_picking(tarea_id: int):
        """
        Reabre una tarea BLOQUEADA → PENDIENTE.
        Libera el inventario congelado por el bloqueo y la devuelve al pool.
        """
        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado != EstadoPicking.BLOQUEADO:
            raise ValueError(f'Solo se pueden reabrir tareas BLOQUEADAS (estado actual: {tarea.estado})')

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()
        if reg:
            cantidad_faltante = max(0, tarea.cantidad_solicitada - (tarea.cantidad_recogida or 0))
            reg.bloqueado = max(0, reg.bloqueado - cantidad_faltante)

        tarea.estado = EstadoPicking.PENDIENTE
        tarea.operario_id = None
        tarea.cantidad_recogida = 0
        tarea.empaques_escaneados = 0
        tarea.motivo_bloqueo = None
        db.session.commit()
        return tarea

    @staticmethod
    def auditar_tarea(tarea_id: int, admin_id: int, resultado: str,
                      cantidad_hallada: int = 0, ubicacion_hallada: str = None,
                      observaciones: str = None) -> TareaPicking:
        """
        El admin cierra el ciclo de una tarea BLOQUEADA registrando qué encontró físicamente.

        Resultados y su efecto sobre el inventario WMS:
          ENCONTRADO_COMPLETO   → descongela bloqueado, ajusta real si difiere, cancela tarea
          ENCONTRADO_PARCIAL    → descongela, reduce cantidad WMS a lo hallado, cancela tarea
          NO_ENCONTRADO         → descongela, reduce inventario a 0 en esa ubicación, cancela tarea
          AVERIA                → descongela, mueve stock a ubicación AVERIAS, cancela tarea
          DISCREPANCIA_SIESA    → descongela, cancela tarea; registra para ajuste manual en Siesa
        """
        RESULTADOS_VALIDOS = {
            'ENCONTRADO_COMPLETO', 'ENCONTRADO_PARCIAL',
            'NO_ENCONTRADO', 'AVERIA', 'DISCREPANCIA_SIESA'
        }
        if resultado not in RESULTADOS_VALIDOS:
            raise ValueError(f'Resultado inválido: {resultado}')

        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.estado != EstadoPicking.BLOQUEADO:
            raise ValueError(f'Solo se pueden auditar tareas BLOQUEADAS (estado: {tarea.estado})')

        cantidad_faltante = max(0, tarea.cantidad_solicitada - (tarea.cantidad_recogida or 0))

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()

        # 1. Descongelar inventario bloqueado en todos los casos
        if reg:
            reg.bloqueado = max(0, reg.bloqueado - cantidad_faltante)

        # 2. Ajuste de inventario según resultado
        if resultado == 'ENCONTRADO_COMPLETO':
            pass  # stock correcto, no hay ajuste

        elif resultado == 'ENCONTRADO_PARCIAL':
            if reg and cantidad_hallada < cantidad_faltante:
                diferencia = cantidad_faltante - cantidad_hallada
                reg.cantidad = max(0, reg.cantidad - diferencia)
                db.session.add(MovimientoInventario(
                    producto_id=tarea.producto_id,
                    ubicacion_id=tarea.ubicacion_id,
                    tipo='AJUSTE_AUDITORIA',
                    cantidad=-diferencia,
                    motivo=f'Auditoría tarea {tarea.codigo}: hallado {cantidad_hallada} de {tarea.cantidad_solicitada} solicitadas',
                ))

        elif resultado == 'NO_ENCONTRADO':
            if reg:
                db.session.add(MovimientoInventario(
                    producto_id=tarea.producto_id,
                    ubicacion_id=tarea.ubicacion_id,
                    tipo='AJUSTE_AUDITORIA',
                    cantidad=-reg.cantidad,
                    motivo=f'Auditoría tarea {tarea.codigo}: faltante confirmado, unidades no encontradas',
                ))
                reg.cantidad = 0

        elif resultado == 'AVERIA':
            if reg and cantidad_hallada > 0:
                from app.models.ubicacion import Ubicacion
                averia_ub = Ubicacion.query.filter_by(
                    almacen_id=tarea.almacen_id, tipo_zona='AVERIAS'
                ).first()
                if averia_ub:
                    reg_averia = UbicacionProducto.query.filter_by(
                        ubicacion_id=averia_ub.id, producto_id=tarea.producto_id
                    ).first()
                    if reg_averia:
                        reg_averia.cantidad += cantidad_hallada
                    else:
                        db.session.add(UbicacionProducto(
                            ubicacion_id=averia_ub.id,
                            producto_id=tarea.producto_id,
                            cantidad=cantidad_hallada,
                        ))
                reg.cantidad = max(0, reg.cantidad - cantidad_hallada)
                db.session.add(MovimientoInventario(
                    producto_id=tarea.producto_id,
                    ubicacion_id=tarea.ubicacion_id,
                    tipo='AJUSTE_AUDITORIA',
                    cantidad=-cantidad_hallada,
                    motivo=f'Auditoría tarea {tarea.codigo}: mercancía averiada trasladada a zona AVERIAS',
                ))

        elif resultado == 'DISCREPANCIA_SIESA':
            pass  # admin ajustará manualmente en Siesa; solo registramos

        # 3. Registrar auditoría y cancelar tarea
        tarea.auditoria_resultado         = resultado
        tarea.auditoria_cantidad_hallada  = cantidad_hallada
        tarea.auditoria_ubicacion_hallada = (ubicacion_hallada or '').strip() or None
        tarea.auditoria_observaciones     = (observaciones or '').strip() or None
        tarea.auditoria_resuelta_por_id   = admin_id
        tarea.fecha_auditoria             = datetime.utcnow()
        tarea.estado                      = EstadoPicking.CANCELADO
        tarea.operario_id                 = None

        db.session.commit()
        return tarea

    @staticmethod
    def reportar_problema(tarea_id: int, operario_id: int, motivo: str,
                           cantidad_encontrada: int = 0, observaciones: str = None) -> dict:
        """
        Lógica compartida entre /picking/<id>/reportar-problema y /mobile/reportar-problema.
        Bloquea la tarea, registra short-pick si aplica, genera auditoría urgente.
        """
        from app.models.inventario import UbicacionProducto, MovimientoInventario
        from app.services.conteo_service import ConteoService
        from datetime import datetime as _dt

        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError(f'Tarea picking {tarea_id} no encontrada')
        if tarea.operario_id != operario_id:
            raise PermissionError('Esta tarea no te pertenece')
        if tarea.estado not in (EstadoPicking.EN_PROCESO, EstadoPicking.PENDIENTE):
            raise ValueError(
                f'Solo se puede reportar problema en tareas EN_PROCESO o PENDIENTE '
                f'(estado actual: {tarea.estado})'
            )

        cantidad_faltante = max(0, tarea.cantidad_solicitada - cantidad_encontrada)

        inv = (UbicacionProducto.query
               .filter_by(ubicacion_id=tarea.ubicacion_id, producto_id=tarea.producto_id)
               .with_for_update().first())

        if inv and cantidad_faltante > 0:
            inv.bloqueado = inv.bloqueado + cantidad_faltante

        if cantidad_encontrada > 0:
            tarea.cantidad_recogida = cantidad_encontrada
            if inv:
                inv.cantidad = max(0, inv.cantidad - cantidad_encontrada)
                inv.reservado = max(0, inv.reservado - cantidad_encontrada)
            db.session.add(MovimientoInventario(
                producto_id=tarea.producto_id,
                ubicacion_id=tarea.ubicacion_id,
                almacen_id=tarea.almacen_id,
                tipo='SHORT_PICK',
                cantidad=cantidad_encontrada,
                motivo=f'Short-pick — tarea {tarea.codigo} — faltó {cantidad_faltante}',
                numero_documento=tarea.referencia_documento,
                usuario_id=operario_id,
                idempotency_key=f'SP-{tarea.id}-{int(_dt.utcnow().timestamp()*1000)}',
            ))

        tarea.estado = EstadoPicking.BLOQUEADO
        tarea.operario_id = None
        tarea.motivo_bloqueo = motivo
        tarea.observaciones_bloqueo = observaciones

        auditoria_id = None
        if cantidad_faltante > 0:
            sesion = ConteoService.generar_auditoria_por_excepcion(
                tarea_picking_id=tarea.id,
                ubicacion_id=tarea.ubicacion_id,
                producto_id=tarea.producto_id,
                almacen_id=tarea.almacen_id,
            )
            sesion.motivo_codigo = motivo
            auditoria_id = sesion.id

        # Capturar referencia antes del commit
        _ref_doc_rp = tarea.referencia_documento

        db.session.commit()

        # Auto-sync packing asociado — refleja el short-pick en cantidad_esperada del packing
        try:
            from app.models.packing import TareaPacking as _TPSyncRP
            from app.services.packing_picking_sync_service import PackingPickingSyncService as _PPSyncRP
            if _ref_doc_rp:
                _sync_pk_rp = _TPSyncRP.query.filter(
                    _TPSyncRP.numero_pedido_siesa == _ref_doc_rp,
                    _TPSyncRP.estado.in_(['PENDIENTE', 'EN_PROCESO'])
                ).first()
                if _sync_pk_rp:
                    _PPSyncRP.sincronizar(_sync_pk_rp.id)
        except Exception as _e_sync_rp:
            logger.warning('[PICKING] Sync packing (reportar_problema) falló: %s', _e_sync_rp)

        return {
            'ok': True,
            'mensaje': 'Problema reportado — el jefe de almacén lo resolverá',
            'motivo': motivo,
            'tarea_id': tarea_id,
            'cantidad_encontrada': cantidad_encontrada,
            'cantidad_faltante': cantidad_faltante,
            'auditoria_id': auditoria_id,
        }