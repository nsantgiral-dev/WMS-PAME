"""
Paneles de Compras — vista de realidad física para el buyer.

4 paneles:
  1. Velocity + ABC    — picks/día por producto, clasificación ABC, alertas A-items
  2. Dock Lock         — rechazos de recepción (excesos, faltantes, sin OC)
  3. Cuarentena/Averías — productos en zona AVERÍAS, días sin gestión
  4. Audit Trail       — búsqueda por OC/proveedor, trazabilidad completa

WMS muestra la verdad física. El buyer ve → va a Siesa a actuar.
"""
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import func, case, desc
from app.extensions import db
from app.routes._auth_helpers import _es_compras, Roles

compras_bp = Blueprint('compras', __name__)


# ──────────────────────────────────────────────────────────────────────────────
# 1. VELOCITY + ABC
# ──────────────────────────────────────────────────────────────────────────────

@compras_bp.route('/velocity', methods=['GET'])
@jwt_required()
def velocity_abc():
    """
    Picks/día por producto en los últimos N días, con clasificación ABC.
    Alerta: items A cuyo consumo > reposición.
    """
    u = _es_compras()
    if not u:
        return jsonify({'error': 'Sin acceso'}), 403

    from app.models.inventario import MovimientoInventario, UbicacionProducto
    from app.models.producto import Producto
    from app.models.producto_clasificacion_abc import ProductoClasificacionABC
    from app.models.ubicacion import Ubicacion

    dias = int(request.args.get('dias', 30))
    almacen_id = request.args.get('almacen_id', type=int)
    desde = datetime.utcnow() - timedelta(days=dias)

    # Picks por producto en el período
    q_picks = (db.session.query(
        MovimientoInventario.producto_id,
        func.count(MovimientoInventario.id).label('total_movimientos'),
        func.sum(MovimientoInventario.cantidad).label('total_unidades'),
    ).filter(
        MovimientoInventario.tipo.in_(['PICKING', 'PICKING_PARCIAL']),
        MovimientoInventario.fecha >= desde,
    ))
    if almacen_id:
        q_picks = q_picks.filter(MovimientoInventario.almacen_id == almacen_id)
    picks = q_picks.group_by(MovimientoInventario.producto_id).all()

    # Reposiciones por producto en el período
    q_repos = (db.session.query(
        MovimientoInventario.producto_id,
        func.sum(MovimientoInventario.cantidad).label('total_repuesto'),
    ).filter(
        MovimientoInventario.tipo == 'REPOSICION',
        MovimientoInventario.fecha >= desde,
    ))
    if almacen_id:
        q_repos = q_repos.filter(MovimientoInventario.almacen_id == almacen_id)
    repos_map = {r.producto_id: r.total_repuesto or 0
                 for r in q_repos.group_by(MovimientoInventario.producto_id).all()}

    # Recepciones (entradas) por producto
    q_entradas = (db.session.query(
        MovimientoInventario.producto_id,
        func.sum(MovimientoInventario.cantidad).label('total_entrada'),
    ).filter(
        MovimientoInventario.tipo == 'RECEPCION',
        MovimientoInventario.fecha >= desde,
    ))
    if almacen_id:
        q_entradas = q_entradas.filter(MovimientoInventario.almacen_id == almacen_id)
    entradas_map = {r.producto_id: r.total_entrada or 0
                    for r in q_entradas.group_by(MovimientoInventario.producto_id).all()}

    # Producto info + ABC
    pids = [p.producto_id for p in picks]
    if not pids:
        return jsonify({'items': [], 'dias': dias, 'alertas_a': 0})

    productos = {p.id: p for p in Producto.query.filter(Producto.id.in_(pids)).all()}

    # ABC por almacén (si disponible)
    abc_map = {}
    if almacen_id:
        abcs = ProductoClasificacionABC.query.filter(
            ProductoClasificacionABC.producto_id.in_(pids),
            ProductoClasificacionABC.almacen_id == almacen_id,
        ).all()
        abc_map = {a.producto_id: a.clasificacion for a in abcs}

    # Stock actual PICKING por producto
    q_stock = (db.session.query(
        UbicacionProducto.producto_id,
        func.sum(UbicacionProducto.cantidad).label('stock_picking'),
    ).join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
     .filter(
        UbicacionProducto.producto_id.in_(pids),
        Ubicacion.tipo_zona == 'PICKING',
    ))
    if almacen_id:
        q_stock = q_stock.filter(Ubicacion.almacen_id == almacen_id)
    stock_map = {r.producto_id: r.stock_picking or 0
                 for r in q_stock.group_by(UbicacionProducto.producto_id).all()}

    items = []
    alertas_a = 0
    for p in picks:
        prod = productos.get(p.producto_id)
        if not prod:
            continue

        abc = abc_map.get(p.producto_id) or prod.clasificacion_abc or '—'
        picks_dia = round((p.total_unidades or 0) / max(dias, 1), 1)
        repuesto = repos_map.get(p.producto_id, 0)
        entrada = entradas_map.get(p.producto_id, 0)
        stock_pick = stock_map.get(p.producto_id, 0)

        # Días de stock restante estimado
        dias_stock = round(stock_pick / picks_dia, 1) if picks_dia > 0 else 999

        # Alerta: item A con consumo > entrada+reposición (se agota sin compra nueva)
        consumo_neto = (p.total_unidades or 0) - entrada - repuesto
        es_alerta = abc == 'A' and consumo_neto > 0 and dias_stock < 15

        if es_alerta:
            alertas_a += 1

        items.append({
            'producto_id': prod.id,
            'codigo': prod.codigo,
            'nombre': prod.nombre,
            'abc': abc,
            'picks_periodo': p.total_unidades or 0,
            'picks_dia': picks_dia,
            'repuesto_periodo': repuesto,
            'entrada_periodo': entrada,
            'stock_picking': stock_pick,
            'dias_stock_estimado': dias_stock,
            'alerta': es_alerta,
        })

    # Ordenar: alertas primero, luego por picks/día desc
    items.sort(key=lambda x: (not x['alerta'], -x['picks_dia']))

    return jsonify({
        'items': items,
        'dias': dias,
        'alertas_a': alertas_a,
        'total_items': len(items),
    })


