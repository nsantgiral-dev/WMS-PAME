"""
Módulo de Rutas y Manifiestos — trazabilidad Milla Cero.
Conductores + Vehículos (catálogo) + RutaDespacho + manifiesto por ruta.
"""
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from app.extensions import db
from app.models.conductor import Conductor
from app.models.vehiculo import Vehiculo
from app.models.ruta_despacho import RutaDespacho

rutas_bp = Blueprint('rutas', __name__)


# ── Conductores ──────────────────────────────────────────────────

@rutas_bp.route('/conductores', methods=['GET'])
@jwt_required()
def listar_conductores():
    solo_activos = request.args.get('activos', 'true').lower() == 'true'
    q = Conductor.query.order_by(Conductor.nombre)
    if solo_activos:
        q = q.filter_by(activo=True)
    return jsonify({'conductores': [c.to_dict() for c in q.all()]}), 200


@rutas_bp.route('/conductores', methods=['POST'])
@jwt_required()
def crear_conductor():
    data = request.get_json()
    for campo in ['nombre', 'cedula']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    if Conductor.query.filter_by(cedula=data['cedula']).first():
        return jsonify({'error': 'Ya existe un conductor con esa cédula'}), 409

    c = Conductor(
        nombre=data['nombre'].strip(),
        cedula=data['cedula'].strip(),
        telefono=data.get('telefono', '').strip() or None,
    )
    db.session.add(c)
    db.session.commit()
    return jsonify({'conductor': c.to_dict()}), 201


