"""
Despacho parcial — módulo administrador.

Responsabilidad única: control de acceso gestión + respuesta HTTP.
La lógica 142945→142943 está delegada 100% a DespachoParialService.
No importa ni toca nada de packing.py ni del flujo del operario.
"""
import logging
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.models.packing import TareaPacking, EstadoPacking
from app.routes._auth_helpers import _es_gestion

despacho_parcial_bp = Blueprint('despacho_parcial', __name__)
logger = logging.getLogger(__name__)


@despacho_parcial_bp.route('/<int:packing_id>/compromisos', methods=['GET'])
@jwt_required()
def get_compromisos(packing_id: int):
    """
    GET /api/despacho_parcial/<packing_id>/compromisos
    Devuelve las líneas de compromiso del pedido desde Siesa para revisión previa.
    Campos clave por ítem: f120_referencia, f400_cant_comprometida_1.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    from app.services.despacho_parcial_service import DespachoParialService
    compromisos = DespachoParialService.obtener_compromisos(
        tarea.tipo_docto_pedido_siesa,
        tarea.consec_docto_pedido_siesa,
    )
    return jsonify({
        'packing_id': packing_id,
        'pedido': tarea.numero_pedido_siesa,
        'compromisos': compromisos,
    }), 200


@despacho_parcial_bp.route('/<int:packing_id>/despachar', methods=['POST'])
@jwt_required()
def despachar_parcial(packing_id: int):
    """
    POST /api/despacho_parcial/<packing_id>/despachar
    Body: {"cantidades": {"CODIGO_PRODUCTO": 10.0, ...}}
    Ejecuta 142945 (RM) → 142943 (FE) y marca la tarea DESPACHADO.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede despachar una tarea cancelada'}), 409

    body = request.get_json(silent=True) or {}
    cantidades = body.get('cantidades')
    if not cantidades or not isinstance(cantidades, dict):
        return jsonify({'error': 'Se requiere body {"cantidades": {"codigo": qty}}'}), 400

    from app.services.despacho_parcial_service import DespachoParialService
    from app.services.cartera_service import RetenidoPorCartera
    from app.services.documento_fiscal import siesa_disponible_para_facturar
    _disponible, _motivo = siesa_disponible_para_facturar()
    if not _disponible:
        return jsonify({'error': _motivo}), 503
    # Segunda puerta al cierre (244328→142945→142943), sin la idempotencia de
    # la vía viva (declarada BORRAR en DEUDA_SIN_UI). Mientras exista, no corre
    # a la vez que la cola de Siesa ni que otro clic: el mismo lock que la
    # DLQ (P1-8). Un doble clic o el job DESPACHO_F470 de la misma tarea
    # procesándose en paralelo eran una segunda remisión.
    from app.utils.lock import LOCK_DLQ, advisory_lock
    with advisory_lock(LOCK_DLQ, 'despacho_parcial_manual') as tomado:
        if not tomado:
            return jsonify({'error': 'Hay un envío a Siesa en curso. Intente de nuevo en un '
                                     'minuto.'}), 409
        try:
            resultado = DespachoParialService.despachar_parcial(tarea, cantidades)
            logger.info(
                '[DESPACHO_PARCIAL] usuario=%s despachó packing_id=%s → %s',
                u.email, packing_id, resultado.get('rm')
            )
            return jsonify({'ok': True, **resultado}), 200
        except ValueError as e:
            return jsonify({'error': str(e)}), 409
        except RetenidoPorCartera as e:
            return jsonify({'error': str(e), 'retenido_por_cartera': True,
                            'retencion_id': e.retencion_id}), 409
        except Exception as e:
            logger.exception('[DESPACHO_PARCIAL] Error Siesa packing_id=%s: %s', packing_id, e)
            return jsonify({'error': f'Error Siesa: {str(e)}'}), 502