# ──────────────────────────────────────────────────────────────────────────────
# 2. DOCK LOCK — Rechazos de recepción
# ──────────────────────────────────────────────────────────────────────────────

@compras_bp.route('/dock-lock', methods=['GET'])
@jwt_required()
def dock_lock():
    """
    Recepciones con discrepancias: excesos, faltantes, parciales.
    El buyer ve esto para reclamar al proveedor en Siesa.
    """
    u = _es_compras()
    if not u:
        return jsonify({'error': 'Sin acceso'}), 403

    from app.models.recepcion import RecepcionMercancia, ItemRecepcion
    from app.models.producto import Producto

    dias = int(request.args.get('dias', 30))
    almacen_id = request.args.get('almacen_id', type=int)
    desde = datetime.utcnow() - timedelta(days=dias)

    q = RecepcionMercancia.query.filter(
        RecepcionMercancia.fecha_creacion >= desde,
        RecepcionMercancia.estado == 'CONFIRMADA',
    )
    if almacen_id:
        q = q.filter(RecepcionMercancia.almacen_id == almacen_id)

    recepciones = q.order_by(RecepcionMercancia.fecha_confirmacion.desc()).all()

    resultado = []
    total_excesos = 0
    total_faltantes = 0
    total_recepciones_con_problema = 0

    for rec in recepciones:
        items_problema = []
        tiene_problema = False

        for item in rec.items:
            if item.tipo == 'BONIFICACION':
                continue

            if item.es_exceso() or item.es_faltante():
                tiene_problema = True
                dif = item.diferencia()
                tipo_problema = 'EXCESO' if dif > 0 else 'FALTANTE'

                if dif > 0:
                    total_excesos += 1
                else:
                    total_faltantes += 1

                items_problema.append({
                    'producto_id': item.producto_id,
                    'producto_codigo': item.producto.codigo if item.producto else None,
                    'producto_nombre': item.producto.nombre if item.producto else None,
                    'cantidad_ordenada': item.cantidad_ordenada,
                    'cantidad_recibida': item.cantidad_recibida,
                    'diferencia': dif,
                    'tipo_problema': tipo_problema,
                    'abc': item.producto.clasificacion_abc if item.producto else None,
                })

        if tiene_problema:
            total_recepciones_con_problema += 1
            resultado.append({
                'recepcion_id': rec.id,
                'codigo': rec.codigo,
                'oc_siesa': rec.numero_oc_siesa,
                'proveedor_codigo': rec.proveedor_codigo,
                'proveedor_nombre': rec.proveedor_nombre,
                'fecha_confirmacion': rec.fecha_confirmacion.isoformat() if rec.fecha_confirmacion else None,
                'es_parcial': rec.es_parcial,
                'items_problema': items_problema,
                'total_problemas': len(items_problema),
            })

    return jsonify({
        'recepciones': resultado,
        'dias': dias,
        'total_recepciones_con_problema': total_recepciones_con_problema,
        'total_excesos': total_excesos,
        'total_faltantes': total_faltantes,
    })


