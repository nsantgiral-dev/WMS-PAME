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


def _tendencia_7d():
    """Actividad diaria de los últimos 7 días para gráfica de tendencias.
    4 queries GROUP BY en lugar de 28 queries individuales.
    """
    from app.models.traslado import SolicitudTraslado
    from app.models.ruta_despacho import RutaDespacho
    from app.extensions import db
    from sqlalchemy import func, cast, Date

    hoy = datetime.utcnow().date()
    inicio_ventana = datetime.combine(hoy - timedelta(days=6), datetime.min.time())

    # Una query por modelo, agrupada por día
    picking_rows = (
        db.session.query(cast(TareaPicking.fecha_completado, Date), func.count(TareaPicking.id))
        .filter(TareaPicking.estado == 'COMPLETADO', TareaPicking.fecha_completado >= inicio_ventana)
        .group_by(cast(TareaPicking.fecha_completado, Date)).all()
    )
    conteo_rows = (
        db.session.query(cast(SesionConteo.fecha_cierre, Date), func.count(SesionConteo.id))
        .filter(SesionConteo.estado.in_(['MATCH', 'AJUSTADO']), SesionConteo.fecha_cierre >= inicio_ventana)
        .group_by(cast(SesionConteo.fecha_cierre, Date)).all()
    )
    traslado_rows = (
        db.session.query(cast(SolicitudTraslado.fecha_entrega, Date), func.count(SolicitudTraslado.id))
        .filter(SolicitudTraslado.estado == 'ENTREGADA', SolicitudTraslado.fecha_entrega >= inicio_ventana)
        .group_by(cast(SolicitudTraslado.fecha_entrega, Date)).all()
    )
    ruta_rows = (
        db.session.query(cast(RutaDespacho.fecha_entregada, Date), func.count(RutaDespacho.id))
        .filter(RutaDespacho.estado == 'ENTREGADA', RutaDespacho.fecha_entregada >= inicio_ventana)
        .group_by(cast(RutaDespacho.fecha_entregada, Date)).all()
    )

    picking_map   = {str(d): c for d, c in picking_rows}
    conteo_map    = {str(d): c for d, c in conteo_rows}
    traslado_map  = {str(d): c for d, c in traslado_rows}
    ruta_map      = {str(d): c for d, c in ruta_rows}

    dias = []
    for i in range(6, -1, -1):
        dia = hoy - timedelta(days=i)
        key = str(dia)
        dias.append({
            'fecha':     dia.strftime('%d/%m'),
            'picking':   picking_map.get(key, 0),
            'conteos':   conteo_map.get(key, 0),
            'traslados': traslado_map.get(key, 0),
            'rutas':     ruta_map.get(key, 0),
        })
    return dias


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

        operario_ids = [o.id for o in operarios]

        # [28] Consolidar los 4 COUNT queries por operario en queries batch con GROUP BY
        hoy = datetime.utcnow().date()

        pickings_por_op = {
            row.operario_id: row.cnt
            for row in db.session.query(
                TareaPicking.operario_id,
                func.count(TareaPicking.id).label('cnt')
            ).filter(
                TareaPicking.operario_id.in_(operario_ids),
                TareaPicking.estado == 'COMPLETADO',
                TareaPicking.fecha_completado >= fecha_inicio
            ).group_by(TareaPicking.operario_id).all()
        }

        packings_por_op = {
            row.empacador_id: row.cnt
            for row in db.session.query(
                TareaPacking.empacador_id,
                func.count(TareaPacking.id).label('cnt')
            ).filter(
                TareaPacking.empacador_id.in_(operario_ids),
                TareaPacking.estado == 'VERIFICADO',
                TareaPacking.fecha_verificado >= fecha_inicio
            ).group_by(TareaPacking.empacador_id).all()
        }

        conteos_por_op = {
            row.operario_id: row.cnt
            for row in db.session.query(
                SesionConteo.operario_id,
                func.count(SesionConteo.id).label('cnt')
            ).filter(
                SesionConteo.operario_id.in_(operario_ids),
                SesionConteo.estado.in_(['MATCH', 'AJUSTADO']),
                SesionConteo.fecha_cierre >= fecha_inicio
            ).group_by(SesionConteo.operario_id).all()
        }

        conteos_hoy_por_op = {
            row.operario_id: row.cnt
            for row in db.session.query(
                SesionConteo.operario_id,
                func.count(SesionConteo.id).label('cnt')
            ).filter(
                SesionConteo.operario_id.in_(operario_ids),
                db.func.date(SesionConteo.fecha_inicio) == hoy
            ).group_by(SesionConteo.operario_id).all()
        }

        resultado = []
        for operario in operarios:
            pickings = pickings_por_op.get(operario.id, 0)
            packings = packings_por_op.get(operario.id, 0)
            conteos = conteos_por_op.get(operario.id, 0)
            conteos_hoy = conteos_hoy_por_op.get(operario.id, 0)
            resultado.append({
                'operario_id': operario.id,
                'nombre': operario.nombre,
                'rol': operario.rol,
                'pickings_completados': pickings,
                'packings_completados': packings,
                'conteos_completados': conteos,
                'conteos_hoy': conteos_hoy,
                'capacidad_diaria_conteo': operario.capacidad_diaria_conteo if operario.capacidad_diaria_conteo is not None else 15,
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

        # [07] Incluir stock_total en el SELECT principal para evitar N+1 por producto
        productos_alerta = db.session.query(Producto, stock_sub.c.stock_total).join(
            stock_sub,
            Producto.id == stock_sub.c.producto_id
        ).filter(
            Producto.activo == True,
            Producto.stock_minimo > 0,
            stock_sub.c.stock_total <= Producto.stock_minimo
        ).all()

        alertas = []
        for p, stock_actual in productos_alerta:
            stock_actual = stock_actual or 0
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

    @staticmethod
    def kpis_traslados_rutas():
        """KPIs de traslados y rutas para el resumen completo del dashboard."""
        from datetime import datetime, timedelta
        from app.models.traslado import SolicitudTraslado
        from app.models.ruta_despacho import RutaDespacho

        hoy = datetime.utcnow().date()
        inicio_hoy = datetime.combine(hoy, datetime.min.time())

        traslados = {
            'en_picking':     SolicitudTraslado.query.filter_by(estado='EN_PICKING').count(),
            'preparado':      SolicitudTraslado.query.filter_by(estado='PREPARADO').count(),
            'en_transito':    SolicitudTraslado.query.filter_by(estado='EN_TRANSITO').count(),
            'entregadas_hoy': SolicitudTraslado.query.filter(
                SolicitudTraslado.estado == 'ENTREGADA',
                SolicitudTraslado.fecha_entrega >= inicio_hoy
            ).count(),
        }
        traslados['total_activos'] = (
            traslados['en_picking'] + traslados['preparado'] + traslados['en_transito']
        )

        rutas = {
            'en_cargue':      RutaDespacho.query.filter_by(estado='EN_CARGUE').count(),
            'en_transito':    RutaDespacho.query.filter_by(estado='EN_TRANSITO').count(),
            'entregadas_hoy': RutaDespacho.query.filter(
                RutaDespacho.estado == 'ENTREGADA',
                RutaDespacho.fecha_entregada >= inicio_hoy
            ).count(),
        }

        return {'traslados': traslados, 'rutas': rutas}
