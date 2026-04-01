"""
Monitor de Muelle — lectura exclusiva desde DB local, cero peticiones a Connekta.
Flujo: siesa_triggered=True → aparece en muelle → operario escanea → CARGADO → desaparece.
"""
from datetime import datetime
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app.extensions import db
from app.models.packing import TareaPacking

muelle_bp = Blueprint('muelle', __name__)


@muelle_bp.route('/listos', methods=['GET'])
@jwt_required()
def listos():
    """
    Devuelve todos los pedidos que ya dispararon Siesa y están esperando ser cargados.
    Ordenados LIFO dentro de cada destino: fecha_verificado ASC (el que llegó primero
    va al fondo del camión → se carga primero → se entrega último).
    """
    tareas = TareaPacking.query.filter(
        TareaPacking.siesa_triggered == True,
        TareaPacking.estado != 'CARGADO',
        TareaPacking.estado != 'CANCELADO'
    ).order_by(TareaPacking.fecha_verificado.asc()).all()

    # Agrupar por cliente/destino
    grupos = {}
    for t in tareas:
        destino = t.cliente or 'Sin destino'
        if destino not in grupos:
            grupos[destino] = []
        grupos[destino].append({
            'id': t.id,
            'codigo': t.codigo,
            'numero_pedido_siesa': t.numero_pedido_siesa,
            'cliente': t.cliente or '',
            'total_items': t.total_items(),
            'siesa_triggered_at': t.siesa_triggered_at.isoformat() if t.siesa_triggered_at else None,
            'fecha_verificado': t.fecha_verificado.isoformat() if t.fecha_verificado else None,
        })

    resultado = [
        {'destino': destino, 'cajas': cajas}
        for destino, cajas in sorted(grupos.items())
    ]

    return jsonify({
        'grupos': resultado,
        'total_cajas': len(tareas)
    }), 200


@muelle_bp.route('/cargar/<string:codigo>', methods=['POST'])
@jwt_required()
def cargar_caja(codigo):
    """
    Scan-to-Truck: el despachador escanea el código de la caja en la puerta del camión.
    Cambia estado a CARGADO → desaparece del monitor.
    """
    tarea = TareaPacking.query.filter_by(codigo=codigo).first()

    if not tarea:
        return jsonify({'error': f'Caja {codigo} no encontrada'}), 404
    if not tarea.siesa_triggered:
        return jsonify({'error': 'Esta caja aún no fue despachada a Siesa'}), 400
    if tarea.estado == 'CARGADO':
        return jsonify({'mensaje': 'Caja ya fue cargada', 'codigo': codigo}), 200

    tarea.estado = 'CARGADO'
    tarea.fecha_cargado = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'ok': True,
        'id': tarea.id,
        'codigo': codigo,
        'numero_pedido_siesa': tarea.numero_pedido_siesa,
        'cliente': tarea.cliente or ''
    }), 200


@muelle_bp.route('/historial', methods=['GET'])
@jwt_required()
def historial():
    """Últimas 50 cajas ya cargadas, para auditoría."""
    tareas = TareaPacking.query.filter_by(
        estado='CARGADO'
    ).order_by(TareaPacking.fecha_cargado.desc()).limit(50).all()

    return jsonify({
        'cajas': [{
            'codigo': t.codigo,
            'numero_pedido_siesa': t.numero_pedido_siesa,
            'cliente': t.cliente or '',
            'fecha_cargado': t.fecha_cargado.isoformat() if t.fecha_cargado else None,
        } for t in tareas]
    }), 200
