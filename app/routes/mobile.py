import logging
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.mobile_service import MobileService
from app.routes._auth_helpers import Roles

logger = logging.getLogger(__name__)

mobile_bp = Blueprint('mobile', __name__)

# Tipos de tarea que solo deben ejecutar roles de almacén (no conductores)
_TIPOS_ALMACEN = {'PICKING', 'PACKING', 'CONTEO', 'REPOSICION'}


def _operario_id():
    """Retorna el id del operario como int, o None si el token es inválido."""
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


# Roles que NO pueden ejecutar tareas de almacén bajo ninguna circunstancia
_ROLES_SIN_ALMACEN = {Roles.CONDUCTOR, Roles.TIENDA}


def _verificar_rol_para_tipo(operario_id: int, tipo: str):
    """
    Devuelve None si el usuario tiene permiso para el tipo de tarea,
    o una tupla (mensaje, status) si debe ser rechazado.
    """
    if not tipo:
        return jsonify({'error': 'tipo de tarea no puede estar vacío'}), 400
    if tipo not in _TIPOS_ALMACEN:
        return None  # Tipos de despacho/entrega — sin restricción adicional
    from app.models.usuario import Usuario
    u = Usuario.query.get(operario_id)
    if not u or u.rol in _ROLES_SIN_ALMACEN:
        return jsonify({'error': f'El rol "{u.rol if u else "desconocido"}" no puede ejecutar tareas de almacén (tipo={tipo})'}), 403
    return None


@mobile_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """Todas las tareas activas del operario — optimizado para tablet."""
    operario_id = _operario_id()
    from app.models.usuario import Usuario
    u = Usuario.query.get(operario_id)
    if not u or u.rol in _ROLES_SIN_ALMACEN:
        return jsonify({'error': 'Sin permiso para acceder a tareas de almacén'}), 403
    resultado = MobileService.get_tareas_operario(operario_id)
    return jsonify(resultado), 200


