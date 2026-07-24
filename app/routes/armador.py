"""
Endpoints del Armador de Contenedor + ROP dual.

GET  /api/compras/rop-dual          — ROP por SKU (nacional + China)
GET  /api/compras/armador/propuesta — propuesta de contenedor (shadow/activo)
GET  /api/compras/armador/g5        — estado de compuertas G5
POST /api/compras/armador/contenedores — CRUD contenedores históricos
GET  /api/compras/armador/sigma-lt  — σ_LT medido vs conservador
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_admin_o_jefe

armador_bp = Blueprint('armador', __name__)


@armador_bp.route('/rop-dual', methods=['GET'])
@jwt_required()
def rop_dual():
    """ROP dual: nacional (LT=5d) + China (LT=105d, σ_LT medido)."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver ROP'}), 403
    nivel = request.args.get('nivel_servicio', 0.95, type=float)
    from app.services.armador_service import ArmadorService
    try:
        resultado = ArmadorService.rop_dual(nivel)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@armador_bp.route('/armador/propuesta', methods=['GET'])
@jwt_required()
def propuesta_contenedor():
    """Propuesta de contenedor — shadow hasta G5 completo."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver propuesta'}), 403
    tipo = request.args.get('tipo', '40STD')
    presupuesto = request.args.get('presupuesto_cop', None, type=float)
    from app.services.armador_service import ArmadorService
    try:
        resultado = ArmadorService.armar_contenedor(tipo, presupuesto)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@armador_bp.route('/armador/g5', methods=['GET'])
@jwt_required()
def estado_g5():
    """Estado de compuertas G5 del Armador."""
    from app.services.armador_service import ArmadorService
    return jsonify(ArmadorService.verificar_g5()), 200


@armador_bp.route('/armador/sigma-lt', methods=['GET'])
@jwt_required()
def sigma_lt():
    """σ_LT medido de contenedores vs default conservador."""
    from app.services.armador_service import ArmadorService
    return jsonify(ArmadorService.calcular_sigma_lt_real()), 200


@armador_bp.route('/armador/contenedores', methods=['GET'])
@jwt_required()
def listar_contenedores():
    """Lista contenedores registrados."""
    from app.models.importacion import Contenedor
    contenedores = Contenedor.query.order_by(Contenedor.fecha_creacion.desc()).all()
    return jsonify({
        'contenedores': [c.to_dict() for c in contenedores],
        'total': len(contenedores),
    }), 200


@armador_bp.route('/armador/contenedores', methods=['POST'])
@jwt_required()
def crear_contenedor():
    """Registra un contenedor (histórico o en curso)."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede registrar contenedores'}), 403
    from app.models.importacion import Contenedor
    from app.extensions import db
    from datetime import datetime

    data = request.get_json() or {}

    def _parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    c = Contenedor(
        numero=data.get('numero'),
        tipo=data.get('tipo', '40STD'),
        proveedor=data.get('proveedor'),
        fecha_oc=_parse_date(data.get('fecha_oc')),
        fecha_etd=_parse_date(data.get('fecha_etd')),
        fecha_eta=_parse_date(data.get('fecha_eta')),
        fecha_llegada_puerto=_parse_date(data.get('fecha_llegada_puerto')),
        fecha_recepcion_cedi=_parse_date(data.get('fecha_recepcion_cedi')),
        cbm_facturado=data.get('cbm_facturado'),
        peso_kg=data.get('peso_kg'),
        valor_fob_usd=data.get('valor_fob_usd'),
        valor_nacionalizado_cop=data.get('valor_nacionalizado_cop'),
        estado=data.get('estado', 'BORRADOR'),
        notas=data.get('notas'),
    )
    db.session.add(c)
    db.session.commit()

    return jsonify({'ok': True, 'contenedor': c.to_dict()}), 201
