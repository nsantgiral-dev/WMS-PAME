"""
Dashboard Service — KPIs operativos en tiempo real.
Consolida datos de todos los módulos para visibilidad gerencial.
"""
import logging
from datetime import datetime, timedelta
from sqlalchemy import func
from app.extensions import db
from app.models.picking import TareaPicking
from app.models.packing import TareaPacking
from app.models.recepcion import RecepcionMercancia
from app.models.conteo import SesionConteo
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.producto import Producto
from app.models.usuario import Usuario
from app.models.ubicacion import Ubicacion
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


class DashboardService:

    @staticmethod
    def kpis_operativos(almacen_id: int):
        """KPIs principales del almacén en tiempo real."""
        hoy = datetime.utcnow().date()
        inicio_hoy = datetime.combine(hoy, datetime.min.time())

        # --- PICKING ---
        picking_pendiente = TareaPicking.query.filter_by(
            almacen_id=almacen_id, estado='PENDIENTE'
        ).count()

        picking_en_proceso = TareaPicking.query.filter_by(
            almacen_id=almacen_id, estado='EN_PROCESO'
        ).count()

        picking_completado_hoy = TareaPicking.query.filter(
            TareaPicking.almacen_id == almacen_id,
            TareaPicking.estado == 'COMPLETADO',
            TareaPicking.fecha_completado >= inicio_hoy
        ).count()

        # --- PACKING ---
        packing_pendiente = TareaPacking.query.filter_by(
            almacen_id=almacen_id, estado='PENDIENTE'
        ).count()

        packing_en_proceso = TareaPacking.query.filter_by(
            almacen_id=almacen_id, estado='EN_PROCESO'
        ).count()

        packing_completado_hoy = TareaPacking.query.filter(
            TareaPacking.almacen_id == almacen_id,
            TareaPacking.estado == 'VERIFICADO',
            TareaPacking.fecha_verificado >= inicio_hoy
        ).count()

        siesa_triggers_hoy = TareaPacking.query.filter(
            TareaPacking.almacen_id == almacen_id,
            TareaPacking.siesa_triggered == True,
            TareaPacking.siesa_triggered_at >= inicio_hoy
        ).count()

        # --- RECEPCIÓN ---
        recepciones_hoy = RecepcionMercancia.query.filter(
            RecepcionMercancia.almacen_id == almacen_id,
            RecepcionMercancia.fecha_confirmacion >= inicio_hoy
        ).count()

        # --- CONTEO CÍCLICO ---
        conteos_pendientes = SesionConteo.query.filter_by(
            almacen_id=almacen_id, estado='PENDIENTE'
        ).count()

        conteos_descuadre = SesionConteo.query.filter_by(
            almacen_id=almacen_id, estado='SEGUNDO_CONTEO'
        ).count()

        conteos_match_hoy = SesionConteo.query.filter(
            SesionConteo.almacen_id == almacen_id,
            SesionConteo.estado == 'MATCH',
            SesionConteo.fecha_cierre >= inicio_hoy
        ).count()

        # --- ALERTAS DE STOCK — misma lógica que alertas_stock() ---
        stock_sub = db.session.query(
            UbicacionProducto.producto_id,
            func.sum(UbicacionProducto.cantidad).label('stock_total')
        ).join(Ubicacion).filter(
            Ubicacion.almacen_id == almacen_id
        ).group_by(UbicacionProducto.producto_id).subquery()

        productos_bajo_minimo = db.session.query(
            func.count(Producto.id)
        ).join(
            stock_sub,
            Producto.id == stock_sub.c.producto_id
        ).filter(
            Producto.activo == True,
            Producto.stock_minimo > 0,
            stock_sub.c.stock_total <= Producto.stock_minimo
        ).scalar() or 0

        return {
            'fecha': hoy.isoformat(),
            'almacen_id': almacen_id,
            'picking': {
                'pendiente': picking_pendiente,
                'en_proceso': picking_en_proceso,
                'completado_hoy': picking_completado_hoy,
                'total_activo': picking_pendiente + picking_en_proceso
            },
            'packing': {
                'pendiente': packing_pendiente,
                'en_proceso': packing_en_proceso,
                'completado_hoy': packing_completado_hoy,
                'facturas_generadas_hoy': siesa_triggers_hoy
            },
            'recepcion': {
                'confirmadas_hoy': recepciones_hoy
            },
            'conteo': {
                'pendientes': conteos_pendientes,
                'en_descuadre': conteos_descuadre,
                'match_hoy': conteos_match_hoy
            },
            'alertas': {
                'productos_bajo_minimo': productos_bajo_minimo,
                'conteos_descuadre': conteos_descuadre
            },
            'connekta': connekta.estado()
        }

    @staticmethod
    def productividad_operarios(almacen_id: int, dias: int = 7):
        """Productividad por operario en los últimos N días."""
        fecha_inicio = datetime.utcnow() - timedelta(days=dias)

        operarios = Usuario.query.filter(
            Usuario.almacen_id == almacen_id,
            Usuario.activo == True
        ).all()

        resultado = []
        for operario in operarios:
            pickings = TareaPicking.query.filter(
                TareaPicking.operario_id == operario.id,
                TareaPicking.estado == 'COMPLETADO',
                TareaPicking.fecha_completado >= fecha_inicio
            ).count()

            packings = TareaPacking.query.filter(
                TareaPacking.empacador_id == operario.id,
                TareaPacking.estado == 'VERIFICADO',
                TareaPacking.fecha_verificado >= fecha_inicio
            ).count()

            conteos = SesionConteo.query.filter(
                SesionConteo.operario_id == operario.id,
                SesionConteo.estado.in_(['MATCH', 'AJUSTADO']),
                SesionConteo.fecha_cierre >= fecha_inicio
            ).count()

            resultado.append({
                'operario_id': operario.id,
                'nombre': operario.nombre,
                'rol': operario.rol,
                'pickings_completados': pickings,
                'packings_completados': packings,
                'conteos_completados': conteos,
                'total_tareas': pickings + packings + conteos
            })

        resultado.sort(key=lambda x: x['total_tareas'], reverse=True)

        return {
            'periodo_dias': dias,
            'almacen_id': almacen_id,
            'operarios': resultado
        }

    @staticmethod
    def movimientos_recientes(almacen_id: int, limite: int = 20):
        """Últimos movimientos de inventario del almacén."""
        movimientos = MovimientoInventario.query.filter_by(
            almacen_id=almacen_id
        ).order_by(
            MovimientoInventario.fecha.desc()
        ).limit(limite).all()

        return {
            'movimientos': [m.to_dict() for m in movimientos],
            'total': len(movimientos)
        }

    @staticmethod
    def alertas_stock(almacen_id: int):
        """Productos bajo mínimo o sin stock."""
        stock_sub = db.session.query(
            UbicacionProducto.producto_id,
            func.sum(UbicacionProducto.cantidad).label('stock_total')
        ).join(Ubicacion).filter(
            Ubicacion.almacen_id == almacen_id
        ).group_by(UbicacionProducto.producto_id).subquery()

        productos_alerta = db.session.query(Producto).join(
            stock_sub,
            Producto.id == stock_sub.c.producto_id
        ).filter(
            Producto.activo == True,
            Producto.stock_minimo > 0,
            stock_sub.c.stock_total <= Producto.stock_minimo
        ).all()

        alertas = []
        for p in productos_alerta:
            stock_actual = db.session.query(
                func.sum(UbicacionProducto.cantidad)
            ).join(Ubicacion).filter(
                UbicacionProducto.producto_id == p.id,
                Ubicacion.almacen_id == almacen_id
            ).scalar() or 0

            alertas.append({
                'producto_id': p.id,
                'codigo': p.codigo,
                'nombre': p.nombre,
                'clasificacion_abc': p.clasificacion_abc,
                'stock_actual': stock_actual,
                'stock_minimo': p.stock_minimo,
                'punto_pedido': p.punto_pedido,
                'urgencia': 'CRITICO' if stock_actual == 0 else 'BAJO'
            })

        alertas.sort(key=lambda x: (
            0 if x['urgencia'] == 'CRITICO' else 1,
            x['clasificacion_abc'] or 'Z'
        ))

        return {
            'almacen_id': almacen_id,
            'total_alertas': len(alertas),
            'alertas': alertas
        }