@mobile_bp.route('/tarea-actual', methods=['GET'])
@jwt_required()
def tarea_actual():
    """La tarea más prioritaria del operario ahora mismo."""
    operario_id = _operario_id()
    from app.models.usuario import Usuario
    u = Usuario.query.get(operario_id)
    if not u or u.rol in _ROLES_SIN_ALMACEN:
        return jsonify({'error': 'Sin permiso para acceder a tareas de almacén'}), 403
    try:
        resultado = MobileService.get_tarea_actual(operario_id)
    except Exception as e:
        current_app.logger.error(f'[MOBILE] /tarea-actual error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500
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

    rechazo = _verificar_rol_para_tipo(operario_id, data['tipo'])
    if rechazo:
        return rechazo

    try:
        resultado = MobileService.procesar_escaneo(
            operario_id=operario_id,
            tarea_id=data['tarea_id'],
            tipo=data['tipo'],
            codigo=data['codigo'],
            cantidad=data.get('cantidad', 1),
            lpn_codigo=data.get('lpn_codigo'),
            total_acumulado=data.get('total_acumulado'),  # idempotencia conteo
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

    rechazo = _verificar_rol_para_tipo(operario_id, data['tipo'])
    if rechazo:
        return rechazo

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
        msg = str(e)
        if 'Conectando con Siesa' in msg or 'Siesa aún no respondió' in msg:
            return jsonify({'error': msg, 'retry_after': 3}), 503
        return jsonify({'error': msg}), 400
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
            rechazo = _verificar_rol_para_tipo(operario_id, item.get('tipo', ''))
            if rechazo:
                resp_body, status_code = rechazo
                resultados.append({
                    'tarea_id': item.get('tarea_id'),
                    'exito': False,
                    'error': f'Sin permiso para tipo {item.get("tipo")}'
                })
                continue
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
    from app.models.conteo import SesionConteo, EstadoConteo

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
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'[MOBILE] reportar_problema PICKING error: {e}', exc_info=True)
            return jsonify({'error': 'Error interno al reportar problema'}), 500

    # ── CONTEO ───────────────────────────────────────────────────
    if tipo == 'CONTEO':
        sesion = SesionConteo.query.get(tarea_id)
        if not sesion:
            return jsonify({'error': f'Sesión de conteo {tarea_id} no encontrada'}), 404
        # Solo el operario asignado puede reportar problema en su propio conteo
        if sesion.operario_id != operario_id:
            return jsonify({'error': 'Esta sesión de conteo no te está asignada'}), 403
        # Guard estado: solo bloquear conteos activos (previene revertir AJUSTADO → BLOQUEADO)
        if sesion.estado not in (EstadoConteo.PENDIENTE, EstadoConteo.EN_PROCESO):
            return jsonify({
                'error': f'No se puede reportar problema en un conteo con estado {sesion.estado}'
            }), 409

        try:
            sesion.estado = EstadoConteo.BLOQUEADO
            sesion.motivo_edicion = f'[{motivo}] {observaciones or ""}'.strip()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'[MOBILE] reportar_problema CONTEO error: {e}', exc_info=True)
            return jsonify({'error': 'Error al bloquear sesión de conteo — reintenta'}), 500
        return jsonify({
            'ok': True,
            'mensaje': 'Problema de conteo reportado — el jefe revisará la ubicación',
            'motivo': motivo,
            'tarea_id': tarea_id,
        }), 200

    # ── PACKING ──────────────────────────────────────────────────
    if tipo == 'PACKING':
        from app.models.packing import TareaPacking, EstadoPacking
        from app.models.usuario import Usuario
        from app.routes._auth_helpers import _puede_empacar
        u = Usuario.query.get(operario_id)
        if not u or not _puede_empacar(u):
            return jsonify({'error': 'Sin permiso para reportar problemas de packing'}), 403
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            return jsonify({'error': f'Tarea packing {tarea_id} no encontrada'}), 404
        # Ownership: solo el empacador asignado puede bloquear su tarea
        from app.routes._auth_helpers import Roles as _R
        if tarea.empacador_id and tarea.empacador_id != operario_id and u.rol not in _R.SUPERVISION:
            return jsonify({'error': 'Esta tarea no está asignada a ti'}), 403
        # Guard estado: solo bloquear packings activos (previene revertir VERIFICADO/DESPACHADO)
        if tarea.estado not in (EstadoPacking.PENDIENTE, EstadoPacking.EN_PROCESO):
            return jsonify({
                'error': f'No se puede reportar problema en una tarea con estado {tarea.estado}'
            }), 409

        try:
            tarea.estado = EstadoPacking.BLOQUEADO
            tarea.observaciones = f'[{motivo}] {observaciones or ""}'.strip()
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'[MOBILE] reportar_problema PACKING error: {e}', exc_info=True)
            return jsonify({'error': 'Error al bloquear tarea de packing — reintenta'}), 500
        return jsonify({
            'ok': True,
            'mensaje': 'Problema de packing reportado',
            'motivo': motivo,
            'tarea_id': tarea_id,
        }), 200

    return jsonify({'error': f'Tipo de tarea no reconocido: {tipo}'}), 400


@mobile_bp.route('/faltante-info', methods=['POST'])
@jwt_required()
def registrar_faltante_info():
    """
    Registra un faltante informativo después de confirmar un picking parcial.
    No toca inventario — crea auditoría urgente y envía email al admin.
    """
    from app.services.faltante_reporte_service import FaltanteReporteService

    operario_id = _operario_id()  # noqa: F841 — futuro: adjuntar al reporte
    data = request.get_json() or {}

    tarea_id = data.get('tarea_id')
    cantidad_recogida = int(data.get('cantidad_recogida', 0))
    cantidad_solicitada = int(data.get('cantidad_solicitada', 0))

    if not tarea_id:
        return jsonify({'error': 'tarea_id es requerido'}), 400

    try:
        resultado = FaltanteReporteService.registrar(
            tarea_id=tarea_id,
            cantidad_recogida=cantidad_recogida,
            cantidad_solicitada=cantidad_solicitada,
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        from app.extensions import db
        db.session.rollback()
        current_app.logger.error(f'[MOBILE] faltante-info error: {e}', exc_info=True)
        return jsonify({'error': 'Error registrando faltante'}), 500