# ──────────────────────────────────────────────────────────────────────────────
# 3. CUARENTENA / AVERÍAS
# ──────────────────────────────────────────────────────────────────────────────

@compras_bp.route('/cuarentena', methods=['GET'])
@jwt_required()
def cuarentena():
    """
    Productos en zona AVERÍAS: mercancía dañada pendiente de logística inversa.
    El buyer ve esto para gestionar devolución/crédito con el proveedor en Siesa.
    """
    u = _es_compras()
    if not u:
        return jsonify({'error': 'Sin acceso'}), 403

    from app.models.devolucion import TareaDevolucion
    from app.models.inventario import UbicacionProducto
    from app.models.ubicacion import Ubicacion
    from app.models.producto import Producto

    almacen_id = request.args.get('almacen_id', type=int)
    ahora = datetime.utcnow()

    # 1) Tareas de devolución averiadas (pendientes + completadas recientes)
    q_dev = TareaDevolucion.query.filter(
        TareaDevolucion.es_averiado == True,
    )
    if almacen_id:
        q_dev = q_dev.filter(TareaDevolucion.almacen_id == almacen_id)
    devoluciones = q_dev.order_by(TareaDevolucion.fecha_creacion.desc()).limit(200).all()

    items_dev = []
    pendientes = 0
    for d in devoluciones:
        dias_sin_gestion = (ahora - d.fecha_creacion).days if d.fecha_creacion else 0
        es_pendiente = d.estado in ('PENDIENTE', 'EN_PROCESO')
        if es_pendiente:
            pendientes += 1

        items_dev.append({
            'id': d.id,
            'codigo': d.codigo,
            'producto_id': d.producto_id,
            'producto_codigo': d.producto.codigo if d.producto else None,
            'producto_nombre': d.producto.nombre if d.producto else None,
            'cantidad': d.cantidad_diferencia,
            'estado': d.estado,
            'dias_sin_gestion': dias_sin_gestion,
            'observaciones': d.observaciones,
            'siesa_triggered': d.siesa_triggered,
            'fecha_creacion': d.fecha_creacion.isoformat() if d.fecha_creacion else None,
            'fecha_completado': d.fecha_completado.isoformat() if d.fecha_completado else None,
        })

    # 2) Stock actual en ubicaciones AVERIAS
    q_stock = (db.session.query(
        UbicacionProducto.producto_id,
        func.sum(UbicacionProducto.cantidad).label('cantidad_averiada'),
        Producto.codigo.label('producto_codigo'),
        Producto.nombre.label('producto_nombre'),
    ).join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
     .join(Producto, Producto.id == UbicacionProducto.producto_id)
     .filter(
        Ubicacion.tipo_zona == 'AVERIAS',
        UbicacionProducto.cantidad > 0,
    ))
    if almacen_id:
        q_stock = q_stock.filter(Ubicacion.almacen_id == almacen_id)

    stock_averias = q_stock.group_by(
        UbicacionProducto.producto_id, Producto.codigo, Producto.nombre
    ).all()

    items_stock = [{
        'producto_id': s.producto_id,
        'producto_codigo': s.producto_codigo,
        'producto_nombre': s.producto_nombre,
        'cantidad_averiada': s.cantidad_averiada,
    } for s in stock_averias]

    return jsonify({
        'devoluciones_averiadas': items_dev,
        'stock_en_averias': items_stock,
        'pendientes': pendientes,
        'total_devoluciones': len(items_dev),
        'total_productos_en_averias': len(items_stock),
    })


