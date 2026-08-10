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
        buscar_safe = buscar.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        query = query.filter(
            db.or_(
                Producto.nombre.ilike(f'%{buscar_safe}%', escape='\\'),
                Producto.codigo.ilike(f'%{buscar_safe}%', escape='\\'),
                Producto.codigo_barras.ilike(f'%{buscar_safe}%', escape='\\'),
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


@productos_bp.route('/sin-codigo-barras', methods=['GET'])
@jwt_required()
def productos_sin_codigo_barras():
    """Los productos activos que NO se pueden escanear.

    Medido el 2026-08-10: 2.118 de 26.294 (8.1%). Cada uno es un SKU que el
    operario tiene que teclear a mano — más lento, y con la posibilidad de
    picar el producto equivocado.

    **La pregunta buena no es cuántos son, es si alguno es de alta rotación**, y
    eso hoy no se puede responder: necesita demanda, la demanda necesita el
    kardex, y el kardex necesita el corte a producción. Por eso esto devuelve la
    lista y no un ranking: la ordena alguien de bodega que reconozca los
    nombres, no un cálculo que todavía no tiene insumos.

    `formato=csv` para bajarla y repartirla.
    """
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para listar productos'}), 403

    from app.services.inventario_siesa_service import cobertura_catalogo

    q = (Producto.query
         .filter(Producto.activo.is_(True),
                 db.or_(Producto.codigo_barras.is_(None),
                        Producto.codigo_barras == ''))
         .order_by(Producto.codigo))

    if request.args.get('formato') == 'csv':
        import csv
        import io
        from flask import Response
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['codigo', 'nombre', 'categoria', 'codigo_barras_empaque'])
        for p in q.all():
            w.writerow([p.codigo, p.nombre, p.categoria or '',
                        p.codigo_barras_empaque or ''])
        return Response(
            buf.getvalue(), mimetype='text/csv',
            headers={'Content-Disposition':
                     'attachment; filename=productos_sin_codigo_barras.csv'})

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 100, type=int), 500)
    pag = q.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        # La cobertura va en la misma respuesta a propósito: una lista de 2.118
        # sin el denominador se lee como catástrofe o como nada, según el ánimo
        # de quien la mire.
        'cobertura': cobertura_catalogo(),
        'productos': [{'id': p.id, 'codigo': p.codigo, 'nombre': p.nombre,
                       'categoria': p.categoria,
                       'codigo_barras_empaque': p.codigo_barras_empaque}
                      for p in pag.items],
        'total': pag.total,
        'paginas': pag.pages,
        'pagina_actual': page,
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