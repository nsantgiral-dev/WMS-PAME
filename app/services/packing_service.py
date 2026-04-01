"""
Servicio de Packing con verificación ítem por ítem y trigger automático a Siesa.
Flujo: Crear → Iniciar → Escanear ítems → Confirmar → Siesa factura solo.
"""
import uuid
import json
from datetime import datetime
from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.models.picking import TareaPicking
from app.services.connekta_gateway import connekta
import logging

logger = logging.getLogger(__name__)


class PackingService:

    @staticmethod
    def crear_desde_picking(tareas_picking_ids: list, numero_pedido_siesa: str, almacen_id: int,
                            tipo_docto_pedido_siesa: str = '', consec_docto_pedido_siesa: str = ''):
        """
        Crea una tarea de packing a partir de tareas de picking completadas.
        Agrupa todos los ítems del pedido en una sola tarea de packing.
        """
        tareas_picking = TareaPicking.query.filter(
            TareaPicking.id.in_(tareas_picking_ids),
            TareaPicking.estado == 'COMPLETADO'
        ).all()

        if not tareas_picking:
            raise ValueError('No hay tareas de picking completadas para empacar')

        # Verificar que no exista ya un packing para este pedido
        existente = TareaPacking.query.filter_by(
            numero_pedido_siesa=numero_pedido_siesa
        ).filter(TareaPacking.estado.notin_(['CANCELADO'])).first()

        if existente:
            raise ValueError(f'Ya existe una tarea de packing para el pedido {numero_pedido_siesa}')

        codigo = f'PACK-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        tarea = TareaPacking(
            codigo=codigo,
            numero_pedido_siesa=numero_pedido_siesa,
            tipo_docto_pedido_siesa=tipo_docto_pedido_siesa,
            consec_docto_pedido_siesa=consec_docto_pedido_siesa,
            almacen_id=almacen_id,
            estado='PENDIENTE'
        )
        db.session.add(tarea)
        db.session.flush()

        # Agrupar ítems por producto
        items_por_producto = {}
        for tp in tareas_picking:
            pid = tp.producto_id
            if pid not in items_por_producto:
                items_por_producto[pid] = {
                    'cantidad': 0,
                    'lote': tp.lote
                }
            items_por_producto[pid]['cantidad'] += tp.cantidad_recogida

        for producto_id, datos in items_por_producto.items():
            item = ItemPacking(
                tarea_id=tarea.id,
                producto_id=producto_id,
                cantidad_esperada=datos['cantidad'],
                lote=datos['lote']
            )
            db.session.add(item)

        db.session.commit()
        return tarea

    @staticmethod
    def crear_manual(numero_pedido_siesa: str, almacen_id: int, items: list,
                     tipo_docto_pedido_siesa: str = '', consec_docto_pedido_siesa: str = ''):
        """
        Crea una tarea de packing manualmente con lista de ítems.
        Útil cuando el pedido viene directo de Siesa sin picking previo.
        items: [{'producto_id': int, 'cantidad': int, 'lote': str}]
        """
        existente = TareaPacking.query.filter_by(
            numero_pedido_siesa=numero_pedido_siesa
        ).filter(TareaPacking.estado.notin_(['CANCELADO'])).first()

        if existente:
            raise ValueError(f'Ya existe una tarea de packing para el pedido {numero_pedido_siesa}')

        codigo = f'PACK-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        tarea = TareaPacking(
            codigo=codigo,
            numero_pedido_siesa=numero_pedido_siesa,
            tipo_docto_pedido_siesa=tipo_docto_pedido_siesa,
            consec_docto_pedido_siesa=consec_docto_pedido_siesa,
            almacen_id=almacen_id,
            estado='PENDIENTE'
        )
        db.session.add(tarea)
        db.session.flush()

        for item_data in items:
            item = ItemPacking(
                tarea_id=tarea.id,
                producto_id=item_data['producto_id'],
                cantidad_esperada=item_data['cantidad'],
                lote=item_data.get('lote')
            )
            db.session.add(item)

        db.session.commit()
        return tarea

    @staticmethod
    def iniciar(tarea_id: int, empacador_id: int):
        """Empacador toma la tarea."""
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.estado != 'PENDIENTE':
            raise ValueError(f'No se puede iniciar una tarea en estado {tarea.estado}')

        tarea.estado = 'EN_PROCESO'
        tarea.empacador_id = empacador_id
        tarea.fecha_inicio = datetime.utcnow()
        db.session.commit()
        return tarea

    @staticmethod
    def escanear_item(tarea_id: int, producto_id: int, cantidad_real: int, lote: str = None):
        """
        El empacador escanea un ítem y registra la cantidad real.
        Este es el corazón del proceso de verificación.
        """
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.estado not in ['EN_PROCESO', 'PENDIENTE']:
            raise ValueError(f'No se puede escanear en estado {tarea.estado}')

        item = ItemPacking.query.filter_by(
            tarea_id=tarea_id,
            producto_id=producto_id
        ).first()

        if not item:
            raise ValueError(f'Producto {producto_id} no pertenece a esta tarea de packing')

        item.cantidad_real = cantidad_real
        item.verificado = True
        if lote:
            item.lote = lote

        # Alerta inmediata si hay diferencia
        alerta = None
        if item.tiene_diferencia():
            diferencia = item.diferencia()
            if diferencia > 0:
                alerta = f'SOBRANTE: hay {diferencia} unidad(es) de más de {item.producto.nombre}'
            else:
                alerta = f'FALTANTE: faltan {abs(diferencia)} unidad(es) de {item.producto.nombre}'
            logger.warning(f'[PACKING] {alerta} en tarea {tarea.codigo}')

        db.session.commit()

        return {
            'item': item.to_dict(),
            'alerta': alerta,
            'items_pendientes': sum(1 for i in tarea.items if not i.verificado)
        }

    @staticmethod
    def confirmar_packing(tarea_id: int, observaciones: str = None, forzar: bool = False):
        """
        Confirma el packing y dispara Siesa automáticamente.
        Si hay diferencias, requiere forzar=True para continuar.
        ESTE ES EL MOMENTO EN QUE SIESA FACTURA AUTOMÁTICAMENTE.
        """
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')

        # VERIFICADO sin siesa_triggered = intento previo donde Siesa falló → permitir reintento
        if tarea.estado == 'VERIFICADO' and not tarea.siesa_triggered:
            pass  # reintento del trigger Siesa
        elif tarea.estado not in ['EN_PROCESO', 'PENDIENTE']:
            raise ValueError(f'No se puede confirmar en estado {tarea.estado}')

        # Verificar que todos los ítems fueron escaneados
        items_sin_verificar = [i for i in tarea.items if not i.verificado]
        if items_sin_verificar:
            nombres = [i.producto.nombre for i in items_sin_verificar[:3]]
            raise ValueError(f'Faltan por escanear: {", ".join(nombres)}')

        # Verificar diferencias
        items_con_diferencia = [i for i in tarea.items if i.tiene_diferencia()]
        if items_con_diferencia and not forzar:
            diferencias = [{
                'producto': i.producto.nombre,
                'esperado': i.cantidad_esperada,
                'real': i.cantidad_real,
                'diferencia': i.diferencia()
            } for i in items_con_diferencia]
            raise ValueError({
                'mensaje': 'Hay diferencias en el packing. Usa forzar=true para confirmar de todas formas.',
                'diferencias': diferencias
            })

        tarea.verificacion_exitosa = not bool(items_con_diferencia)
        tarea.observaciones = observaciones
        tarea.estado = 'VERIFICADO'
        tarea.fecha_verificado = datetime.utcnow()

        # Construir payload para Siesa
        items_payload = [{
            'producto_codigo': i.producto.codigo_siesa or i.producto.codigo,
            'cantidad_empacada': i.cantidad_real,
            'cantidad_pedida': i.cantidad_esperada,
            'lote': i.lote
        } for i in tarea.items]

        # TRIGGER A SIESA — aquí Siesa genera remisión + factura
        try:
            respuesta_siesa = connekta.trigger_despacho(
                tipo_docto_pedido=tarea.tipo_docto_pedido_siesa or '',
                consec_docto_pedido=tarea.consec_docto_pedido_siesa or tarea.numero_pedido_siesa,
                items=items_payload
            )
            tarea.siesa_triggered = True
            tarea.siesa_response = json.dumps(respuesta_siesa)
            tarea.siesa_triggered_at = datetime.utcnow()
            logger.info(f'[PACKING] Siesa triggered exitosamente para pedido {tarea.numero_pedido_siesa}')

        except Exception as e:
            logger.error(f'[PACKING] Error triggering Siesa: {str(e)}')
            tarea.siesa_triggered = False
            tarea.siesa_response = str(e)
            # No commit aquí — dejar estado VERIFICADO en sesión sin persistir el error
            # para que el empacador pueda reintentar "Cerrar Caja"
            db.session.rollback()
            raise Exception(f'Packing verificado pero error al comunicar con Siesa: {str(e)}')

        db.session.commit()
        return tarea

    @staticmethod
    def cancelar(tarea_id: int, motivo: str = None):
        """Cancela una tarea de packing."""
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.estado in ['VERIFICADO', 'DESPACHADO']:
            raise ValueError('No se puede cancelar una tarea ya verificada o despachada')

        tarea.estado = 'CANCELADO'
        tarea.observaciones = motivo
        db.session.commit()
        return tarea