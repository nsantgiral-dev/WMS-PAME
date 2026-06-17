import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload, subqueryload
from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado, EstadoTraslado
from app.models.usuario import Usuario
from app.routes._auth_helpers import Roles
from app.services.traslado_service import TrasladoService

_BODEGA_CO_MAP = {
    'NC1': '002', 'NS1': '001', 'PC1': '004', 'FC1': '006',
    'FF1': '009', 'FN1': '007', 'NB1': '003', 'PT1': '005',
}

traslados_bp = Blueprint('traslados', __name__)
logger = logging.getLogger(__name__)


@traslados_bp.route('/', methods=['GET'])
@jwt_required()
def listar_solicitudes():
    """Lista solicitudes — admin ve todas, tienda solo las suyas."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)

    estado = request.args.get('estado')
    page = request.args.get('page', 1, type=int)

    query = SolicitudTraslado.query\
        .options(
            joinedload(SolicitudTraslado.solicitante),
            joinedload(SolicitudTraslado.aprobador),
            joinedload(SolicitudTraslado.operario),
            subqueryload(SolicitudTraslado.items)
            .joinedload(ItemSolicitudTraslado.producto),
        )\
        .order_by(SolicitudTraslado.fecha_creacion.desc())

    # Roles con acceso total: gestión de bodega y admin
    _roles_gestion = ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    if not usuario or usuario.rol not in _roles_gestion + ('tienda',):
        return jsonify({'error': 'Sin permiso para ver traslados'}), 403
    if usuario.rol == 'tienda':
        query = query.filter_by(solicitante_id=usuario_id)
    if estado:
        query = query.filter_by(estado=estado)

    pag = query.paginate(page=page, per_page=30, error_out=False)
    return jsonify({
        'solicitudes': [s.to_dict() for s in pag.items],
        'total': pag.total,
        'pagina': page,
    }), 200


@traslados_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_solicitud(id):
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)

    s = SolicitudTraslado.query\
        .options(
            joinedload(SolicitudTraslado.solicitante),
            joinedload(SolicitudTraslado.aprobador),
            joinedload(SolicitudTraslado.operario),
            subqueryload(SolicitudTraslado.items)
            .joinedload(ItemSolicitudTraslado.producto),
        )\
        .get_or_404(id)

    # Tienda solo puede ver sus propias solicitudes
    if usuario and usuario.rol == Roles.TIENDA and s.solicitante_id != usuario_id:
        return jsonify({'error': 'Sin permiso para ver esta solicitud'}), 403

    return jsonify(s.to_dict()), 200


@traslados_bp.route('/', methods=['POST'])
@jwt_required()
def crear_solicitud():
    """Tienda crea solicitud en BORRADOR."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO + (Roles.TIENDA,):
        return jsonify({'error': 'Sin permiso para crear solicitudes de traslado'}), 403
    data = request.get_json() or {}

    # Destino: siempre la tienda del usuario logueado
    bodega_destino = data.get('bodega_destino_siesa') or (usuario.bodega_siesa_id if usuario else None)
    nombre_pv = data.get('nombre_punto_venta') or (usuario.nombre_punto_venta if usuario else None)
    # Origen: bodega fuente seleccionada en "Pedir desde" (default NB1)
    bodega_origen = data.get('bodega_origen_siesa') or None

    if not bodega_destino:
        return jsonify({'error': 'bodega_destino_siesa es requerida (o configurar en perfil de usuario)'}), 400
    if not data.get('items'):
        return jsonify({'error': 'items es requerido'}), 400

    try:
        s = TrasladoService.crear_solicitud(
            solicitante_id=usuario_id,
            bodega_destino=bodega_destino,
            nombre_punto_venta=nombre_pv,
            items=data['items'],
            bodega_origen=bodega_origen,
            observaciones=data.get('observaciones'),
        )
        return jsonify(s.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(str(e))
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/enviar', methods=['POST'])
@jwt_required()
def enviar_solicitud(id):
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 401
    s = SolicitudTraslado.query.get_or_404(id)
    _roles_gestion = ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    if usuario.rol not in _roles_gestion + ('tienda',):
        return jsonify({'error': 'Sin permiso para enviar solicitudes de traslado'}), 403
    if usuario.rol == 'tienda' and s.solicitante_id != usuario_id:
        return jsonify({'error': 'Solo puedes enviar tus propias solicitudes'}), 403
    try:
        s = TrasladoService.enviar_solicitud(id)
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/<int:id>/aprobar', methods=['POST'])
@jwt_required()
def aprobar_solicitud(id):
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden aprobar solicitudes'}), 403
    data = request.get_json() or {}
    try:
        s = TrasladoService.aprobar_solicitud(
            solicitud_id=id,
            aprobador_id=usuario_id,
            items_aprobados=data.get('items_aprobados'),
            operario_id=data.get('operario_id'),
        )
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(str(e))
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/rechazar', methods=['POST'])
@jwt_required()
def rechazar_solicitud(id):
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden rechazar solicitudes de traslado'}), 403
    data = request.get_json() or {}
    motivo = data.get('motivo', 'Sin motivo especificado')
    try:
        s = TrasladoService.rechazar_solicitud(id, usuario_id, motivo)
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/<int:id>/cancelar', methods=['POST'])
@jwt_required()
def cancelar_solicitud(id):
    """
    Cancela una solicitud.
    - Tienda: solo BORRADOR o ENVIADA, y solo las propias.
    - Admin/supervisor: BORRADOR, ENVIADA o EN_PICKING.
    EN_TRANSITO y ENTREGADA no se pueden cancelar — el camión ya salió.
    """
    from app.extensions import db
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    s = SolicitudTraslado.query.get_or_404(id)

    if usuario.rol == 'tienda':
        if s.solicitante_id != usuario_id:
            return jsonify({'error': 'Solo puedes cancelar tus propias solicitudes'}), 403
        permitidos = ('BORRADOR', 'ENVIADA')
    elif usuario.rol in ('admin', 'supervisor'):
        permitidos = ('BORRADOR', 'ENVIADA', 'EN_PICKING', 'PREPARADO')
    else:
        return jsonify({'error': 'No autorizado'}), 403

    if s.estado not in permitidos:
        return jsonify({'error': f'No se puede cancelar en estado {s.estado}'}), 400

    data = request.get_json() or {}
    # Liberar reservas de picking antes de cancelar
    if s.estado in ('EN_PICKING', 'PREPARADO'):
        from app.services.traslado_service import TrasladoService
        try:
            TrasladoService._liberar_reservas_traslado(s)
        except Exception as _e:
            from app.extensions import db as _db
            _db.session.rollback()
            logger.error(f'[TRASLADO] Error liberando reservas en {id}: {_e}', exc_info=True)
            return jsonify({'error': f'Error liberando reservas de picking: {_e}'}), 500
    s.estado = EstadoTraslado.CANCELADA
    s.motivo_rechazo = data.get('motivo', 'Cancelada por usuario')
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f'[TRASLADO] Error cancelando solicitud {id}: {e}', exc_info=True)
        return jsonify({'error': 'Error cancelando traslado — reintenta'}), 500
    logger.info(f'[TRASLADO] {s.codigo} → CANCELADA por usuario {usuario_id}')
    return jsonify(s.to_dict()), 200