@despacho_parcial_bp.route('/<int:packing_id>/facturar-remision', methods=['POST'])
@jwt_required()
def facturar_remision(packing_id: int):
    """
    POST /api/despacho_parcial/<packing_id>/facturar-remision

    Carril de recuperación: detecta la RM existente en Siesa y genera la FE (142943).
    No requiere body. Usar cuando cerrar_caja creó la RM pero la FE falló.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede facturar una tarea cancelada'}), 409

    from app.services.despacho_parcial_service import DespachoParialService
    try:
        resultado = DespachoParialService.facturar_remision_existente(tarea)
        logger.info(
            '[FACTURAR_RM] usuario=%s facturó remisión packing_id=%s → %s',
            u.email, packing_id, resultado.get('rm')
        )
        return jsonify({'ok': True, **resultado}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        logger.exception('[FACTURAR_RM] Error Siesa packing_id=%s: %s', packing_id, e)
        return jsonify({'error': f'Error Siesa: {str(e)}'}), 502


@despacho_parcial_bp.route('/<int:packing_id>/facturar-rm-manual', methods=['POST'])
@jwt_required()
def facturar_rm_manual(packing_id: int):
    """
    POST /api/despacho_parcial/<packing_id>/facturar-rm-manual
    Body: {"tipo_rm": "RS", "consec_rm": 1234}

    Carril de emergencia: convierte una RM conocida a FE (142943) cuando
    el consecutivo no está en BD y API_v2_Ventas_Remisiones_DesdePedido no existe.
    El operario busca el número de RM en Siesa y lo ingresa manualmente.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)
    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede facturar una tarea cancelada'}), 409

    body = request.get_json(silent=True) or {}

    # La otra salida de una remisión enviada sin confirmar: la RM NO existe.
    # Se vuelve a preguntar a Siesa antes de quitar el pre-flag; motivo
    # obligatorio y bitácora (`declarar_rm_inexistente`).
    if body.get('rm_inexistente') is True:
        from app.extensions import db
        from app.services.bitacora import MotivoRequerido
        from app.services.despacho_parcial_service import DespachoParialService
        try:
            r = DespachoParialService.declarar_rm_inexistente(
                tarea, usuario_id=u.id, motivo=body.get('motivo'))
        except MotivoRequerido as e:
            return jsonify({'error': str(e)}), 400
        except ValueError as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 409
        if r.get('rm_encontrada'):
            return jsonify({'ok': True, **r, 'mensaje': (
                f"Siesa sí tiene la remisión {r['rm_encontrada']}: quedó registrada. "
                f"Complete la factura con «Facturar remisión».")}), 200
        return jsonify({'ok': True, **r, 'mensaje': (
            'Registrado que la remisión no existe. Puede reintentar el envío a Siesa.')}), 200

    tipo_rm   = str(body.get('tipo_rm', '')).strip().upper()
    consec_rm = body.get('consec_rm')

    if not tipo_rm or consec_rm is None:
        return jsonify({'error': 'Se requiere body {"tipo_rm": "RS", "consec_rm": 1234}'}), 400
    try:
        consec_rm = int(consec_rm)
    except (TypeError, ValueError):
        return jsonify({'error': 'consec_rm debe ser un entero'}), 400
    if consec_rm <= 0 or not tipo_rm.isalnum() or len(tipo_rm) > 10:
        return jsonify({'error': 'La remisión se escribe como tipo (letras, hasta 10) '
                                 'y consecutivo mayor que cero.'}), 400

    # La remisión la digita una persona y el WMS no la puede verificar contra
    # Siesa (`f460_*` no existe en la API: no hay forma de preguntar si esa
    # remisión es de este pedido ni si ya tiene factura por remisión). Se exige
    # la confirmación explícita —el documento repetido tal cual— y un motivo,
    # y queda FORZAR en la bitácora (P1-8). Una factura sobre la remisión
    # equivocada es un documento fiscal que se anula con nota crédito.
    documento = f'{tipo_rm}-{consec_rm}'
    if str(body.get('confirmacion') or '').strip().upper() != documento:
        return jsonify({'error': f'Confirme la remisión escribiendo exactamente {documento}',
                        'requiere_confirmacion': documento}), 400
    from app.services.bitacora import (FORZADO_FE_SOBRE_RM_DIGITADA, MotivoRequerido,
                                       motivo_obligatorio, registrar_accion)
    try:
        motivo = motivo_obligatorio(body.get('motivo'), 'facturar sobre una remisión digitada')
    except MotivoRequerido as e:
        return jsonify({'error': str(e)}), 400
    registrar_accion('FORZAR', tarea, usuario_id=u.id, motivo=motivo,
                     entidad_codigo=tarea.numero_pedido_siesa,
                     despues={'forzado': FORZADO_FE_SOBRE_RM_DIGITADA,
                              'remision': documento})

    from app.services.despacho_parcial_service import DespachoParialService
    try:
        resultado = DespachoParialService.facturar_rm_con_consec(tarea, tipo_rm, consec_rm)
        logger.info(
            '[FACTURAR_RM_MANUAL] usuario=%s packing_id=%s %s-%s → ok',
            u.email, packing_id, tipo_rm, consec_rm
        )
        return jsonify({'ok': True, **resultado}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        logger.exception('[FACTURAR_RM_MANUAL] Error packing_id=%s: %s', packing_id, e)
        return jsonify({'error': f'Error Siesa: {str(e)}'}), 502
