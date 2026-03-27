from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.mobile_service import MobileService

mobile_bp = Blueprint('mobile', __name__)


@mobile_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """Todas las tareas activas del operario — optimizado para tablet."""
    operario_id = int(get_jwt_identity())
    resultado = MobileService.get_tareas_operario(operario_id)
    return jsonify(resultado), 200


@mobile_bp.route('/tarea-actual', methods=['GET'])
@jwt_required()
def tarea_actual():
    """La tarea más prioritaria del operario ahora mismo."""
    operario_id = int(get_jwt_identity())
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
    operario_id = int(get_jwt_identity())
    data = request.get_json()

    if 'codigo' not in data or 'tarea_id' not in data or 'tipo' not in data:
        return jsonify({'error': 'codigo, tarea_id y tipo son requeridos'}), 400

    try:
        resultado = MobileService.procesar_escaneo(
            operario_id=operario_id,
            tarea_id=data['tarea_id'],
            tipo=data['tipo'],
            codigo=data['codigo'],
            cantidad=data.get('cantidad', 1)
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e), 'tipo': 'VALIDACION'}), 400
    except Exception as e:
        return jsonify({'error': str(e), 'tipo': 'ERROR'}), 500


@mobile_bp.route('/confirmar', methods=['POST'])
@jwt_required()
def confirmar_tarea():
    """Confirma la tarea actual completa."""
    operario_id = int(get_jwt_identity())
    data = request.get_json()

    if 'tarea_id' not in data or 'tipo' not in data:
        return jsonify({'error': 'tarea_id y tipo son requeridos'}), 400

    try:
        resultado = MobileService.confirmar_tarea(
            operario_id=operario_id,
            tarea_id=data['tarea_id'],
            tipo=data['tipo'],
            items_escaneados=data.get('items_escaneados', [])
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@mobile_bp.route('/sync', methods=['POST'])
@jwt_required()
def sync_offline():
    """
    Sincroniza tareas completadas offline.
    El Service Worker llama esto cuando recupera WiFi.
    """
    operario_id = int(get_jwt_identity())
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