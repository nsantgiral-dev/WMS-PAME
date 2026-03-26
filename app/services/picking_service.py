"""
Servicio de Picking con lógica FEFO
(First Expired First Out — primero vence, primero sale)
"""
from datetime import datetime
import uuid
from app.extensions import db
from app.models.picking import TareaPicking
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
                estado='PENDIENTE',
                prioridad=prioridad,
                referencia_documento=referencia_documento,
                tipo_documento=tipo_documento
            )

            # Reservar el stock
            reg = UbicacionProducto.query.filter_by(
                ubicacion_id=asig['ubicacion_id'],
                producto_id=producto_id
            ).first()
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
        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado == 'COMPLETADO':
            raise ValueError('Tarea ya completada')

        if tarea.estado == 'CANCELADO':
            raise ValueError('Tarea cancelada')

        if cantidad_recogida > tarea.cantidad_solicitada:
            raise ValueError('Cantidad recogida supera la solicitada')

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).first()

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
            idempotency_key=f'PICK-{tarea.id}-{uuid.uuid4()}'
        )

        # Actualizar tarea
        tarea.cantidad_recogida = cantidad_recogida
        tarea.estado = 'COMPLETADO'
        tarea.fecha_completado = datetime.utcnow()
        if not tarea.fecha_inicio:
            tarea.fecha_inicio = datetime.utcnow()

        db.session.add(movimiento)
        db.session.commit()

        return tarea

    @staticmethod
    def iniciar_picking(tarea_id: int, operario_id: int):
        """Marca la tarea como en proceso."""
        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado != 'PENDIENTE':
            raise ValueError(f'No se puede iniciar una tarea en estado {tarea.estado}')

        tarea.estado = 'EN_PROCESO'
        tarea.operario_id = operario_id
        tarea.fecha_inicio = datetime.utcnow()
        db.session.commit()
        return tarea

    @staticmethod
    def cancelar_picking(tarea_id: int, motivo: str = None):
        """Cancela una tarea y libera la reserva."""
        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado == 'COMPLETADO':
            raise ValueError('No se puede cancelar una tarea completada')

        # Liberar reserva
        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).first()
        if reg:
            reg.reservado = max(0, reg.reservado - tarea.cantidad_solicitada)

        tarea.estado = 'CANCELADO'
        db.session.commit()
        return tarea