"""
Endpoints del Kardex transaccional — reconstrucción de stock diario.

POST /api/kardex/descargar          — descarga movimientos de Siesa
POST /api/kardex/reconstruir        — reconstruye stock diario hacia atrás
GET  /api/kardex/tasa-censurada     — velocity censurada por SKU
GET  /api/kardex/stock-diario       — stock reconstruido por referencia+bodega
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_admin_o_jefe

kardex_bp = Blueprint('kardex', __name__)


@kardex_bp.route('/descargar', methods=['POST', 'GET'])
@jwt_required()
def descargar_kardex():
    """Descarga movimientos del kardex de Siesa (consulta dinámica T470+T350+T120)."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede descargar kardex'}), 403
    if request.method == 'GET':
        fecha_desde = request.args.get('fecha_desde', '20250723')
        fecha_hasta = request.args.get('fecha_hasta')
    else:
        data = request.get_json() or {}
        fecha_desde = data.get('fecha_desde')
        fecha_hasta = data.get('fecha_hasta')
    if not fecha_desde:
        return jsonify({'error': 'fecha_desde es requerido (YYYYMMDD)'}), 400
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.descargar_kardex(fecha_desde, fecha_hasta)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, **resultado}), 200


@kardex_bp.route('/reconstruir', methods=['POST'])
@jwt_required()
def reconstruir_stock():
    """Reconstruye stock diario hacia atrás desde saldo actual - movimientos."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede reconstruir stock'}), 403
    data = request.get_json() or {}
    bodega = data.get('bodega')
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.reconstruir_stock_diario(bodega)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, **resultado}), 200


@kardex_bp.route('/reconciliar', methods=['GET'])
@jwt_required()
def reconciliar():
    """COMPUERTA: cruza kardex vs facturación para verificar completitud."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede reconciliar'}), 403
    meses = request.args.get('meses', 12, type=int)
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.reconciliar_kardex(meses)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/tasa-servida-corregida', methods=['GET'])
@jwt_required()
def tasa_servida_corregida():
    """Tasa servida corregida: demanda neta / días con stock.
    nivel=bodega para reposición, nivel=red para clasificación S-B."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver tasa servida'}), 403
    meses = request.args.get('meses', 12, type=int)
    nivel = request.args.get('nivel', 'bodega')  # 'bodega' o 'red'
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.calcular_tasa_servida_corregida(meses, nivel)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/clasificacion-sb', methods=['GET'])
@jwt_required()
def clasificacion_sb():
    """Clasificación Syntetos-Boylan a nivel de RED.
    Excluye estacionales (pasar como ?estacionales=REF1,REF2)."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede clasificar'}), 403
    meses = request.args.get('meses', 12, type=int)
    est_raw = request.args.get('estacionales', '')
    estacionales = [x.strip() for x in est_raw.split(',') if x.strip()] if est_raw else []
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.clasificar_syntetos_boylan(meses, estacionales)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/stock-diario', methods=['GET'])
@jwt_required()
def stock_diario():
    """Stock reconstruido por referencia + bodega."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver stock diario'}), 403
    ref = request.args.get('referencia')
    bod = request.args.get('bodega')
    if not ref:
        return jsonify({'error': 'referencia es requerido'}), 400
    from app.services.kardex_service import StockDiario
    query = StockDiario.query.filter_by(referencia=ref)
    if bod:
        query = query.filter_by(bodega=bod)
    registros = query.order_by(StockDiario.fecha.desc()).limit(365).all()
    return jsonify({
        'referencia': ref,
        'bodega': bod or 'todas',
        'dias': [{
            'fecha': r.fecha.isoformat(),
            'bodega': r.bodega,
            'stock_cierre': float(r.stock_cierre),
            'tuvo_stock': r.tuvo_stock,
        } for r in registros],
    }), 200
