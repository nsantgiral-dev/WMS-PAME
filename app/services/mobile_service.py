"""
Servicio Mobile — API optimizada para tablets y celulares de operarios.
Soporta escaneo por cámara (ahora) y láser Bluetooth (después).
"""
import logging
from datetime import datetime
from app.models.picking import TareaPicking
from app.models.packing import TareaPacking, ItemPacking
from app.models.conteo import SesionConteo
from app.models.producto import Producto
from app.services.picking_service import PickingService
from app.services.packing_service import PackingService
from app.services.conteo_service import ConteoService

logger = logging.getLogger(__name__)


class MobileService:

    @staticmethod
    def get_tareas_operario(operario_id: int):
        """Todas las tareas activas del operario."""
        pickings = TareaPicking.query.filter(
            TareaPicking.operario_id == operario_id,
            TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        ).order_by(TareaPicking.prioridad.desc()).all()

        packings = TareaPacking.query.filter(
            TareaPacking.empacador_id == operario_id,
            TareaPacking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        ).all()

        conteos = SesionConteo.query.filter(
            SesionConteo.operario_id == operario_id,
            SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        ).all()

        tareas = []

        for p in pickings:
            tareas.append({
                'id': p.id,
                'tipo': 'PICKING',
                'prioridad': p.prioridad,
                'ubicacion': p.ubicacion.codigo if p.ubicacion else '',
                'producto_codigo': p.producto.codigo if p.producto else '',
                'producto_nombre': p.producto.nombre if p.producto else '',
                'cantidad_requerida': p.cantidad_solicitada,
                'cantidad_escaneada': p.cantidad_recogida,
                'estado': p.estado,
                'referencia': p.referencia_documento,
                'lote': p.lote
            })

        for pk in packings:
            for item in pk.items:
                if not item.verificado:
                    tareas.append({
                        'id': pk.id,
                        'tipo': 'PACKING',
                        'prioridad': 2,
                        'ubicacion': 'ZONA PACKING',
                        'producto_codigo': item.producto.codigo if item.producto else '',
                        'producto_nombre': item.producto.nombre if item.producto else '',
                        'cantidad_requerida': item.cantidad_esperada,
                        'cantidad_escaneada': item.cantidad_real,
                        'estado': pk.estado,
                        'referencia': pk.numero_pedido_siesa,
                        'item_packing_id': item.id
                    })

        for c in conteos:
            tareas.append({
                'id': c.id,
                'tipo': 'CONTEO',
                'prioridad': 1,
                'ubicacion': c.ubicacion.codigo if c.ubicacion else '',
                'producto_codigo': c.producto.codigo if c.producto else '',
                'producto_nombre': c.producto.nombre if c.producto else '',
                'cantidad_requerida': None,
                'cantidad_escaneada': 0,
                'estado': c.estado,
                'referencia': c.codigo,
                'maneja_lote': c.maneja_lote
            })

        tareas.sort(key=lambda x: x['prioridad'], reverse=True)

        return {
            'operario_id': operario_id,
            'total_tareas': len(tareas),
            'tareas': tareas
        }

    @staticmethod
    def get_tarea_actual(operario_id: int):
        """La tarea más prioritaria del operario."""
        resultado = MobileService.get_tareas_operario(operario_id)
        tareas = resultado['tareas']
        if not tareas:
            return None
        return tareas[0]

    @staticmethod
    def procesar_escaneo(operario_id: int, tarea_id: int,
                         tipo: str, codigo: str, cantidad: int = 1):
        """
        Procesa un escaneo — funciona con cámara o láser Bluetooth.
        Valida que el código escaneado corresponde al producto de la tarea.
        """
        if tipo == 'PICKING':
            tarea = TareaPicking.query.get(tarea_id)
            if not tarea:
                raise ValueError('Tarea no encontrada')

            # Verificar que el código escaneado es el producto correcto
            producto = tarea.producto
            if not producto:
                raise ValueError('Producto no encontrado en la tarea')

            if codigo not in [producto.codigo, producto.codigo_siesa or '']:
                raise ValueError({
                    'tipo': 'PRODUCTO_INCORRECTO',
                    'mensaje': f'Escaneaste {codigo} pero la tarea pide {producto.codigo}',
                    'esperado': producto.codigo,
                    'escaneado': codigo
                })

            nueva_cantidad = tarea.cantidad_recogida + cantidad
            if nueva_cantidad > tarea.cantidad_solicitada:
                raise ValueError({
                    'tipo': 'EXCESO',
                    'mensaje': f'Ya tienes {tarea.cantidad_recogida} de {tarea.cantidad_solicitada}',
                    'cantidad_actual': tarea.cantidad_recogida,
                    'cantidad_maxima': tarea.cantidad_solicitada
                })

            tarea.cantidad_recogida = nueva_cantidad
            if tarea.estado == 'PENDIENTE':
                tarea.estado = 'EN_PROCESO'
                tarea.operario_id = operario_id
                tarea.fecha_inicio = datetime.utcnow()

            from app.extensions import db
            db.session.commit()

            completado = nueva_cantidad >= tarea.cantidad_solicitada

            return {
                'exito': True,
                'tipo': 'PICKING',
                'codigo_escaneado': codigo,
                'cantidad_actual': nueva_cantidad,
                'cantidad_requerida': tarea.cantidad_solicitada,
                'completado': completado,
                'puede_confirmar': completado,
                'mensaje': '¡Completo! Presiona confirmar' if completado else f'{nueva_cantidad} de {tarea.cantidad_solicitada}'
            }

        elif tipo == 'PACKING':
            tarea = TareaPacking.query.get(tarea_id)
            if not tarea:
                raise ValueError('Tarea no encontrada')

            # Buscar el ítem por código de producto
            producto = Producto.query.filter(
                Producto.codigo == codigo
            ).first()

            if not producto:
                raise ValueError(f'Producto {codigo} no encontrado')

            item = ItemPacking.query.filter_by(
                tarea_id=tarea_id,
                producto_id=producto.id
            ).first()

            if not item:
                raise ValueError(f'Producto {codigo} no pertenece a este pedido')

            item.cantidad_real += cantidad
            item.verificado = item.cantidad_real >= item.cantidad_esperada

            from app.extensions import db
            db.session.commit()

            todos_verificados = all(i.verificado for i in tarea.items)

            return {
                'exito': True,
                'tipo': 'PACKING',
                'codigo_escaneado': codigo,
                'producto_nombre': producto.nombre,
                'cantidad_actual': item.cantidad_real,
                'cantidad_requerida': item.cantidad_esperada,
                'item_completado': item.verificado,
                'todos_completados': todos_verificados,
                'puede_confirmar': todos_verificados,
                'tiene_diferencia': item.tiene_diferencia(),
                'mensaje': '¡Pedido listo!' if todos_verificados else f'{item.cantidad_real} de {item.cantidad_esperada}'
            }

        elif tipo == 'CONTEO':
            sesion = SesionConteo.query.get(tarea_id)
            if not sesion:
                raise ValueError('Sesión de conteo no encontrada')

            producto = sesion.producto
            if codigo not in [producto.codigo, producto.codigo_siesa or '']:
                raise ValueError(f'Producto incorrecto — escanea {producto.codigo}')

            from app.extensions import db
            if not sesion.cantidad_fisica:
                sesion.cantidad_fisica = 0
            sesion.cantidad_fisica += cantidad
            db.session.commit()

            return {
                'exito': True,
                'tipo': 'CONTEO',
                'codigo_escaneado': codigo,
                'cantidad_contada': sesion.cantidad_fisica,
                'puede_confirmar': True,
                'mensaje': f'Contando: {sesion.cantidad_fisica} unidades'
            }

        raise ValueError(f'Tipo de tarea desconocido: {tipo}')

    @staticmethod
    def confirmar_tarea(operario_id: int, tarea_id: int,
                        tipo: str, items_escaneados: list = None):
        """Confirma la tarea completa."""
        if tipo == 'PICKING':
            tarea = TareaPicking.query.get(tarea_id)
            if not tarea:
                raise ValueError('Tarea no encontrada')
            return PickingService.confirmar_picking(
                tarea_id=tarea_id,
                cantidad_recogida=tarea.cantidad_recogida,
                usuario_id=operario_id
            ).to_dict()

        elif tipo == 'PACKING':
            resultado = PackingService.confirmar_packing(tarea_id=tarea_id)
            return resultado.to_dict()

        elif tipo == 'CONTEO':
            sesion = SesionConteo.query.get(tarea_id)
            if not sesion or sesion.cantidad_fisica is None:
                raise ValueError('No hay cantidad registrada para confirmar')
            return ConteoService.registrar_conteo(
                sesion_id=tarea_id,
                operario_id=operario_id,
                cantidad_fisica=sesion.cantidad_fisica
            )

        raise ValueError(f'Tipo desconocido: {tipo}')