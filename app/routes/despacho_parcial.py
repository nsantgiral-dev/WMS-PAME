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
from app.routes._auth_helpers import _es_gestion, _solo_admin

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
    # Segunda puerta al cierre (244328→142945→142943), sin la idempotencia de
    # la vía viva (declarada BORRAR en DEUDA_SIN_UI). Mientras exista, no corre
    # a la vez que el DESPACHO_F470 del mismo pedido ni que otro clic: el
    # candado de emisión del pedido, el mismo de la DLQ y de los otros
    # carriles (`emision_exclusiva`, H3). Pregunta antes si Siesa está.
    from app.services.documento_fiscal import emision_exclusiva
    with emision_exclusiva(tarea, 'despacho_parcial_manual') as permiso:
        if not permiso:
            return jsonify({'error': permiso.motivo, 'estado': permiso.estado}), permiso.status
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
    from app.services.documento_fiscal import emision_exclusiva
    # Postea el 142943: con Siesa caído no se intenta, y nunca a la vez que
    # el DESPACHO_F470 del mismo pedido (H3: el anti-duplicado por pedido no
    # ve una FE que Siesa tarda 30–60 s en mostrar).
    with emision_exclusiva(tarea, 'facturar_remision') as permiso:
        if not permiso:
            return jsonify({'error': permiso.motivo, 'estado': permiso.estado}), permiso.status
        try:
            resultado = DespachoParialService.facturar_remision_existente(
                tarea, a_mano_por=u.id)
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
        from app.services.documento_fiscal import emision_exclusiva
        # Quita el pre-flag: con el DESPACHO_F470 del pedido identificando la
        # RM a la vez, el job podría reenviar el 142945. Mismo candado.
        with emision_exclusiva(tarea, 'rm_inexistente') as permiso:
            if not permiso:
                return jsonify({'error': permiso.motivo, 'estado': permiso.estado}), permiso.status
            try:
                r = DespachoParialService.declarar_rm_inexistente(
                    tarea, usuario_id=u.id, motivo=body.get('motivo'),
                    sin_barrido_completo=body.get('sin_barrido_completo') is True,
                    confirmacion=body.get('confirmacion'))
            except MotivoRequerido as e:
                return jsonify({'error': str(e)}), 400
            except ValueError as e:
                db.session.rollback()
                return jsonify({'error': str(e),
                                **({'requiere_confirmacion': e.requiere_confirmacion}
                                   if getattr(e, 'requiere_confirmacion', None) else {})}), 409
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

    from app.services.despacho_parcial_service import DespachoParialService
    from app.services.documento_fiscal import emision_exclusiva
    with emision_exclusiva(tarea, 'facturar_rm_manual') as permiso:
        if not permiso:
            return jsonify({'error': permiso.motivo, 'estado': permiso.estado}), permiso.status
        registrar_accion('FORZAR', tarea, usuario_id=u.id, motivo=motivo,
                         entidad_codigo=tarea.numero_pedido_siesa,
                         despues={'forzado': FORZADO_FE_SOBRE_RM_DIGITADA,
                                  'remision': documento})
        try:
            resultado = DespachoParialService.facturar_rm_con_consec(
                tarea, tipo_rm, consec_rm, a_mano_por=u.id)
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


@despacho_parcial_bp.route('/anteriores-control-fiscal', methods=['GET'])
@jwt_required()
def anteriores_control_fiscal():
    """Cajas de antes del control fiscal (m048fiscal): `siesa_triggered` sin
    remisión identificada. No pueden salir hasta registrar su RM. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    from app.services.documento_fiscal import cajas_anteriores_al_control_fiscal
    return jsonify(cajas_anteriores_al_control_fiscal()), 200


@despacho_parcial_bp.route('/<int:packing_id>/verificar-en-siesa', methods=['GET'])
@jwt_required()
def verificar_en_siesa(packing_id: int):
    """Solo lectura: la factura y la remisión que Siesa tiene del pedido."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    tarea = TareaPacking.query.get_or_404(packing_id)
    from app.services.despacho_parcial_service import DespachoParialService
    return jsonify({'packing_id': packing_id, 'pedido': tarea.numero_pedido_siesa,
                    **DespachoParialService.verificar_en_siesa(tarea)}), 200
