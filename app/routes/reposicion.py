"""
Rutas de Reposición — Abastecedor + Admin.

/api/reposicion/tarea-actual          GET   → próxima tarea para el abastecedor
/api/reposicion/mis-tareas            GET   → todas las tareas activas del abastecedor
/api/reposicion/confirmar             POST  → romper paca y abastecer zona PICKING
/api/reposicion/verificar-stock       POST  → dispara verificación manual (admin)
/api/reposicion/pendientes            GET   → admin — todas las tareas PENDIENTE/EN_PROCESO
/api/reposicion/cancelar/<id>         POST  → admin — cancelar tarea

/api/reposicion/sync-ubicaciones      POST  → dispara sync nocturno desde Siesa (admin)
/api/reposicion/sync-ubicaciones/estado GET → estado del último sync
/api/siesa/debug-ubicaciones-raw      GET   → raw de la API 43 para certificar campos
"""

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

reposicion_bp = Blueprint('reposicion', __name__)


# ──────────────────────────────────────────────────────────────────────────────
# Abastecedor — Mobile
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/tarea-actual', methods=['GET'])
@jwt_required()
def tarea_actual():
    """Próxima tarea de reposición para el abastecedor."""
    from app.services.reposicion_service import get_tarea_abastecedor
    abastecedor_id = int(get_jwt_identity())
    tarea = get_tarea_abastecedor(abastecedor_id)
    if not tarea:
        return jsonify({'sin_tareas': True, 'mensaje': 'No hay reposiciones pendientes'}), 200
    return jsonify(tarea), 200


@reposicion_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """Todas las tareas activas del abastecedor."""
    from app.services.reposicion_service import get_tareas_abastecedor
    abastecedor_id = int(get_jwt_identity())
    tareas = get_tareas_abastecedor(abastecedor_id)
    return jsonify({'tareas': tareas, 'total': len(tareas)}), 200


@reposicion_bp.route('/confirmar', methods=['POST'])
@jwt_required()
def confirmar():
    """
    Confirma la reposición — "rompe la paca".

    Payload: { tarea_id, lpn_codigo (opcional — para validar escaneo) }
    """
    from app.services.reposicion_service import confirmar_reposicion
    abastecedor_id = int(get_jwt_identity())
    data = request.get_json() or {}

    tarea_id = data.get('tarea_id')
    if not tarea_id:
        return jsonify({'error': 'tarea_id es requerido'}), 400

    try:
        resultado = confirmar_reposicion(
            tarea_id=tarea_id,
            abastecedor_id=abastecedor_id,
            lpn_codigo_escaneado=data.get('lpn_codigo'),
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Admin
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/verificar-stock', methods=['POST'])
@jwt_required()
def verificar_stock():
    """Fuerza una verificación de stock en todas las zonas PICKING."""
    from app.services.reposicion_service import verificar_stock_picking
    data = request.get_json() or {}
    almacen_id = data.get('almacen_id')
    try:
        generadas = verificar_stock_picking(almacen_id=almacen_id)
        return jsonify({'ok': True, 'tareas_generadas': generadas}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reposicion_bp.route('/pendientes', methods=['GET'])
@jwt_required()
def pendientes():
    """Lista tareas PENDIENTE + EN_PROCESO (admin / jefe de almacén)."""
    from app.models.tarea_reposicion import TareaReposicion
    tareas = TareaReposicion.query.filter(
        TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO'])
    ).order_by(TareaReposicion.fecha_creacion.asc()).all()
    return jsonify({'tareas': [t.to_dict() for t in tareas], 'total': len(tareas)}), 200


@reposicion_bp.route('/cancelar/<int:tarea_id>', methods=['POST'])
@jwt_required()
def cancelar(tarea_id):
    """Cancela una tarea de reposición (admin)."""
    from app.extensions import db
    from app.models.tarea_reposicion import TareaReposicion
    data = request.get_json() or {}

    tarea = TareaReposicion.query.get(tarea_id)
    if not tarea:
        return jsonify({'error': f'Tarea {tarea_id} no encontrada'}), 404
    if tarea.estado in ('COMPLETADA', 'CANCELADA'):
        return jsonify({'error': f'Tarea ya está en estado {tarea.estado}'}), 400

    tarea.estado = 'CANCELADA'
    tarea.notas = (tarea.notas or '') + f' | Cancelada: {data.get("motivo", "sin motivo")}'
    db.session.commit()
    return jsonify({'ok': True, 'tarea': tarea.to_dict()}), 200


# ──────────────────────────────────────────────────────────────────────────────
# Sync ubicaciones desde Siesa
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/sync-ubicaciones', methods=['POST'])
@jwt_required()
def sync_ubicaciones():
    """
    Dispara la sincronización de ubicaciones desde Siesa (API_v2_Ubicaciones ID 43).
    Corre en hilo de fondo — retorna inmediatamente.
    """
    from app.services.ubicaciones_sync_service import sync_ubicaciones_desde_siesa
    data = request.get_json() or {}
    bodega_id = data.get('bodega_id')
    try:
        resultado = sync_ubicaciones_desde_siesa(
            app=current_app._get_current_object(),
            bodega_id=bodega_id,
            en_hilo=True,
        )
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reposicion_bp.route('/sync-ubicaciones/estado', methods=['GET'])
@jwt_required()
def sync_ubicaciones_estado():
    from app.services.ubicaciones_sync_service import get_estado
    return jsonify(get_estado()), 200


@reposicion_bp.route('/debug-ubicaciones-raw', methods=['GET'])
@jwt_required()
def debug_ubicaciones_raw():
    """
    Descarga la primera página de API_v2_Ubicaciones sin procesar.
    Usar para certificar nombres exactos de campos antes del primer sync real.
    """
    from app.services.connekta_gateway import connekta
    bodega_id = request.args.get('bodega_id', connekta.bodega)
    try:
        resp = connekta.get_ubicaciones_siesa(bodega_id=bodega_id, pagina=1)
        rows = resp.get('detalle', {}).get('Table', [])
        return jsonify({
            'total_filas': len(rows),
            'primera_fila': rows[0] if rows else None,
            'muestra_5': rows[:5],
            'raw': resp,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
