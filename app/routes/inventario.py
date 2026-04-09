from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
import uuid

inventario_bp = Blueprint('inventario', __name__)

@inventario_bp.route('/stock/<int:producto_id>', methods=['GET'])
@jwt_required()
def stock_producto(producto_id):
    producto = Producto.query.get_or_404(producto_id)
    registros = UbicacionProducto.query.filter_by(
        producto_id=producto_id
    ).filter(UbicacionProducto.cantidad > 0).all()

    return jsonify({
        'producto': producto.to_dict(),
        'stock_total': producto.stock_total,
        'stock_disponible': producto.stock_disponible,
        'ubicaciones': [r.to_dict() for r in registros]
    }), 200


@inventario_bp.route('/ajuste', methods=['POST'])
@jwt_required()
def ajuste_inventario():
    data = request.get_json()
    usuario_id = get_jwt_identity()

    requeridos = ['producto_id', 'ubicacion_id', 'cantidad', 'tipo', 'motivo', 'idempotency_key']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    # Idempotencia real — si ya existe este key, devolver el movimiento original sin tocar stock
    existente = MovimientoInventario.query.filter_by(
        idempotency_key=data['idempotency_key']
    ).first()
    if existente:
        return jsonify({
            'mensaje': 'Ajuste ya aplicado (idempotente)',
            'saldo_antes': existente.saldo_antes,
            'saldo_despues': existente.saldo_despues,
            'movimiento_id': existente.id
        }), 200

    producto = Producto.query.get_or_404(data['producto_id'])
    ubicacion = Ubicacion.query.get_or_404(data['ubicacion_id'])

    # Lock pesimista — evita ajuste concurrente sobre la misma ubicación-producto
    reg = UbicacionProducto.query.filter_by(
        producto_id=producto.id,
        ubicacion_id=ubicacion.id
    ).with_for_update().first()

    if not reg:
        reg = UbicacionProducto(
            producto_id=producto.id,
            ubicacion_id=ubicacion.id,
            cantidad=0
        )
        db.session.add(reg)
        db.session.flush()

    saldo_antes = reg.cantidad
    cantidad = int(data['cantidad'])

    if data['tipo'] == 'ENTRADA':
        reg.cantidad += cantidad
    elif data['tipo'] == 'SALIDA':
        if reg.cantidad < cantidad:
            return jsonify({'error': 'Stock insuficiente'}), 400
        reg.cantidad -= cantidad
    elif data['tipo'] == 'AJUSTE':
        reg.cantidad = cantidad
    else:
        return jsonify({'error': f'Tipo inválido: {data["tipo"]}. Usar ENTRADA, SALIDA o AJUSTE'}), 400

    saldo_despues = reg.cantidad
    reg.row_version += 1

    movimiento = MovimientoInventario(
        producto_id=producto.id,
        ubicacion_id=ubicacion.id,
        almacen_id=ubicacion.almacen_id,
        tipo=data['tipo'],
        cantidad=cantidad,
        saldo_antes=saldo_antes,
        saldo_despues=saldo_despues,
        motivo=data['motivo'],
        numero_documento=data.get('numero_documento'),
        usuario_id=int(usuario_id),
        idempotency_key=data['idempotency_key']
    )

    db.session.add(movimiento)
    db.session.commit()

    return jsonify({
        'mensaje': 'Ajuste aplicado correctamente',
        'saldo_antes': saldo_antes,
        'saldo_despues': saldo_despues,
        'movimiento_id': movimiento.id
    }), 200


@inventario_bp.route('/movimientos', methods=['GET'])
@jwt_required()
def listar_movimientos():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)
    producto_id = request.args.get('producto_id', type=int)

    query = MovimientoInventario.query.order_by(
        MovimientoInventario.fecha.desc()
    )

    if producto_id:
        query = query.filter_by(producto_id=producto_id)

    movimientos = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'movimientos': [m.to_dict() for m in movimientos.items],
        'total': movimientos.total,
        'pagina_actual': page
    }), 200