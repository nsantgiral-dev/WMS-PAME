from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.producto import Producto

productos_bp = Blueprint('productos', __name__)


from app.routes._auth_helpers import _solo_admin, _es_personal_almacen

@productos_bp.route('/', methods=['GET'])
@jwt_required()
def listar_productos():
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para listar productos'}), 403
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)
    buscar = request.args.get('q', '')
    categoria = request.args.get('categoria', '')

    query = Producto.query.filter_by(activo=True)

    if buscar:
        query = query.filter(
            db.or_(
                Producto.nombre.ilike(f'%{buscar}%'),
                Producto.codigo.ilike(f'%{buscar}%')
            )
        )

    if categoria:
        query = query.filter_by(categoria=categoria)

    productos = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'productos': [p.to_dict() for p in productos.items],
        'total': productos.total,
        'paginas': productos.pages,
        'pagina_actual': page
    }), 200


@productos_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_producto(id):
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para consultar productos'}), 403
    producto = Producto.query.get_or_404(id)
    return jsonify(producto.to_dict()), 200


@productos_bp.route('/', methods=['POST'])
@jwt_required()
def crear_producto():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear productos'}), 403
    data = request.get_json()
    if not data or not data.get('codigo') or not data.get('nombre'):
        return jsonify({'error': 'Codigo y nombre requeridos'}), 400

    if Producto.query.filter_by(codigo=data['codigo']).first():
        return jsonify({'error': 'El codigo ya existe'}), 409

    producto = Producto(
        codigo=data['codigo'],
        nombre=data['nombre'],
        descripcion=data.get('descripcion'),
        categoria=data.get('categoria'),
        unidad_medida=data.get('unidad_medida', 'UND'),
        peso=data.get('peso'),
        precio_compra=data.get('precio_compra', 0),
        precio_venta=data.get('precio_venta', 0),
        stock_minimo=data.get('stock_minimo', 0),
        stock_maximo=data.get('stock_maximo', 0),
        punto_pedido=data.get('punto_pedido', 0),
        codigo_siesa=data.get('codigo_siesa')
    )

    db.session.add(producto)
    db.session.commit()

    return jsonify(producto.to_dict()), 201


@productos_bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_producto(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar productos'}), 403
    producto = Producto.query.get_or_404(id)
    data = request.get_json()

    for campo in ['nombre', 'descripcion', 'categoria', 'unidad_medida',
                  'precio_compra', 'precio_venta', 'stock_minimo',
                  'stock_maximo', 'punto_pedido', 'codigo_siesa']:
        if campo in data:
            setattr(producto, campo, data[campo])

    db.session.commit()
    return jsonify(producto.to_dict()), 200


@productos_bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def eliminar_producto(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede desactivar productos'}), 403
    producto = Producto.query.get_or_404(id)
    producto.activo = False
    db.session.commit()
    return jsonify({'mensaje': 'Producto desactivado'}), 200