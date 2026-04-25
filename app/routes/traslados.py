import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload, subqueryload
from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
from app.models.usuario import Usuario
from app.routes._auth_helpers import Roles
from app.services.traslado_service import TrasladoService

traslados_bp = Blueprint('traslados', __name__)
logger = logging.getLogger(__name__)


@traslados_bp.route('/', methods=['GET'])
@jwt_required()
def listar_solicitudes():
    """Lista solicitudes — admin ve todas, tienda solo las suyas."""
    usuario_id = int(get_jwt_identity())
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

    if usuario and usuario.rol == 'tienda':
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
    s = SolicitudTraslado.query\
        .options(
            joinedload(SolicitudTraslado.solicitante),
            joinedload(SolicitudTraslado.aprobador),
            joinedload(SolicitudTraslado.operario),
            subqueryload(SolicitudTraslado.items)
            .joinedload(ItemSolicitudTraslado.producto),
        )\
        .get_or_404(id)
    return jsonify(s.to_dict()), 200


@traslados_bp.route('/', methods=['POST'])
@jwt_required()
def crear_solicitud():
    """Tienda crea solicitud en BORRADOR."""
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO + (Roles.TIENDA,):
        return jsonify({'error': 'Sin permiso para crear solicitudes de traslado'}), 403
    data = request.get_json() or {}

    bodega_destino = data.get('bodega_destino_siesa') or (usuario.bodega_siesa_id if usuario else None)
    nombre_pv = data.get('nombre_punto_venta') or (usuario.nombre_punto_venta if usuario else None)

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
            observaciones=data.get('observaciones'),
        )
        return jsonify(s.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/enviar', methods=['POST'])
@jwt_required()
def enviar_solicitud(id):
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 401
    s = SolicitudTraslado.query.get_or_404(id)
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
    usuario_id = int(get_jwt_identity())
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
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/rechazar', methods=['POST'])
@jwt_required()
def rechazar_solicitud(id):
    usuario_id = int(get_jwt_identity())
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
    usuario_id = int(get_jwt_identity())
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
        TrasladoService._liberar_reservas_traslado(s)
    s.estado = 'CANCELADA'
    s.motivo_rechazo = data.get('motivo', 'Cancelada por usuario')
    db.session.commit()
    logger.info(f'[TRASLADO] {s.codigo} → CANCELADA por usuario {usuario_id}')
    return jsonify(s.to_dict()), 200


@traslados_bp.route('/<int:id>/confirmar-picking', methods=['POST'])
@jwt_required()
def confirmar_picking(id):
    """
    Operario confirma que recogió físicamente los ítems y declara las cantidades reales.
    Transiciona EN_PICKING → PREPARADO, habilitando el botón Despachar para el admin.

    Body opcional: {"items_confirmados": [{"id": 1, "cantidad_confirmada": 5}]}
    Sin body: confirma cantidades aprobadas en su totalidad.
    """
    from app.extensions import db
    usuario_id = int(get_jwt_identity())
    s = SolicitudTraslado.query.get_or_404(id)

    if s.estado != 'EN_PICKING':
        return jsonify({'error': f'Solo se puede confirmar recogida en EN_PICKING (estado: {s.estado})'}), 400

    # Verificar que el operario que confirma es el asignado (o un admin)
    usuario = Usuario.query.get(usuario_id)
    es_admin = usuario and usuario.rol in ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    if not es_admin and s.operario_id and s.operario_id != usuario_id:
        return jsonify({'error': 'Solo el operario asignado puede confirmar la recogida'}), 403

    data = request.get_json() or {}
    confirmados = data.get('items_confirmados')

    if confirmados:
        confirmados_map = {i['id']: i['cantidad_confirmada'] for i in confirmados}
        for item in s.items:
            cantidad = confirmados_map.get(item.id)
            if cantidad is not None:
                if cantidad < 0:
                    return jsonify({'error': f'Cantidad negativa para ítem {item.id}'}), 400
                item.cantidad_enviada = cantidad
    else:
        for item in s.items:
            item.cantidad_enviada = item.cantidad_aprobada or item.cantidad_solicitada

    s.estado = 'PREPARADO'
    db.session.commit()
    logger.info(f'[TRASLADO] {s.codigo} → PREPARADO (recogida confirmada por usuario {usuario_id})')
    return jsonify({
        'ok': True,
        'mensaje': 'Recogida confirmada — el traslado está listo para despachar',
        'solicitud': s.to_dict(),
    }), 200


@traslados_bp.route('/<int:id>/reasignar-operario', methods=['POST'])
@jwt_required()
def reasignar_operario(id):
    """Admin cambia el operario asignado a un traslado EN_PICKING."""
    usuario_id = int(get_jwt_identity())
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
    db.session.commit()
    logger.info(f'[TRASLADO] {s.codigo} → operario reasignado a {nuevo_op.nombre} por {usuario_id}')
    return jsonify({'ok': True, 'solicitud': s.to_dict()}), 200


@traslados_bp.route('/<int:id>/despachar', methods=['POST'])
@jwt_required()
def despachar(id):
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Solo administradores pueden despachar traslados'}), 403
    try:
        s = TrasladoService.despachar(id)
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/recibir', methods=['POST'])
@jwt_required()
def confirmar_recepcion(id):
    usuario_id = int(get_jwt_identity())
    s = SolicitudTraslado.query.get_or_404(id)
    usuario = Usuario.query.get(usuario_id)
    # Tienda solo puede confirmar sus propias recepciones; admin puede confirmar cualquiera
    es_admin = usuario and usuario.rol in ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    if not es_admin and s.solicitante_id != usuario_id:
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
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/lpns', methods=['GET'])
@jwt_required()
def listar_lpns_traslado(id):
    """Lista los LPNs (pacas/cajas) vinculados a este traslado."""
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
    usuario_id = int(get_jwt_identity())
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
    usuario_id = int(get_jwt_identity())
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
                consec_requisicion=s.siesa_requisicion_consec
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


@traslados_bp.route('/mis-traslados', methods=['GET'])
@jwt_required()
def mis_traslados():
    """Operario: lista sus solicitudes de traslado asignadas en estado EN_PICKING."""
    operario_id = int(get_jwt_identity())
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
    operarios = Usuario.query.filter(
        Usuario.activo == True,
        Usuario.rol == 'operario',
    ).order_by(Usuario.nombre).all()
    return jsonify({
        'operarios': [{'id': u.id, 'nombre': u.nombre} for u in operarios]
    }), 200


@traslados_bp.route('/stock-disponible', methods=['GET'])
@jwt_required()
def stock_disponible():
    """
    Stock disponible en bodega principal para armar solicitud de traslado.
    Devuelve todos los productos con disponible > 0 sin paginar — el filtrado
    es client-side en la tienda (buscador local sobre _TIENDA_STOCK).
    """
    bodega = request.args.get('bodega')
    try:
        resultado = TrasladoService.get_stock_disponible(bodega)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/bodegas-siesa', methods=['GET'])
@jwt_required()
def bodegas_siesa():
    """Lista bodegas configuradas en Siesa (para seleccionar punto de venta)."""
    try:
        resultado = TrasladoService.get_bodegas_disponibles()
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