@traslados_bp.route('/<int:id>/confirmar-picking', methods=['POST'])
@jwt_required()
def confirmar_picking(id):
    """
    Operario terminó el picking de la transferencia.
    Dispara 174646 (RIT con ubicaciones reales) y transiciona EN_PICKING → EN_PACKING.
    Body opcional: {"items_confirmados": [{"id": 1, "cantidad_confirmada": 5}]}
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    s = SolicitudTraslado.query.get_or_404(id)
    usuario = Usuario.query.get(usuario_id)
    es_admin = usuario and usuario.rol in ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    if not es_admin and s.operario_id != usuario_id:
        return jsonify({'error': 'Solo el operario asignado o un admin puede confirmar la recogida'}), 403
    data = request.get_json() or {}
    try:
        s = TrasladoService.confirmar_picking_traslado(
            solicitud_id=id,
            usuario_id=usuario_id,
            items_confirmados=data.get('items_confirmados'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f'[TRASLADO] Error confirmar-picking {id}: {e}', exc_info=True)
        return jsonify({'error': 'Error interno — reintenta'}), 500
    return jsonify({
        'ok': True,
        'mensaje': 'Picking confirmado — listo para verificación de empaque',
        'solicitud': s.to_dict(),
    }), 200


@traslados_bp.route('/<int:id>/confirmar-packing', methods=['POST'])
@jwt_required()
def confirmar_packing(id):
    """
    Operario verificó el empaque (segundo conteo de la transferencia).
    Dispara 174720 (Compromisos desde RIT) y transiciona EN_PACKING → PREPARADO.
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    s = SolicitudTraslado.query.get_or_404(id)
    usuario = Usuario.query.get(usuario_id)
    es_admin = usuario and usuario.rol in ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    if not es_admin and s.operario_id != usuario_id:
        return jsonify({'error': 'Solo el operario asignado o un admin puede confirmar el empaque'}), 403
    try:
        s = TrasladoService.confirmar_packing_traslado(
            solicitud_id=id,
            usuario_id=usuario_id,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f'[TRASLADO] Error confirmar-packing {id}: {e}', exc_info=True)
        return jsonify({'error': 'Error interno — reintenta'}), 500
    return jsonify({
        'ok': True,
        'mensaje': 'Empaque verificado — listo para despachar',
        'solicitud': s.to_dict(),
    }), 200