@rutas_bp.route('/conductores/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_conductor(id):
    c = Conductor.query.get_or_404(id)
    data = request.get_json()
    if 'nombre'   in data: c.nombre   = data['nombre'].strip()
    if 'telefono' in data: c.telefono = data['telefono'].strip() or None
    if 'activo'   in data: c.activo   = bool(data['activo'])
    db.session.commit()
    return jsonify({'conductor': c.to_dict()}), 200


@rutas_bp.route('/conductores/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_conductor(id):
    c = Conductor.query.get_or_404(id)
    c.activo = False
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── Vehículos ─────────────────────────────────────────────────────

TIPOS_VALIDOS = ('NHR', 'Turbo', 'Moto', 'Van', 'Camión')


@rutas_bp.route('/vehiculos', methods=['GET'])
@jwt_required()
def listar_vehiculos():
    solo_activos = request.args.get('activos', 'true').lower() == 'true'
    q = Vehiculo.query.order_by(Vehiculo.placa)
    if solo_activos:
        q = q.filter_by(activo=True)
    return jsonify({'vehiculos': [v.to_dict() for v in q.all()]}), 200


@rutas_bp.route('/vehiculos', methods=['POST'])
@jwt_required()
def crear_vehiculo():
    data = request.get_json()
    for campo in ['placa', 'tipo']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    placa = data['placa'].strip().upper()
    if Vehiculo.query.filter_by(placa=placa).first():
        return jsonify({'error': f'Ya existe un vehículo con placa {placa}'}), 409

    v = Vehiculo(
        placa=placa,
        tipo=data['tipo'].strip(),
        capacidad_kg=data.get('capacidad_kg') or None,
    )
    db.session.add(v)
    db.session.commit()
    return jsonify({'vehiculo': v.to_dict()}), 201


@rutas_bp.route('/vehiculos/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_vehiculo(id):
    v = Vehiculo.query.get_or_404(id)
    data = request.get_json()
    if 'tipo'         in data: v.tipo         = data['tipo'].strip()
    if 'capacidad_kg' in data: v.capacidad_kg = data['capacidad_kg'] or None
    if 'activo'       in data: v.activo       = bool(data['activo'])
    db.session.commit()
    return jsonify({'vehiculo': v.to_dict()}), 200


@rutas_bp.route('/vehiculos/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_vehiculo(id):
    v = Vehiculo.query.get_or_404(id)
    v.activo = False
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── Rutas de Despacho ────────────────────────────────────────────

@rutas_bp.route('/', methods=['GET'])
@jwt_required()
def listar_rutas():
    """Historial de rutas — filtros opcionales: fecha, conductor_id, vehiculo_id, estado."""
    fecha      = request.args.get('fecha')         # YYYY-MM-DD
    cond_id    = request.args.get('conductor_id', type=int)
    veh_id     = request.args.get('vehiculo_id', type=int)
    estado     = request.args.get('estado')
    page       = request.args.get('page', 1, type=int)

    q = RutaDespacho.query.order_by(RutaDespacho.fecha_creacion.desc())
    if estado:
        q = q.filter_by(estado=estado)
    if cond_id:
        q = q.filter_by(conductor_id=cond_id)
    if veh_id:
        q = q.filter_by(vehiculo_id=veh_id)
    if fecha:
        from sqlalchemy import func
        q = q.filter(func.date(RutaDespacho.fecha_creacion) == fecha)

    rutas = q.paginate(page=page, per_page=50, error_out=False)
    return jsonify({
        'rutas': [r.to_dict() for r in rutas.items],
        'total': rutas.total,
    }), 200


@rutas_bp.route('/', methods=['POST'])
@jwt_required()
def crear_ruta():
    """Abre una nueva ruta en estado EN_CARGUE."""
    data = request.get_json()
    for campo in ['conductor_id', 'vehiculo_id', 'tipo_ruta']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    if data['tipo_ruta'] not in ('Urbana', 'Municipal'):
        return jsonify({'error': 'tipo_ruta debe ser Urbana o Municipal'}), 400

    conductor = Conductor.query.get(data['conductor_id'])
    if not conductor or not conductor.activo:
        return jsonify({'error': 'Conductor no encontrado o inactivo'}), 404

    vehiculo = Vehiculo.query.get(data['vehiculo_id'])
    if not vehiculo or not vehiculo.activo:
        return jsonify({'error': 'Vehículo no encontrado o inactivo'}), 404

    ruta = RutaDespacho(
        conductor_id=data['conductor_id'],
        vehiculo_id=data['vehiculo_id'],
        tipo_ruta=data['tipo_ruta'],
        notas=data.get('notas', '').strip() or None,
        estado='EN_CARGUE',
    )
    db.session.add(ruta)
    db.session.commit()
    return jsonify({'ruta': ruta.to_dict()}), 201


@rutas_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_ruta(id):
    """Detalle completo con manifiesto (bultos agrupados por destino)."""
    ruta = RutaDespacho.query.get_or_404(id)
    return jsonify({'ruta': ruta.to_dict(include_bultos=True)}), 200


@rutas_bp.route('/<int:id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_ruta(id):
    """EN_CARGUE → EN_TRANSITO. El camión sale."""
    from app.models.bulto import Bulto
    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != 'EN_CARGUE':
        return jsonify({'error': f'La ruta ya está en estado {ruta.estado}'}), 400
    if not ruta.bultos:
        return jsonify({'error': 'No hay bultos asignados a esta ruta'}), 400

    sin_confirmar = Bulto.query.filter_by(ruta_despacho_id=ruta.id, estado='PENDIENTE').count()
    if sin_confirmar > 0:
        return jsonify({
            'error': f'Faltan {sin_confirmar} bulto{"s" if sin_confirmar != 1 else ""} por confirmar. '
                     f'Escanéalos en el muelle antes de cerrar la ruta.'
        }), 400

    ruta.estado = 'EN_TRANSITO'
    ruta.fecha_cierre = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'ruta': ruta.to_dict(include_bultos=True)}), 200


@rutas_bp.route('/<int:id>/entregar', methods=['POST'])
@jwt_required()
def entregar_ruta(id):
    """EN_TRANSITO → ENTREGADA."""
    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != 'EN_TRANSITO':
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO, está {ruta.estado}'}), 400

    ruta.estado = 'ENTREGADA'
    ruta.fecha_entregada = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'ruta': ruta.to_dict()}), 200
