import logging
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.mobile_service import MobileService
from app.services.avisos_conteo_service import obtener_avisos_pendientes
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


def _opera_almacen(u) -> bool:
    """¿Este usuario ejecuta tareas de almacén? Lista BLANCA
    (`Roles.PERSONAL_ALMACEN`, la misma de `_es_personal_almacen`). Era una
    lista negra —«todos menos conductor y tienda»— y `control_flota` registraba
    conteos por `/api/mobile/conteo/*` (2026-09-25)."""
    return bool(u) and u.rol in Roles.PERSONAL_ALMACEN


def _verificar_rol_para_tipo(operario_id: int, tipo: str):
    """
    Devuelve None si el usuario tiene permiso para el tipo de tarea,
    o una tupla (mensaje, status) si debe ser rechazado.
    """
    if not tipo:
        return jsonify({'error': 'tipo de tarea no puede estar vacío'}), 400
    if tipo not in _TIPOS_ALMACEN:
        return None  # Tipos de despacho/entrega — sin restricción adicional
    from app.extensions import db
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, operario_id)
    if not _opera_almacen(u):
        return jsonify({'error': f'El rol "{u.rol if u else "desconocido"}" no puede ejecutar tareas de almacén (tipo={tipo})'}), 403
    return None


@mobile_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """Todas las tareas activas del operario — optimizado para tablet."""
    operario_id = _operario_id()
    from app.extensions import db
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, operario_id)
    if not _opera_almacen(u):
        return jsonify({'error': 'Sin permiso para acceder a tareas de almacén'}), 403
    resultado = MobileService.get_tareas_operario(operario_id)
    return jsonify(resultado), 200