@traslados_bp.route('/<int:id>/reasignar-operario', methods=['POST'])
@jwt_required()
def reasignar_operario(id):
    """Admin cambia el operario asignado a un traslado EN_PICKING."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden reasignar operarios'}), 403

    s = SolicitudTraslado.query.get_or_404(id)
    if s.estado != 'EN_PICKING':
        return jsonify({'error': 'Solo se puede reasignar en estado EN_PICKING'}), 400

    data = request.get_json() or {}
    nuevo_operario_id = data.get('operario_id')
    if not nuevo_operario_id:
        return jsonify({'error': 'operario_id es requerido'}), 400

    nuevo_op = Usuario.query.get(nuevo_operario_id)
    if not nuevo_op:
        return jsonify({'error': 'Operario no encontrado'}), 404

    from app.models.picking import TareaPicking
    # Reasignar solo tareas PENDIENTES (las EN_PROCESO o COMPLETADO se dejan)
    TareaPicking.query.filter_by(
        referencia_documento=s.codigo,
        tipo_documento='TRASLADO',
        estado='PENDIENTE',
    ).update({'operario_id': nuevo_operario_id}, synchronize_session=False)

    s.operario_id = nuevo_operario_id
    from app.extensions import db
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f'[TRASLADO] Error reasignando operario en {id}: {e}', exc_info=True)
        return jsonify({'error': 'Error reasignando operario — reintenta'}), 500
    logger.info(f'[TRASLADO] {s.codigo} → operario reasignado a {nuevo_op.nombre} por {usuario_id}')
    return jsonify({'ok': True, 'solicitud': s.to_dict()}), 200


@traslados_bp.route('/<int:id>/despachar', methods=['POST'])
@jwt_required()
def despachar(id):
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden despachar traslados'}), 403
    try:
        s = TrasladoService.despachar(id)
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(str(e))
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/revertir', methods=['POST'])
@jwt_required()
def revertir_traslado(id):
    """
    Admin revierte un traslado EN_TRANSITO: devuelve unidades al inventario origen.
    Body: {"motivo": "texto opcional"}
    IMPORTANTE: el documento STS en Siesa debe anularse manualmente.
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden revertir traslados'}), 403
    data = request.get_json() or {}
    motivo = data.get('motivo', '').strip()
    try:
        s = TrasladoService.reversar_traslado(
            solicitud_id=id,
            usuario_id=usuario_id,
            motivo=motivo,
        )
        return jsonify({
            'ok': True,
            'mensaje': (
                f'Traslado {s.codigo} revertido. Unidades devueltas al inventario origen. '
                f'Recuerda anular el STS en Siesa manualmente.'
            ),
            'solicitud': s.to_dict(),
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f'[TRASLADO] Error revertir {id}: {e}', exc_info=True)
        return jsonify({'error': 'Error interno — reintenta'}), 500


@traslados_bp.route('/<int:id>/recibir', methods=['POST'])
@jwt_required()
def confirmar_recepcion(id):
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    s = SolicitudTraslado.query.get_or_404(id)
    usuario = Usuario.query.get(usuario_id)
    es_admin = usuario and usuario.rol in ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    # Recepcionista puede confirmar si su bodega_siesa_id == bodega_destino del traslado
    es_recep_destino = (
        usuario and usuario.rol == 'recepcionista' and
        usuario.bodega_siesa_id and
        usuario.bodega_siesa_id == s.bodega_destino_siesa
    )
    if not es_admin and not es_recep_destino and s.solicitante_id != usuario_id:
        return jsonify({'error': 'Solo puedes confirmar la recepción de tus propios traslados'}), 403
    data = request.get_json() or {}
    try:
        s = TrasladoService.confirmar_recepcion(
            solicitud_id=id,
            usuario_id=usuario_id,
            items_recibidos=data.get('items_recibidos')
        )
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(str(e))
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/lpns', methods=['GET'])
@jwt_required()
def listar_lpns_traslado(id):
    """Lista los LPNs (pacas/cajas) vinculados a este traslado."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    _roles_gestion = ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    if not usuario or usuario.rol not in _roles_gestion + ('tienda', 'operario', 'empacador'):
        return jsonify({'error': 'Sin permiso'}), 403
    from app.models.lpn import LPN
    lpns = LPN.query.filter_by(traslado_id=id).order_by(LPN.id).all()
    return jsonify({
        'lpns': [l.to_dict() for l in lpns],
        'total': len(lpns),
    }), 200


@traslados_bp.route('/<int:id>/reintentar-siesa', methods=['POST'])
@jwt_required()
def reintentar_siesa(id):
    """
    Admin: dispara/reintenta el conector 174646 (Requisición de traslado).
    NOTA: 174646 NO forma parte del flujo normal del WMS — el flujo real usa
    173076 al despachar y 173079 al recibir. Este endpoint es para casos en que
    el consultor Siesa requiera una requisición formal previa a la transferencia.
    Solo disponible en estados EN_PICKING o APROBADA.
    ?debug=true devuelve el payload sin llamar a Siesa.
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo admin/jefe puede disparar conectores Siesa de traslado'}), 403

    from app.models.traslado import SolicitudTraslado
    from app.extensions import db
    s = SolicitudTraslado.query.get_or_404(id)

    if s.estado not in ('EN_PICKING', 'PREPARADO'):
        return jsonify({
            'error': f'174646 solo aplica en EN_PICKING o PREPARADO (estado: {s.estado}). '
                     f'Para reintentar el despacho usa /reintentar-despacho.'
        }), 400
    body = request.get_json(silent=True) or {}
    debug = body.get('debug', False) or request.args.get('debug', '').lower() == 'true'

    items_payload = [
        {
            'codigo_siesa': item.producto_codigo_siesa,
            'codigo': item.producto.codigo if item.producto else '',
            'cantidad': item.cantidad_aprobada or item.cantidad_solicitada,
            'unidad_medida': item.producto.unidad_medida if item.producto else '',
            'unidad_negocio_id': item.producto.unidad_negocio_id if item.producto else '',
        }
        for item in s.items if (item.cantidad_aprobada or item.cantidad_solicitada)
    ]

    from app.services.connekta_gateway import connekta

    if debug:
        # Construir payload sin llamar a Siesa — solo para inspección
        from datetime import datetime as _dt
        fecha_hoy = _dt.utcnow().strftime('%Y%m%d')
        payload_preview = {
            'Inicial': [{'F_CIA': connekta.id_cia_siesa}],
            'Documentos': [{
                'F_CIA': connekta.id_cia_siesa,
                'f440_id_co': connekta.centro_op,
                'f440_id_tipo_docto': connekta.tipo_docto_req_traslado,
                'f440_id_solicitante': connekta.req_solicitante,
                'f440_fecha': fecha_hoy,
                'f440_fecha_entrega': fecha_hoy,
                'f440_id_bodega_salida': s.bodega_origen_siesa,
                'f440_id_bodega_entrada': s.bodega_destino_siesa,
            }],
            'Movimientos': [
                {
                    'F_CIA': connekta.id_cia_siesa,
                    'f441_id_co': connekta.centro_op,
                    'f441_id_tipo_docto': connekta.tipo_docto_req_traslado,
                    'f441_consec_docto': 0,
                    'f441_nro_registro': idx + 1,
                    'f441_id_item': '',
                    'f441_referencia_item': item.get('codigo_siesa') or item.get('codigo'),
                    'f441_codigo_barras': '',
                    'f441_id_ext1_detalle': '',
                    'f441_id_ext2_detalle': '',
                    'f441_id_bodega': s.bodega_origen_siesa,
                    'f441_id_motivo': connekta.motivo_traslado,
                    'f441_id_unidad_medida': item.get('unidad_medida') or '',
                    'f441_cant_base': abs(item.get('cantidad', 0)),
                    'f441_cant_2': 0,
                    'f441_fecha_entrega': fecha_hoy,
                    'f441_num_dias_entrega': 0,
                    'f441_id_co_movto': connekta.centro_op,
                    'f441_id_ccosto_movto': '',
                    'f441_id_proyecto': '',
                    'f441_notas': '',
                    'f441_id_un_movto': connekta.centro_op,
                    'f441_precio_unitario': 0,
                    'f441_id_ubicacion_sal': '',
                    'f441_id_proy_etapa': '',
                    'f441_id_rubro_pof': '',
                }
                for idx, item in enumerate(items_payload)
            ],
            'Final': [{'F_CIA': connekta.id_cia_siesa}],
        }
        return jsonify({'debug': True, 'payload': payload_preview, 'items': items_payload}), 200

    try:
        from app.services.traslado_service import TrasladoService
        res = connekta.crear_requisicion_traslado(
            bodega_origen=s.bodega_origen_siesa,
            bodega_destino=s.bodega_destino_siesa,
            items=items_payload,
            codigo_solicitud=s.codigo
        )
        if not res.get('simulado') and not res.get('modo_ensayo'):
            consec = TrasladoService._extraer_consec(res)
            if consec:
                s.siesa_requisicion_consec = consec
        s.siesa_error = None
        db.session.commit()
        return jsonify({'ok': True, 'siesa_response': res, 'solicitud': s.to_dict()}), 200
    except Exception as e:
        s.siesa_error = f'174646: {str(e)}'
        db.session.commit()
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/<int:id>/reintentar-despacho', methods=['POST'])
@jwt_required()
def reintentar_despacho(id):
    """Admin: reintenta el trigger Siesa de despacho (173066/173076) sin cambiar el estado."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden reintentar despachos'}), 403
    from app.models.traslado import SolicitudTraslado
    from app.extensions import db
    from app.services.connekta_gateway import connekta
    s = SolicitudTraslado.query.get_or_404(id)
    if s.estado not in ('EN_TRANSITO', 'ENTREGADA'):
        return jsonify({'error': f'Solo se puede reintentar despacho en EN_TRANSITO o ENTREGADA (estado: {s.estado})'}), 400

    # Usar cantidad_enviada (lo que salió físicamente) para consistencia con el despacho original
    items_payload = [
        {
            'codigo_siesa': item.producto_codigo_siesa,
            'codigo': item.producto.codigo if item.producto else '',
            'cantidad': item.cantidad_enviada or item.cantidad_aprobada or item.cantidad_solicitada,
            'unidad_medida': item.producto.unidad_medida if item.producto else '',
            'unidad_negocio_id': item.producto.unidad_negocio_id if item.producto else '',
        }
        for item in s.items
        if (item.cantidad_enviada or item.cantidad_aprobada or item.cantidad_solicitada)
    ]

    try:
        if s.modo_transferencia == 'EN_TRANSITO':
            bodega_transito = s.bodega_transito_siesa or connekta.bodega_transito
            res = connekta.transferencia_transito_salida(
                bodega_origen=s.bodega_origen_siesa,
                bodega_transito=bodega_transito,
                items=items_payload,
                codigo_solicitud=s.codigo,
                consec_requisicion=s.siesa_requisicion_consec,
                bodega_destino=s.bodega_destino_siesa,
            )
        else:
            res = connekta.transferencia_directa(
                bodega_origen=s.bodega_origen_siesa,
                bodega_destino=s.bodega_destino_siesa,
                items=items_payload,
                codigo_solicitud=s.codigo
            )
        from app.services.traslado_service import TrasladoService
        if not res.get('simulado') and not res.get('modo_ensayo'):
            consec = TrasladoService._extraer_consec(res)
            if consec:
                s.siesa_salida_consec = consec
        s.siesa_error = None
        db.session.commit()
        return jsonify({'ok': True, 'siesa_response': res, 'solicitud': s.to_dict()}), 200
    except Exception as e:
        s.siesa_error = f'Despacho Siesa: {str(e)}'
        db.session.commit()
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/<int:id>/reintentar-recepcion', methods=['POST'])
@jwt_required()
def reintentar_recepcion_siesa(id):
    """Admin: reintenta 173079 (entrada tránsito) cuando falló al confirmar recepción."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden reintentar la entrada Siesa'}), 403

    from app.extensions import db
    from app.services.connekta_gateway import connekta
    s = SolicitudTraslado.query.get_or_404(id)

    if s.estado != 'ENTREGADA':
        return jsonify({'error': f'Solo aplica en ENTREGADA (estado: {s.estado})'}), 400
    if s.siesa_entrada_consec:
        return jsonify({'error': f'173079 ya registrado (consec={s.siesa_entrada_consec})'}), 400
    if s.modo_transferencia != 'EN_TRANSITO':
        return jsonify({'error': 'Solo aplica a traslados EN_TRANSITO'}), 400

    items_payload = [
        {
            'codigo_siesa': item.producto_codigo_siesa,
            'codigo': item.producto.codigo if item.producto else '',
            'cantidad': item.cantidad_recibida or item.cantidad_enviada or item.cantidad_aprobada or item.cantidad_solicitada,
            'unidad_medida': item.producto.unidad_medida if item.producto else '',
            'unidad_negocio_id': item.producto.unidad_negocio_id if item.producto else '',
        }
        for item in s.items
        if (item.cantidad_recibida or item.cantidad_enviada or item.cantidad_aprobada or item.cantidad_solicitada)
    ]

    _co_destino = _BODEGA_CO_MAP.get(s.bodega_destino_siesa)

    try:
        res = connekta.transferencia_transito_entrada(
            bodega_transito=s.bodega_transito_siesa or connekta.bodega_transito,
            bodega_destino=s.bodega_destino_siesa,
            items=items_payload,
            codigo_solicitud=s.codigo,
            consec_salida=s.siesa_salida_consec,
            co_destino=_co_destino,
            bodega_origen=s.bodega_origen_siesa or connekta.bodega,
        )
        from app.services.traslado_service import TrasladoService
        if not res.get('simulado') and not res.get('modo_ensayo'):
            consec = TrasladoService._extraer_consec(res)
            if consec:
                s.siesa_entrada_consec = consec
        s.siesa_error = None
        db.session.commit()
        return jsonify({'ok': True, 'siesa_response': res, 'solicitud': s.to_dict()}), 200
    except Exception as e:
        s.siesa_error = f'173079 retry: {str(e)}'
        db.session.commit()
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/pendientes-recepcion', methods=['GET'])
@jwt_required()
def pendientes_recepcion():
    """Recepcionista NB1: traslados EN_TRANSITO/DESPACHADA cuyo destino es su bodega."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    _roles_rec = ('admin', 'supervisor', 'gerente', 'jefe_almacen', 'recepcionista')
    if not usuario or usuario.rol not in _roles_rec:
        return jsonify({'error': 'Sin permiso'}), 403

    bodega = request.args.get('bodega') or (usuario.bodega_siesa_id if usuario.rol == 'recepcionista' else None)
    if not bodega:
        return jsonify({'solicitudes': []}), 200

    solicitudes = (
        SolicitudTraslado.query
        .options(
            joinedload(SolicitudTraslado.solicitante),
            joinedload(SolicitudTraslado.aprobador),
            joinedload(SolicitudTraslado.operario),
            subqueryload(SolicitudTraslado.items)
            .joinedload(ItemSolicitudTraslado.producto),
        )
        .filter(
            SolicitudTraslado.bodega_destino_siesa == bodega,
            SolicitudTraslado.estado.in_(['EN_TRANSITO', 'DESPACHADA']),
        )
        .order_by(SolicitudTraslado.fecha_creacion.desc())
        .all()
    )
    return jsonify({'solicitudes': [s.to_dict() for s in solicitudes]}), 200


@traslados_bp.route('/mis-traslados', methods=['GET'])
@jwt_required()
def mis_traslados():
    """Operario: lista sus solicitudes de traslado asignadas en estado EN_PICKING."""
    try:
        operario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    solicitudes = SolicitudTraslado.query\
        .options(
            joinedload(SolicitudTraslado.solicitante),
            joinedload(SolicitudTraslado.aprobador),
            joinedload(SolicitudTraslado.operario),
            subqueryload(SolicitudTraslado.items)
            .joinedload(ItemSolicitudTraslado.producto),
        )\
        .filter_by(operario_id=operario_id, estado='EN_PICKING')\
        .order_by(SolicitudTraslado.fecha_aprobacion.desc()).all()
    return jsonify({'traslados': [s.to_dict() for s in solicitudes]}), 200


@traslados_bp.route('/operarios-disponibles', methods=['GET'])
@jwt_required()
def operarios_disponibles():
    """Admin: lista operarios activos para asignar a un traslado."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Sin permiso'}), 403
    operarios = Usuario.query.filter(
        Usuario.activo == True,
        Usuario.rol == 'operario',
    ).order_by(Usuario.nombre).all()
    return jsonify({
        'operarios': [{'id': u.id, 'nombre': u.nombre} for u in operarios]
    }), 200


@traslados_bp.route('/<int:id>/items-picking', methods=['GET'])
@jwt_required()
def items_picking_detail(id):
    """Items del traslado enriquecidos con ubicación real de TareaPicking — para el HUD de picking/packing."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    _roles = ('admin', 'supervisor', 'gerente', 'jefe_almacen', 'operario', 'empacador', 'tienda')
    if not usuario or usuario.rol not in _roles:
        return jsonify({'error': 'Sin permiso'}), 403

    s = SolicitudTraslado.query\
        .options(subqueryload(SolicitudTraslado.items)
                 .joinedload(ItemSolicitudTraslado.producto))\
        .get_or_404(id)

    from app.models.picking import TareaPicking
    from sqlalchemy.orm import joinedload as _jl
    tareas = (TareaPicking.query
              .options(_jl(TareaPicking.ubicacion))
              .filter_by(referencia_documento=s.codigo, tipo_documento='TRASLADO')
              .all())
    tarea_por_producto = {}
    for t in tareas:
        if t.producto_id not in tarea_por_producto:
            tarea_por_producto[t.producto_id] = t

    result = []
    for item in s.items:
        t = tarea_por_producto.get(item.producto_id)
        result.append({
            'item_id': item.id,
            'producto_id': item.producto_id,
            'producto_codigo': item.producto.codigo if item.producto else '',
            'producto_nombre': item.producto.nombre if item.producto else '',
            'producto_codigo_barras': (item.producto.codigo_barras or '') if item.producto else '',
            'ubicacion': t.ubicacion.codigo if t and t.ubicacion else 'BODEGA',
            'cantidad_aprobada': item.cantidad_aprobada or item.cantidad_solicitada or 0,
            'cantidad_enviada': item.cantidad_enviada or 0,
            'tarea_picking_id': t.id if t else None,
            'tarea_picking_estado': t.estado if t else None,
            'cantidad_recogida': t.cantidad_recogida if t else 0,
        })
    return jsonify({'solicitud_id': id, 'codigo': s.codigo, 'items': result}), 200


@traslados_bp.route('/stock-disponible', methods=['GET'])
@jwt_required()
def stock_disponible():
    """
    Stock disponible en bodega para armar solicitud de traslado.
    ?bodega=NC1  — filtra por bodega origen (default BODEGA_ORIGEN_DEFAULT)
    ?debug=true  — incluye detalle de lo que Siesa devolvió vs WMS (solo admin)
    ?forzar=true — invalida cache y recarga desde Siesa
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO + (Roles.TIENDA,):
        return jsonify({'error': 'Sin permiso para ver stock disponible'}), 403

    bodega = request.args.get('bodega')
    debug = request.args.get('debug', '').lower() == 'true'
    forzar = request.args.get('forzar', '').lower() == 'true'

    if forzar:
        TrasladoService.invalidar_cache_stock(bodega)

    try:
        resultado = TrasladoService.get_stock_disponible(bodega, forzar=forzar)
        if debug and usuario.rol in ('admin', 'supervisor', 'gerente', 'jefe_almacen'):
            # Expone métricas de diagnóstico sin datos sensibles adicionales
            resultado['_debug'] = {
                'bodega': resultado.get('bodega'),
                'fuente': resultado.get('fuente'),
                'siesa_total_rows': resultado.get('siesa_total_rows'),
                'siesa_con_stock': resultado.get('siesa_con_stock'),
                'wms_mapeados': resultado.get('total'),
                'sin_mapeo': (resultado.get('siesa_con_stock') or 0) - (resultado.get('total') or 0),
            }
        return jsonify(resultado), 200
    except Exception as e:
        logger.exception(str(e))
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/invalidar-cache-stock', methods=['POST'])
@jwt_required()
def invalidar_cache_stock():
    """Invalida el cache TTL de stock Siesa para forzar recarga en la próxima consulta."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    _roles = Roles.DESPACHO + (Roles.TIENDA, Roles.PICKER_TRASLADO, Roles.PACKER_TRASLADO)
    if not usuario or usuario.rol not in _roles:
        return jsonify({'error': 'Sin permiso'}), 403
    bodega = request.args.get('bodega')
    TrasladoService.invalidar_cache_stock(bodega)
    return jsonify({'ok': True, 'bodega': bodega or 'todas'}), 200


@traslados_bp.route('/bodegas-siesa', methods=['GET'])
@jwt_required()
def bodegas_siesa():
    """Lista bodegas configuradas en Siesa (para seleccionar punto de venta)."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    _roles_permitidos = ('admin', 'supervisor', 'gerente', 'jefe_almacen', 'tienda')
    if not usuario or usuario.rol not in _roles_permitidos:
        return jsonify({'error': 'Sin permiso'}), 403
    try:
        resultado = TrasladoService.get_bodegas_disponibles()
        return jsonify(resultado), 200
    except Exception as e:
        logger.exception(str(e))
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/debug-packing', methods=['GET'])
@jwt_required()
def debug_packing():
    """Admin — diagnóstico de TareasPackings de traslados y solicitudes stuck."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in ('admin', 'supervisor', 'gerente', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin'}), 403

    from app.models.packing import TareaPacking
    from app.models.almacen import Almacen
    from app.extensions import db

    packs = TareaPacking.query.filter_by(tipo_documento='TRASLADO').order_by(TareaPacking.id.desc()).limit(20).all()
    solicitudes_stuck = SolicitudTraslado.query.filter(
        SolicitudTraslado.estado.in_(['EN_PICKING', 'EN_PACKING'])
    ).order_by(SolicitudTraslado.id.desc()).limit(20).all()

    almacenes = {a.id: {'id': a.id, 'codigo': a.codigo, 'bodega_siesa_id': a.bodega_siesa_id}
                 for a in Almacen.query.all()}

    return jsonify({
        'usuario': {
            'id': usuario.id,
            'rol': usuario.rol,
            'almacen_id': usuario.almacen_id,
            'puede_empacar': usuario.puede_empacar,
            'almacen': almacenes.get(usuario.almacen_id),
        },
        'traslado_packings': [
            {
                'id': p.id,
                'codigo': p.codigo,
                'referencia_doc': p.referencia_doc,
                'solicitud_id': p.solicitud_id,
                'almacen_id': p.almacen_id,
                'bodega_origen_siesa': p.bodega_origen_siesa,
                'estado': p.estado,
                'fecha_creacion': p.fecha_creacion.isoformat() if p.fecha_creacion else None,
            }
            for p in packs
        ],
        'solicitudes_stuck': [
            {
                'id': s.id,
                'codigo': s.codigo,
                'estado': s.estado,
                'bodega_origen': s.bodega_origen_siesa,
                'tiene_packing': bool(TareaPacking.query.filter_by(solicitud_id=s.id).first()),
            }
            for s in solicitudes_stuck
        ],
        'almacenes': list(almacenes.values()),
    }), 200


@traslados_bp.route('/recuperar-packing', methods=['POST'])
@jwt_required()
def recuperar_packing():
    """
    Admin — crea TareasPackings faltantes para solicitudes en EN_PICKING o EN_PACKING
    que no tienen TareaPacking. Útil para recuperar traslados stuck por fallo del auto-trigger.
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in ('admin', 'supervisor', 'gerente', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin'}), 403

    from app.models.packing import TareaPacking
    from app.extensions import db

    candidatas = SolicitudTraslado.query.filter(
        SolicitudTraslado.estado.in_(['EN_PICKING', 'EN_PACKING'])
    ).all()

    recuperadas = []
    errores = []
    for s in candidatas:
        tiene_pack = TareaPacking.query.filter_by(solicitud_id=s.id).first()
        if tiene_pack:
            continue
        # Si está EN_PACKING sin TareaPacking (stuck: confirmar_picking_traslado falló
        # después de setear el estado), resetear a EN_PICKING para que el recovery funcione.
        if s.estado == 'EN_PACKING':
            try:
                s.estado = 'EN_PICKING'
                db.session.commit()
                logger.info('[TRASLADO] recuperar-packing: %s reseteado EN_PACKING → EN_PICKING', s.codigo)
            except Exception as e_reset:
                db.session.rollback()
                logger.error('[TRASLADO] recuperar-packing no pudo resetear %s: %s', s.codigo, e_reset)
                errores.append({'codigo': s.codigo, 'error': f'Reset estado falló: {e_reset}'})
                continue
        try:
            TrasladoService.confirmar_picking_traslado(
                solicitud_id=s.id,
                usuario_id=usuario_id,
            )
            recuperadas.append(s.codigo)
            logger.info('[TRASLADO] %s TareaPacking creada por recuperar-packing', s.codigo)
        except Exception as e:
            logger.error('[TRASLADO] recuperar-packing error en %s: %s', s.codigo, e, exc_info=True)
            errores.append({'codigo': s.codigo, 'error': str(e)})

    return jsonify({
        'recuperadas': recuperadas,
        'errores': errores,
        'total_recuperadas': len(recuperadas),
    }), 200