# ──────────────────────────────────────────────────────────────────────────────
# 4. AUDIT TRAIL — Trazabilidad física
# ──────────────────────────────────────────────────────────────────────────────

@compras_bp.route('/audit-trail', methods=['GET'])
@jwt_required()
def audit_trail():
    """
    Búsqueda por OC o proveedor: quién escaneó qué, cuándo, dónde.
    Evidencia física para disputas con proveedores.
    """
    u = _es_compras()
    if not u:
        return jsonify({'error': 'Sin acceso'}), 403

    from app.models.recepcion import RecepcionMercancia, ItemRecepcion
    from app.models.inventario import MovimientoInventario
    from app.models.producto import Producto
    from app.models.usuario import Usuario

    busqueda = request.args.get('q', '').strip()
    almacen_id = request.args.get('almacen_id', type=int)
    dias = int(request.args.get('dias', 90))
    desde = datetime.utcnow() - timedelta(days=dias)
    limite = int(request.args.get('limite', 50))

    if not busqueda:
        return jsonify({'error': 'Parámetro q requerido (OC, proveedor, o código producto)'}), 400

    # Buscar recepciones que matcheen
    q = RecepcionMercancia.query.filter(
        RecepcionMercancia.fecha_creacion >= desde,
    )
    if almacen_id:
        q = q.filter(RecepcionMercancia.almacen_id == almacen_id)

    # Buscar por OC, proveedor o remisión
    q = q.filter(db.or_(
        RecepcionMercancia.numero_oc_siesa.ilike(f'%{busqueda}%'),
        RecepcionMercancia.proveedor_codigo.ilike(f'%{busqueda}%'),
        RecepcionMercancia.proveedor_nombre.ilike(f'%{busqueda}%'),
        RecepcionMercancia.num_remision_prov.ilike(f'%{busqueda}%'),
        RecepcionMercancia.codigo.ilike(f'%{busqueda}%'),
    ))

    recepciones = q.order_by(RecepcionMercancia.fecha_creacion.desc()).limit(limite).all()

    resultado = []
    for rec in recepciones:
        recepcionista = None
        if rec.recepcionista_id:
            usr = Usuario.query.get(rec.recepcionista_id)
            recepcionista = usr.nombre if usr else None

        items_detalle = []
        for item in rec.items:
            items_detalle.append({
                'producto_codigo': item.producto.codigo if item.producto else None,
                'producto_nombre': item.producto.nombre if item.producto else None,
                'cantidad_ordenada': item.cantidad_ordenada,
                'cantidad_recibida': item.cantidad_recibida,
                'diferencia': item.diferencia(),
                'es_exceso': item.es_exceso(),
                'es_faltante': item.es_faltante(),
                'destino': item.destino,
                'lote': item.lote,
                'tipo': item.tipo,
            })

        resultado.append({
            'recepcion_id': rec.id,
            'codigo': rec.codigo,
            'oc_siesa': rec.numero_oc_siesa,
            'proveedor_codigo': rec.proveedor_codigo,
            'proveedor_nombre': rec.proveedor_nombre,
            'remision': rec.num_remision_prov,
            'estado': rec.estado,
            'recepcionista': recepcionista,
            'es_parcial': rec.es_parcial,
            'tiene_excesos': rec.tiene_excesos,
            'fecha_creacion': rec.fecha_creacion.isoformat() if rec.fecha_creacion else None,
            'fecha_inicio': rec.fecha_inicio.isoformat() if rec.fecha_inicio else None,
            'fecha_confirmacion': rec.fecha_confirmacion.isoformat() if rec.fecha_confirmacion else None,
            'items': items_detalle,
            'total_items': len(items_detalle),
            'siesa_triggered': rec.siesa_triggered,
        })

    return jsonify({
        'resultados': resultado,
        'busqueda': busqueda,
        'total': len(resultado),
    })


