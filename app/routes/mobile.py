import logging
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.mobile_service import MobileService

logger = logging.getLogger(__name__)

mobile_bp = Blueprint('mobile', __name__)


def _operario_id():
    """Retorna el id del operario como int, o None si el token es inválido."""
    try:
        return _operario_id()
    except (TypeError, ValueError):
        return None


@mobile_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """Todas las tareas activas del operario — optimizado para tablet."""
    operario_id = _operario_id()
    resultado = MobileService.get_tareas_operario(operario_id)
    return jsonify(resultado), 200


@mobile_bp.route('/tarea-actual', methods=['GET'])
@jwt_required()
def tarea_actual():
    """La tarea más prioritaria del operario ahora mismo."""
    operario_id = _operario_id()
    resultado = MobileService.get_tarea_actual(operario_id)
    if not resultado:
        return jsonify({'sin_tareas': True, 'mensaje': 'No tienes tareas pendientes'}), 200
    return jsonify(resultado), 200


@mobile_bp.route('/escanear', methods=['POST'])
@jwt_required()
def escanear():
    """
    El escáner láser dispara este endpoint.
    Recibe el código escaneado y lo procesa según el tipo de tarea.
    """
    operario_id = _operario_id()
    data = request.get_json()

    if 'codigo' not in data or 'tarea_id' not in data or 'tipo' not in data:
        return jsonify({'error': 'codigo, tarea_id y tipo son requeridos'}), 400

    try:
        resultado = MobileService.procesar_escaneo(
            operario_id=operario_id,
            tarea_id=data['tarea_id'],
            tipo=data['tipo'],
            codigo=data['codigo'],
            cantidad=data.get('cantidad', 1),
            lpn_codigo=data.get('lpn_codigo'),
        )
        return jsonify(resultado), 200
    except ValueError as e:
        msg = e.args[0]
        if isinstance(msg, dict):
            msg = msg.get('mensaje', str(msg))
        return jsonify({'error': msg}), 400
    except Exception as e:
        current_app.logger.error(f'[MOBILE] /escanear error inesperado: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@mobile_bp.route('/confirmar', methods=['POST'])
@jwt_required()
def confirmar_tarea():
    """Confirma la tarea actual completa."""
    operario_id = _operario_id()
    data = request.get_json()

    if 'tarea_id' not in data or 'tipo' not in data:
        return jsonify({'error': 'tarea_id y tipo son requeridos'}), 400

    try:
        resultado = MobileService.confirmar_tarea(
            operario_id=operario_id,
            tarea_id=data['tarea_id'],
            tipo=data['tipo'],
            items_escaneados=data.get('items_escaneados', []),
            cantidad_manual=data.get('cantidad_manual')
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f'[MOBILE] /confirmar error inesperado: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@mobile_bp.route('/sync', methods=['POST'])
@jwt_required()
def sync_offline():
    """
    Sincroniza tareas completadas offline.
    El Service Worker llama esto cuando recupera WiFi.
    """
    operario_id = _operario_id()
    data = request.get_json()
    cola = data.get('cola', [])

    resultados = []
    for item in cola:
        try:
            resultado = MobileService.confirmar_tarea(
                operario_id=operario_id,
                tarea_id=item['tarea_id'],
                tipo=item['tipo'],
                items_escaneados=item.get('items_escaneados', [])
            )
            resultados.append({
                'tarea_id': item['tarea_id'],
                'exito': True,
                'resultado': resultado
            })
        except Exception as e:
            current_app.logger.error(
                f'[MOBILE] /sync error en tarea {item.get("tarea_id")}: {e}', exc_info=True
            )
            resultados.append({
                'tarea_id': item['tarea_id'],
                'exito': False,
                'error': str(e)
            })

    return jsonify({
        'sincronizados': len([r for r in resultados if r['exito']]),
        'fallidos': len([r for r in resultados if not r['exito']]),
        'resultados': resultados
    }), 200


@mobile_bp.route('/reportar-problema', methods=['POST'])
@jwt_required()
def reportar_problema():
    """
    Endpoint unificado para reportar problemas desde la pantalla del operario.
    Maneja PICKING, CONTEO y PACKING según el campo `tipo`.

    Payload: { tarea_id, tipo, motivo, cantidad_encontrada (opcional) }
    Motivos: UBICACION_VACIA | FALTANTE | MERCANCIA_AVERIADA | PRODUCTO_INCORRECTO
    """
    from app.extensions import db
    from app.models.inventario import UbicacionProducto, MovimientoInventario
    from app.models.picking import TareaPicking
    from app.models.conteo import SesionConteo
    from app.services.conteo_service import ConteoService
    from datetime import datetime as _dt
    import uuid as _uuid

    operario_id = _operario_id()
    data = request.get_json() or {}

    tarea_id = data.get('tarea_id')
    tipo = data.get('tipo', 'PICKING')
    motivo = data.get('motivo', 'UBICACION_VACIA')
    cantidad_encontrada = int(data.get('cantidad_encontrada', 0))
    observaciones = data.get('observaciones') or None

    if not tarea_id:
        return jsonify({'error': 'tarea_id es requerido'}), 400

    # ── PICKING ──────────────────────────────────────────────────
    if tipo == 'PICKING':
        from app.services.picking_service import PickingService
        try:
            resultado = PickingService.reportar_problema(
                tarea_id=tarea_id,
                operario_id=operario_id,
                motivo=motivo,
                cantidad_encontrada=cantidad_encontrada,
                observaciones=observaciones,
            )
            return jsonify(resultado), 200
        except PermissionError as e:
            return jsonify({'error': str(e)}), 403
        except ValueError as e:
            return jsonify({'error': str(e)}), 404

    # ── CONTEO ───────────────────────────────────────────────────
    if tipo == 'CONTEO':
        sesion = SesionConteo.query.get(tarea_id)
        if not sesion:
            return jsonify({'error': f'Sesión de conteo {tarea_id} no encontrada'}), 404

        sesion.estado = 'BLOQUEADO'
        sesion.motivo_edicion = f'[{motivo}] {observaciones or ""}'.strip()
        db.session.commit()
        return jsonify({
            'ok': True,
            'mensaje': 'Problema de conteo reportado — el jefe revisará la ubicación',
            'motivo': motivo,
            'tarea_id': tarea_id,
        }), 200

    # ── PACKING ──────────────────────────────────────────────────
    if tipo == 'PACKING':
        from app.models.packing import TareaPacking
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            return jsonify({'error': f'Tarea packing {tarea_id} no encontrada'}), 404

        tarea.estado = 'BLOQUEADO'
        tarea.observaciones = f'[{motivo}] {observaciones or ""}'.strip()
        db.session.commit()
        return jsonify({
            'ok': True,
            'mensaje': 'Problema de packing reportado',
            'motivo': motivo,
            'tarea_id': tarea_id,
        }), 200

    return jsonify({'error': f'Tipo de tarea no reconocido: {tipo}'}), 400