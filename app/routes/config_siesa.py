"""
CRUD administrable para la tabla siesa_mapeo_unidades.
Escritura solo accesible para admin.
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from app.models.siesa_mapeo_unidades import SiesaMapeoUnidades
from app.extensions import db

config_siesa_bp = Blueprint('config_siesa', __name__)


from app.routes._auth_helpers import _solo_admin


@config_siesa_bp.route('/mapeo-unidades', methods=['GET'])
@jwt_required()
def listar_mapeos():
    # [M3] Configuración Siesa es información interna — solo gestión puede leerla.
    from app.routes._auth_helpers import _es_gestion
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para ver configuración Siesa'}), 403
    mapeos = SiesaMapeoUnidades.query.order_by(SiesaMapeoUnidades.tipo_inv_siesa).all()
    return jsonify([m.to_dict() for m in mapeos]), 200


@config_siesa_bp.route('/mapeo-unidades', methods=['POST'])
@jwt_required()
def crear_mapeo():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear mapeos'}), 403
    data = request.get_json() or {}
    tipo = (data.get('tipo_inv_siesa') or '').strip()
    unidad = (data.get('unidad_negocio_id') or '').strip()
    if not tipo or not unidad:
        return jsonify({'error': 'tipo_inv_siesa y unidad_negocio_id son requeridos'}), 400
    if SiesaMapeoUnidades.query.filter_by(tipo_inv_siesa=tipo).first():
        return jsonify({'error': f'Ya existe mapeo para {tipo}'}), 409
    m = SiesaMapeoUnidades(
        tipo_inv_siesa=tipo,
        unidad_negocio_id=unidad,
        descripcion=data.get('descripcion', ''),
    )
    db.session.add(m)
    db.session.commit()
    return jsonify(m.to_dict()), 201


@config_siesa_bp.route('/mapeo-unidades/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_mapeo(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar mapeos'}), 403
    m = SiesaMapeoUnidades.query.get_or_404(id)
    data = request.get_json() or {}
    if 'unidad_negocio_id' in data:
        m.unidad_negocio_id = data['unidad_negocio_id'].strip()
    if 'descripcion' in data:
        m.descripcion = data['descripcion']
    db.session.commit()
    return jsonify(m.to_dict()), 200


@config_siesa_bp.route('/mapeo-unidades/<int:id>', methods=['DELETE'])
@jwt_required()
def eliminar_mapeo(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede eliminar mapeos'}), 403
    m = SiesaMapeoUnidades.query.get_or_404(id)
    db.session.delete(m)
    db.session.commit()
    return jsonify({'ok': True}), 200


@config_siesa_bp.route('/mapeo-unidades/tipos-sin-mapeo', methods=['GET'])
@jwt_required()
def tipos_sin_mapeo():
    """
    Lista los f120_id_tipo_inv_serv que Siesa devolvió pero no están en la tabla
    de mapeo — son los que dejan unidad_negocio_id=NULL en productos.
    """
    from app.routes._auth_helpers import _es_gestion
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para ver configuración Siesa'}), 403
    from app.models.producto import Producto
    rows = (
        db.session.query(Producto.unidad_negocio_id, db.func.count(Producto.id))
        .filter(Producto.unidad_negocio_id.is_(None))
        .filter(Producto.activo.is_(True))
        .group_by(Producto.unidad_negocio_id)
        .all()
    )
    sin_mapeo = db.session.query(Producto.codigo_siesa, Producto.nombre).filter(
        (Producto.unidad_negocio_id.is_(None)) | (Producto.unidad_negocio_id == ''),
        Producto.activo.is_(True)
    ).limit(50).all()

    return jsonify({
        'total_sin_unidad_negocio': rows[0][1] if rows else 0,
        'muestra': [{'codigo_siesa': r[0], 'nombre': r[1]} for r in sin_mapeo],
    }), 200