# ──────────────────────────────────────────────────────────────────────────────
# RESUMEN — KPIs rápidos para el header del panel compras
# ──────────────────────────────────────────────────────────────────────────────

@compras_bp.route('/resumen', methods=['GET'])
@jwt_required()
def resumen():
    """KPIs de alto nivel para la pantalla principal de compras."""
    u = _es_compras()
    if not u:
        return jsonify({'error': 'Sin acceso'}), 403

    from app.models.inventario import MovimientoInventario
    from app.models.recepcion import RecepcionMercancia, ItemRecepcion
    from app.models.devolucion import TareaDevolucion
    from app.models.picking import TareaPicking

    almacen_id = request.args.get('almacen_id', type=int)
    ahora = datetime.utcnow()
    hace_30d = ahora - timedelta(days=30)
    hace_7d = ahora - timedelta(days=7)

    # Picks últimos 7 días
    q_picks = db.session.query(func.sum(MovimientoInventario.cantidad)).filter(
        MovimientoInventario.tipo.in_(['PICKING', 'PICKING_PARCIAL']),
        MovimientoInventario.fecha >= hace_7d,
    )
    if almacen_id:
        q_picks = q_picks.filter(MovimientoInventario.almacen_id == almacen_id)
    picks_7d = q_picks.scalar() or 0

    # Recepciones con problema últimos 30 días
    q_rec = RecepcionMercancia.query.filter(
        RecepcionMercancia.fecha_creacion >= hace_30d,
        RecepcionMercancia.estado == 'CONFIRMADA',
        db.or_(
            RecepcionMercancia.tiene_excesos == True,
            RecepcionMercancia.es_parcial == True,
        )
    )
    if almacen_id:
        q_rec = q_rec.filter(RecepcionMercancia.almacen_id == almacen_id)
    recepciones_problema = q_rec.count()

    # Averías pendientes
    q_aver = TareaDevolucion.query.filter(
        TareaDevolucion.es_averiado == True,
        TareaDevolucion.estado.in_(['PENDIENTE', 'EN_PROCESO']),
    )
    if almacen_id:
        q_aver = q_aver.filter(TareaDevolucion.almacen_id == almacen_id)
    averias_pendientes = q_aver.count()

    # Auditorías picking con resultado AVERIA o DISCREPANCIA
    q_aud = TareaPicking.query.filter(
        TareaPicking.auditoria_resultado.in_(['AVERIA', 'DISCREPANCIA_SIESA']),
        TareaPicking.fecha_auditoria >= hace_30d,
    )
    if almacen_id:
        q_aud = q_aud.filter(TareaPicking.almacen_id == almacen_id)
    auditorias_criticas = q_aud.count()

    return jsonify({
        'picks_7d': picks_7d,
        'recepciones_problema_30d': recepciones_problema,
        'averias_pendientes': averias_pendientes,
        'auditorias_criticas_30d': auditorias_criticas,
    })