@mobile_bp.route('/tarea-actual', methods=['GET'])
@jwt_required()
def tarea_actual():
    """La tarea más prioritaria del operario ahora mismo."""
    operario_id = _operario_id()
    from app.extensions import db
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, operario_id)
    if not _opera_almacen(u):
        return jsonify({'error': 'Sin permiso para acceder a tareas de almacén'}), 403
    try:
        resultado = MobileService.get_tarea_actual(operario_id)
    except Exception as e:
        current_app.logger.error(f'[MOBILE] /tarea-actual error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500

    avisos = obtener_avisos_pendientes(operario_id)
    if not resultado:
        return jsonify({'sin_tareas': True, 'mensaje': 'No tienes tareas pendientes', 'avisos_pendientes': avisos}), 200
    resultado['avisos_pendientes'] = avisos
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
            total_acumulado=data.get('total_acumulado'),  # idempotencia picking/packing
            total_previo=data.get('total_previo'),        # idempotencia conteo
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
            cantidad_manual=data.get('cantidad_manual'),
            total_contado=data.get('total_contado'),
            cero_confirmado=data.get('cero_confirmado') is True,
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


def _sync_cerrar_packing(operario_id, item):
    """Cierre de caja de packing (dispara Siesa) — acción distinta al
    confirmar_tarea genérico: necesita bultos_data y el mismo guard de
    ownership que ya tiene la ruta HTTP directa."""
    from app.extensions import db
    from app.models.usuario import Usuario
    from app.models.packing import TareaPacking
    from app.routes._auth_helpers import _puede_empacar, Roles as _R
    from app.services.packing_service import PackingService
    u = db.session.get(Usuario, operario_id)
    if not u or not _puede_empacar(u):
        return False, 'No autorizado'
    tarea_chk = db.session.get(TareaPacking, item.get('tarea_id'))
    if tarea_chk and tarea_chk.empacador_id and tarea_chk.empacador_id != operario_id \
            and u.rol not in _R.SUPERVISION:
        return False, 'No puedes cerrar una tarea asignada a otro empacador'
    resultado = PackingService.cerrar_packing_resultado(
        tarea_id=item['tarea_id'],
        bultos_data=item.get('bultos', []),
        usuario_id=operario_id,
    )
    return True, resultado


def _sync_reposicion_confirmar(operario_id, item):
    """Confirmación de reposición (RESERVA→PICKING) — no pasa por
    confirmar_tarea genérico (nunca lo soportó pese a que _TIPOS_ALMACEN lo
    lista); 100% WMS, nunca toca Siesa, y el guard de estado en
    confirmar_reposicion ya la hace segura de reintentar."""
    from app.extensions import db
    from app.models.usuario import Usuario
    from app.routes._auth_helpers import Roles as _R
    from app.services.reposicion_service import confirmar_reposicion
    u = db.session.get(Usuario, operario_id)
    if not u or (not u.puede_abastecer and u.rol not in _R.SUPERVISION):
        return False, 'Sin permiso — se requiere permiso de abastecedor'
    resultado = confirmar_reposicion(
        tarea_id=item['tarea_id'],
        abastecedor_id=operario_id,
        lpn_codigo_escaneado=item.get('lpn_codigo'),
    )
    return True, resultado


def _sync_picking_escanear(operario_id, item):
    """Escaneo individual de picking — a diferencia de confirmar_tarea (una
    vez por tarea), esto se llama una vez por código escaneado. Solo se
    encola cuando el frontend ya mandó total_acumulado (idempotente)."""
    rechazo = _verificar_rol_para_tipo(operario_id, 'PICKING')
    if rechazo:
        return False, 'Sin permiso para tipo PICKING'
    resultado = MobileService.procesar_escaneo(
        operario_id=operario_id,
        tarea_id=item['tarea_id'],
        tipo='PICKING',
        codigo=item['codigo'],
        cantidad=item.get('cantidad', 1),
        lpn_codigo=item.get('lpn_codigo'),
        total_acumulado=item.get('total_acumulado'),
    )
    return True, resultado


def _sync_packing_escanear(operario_id, item):
    """Escaneo individual de packing — mismo patrón que picking_escanear."""
    rechazo = _verificar_rol_para_tipo(operario_id, 'PACKING')
    if rechazo:
        return False, 'Sin permiso para tipo PACKING'
    resultado = MobileService.procesar_escaneo(
        operario_id=operario_id,
        tarea_id=item['tarea_id'],
        tipo='PACKING',
        codigo=item['codigo'],
        cantidad=item.get('cantidad', 1),
        total_acumulado=item.get('total_acumulado'),
    )
    return True, resultado


def _sync_recepcion_escanear(operario_id, item):
    """Escaneo de recepción — recepcion.js no pasa por el sistema genérico
    (tiene su propia ruta, con su propio scan_id). Segura de
    reintentar/encolar gracias al scan_id que deduplica en
    RecepcionService.escanear_producto."""
    from app.routes.recepcion import _es_recepcion_autorizado
    from app.services.recepcion_service import RecepcionService
    if not _es_recepcion_autorizado():
        return False, 'Sin permiso para escanear productos en recepción'
    resultado = RecepcionService.escanear_producto(
        recepcion_id=item['recepcion_id'],
        producto_id=item['producto_id'],
        cantidad=item['cantidad'],
        es_empaque=item.get('es_empaque', False),
        es_bonificacion=item.get('es_bonificacion', False),
        scan_id=item.get('scan_id'),
    )
    return True, resultado


# Registro de acciones de /sync — una 5ª rama futura no puede olvidar el
# formato de respuesta (tarea_id/_qid/accion/exito/resultado|error): cada
# handler solo decide permiso + llamada al servicio, devolviendo
# (exito: bool, resultado_o_mensaje_error); el loop arma la entrada una
# sola vez, en un solo lugar.
_SYNC_HANDLERS = {
    'cerrar_packing': _sync_cerrar_packing,
    'reposicion_confirmar': _sync_reposicion_confirmar,
    'picking_escanear': _sync_picking_escanear,
    'packing_escanear': _sync_packing_escanear,
    'recepcion_escanear': _sync_recepcion_escanear,
}

# accion → campo del item que identifica la fila en `resultados` (default 'tarea_id')
_SYNC_ID_FIELD = {'recepcion_escanear': 'recepcion_id'}


def _clasificar_fallo_de_sync(e) -> dict:
    """¿Reenviar este ítem de la cola lo arregla? (2026-09-25)

    Antes todo fallo quedaba en la cola del teléfono para siempre: un cierre de
    caja retenido por cartera dejaba al empacador frente a «Reintentando
    automáticamente…» sin fin. `definitivo` = el servidor decidió (una regla de
    negocio, un permiso, algo que no existe): sale de la cola y se dice.
    Siesa caído NO es definitivo: se reintenta, y la pantalla lo dice.
    """
    from app.services.packing_service import CierreNoEmitido
    from app.services.closing.base import SIESA_NO_DISPONIBLE
    if isinstance(e, CierreNoEmitido):
        return {'definitivo': e.estado != SIESA_NO_DISPONIBLE,
                'estado_cierre': e.estado, 'retencion_id': e.retencion_id}
    if isinstance(e, (ValueError, PermissionError, LookupError, KeyError)):
        return {'definitivo': True}
    return {'definitivo': False}


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
        qid = item.get('_qid')
        accion = item.get('accion')
        try:
            handler = _SYNC_HANDLERS.get(accion)
            if handler:
                exito, payload = handler(operario_id, item)
                entrada = {
                    'tarea_id': item.get(_SYNC_ID_FIELD.get(accion, 'tarea_id')),
                    '_qid': qid, 'accion': accion,
                    'exito': exito,
                }
                entrada['resultado' if exito else 'error'] = payload
                if not exito:
                    # El handler dijo que no (permiso, dueño): reenviar no lo arregla.
                    entrada['definitivo'] = True
                resultados.append(entrada)
                continue

            rechazo = _verificar_rol_para_tipo(operario_id, item.get('tipo', ''))
            if rechazo:
                resp_body, status_code = rechazo
                resultados.append({
                    'tarea_id': item.get('tarea_id'),
                    '_qid': qid,
                    'exito': False,
                    'error': f'Sin permiso para tipo {item.get("tipo")}'
                })
                continue
            resultado = MobileService.confirmar_tarea(
                operario_id=operario_id,
                tarea_id=item['tarea_id'],
                tipo=item['tipo'],
                items_escaneados=item.get('items_escaneados', []),
                # Un conteo confirmado sin señal viaja con lo que se declaró.
                total_contado=item.get('total_contado'),
                cero_confirmado=item.get('cero_confirmado') is True,
            )
            resultados.append({
                'tarea_id': item['tarea_id'],
                '_qid': qid,
                'exito': True,
                'resultado': resultado
            })
        except Exception as e:
            current_app.logger.error(
                f'[MOBILE] /sync error en tarea {item.get("tarea_id")} (accion={accion}): {e}', exc_info=True
            )
            from app.extensions import db as _db_sync
            _db_sync.session.rollback()
            resultados.append({
                'tarea_id': item.get('tarea_id'),
                '_qid': qid,
                'accion': accion,
                'exito': False,
                'error': str(e),
                **_clasificar_fallo_de_sync(e),
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

    operario_id = _operario_id()
    data = request.get_json() or {}

    tarea_id = data.get('tarea_id')
    tipo = data.get('tipo', 'PICKING')
    motivo = data.get('motivo')
    # Crudo: lo valida `picking_service.cantidad_encontrada_declarada`. Un
    # `int(data.get(..., 0))` acá convertía «no lo dijo» en «encontró 0».
    cantidad_encontrada = data.get('cantidad_encontrada')
    observaciones = data.get('observaciones') or None

    if not tarea_id:
        return jsonify({'error': 'tarea_id es requerido'}), 400

    # ── PICKING ──────────────────────────────────────────────────
    if tipo == 'PICKING':
        from app.services.picking_service import PickingService, DeclaracionInvalida
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
        except DeclaracionInvalida as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400
        except ValueError as e:
            return jsonify({'error': str(e)}), 404
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'[MOBILE] reportar_problema PICKING error: {e}', exc_info=True)
            return jsonify({'error': 'Error interno al reportar problema'}), 500

    # ── CONTEO ───────────────────────────────────────────────────
    # La política (motivos válidos, dueño, estado) vive en el servicio.
    if tipo == 'CONTEO':
        from app.services.conteo_service import ConteoService
        try:
            return jsonify(ConteoService.bloquear_conteo(
                sesion_id=tarea_id, operario_id=operario_id,
                motivo=data.get('motivo') or '', observaciones=observaciones)), 200
        except LookupError as e:
            return jsonify({'error': str(e)}), 404
        except PermissionError as e:
            return jsonify({'error': str(e)}), 403
        except ValueError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'[MOBILE] reportar_problema CONTEO error: {e}', exc_info=True)
            return jsonify({'error': 'Error al bloquear sesión de conteo — reintenta'}), 500

    # ── PACKING ──────────────────────────────────────────────────
    if tipo == 'PACKING':
        from app.extensions import db
        from app.models.packing import TareaPacking, EstadoPacking
        from app.models.usuario import Usuario
        from app.routes._auth_helpers import _puede_empacar
        u = db.session.get(Usuario, operario_id)
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


@mobile_bp.route('/conteo/total', methods=['POST'])
@jwt_required()
def conteo_fijar_total():
    """El operario tecleó una cantidad o deshizo el último movimiento en el
    HUD de conteo: el PWA manda el TOTAL (`total_acumulado`), el servidor lo
    fija. Idempotente: reintentar escribe lo mismo."""
    operario_id = _operario_id()
    rechazo = _verificar_rol_para_tipo(operario_id, 'CONTEO')
    if rechazo:
        return rechazo
    data = request.get_json() or {}
    if 'tarea_id' not in data or 'total_acumulado' not in data:
        return jsonify({'error': 'tarea_id y total_acumulado son requeridos'}), 400
    try:
        return jsonify(MobileService.fijar_total_conteo(
            operario_id, data['tarea_id'], data['total_acumulado'])), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@mobile_bp.route('/conteo/sin-codigo', methods=['POST'])
@jwt_required()
def conteo_mercancia_sin_codigo():
    """«Encontré mercancía sin código» desde el HUD de conteo. Queda para el
    líder y no toca el conteo en curso."""
    from app.services.conteo_service import ConteoService
    operario_id = _operario_id()
    rechazo = _verificar_rol_para_tipo(operario_id, 'CONTEO')
    if rechazo:
        return rechazo
    data = request.get_json() or {}
    if not data.get('tarea_id'):
        return jsonify({'error': 'tarea_id es requerido'}), 400
    try:
        nov = ConteoService.registrar_novedad_sin_codigo(
            data['tarea_id'], operario_id, data.get('descripcion') or '')
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'novedad_id': nov.id,
                    'mensaje': 'Anotado para el líder — seguí contando'}), 200


@mobile_bp.route('/faltante-info', methods=['POST'])
@jwt_required()
def registrar_faltante_info():
    """
    Registra un faltante informativo después de confirmar un picking parcial.
    No toca inventario — crea auditoría urgente y envía email al admin.
    """
    from app.routes._auth_helpers import _es_personal_almacen
    from app.services.faltante_reporte_service import FaltanteReporteService

    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso'}), 403

    operario_id = _operario_id()
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
            operario_id=operario_id,
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        from app.extensions import db
        db.session.rollback()
        current_app.logger.error(f'[MOBILE] faltante-info error: {e}', exc_info=True)
        return jsonify({'error': 'Error registrando faltante'}), 500