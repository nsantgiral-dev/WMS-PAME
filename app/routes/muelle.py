"""
Monitor de Muelle — lectura exclusiva desde DB local, cero peticiones a Connekta.
Flujo: siesa_triggered → bultos PENDIENTE aparecen en muelle → scan-to-truck → CARGADO.
"""
from datetime import datetime
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app.extensions import db
from app.models.bulto import Bulto
from app.models.packing import TareaPacking

muelle_bp = Blueprint('muelle', __name__)


@muelle_bp.route('/listos', methods=['GET'])
@jwt_required()
def listos():
    """
    Bultos físicos pendientes de cargue, agrupados por municipio (destino).
    Orden LIFO dentro de cada grupo: el primer bulto creado va al fondo del camión.
    """
    bultos = (
        Bulto.query
        .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
        .filter(
            TareaPacking.siesa_triggered == True,
            TareaPacking.estado != 'CANCELADO',
            Bulto.estado == 'PENDIENTE'
        )
        .order_by(TareaPacking.fecha_despachado.asc(), Bulto.numero.asc())
        .all()
    )

    # Agrupar por municipio (fallback: cliente)
    grupos = {}
    for b in bultos:
        destino = b.tarea.municipio or b.tarea.cliente or 'Sin destino'
        if destino not in grupos:
            grupos[destino] = []
        grupos[destino].append(b.to_dict())

    resultado = [
        {'destino': destino, 'bultos': lista, 'total': len(lista)}
        for destino, lista in sorted(grupos.items())
    ]

    return jsonify({
        'grupos': resultado,
        'total_bultos': len(bultos)
    }), 200


@muelle_bp.route('/cargar/<string:codigo_barras>', methods=['POST'])
@jwt_required()
def cargar_bulto(codigo_barras):
    """
    Scan-to-Truck: escanea el bulto y lo vincula a la ruta activa.
    Body opcional: {"ruta_id": 12}
    """
    from app.models.ruta_despacho import RutaDespacho
    data = request.get_json(silent=True) or {}
    ruta_id = data.get('ruta_id')

    bulto = Bulto.query.filter_by(codigo_barras=codigo_barras.upper()).first()

    if not bulto:
        return jsonify({'error': f'Bulto {codigo_barras} no encontrado'}), 404
    if not bulto.tarea.siesa_triggered:
        return jsonify({'error': 'Este pedido aún no fue procesado por Siesa'}), 400
    if bulto.estado == 'CARGADO':
        return jsonify({
            'mensaje': 'Bulto ya fue cargado',
            'codigo_barras': codigo_barras,
            'ya_cargado': True,
            'ruta_despacho_id': bulto.ruta_despacho_id
        }), 200

    if ruta_id:
        ruta = RutaDespacho.query.get(ruta_id)
        if not ruta:
            return jsonify({'error': 'Ruta no encontrada'}), 404
        if ruta.estado != 'EN_CARGUE':
            return jsonify({'error': f'La ruta #{ruta_id} ya está {ruta.estado}'}), 400
        bulto.ruta_despacho_id = ruta_id

    bulto.estado = 'CARGADO'
    bulto.fecha_cargado = datetime.utcnow()
    db.session.commit()

    tarea = bulto.tarea
    pendientes = Bulto.query.filter_by(tarea_id=tarea.id, estado='PENDIENTE').count()

    return jsonify({
        'ok': True,
        'id': bulto.id,
        'codigo_barras': codigo_barras,
        'tipo': bulto.tipo,
        'numero': bulto.numero,
        'total': bulto.total,
        'numero_pedido_siesa': tarea.numero_pedido_siesa,
        'cliente': tarea.cliente or '',
        'municipio': tarea.municipio or '',
        'ruta_despacho_id': bulto.ruta_despacho_id,
        'pedido_completo': pendientes == 0,
        'bultos_pendientes_pedido': pendientes
    }), 200


@muelle_bp.route('/manifiesto', methods=['GET'])
@jwt_required()
def manifiesto():
    """
    Manifiesto de ruta: todos los bultos cargados hoy, agrupados por municipio.
    El conductor lleva esto impreso — listado exacto de piezas por cliente.
    """
    from datetime import date
    from sqlalchemy import func

    hoy = date.today()
    bultos = (
        Bulto.query
        .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
        .filter(
            Bulto.estado == 'CARGADO',
            func.date(Bulto.fecha_cargado) == hoy
        )
        .order_by(TareaPacking.municipio, TareaPacking.cliente, Bulto.numero)
        .all()
    )

    grupos = {}
    for b in bultos:
        destino = b.tarea.municipio or b.tarea.cliente or 'Sin destino'
        if destino not in grupos:
            grupos[destino] = {}
        pedido = b.tarea.numero_pedido_siesa
        if pedido not in grupos[destino]:
            grupos[destino][pedido] = {
                'numero_pedido': pedido,
                'cliente': b.tarea.cliente or '',
                'bultos': []
            }
        grupos[destino][pedido]['bultos'].append({
            'codigo_barras': b.codigo_barras,
            'tipo': b.tipo,
            'numero': b.numero,
            'total': b.total
        })

    resultado = []
    for destino, pedidos in sorted(grupos.items()):
        parada = {'destino': destino, 'pedidos': []}
        for p in pedidos.values():
            resumen = {}
            for bul in p['bultos']:
                resumen[bul['tipo']] = resumen.get(bul['tipo'], 0) + 1
            p['resumen'] = resumen
            p['total_bultos'] = len(p['bultos'])
            parada['pedidos'].append(p)
        parada['total_bultos'] = sum(p['total_bultos'] for p in parada['pedidos'])
        resultado.append(parada)

    return jsonify({
        'manifiesto': resultado,
        'fecha': hoy.isoformat(),
        'total_bultos': len(bultos)
    }), 200


@muelle_bp.route('/historial', methods=['GET'])
@jwt_required()
def historial():
    """Últimos 100 bultos cargados, para auditoría."""
    bultos = (
        Bulto.query
        .filter_by(estado='CARGADO')
        .order_by(Bulto.fecha_cargado.desc())
        .limit(100).all()
    )
    return jsonify({'bultos': [b.to_dict() for b in bultos]}), 200
