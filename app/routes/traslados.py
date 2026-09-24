import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload, subqueryload
from app.models.traslado import (SolicitudTraslado, ItemSolicitudTraslado,
                                EstadoTraslado, ClaseTraslado)
from app.models.usuario import Usuario
from app.routes._auth_helpers import Roles
from app.services.traslado_service import (TrasladoService,
                                           BODEGA_AVERIAS_DESTINO)
from app.services.bodegas import co_de_bodega

traslados_bp = Blueprint('traslados', __name__)
logger = logging.getLogger(__name__)

# Roles con acceso total a solicitudes: gestión de bodega y admin. `tienda`
# también entra, pero solo ve las suyas — ver `_query_solicitudes_visibles`.
_ROLES_GESTION_TRASLADOS = ('admin', 'supervisor', 'gerente', 'jefe_almacen')


def _usuario_autorizado_traslados(usuario):
    return bool(usuario and usuario.rol in _ROLES_GESTION_TRASLADOS + ('tienda',))


def _query_solicitudes_visibles(usuario):
    """Query base de solicitudes visibles para el usuario — admin/gestión ve
    todas, tienda solo las suyas. Una sola función: `listar_solicitudes` y
    `conteos_por_estado` la comparten para que "qué ve cada rol" no diverja
    entre el conteo del badge y la lista real (Regla 0)."""
    query = SolicitudTraslado.query
    if usuario.rol == 'tienda':
        query = query.filter_by(solicitante_id=usuario.id)
    return query


@traslados_bp.route('/', methods=['GET'])
@jwt_required()
def listar_solicitudes():
    """Lista solicitudes — admin ve todas, tienda solo las suyas."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not _usuario_autorizado_traslados(usuario):
        return jsonify({'error': 'Sin permiso para ver traslados'}), 403

    estado = request.args.get('estado')
    page = request.args.get('page', 1, type=int)

    query = _query_solicitudes_visibles(usuario)\
        .options(
            joinedload(SolicitudTraslado.solicitante),
            joinedload(SolicitudTraslado.aprobador),
            joinedload(SolicitudTraslado.operario),
            subqueryload(SolicitudTraslado.items)
            .joinedload(ItemSolicitudTraslado.producto),
        )\
        .order_by(SolicitudTraslado.fecha_creacion.desc())
    if estado:
        query = query.filter_by(estado=estado)

    pag = query.paginate(page=page, per_page=30, error_out=False)
    return jsonify({
        'solicitudes': [s.to_dict() for s in pag.items],
        'total': pag.total,
        'paginas': pag.pages or 1,
        'pagina': page,
    }), 200


@traslados_bp.route('/conteos-por-estado', methods=['GET'])
@jwt_required()
def conteos_por_estado():
    """Conteo de solicitudes por estado — alimenta los badges de las pestañas
    de Requisiciones sin traer la lista completa de cada una. Antes, pintar
    los 6 contadores exigía pedir las 6 listas enteras en paralelo; ahora es
    un solo GROUP BY."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not _usuario_autorizado_traslados(usuario):
        return jsonify({'error': 'Sin permiso para ver traslados'}), 403

    from sqlalchemy import func
    filas = (
        _query_solicitudes_visibles(usuario)
        .with_entities(SolicitudTraslado.estado, func.count(SolicitudTraslado.id))
        .group_by(SolicitudTraslado.estado)
        .all()
    )
    return jsonify({'conteos': {estado: n for estado, n in filas}}), 200


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

    _clase = (data.get('clase_traslado') or ClaseTraslado.NORMAL).strip().upper()

    if _clase == ClaseTraslado.AVERIAS:
        # **El traslado de averías corre al revés que el normal.**
        #
        # En uno normal el punto PIDE: el destino es el punto y el origen es
        # el CD. En uno de averías el punto MANDA: el origen es el punto y el
        # destino es el CD. Si esto se dedujera del mismo `if`, un traslado de
        # averías saldría del CD hacia el punto —es decir, al revés— y el STS
        # descontaría del CD mercancía que nunca estuvo rota.
        #
        # El origen se toma del usuario, no del payload: quien declara una
        # avería la declara de SU punto. `bodega_del_usuario` es la única
        # función que contesta de qué bodega es alguien, y falla cerrada.
        from app.services.alcance import bodega_del_usuario
        bodega_origen = bodega_del_usuario(usuario)
        if not bodega_origen:
            return jsonify({'error':
                'Tu usuario no tiene una bodega asignada, así que no se puede '
                'saber de qué punto sale esta avería. Pedí que te la '
                'configuren antes de declararla.'}), 400
        bodega_destino = BODEGA_AVERIAS_DESTINO
        nombre_pv = (usuario.nombre_punto_venta if usuario else None)
    else:
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
            clase_traslado=_clase,
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
        # ── La excepción del administrador del punto ─────────────────────
        #
        # `Roles.DESPACHO` es quién aprueba un traslado NORMAL, y ahí el que
        # aprueba es el CD: la tienda PIDE y el CD decide si manda. Un
        # traslado de averías corre al revés —el punto MANDA— así que el que
        # aprueba es el punto, y ningún punto tiene un usuario de DESPACHO.
        #
        # Medido en producción el 2026-09-17: cada punto satélite tiene un
        # solo usuario administrativo, con rol `tienda` (NC1, NS1, PC1, FC1:
        # uno cada uno). Sin esta excepción la avería se traba en ENVIADA y
        # nadie del punto puede destrabarla.
        #
        # **No se ensancha `Roles.DESPACHO`.** Esa tupla decide sobre todos
        # los traslados de la red y el repo ya tiene la cicatriz de una tupla
        # de permisos que creció por el borde que nadie miró. Esto es una
        # puerta lateral con tres llaves simultáneas —clase, rol y bodega— y
        # cada una acota qué puede pasar por ella:
        #
        #   · la solicitud es de averías        → no toca ningún traslado normal
        #   · el rol es `tienda`                → no habilita a pickers ni packers
        #   · el origen es SU bodega            → no puede aprobar la de otro punto
        #
        # El alcance se resuelve con `bodega_del_usuario`, que es la única
        # función que contesta de qué bodega es alguien y falla cerrada.
        from app.services.alcance import usuario_es_de_la_bodega
        _s = SolicitudTraslado.query.get_or_404(id)
        _es_admin_de_su_punto = (
            _s.es_averia()
            and usuario.rol == Roles.TIENDA
            and usuario_es_de_la_bodega(usuario, _s.bodega_origen_siesa)
        )
        if not _es_admin_de_su_punto:
            return jsonify({'error': 'Solo administradores pueden aprobar solicitudes'}), 403
    data = request.get_json() or {}
    try:
        s = TrasladoService.aprobar_solicitud(
            solicitud_id=id,
            aprobador_id=usuario_id,
            items_aprobados=data.get('items_aprobados'),
            operario_id=data.get('operario_id'),
            averia_evidencia=data.get('averia_evidencia'),
        )
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(str(e))
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/averias-pendientes', methods=['GET'])
@jwt_required()
def averias_pendientes():
    """Las averías que llegaron al CD y nadie dictaminó.

    Endpoint propio y no un filtro sobre la lista general, por dos razones
    medidas:

    · **La lista general pagina de a 30 y el front nunca manda `page`.** Una
      avería pendiente entre traslados ENTREGADA deja de ser alcanzable en
      cuanto haya 30 entregados más nuevos. La cola de una decisión pendiente
      no puede depender de cuántas cosas terminaron después.

    · **«Historial» es el lugar equivocado.** Ahí es donde uno mira lo que ya
      pasó; esto es lo que falta hacer, y antes de ubicar la mercancía.

    Devuelve además el conteo, que es lo que alimenta el badge: sin un número
    a la vista, la pestaña hay que acordarse de abrirla — y acordarse no es un
    control.
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401

    from app.extensions import db
    usuario = db.session.get(Usuario, usuario_id)
    if not usuario or usuario.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Solo administración del CD ve esta cola'}), 403

    # `is_(None)` y no `.isnot(True)`: un veredicto `False` («no estaba
    # averiada») es una decisión tomada, no un pendiente.
    q = (SolicitudTraslado.query
         .filter(
             SolicitudTraslado.clase_traslado == ClaseTraslado.AVERIAS,
             SolicitudTraslado.estado == EstadoTraslado.ENTREGADA,
             SolicitudTraslado.averia_veredicto.is_(None),
         )
         .order_by(SolicitudTraslado.fecha_entrega.asc().nullsfirst()))

    # Sin paginar, a propósito: son las que están esperando una decisión. Si
    # algún día son tantas que hay que paginarlas, el problema no es la lista.
    solicitudes = q.all()
    return jsonify({
        'solicitudes': [s.to_dict() for s in solicitudes],
        'total': len(solicitudes),
    }), 200


@traslados_bp.route('/<int:id>/dictaminar-averia', methods=['POST'])
@jwt_required()
def dictaminar_averia(id):
    """NB1 dictamina si la mercancía que llegó estaba realmente averiada.

    Cuarto y último momento de validación del proceso de averías. El guard de
    rol y de bodega vive en el servicio, no acá: la regla de quién puede
    dictaminar es de negocio, no de transporte, y así un segundo llamador no
    puede entrar por una puerta más floja.
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401

    # Puerta barata acá, autoridad abajo. El servicio vuelve a mirar el rol y
    # además exige bodega, estado y que no sea quien declaró — esos son los
    # guards que mandan, y viven ahí para que un segundo llamador no pueda
    # entrar por una puerta más floja.
    #
    # Este chequeo no es redundancia decorativa: `test_todo_endpoint_verifica_rol`
    # exige que la función de ruta sepa quién escribe, y tiene razón. La
    # auditoría del 2026-08-04 encontró 28 rutas que delegaban el control
    # «más abajo» y en varias no había ningún abajo. Es el mismo patrón de
    # `PUT /api/conteo/<id>/ajustar`.
    from app.extensions import db
    usuario = db.session.get(Usuario, usuario_id)
    if not usuario or usuario.rol not in Roles.SUPERVISION:
        return jsonify({'error':
            'Dictaminar una avería es del administrador o supervisor del '
            'centro de distribución.'}), 403

    data = request.get_json() or {}

    # Se exige el campo explícito: un `data.get('confirmada')` ausente sería
    # `None`, y `None` ya significa «nadie miró». El payload tiene que decir
    # sí o no, no omitir. Es el mismo criterio que
    # `POST /api/rutas/<r>/recaudos/<x>/confirmar-retencion`.
    if 'confirmada' not in data:
        return jsonify({'error': "Falta 'confirmada' (true/false)"}), 400

    try:
        s = TrasladoService.dictaminar_averia(
            solicitud_id=id,
            usuario_id=usuario_id,
            confirmada=bool(data['confirmada']),
            nota=data.get('nota'),
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
    from app.services.bitacora import registrar_accion, motivo_obligatorio, foto, MotivoRequerido
    try:
        motivo = motivo_obligatorio(data.get('motivo'), 'cancelar un traslado')
    except MotivoRequerido as e:
        return jsonify({'error': str(e)}), 400
    antes = foto(s, ['estado', 'motivo_rechazo'])
    # Liberar reservas de picking antes de cancelar
    if s.estado in ('EN_PICKING', 'PREPARADO'):
        from app.services.traslado_service import TrasladoService
        try:
            TrasladoService._liberar_reservas_traslado(s, usuario_id=usuario_id, motivo=motivo)
        except Exception as _e:
            from app.extensions import db as _db
            _db.session.rollback()
            logger.error(f'[TRASLADO] Error liberando reservas en {id}: {_e}', exc_info=True)
            return jsonify({'error': f'Error liberando reservas de picking: {_e}'}), 500
    s.estado = EstadoTraslado.CANCELADA
    s.motivo_rechazo = motivo
    registrar_accion('CANCELAR', s, usuario_id=usuario_id, motivo=motivo,
                     antes=antes, despues={'estado': s.estado})
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

    # ── Quien confirma que algo llegó tiene que estar donde llegó ──────────
    #
    # Antes acá decía `s.solicitante_id != usuario_id`, y funcionaba **por
    # accidente**: hasta hoy todo traslado lo pedía su propio destinatario, así
    # que «sos el solicitante» y «estás en el destino» eran la misma persona.
    # Medido el 2026-09-18 sobre los 174 traslados de producción: en 166 el
    # solicitante es del destino, y los 8 restantes los creó el Administrador,
    # que entra por `es_admin`. La regla vieja no se apoyaba en un permiso,
    # se apoyaba en una coincidencia.
    #
    # El traslado de averías es el primero que rompe esa coincidencia: lo pide
    # el punto y llega al CD. Con la regla vieja, **el mismo punto que declaró
    # la avería podía confirmar su recepción en el CD**, disparar el ETS y
    # saltarse entero el tercer momento de validación del proceso —«en NB1
    # recepción valida la cantidad»— sin que nadie del CD la hubiera contado.
    #
    # Ahora se pregunta lo que de verdad importa, con la única función que
    # contesta de qué bodega es alguien y que falla cerrada. Esto además
    # subsume el caso del recepcionista del destino, que antes se escribía
    # aparte leyendo solo `bodega_siesa_id` y por eso no veía a quien resuelve
    # su bodega por almacén.
    from app.services.alcance import usuario_es_de_la_bodega
    es_del_destino = usuario_es_de_la_bodega(usuario, s.bodega_destino_siesa)

    if not es_admin and not es_del_destino:
        return jsonify({'error':
            'La recepción la confirma quien está en la bodega de destino '
            f'({s.bodega_destino_siesa}) — es quien puede contar lo que '
            'llegó.'}), 403
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
        from app.utils.fecha import fecha_hoy_bogota
        fecha_hoy = fecha_hoy_bogota()
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
    from app.services.siesa_traslado_adapter import siesa_traslado
    s = SolicitudTraslado.query.get_or_404(id)
    if s.estado not in ('EN_TRANSITO', 'ENTREGADA'):
        return jsonify({'error': f'Solo se puede reintentar despacho en EN_TRANSITO o ENTREGADA (estado: {s.estado})'}), 400
    # Idempotencia, igual que la ruta hermana del ETS (`reintentar-recepcion`).
    # Sin esto, cada clic emitía un STS nuevo: la mercancía se descargaba otra
    # vez del origen y se cargaba otra vez a la bodega de tránsito.
    if s.siesa_salida_consec:
        return jsonify({'error': f'173076 ya registrado (consec={s.siesa_salida_consec})'}), 400

    # Verificar ANTES de reenviar — mismo motivo que ya tiene reintentar-recepcion:
    # un intento previo puede haber recibido 200 de Siesa sin un consecutivo
    # parseable en la respuesta. Sin este chequeo, reintentar sobre uno de esos
    # casos manda un 173076 SEGUNDO y descarga el origen dos veces.
    consec_existente = siesa_traslado.recuperar_consec_salida(s.codigo)
    if consec_existente:
        s.siesa_salida_consec = consec_existente
        s.siesa_error = None
        db.session.commit()
        return jsonify({
            'ok': True,
            'recuperado_sin_reenvio': True,
            'solicitud': s.to_dict(),
        }), 200

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
            if not consec:
                # El STS **sí se creó**: lo que falló fue leer su consecutivo.
                # El servicio ya hace esta recuperación (`traslado_service.py:654`);
                # esta ruta no la hacía, borraba `siesa_error` y respondía
                # `ok: True` — y el botón del PWA se muestra justamente cuando
                # `siesa_salida_consec` está vacío, así que seguía ahí y cada
                # clic emitía otro STS. (`siesa_traslado` ya está importado
                # arriba, en el chequeo proactivo — este import local tenía
                # además la ruta equivocada, `app.services` en vez de
                # `app.services.siesa_traslado_adapter`, y nunca se ejecutó
                # con éxito.)
                logger.warning(
                    '[TRASLADO] %s: consecutivo null en respuesta 173076 — '
                    'intentando recovery', s.codigo)
                consec = siesa_traslado.recuperar_consec_salida(s.codigo)
            if consec:
                s.siesa_salida_consec = consec
            else:
                # No se degrada a éxito. Sin consecutivo el ETS 173079 no se
                # puede emitir nunca y la mercancía queda en la bodega de
                # tránsito — el limbo que los invariantes de traslado existen
                # para detectar. Se declara y se deja el error puesto.
                s.siesa_error = (
                    'El STS (173076) se envió y Siesa no devolvió su '
                    'consecutivo, ni se pudo recuperar. EL DOCUMENTO PUEDE '
                    'EXISTIR: verificalo en Siesa antes de reintentar — otro '
                    'envío descarga la mercancía dos veces.')
                db.session.commit()
                return jsonify({
                    'ok': False,
                    'resultado_desconocido': True,
                    'error': s.siesa_error,
                    'siesa_response': res,
                    'solicitud': s.to_dict(),
                }), 409
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
    from app.services.siesa_traslado_adapter import siesa_traslado
    s = SolicitudTraslado.query.get_or_404(id)

    if s.estado != 'ENTREGADA':
        return jsonify({'error': f'Solo aplica en ENTREGADA (estado: {s.estado})'}), 400
    if s.siesa_entrada_consec:
        return jsonify({'error': f'173079 ya registrado (consec={s.siesa_entrada_consec})'}), 400
    if s.modo_transferencia != 'EN_TRANSITO':
        return jsonify({'error': 'Solo aplica a traslados EN_TRANSITO'}), 400

    # Verificar ANTES de reenviar: un intento previo puede haber recibido 200
    # de Siesa sin un consecutivo parseable en la respuesta (el hueco que
    # `resolver_consecutivo_entrada` existe para cerrar). Sin este chequeo,
    # reintentar sobre uno de esos casos manda un 173079 SEGUNDO y crea un
    # documento de entrada duplicado en Siesa — exactamente lo que el aviso
    # "Verificá primero en Siesa que el documento no exista" advierte.
    consec_existente = siesa_traslado.recuperar_consec_entrada(s.codigo)
    if consec_existente:
        s.siesa_entrada_consec = consec_existente
        s.siesa_error = None
        db.session.commit()
        return jsonify({
            'ok': True,
            'recuperado_sin_reenvio': True,
            'solicitud': s.to_dict(),
        }), 200

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

    _co_destino = co_de_bodega(s.bodega_destino_siesa)

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
        consec, error = TrasladoService.resolver_consecutivo_entrada(s.codigo, res)
        if consec:
            s.siesa_entrada_consec = consec
        s.siesa_error = error
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

    # El parámetro `?bodega=` NO le gana a la bodega del usuario.
    #
    # Antes era `request.args.get('bodega') or (la del usuario)`: la propia era
    # el valor por defecto, no el límite. Y el front la manda desde
    # `localStorage` (`recepcion.js`), así que editarla en el navegador —o
    # llamar la API a mano— alcanzaba para leer la cola de recepción de otro
    # punto, sin tocar código.
    #
    # Los roles administrativos conservan el parámetro: es su ÚNICO acceso
    # —no tienen bodega propia (el admin de producción no tiene ni bodega ni
    # almacén)— y quitárselo los dejaría sin la pantalla entera.
    from app.services.alcance import bodega_del_usuario
    _ADMINISTRATIVOS = ('admin', 'supervisor', 'gerente', 'jefe_almacen')
    _propia = bodega_del_usuario(usuario)
    if usuario.rol in _ADMINISTRATIVOS:
        bodega = request.args.get('bodega') or _propia
    else:
        bodega = _propia
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
            # 'DESPACHADA' no existe en EstadoTraslado y producción no tiene
            # ninguna fila con ese valor — rama muerta, retirada el 2026-09-17.
            SolicitudTraslado.estado == 'EN_TRANSITO',
        )
        .order_by(SolicitudTraslado.fecha_creacion.desc())
        .all()
    )
    return jsonify({'solicitudes': [s.to_dict() for s in solicitudes]}), 200


@traslados_bp.route('/mis-traslados', methods=['GET'])
@jwt_required()
def mis_traslados():
    """Picker: traslados EN_PICKING asignados a mí O de mi bodega origen."""
    try:
        operario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(operario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    from sqlalchemy import or_
    base = SolicitudTraslado.query\
        .options(
            joinedload(SolicitudTraslado.solicitante),
            joinedload(SolicitudTraslado.aprobador),
            joinedload(SolicitudTraslado.operario),
            subqueryload(SolicitudTraslado.items)
            .joinedload(ItemSolicitudTraslado.producto),
        )\
        .filter_by(estado='EN_PICKING')
    filtro_asignado = SolicitudTraslado.operario_id == operario_id
    if usuario.bodega_siesa_id:
        filtro_bodega = SolicitudTraslado.bodega_origen_siesa == usuario.bodega_siesa_id
        solicitudes = base.filter(or_(filtro_asignado, filtro_bodega))
    else:
        solicitudes = base.filter(filtro_asignado)
    solicitudes = solicitudes.order_by(SolicitudTraslado.fecha_aprobacion.desc()).all()
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
    ?bodega=NC1    — filtra por bodega origen (default BODEGA_ORIGEN_DEFAULT)
    ?q=texto       — filtra por nombre/código Siesa (paginado sobre el resultado filtrado)
    ?page=1        — página (default 1)
    ?per_page=30   — tamaño de página (default 30, tope 200)
    ?debug=true    — incluye detalle de lo que Siesa devolvió vs WMS (solo admin)
    ?forzar=true   — invalida cache y recarga desde Siesa

    El cache de `TrasladoService.get_stock_disponible` guarda TODO el
    inventario de la bodega (hasta ~4000 SKU, ver CLAUDE.md — verificado en
    vivo contra Siesa QA) — cargarlo entero en el celular en cada filtro o
    cambio de página es el costo real que un catálogo de esta bodega paga en
    señal de bodega/tienda. La búsqueda y el recorte de página se hacen acá,
    sobre la lista ya cacheada — no se repite el fetch a Siesa por paginar.
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
    q = (request.args.get('q') or '').strip().lower()
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get('per_page', 30))
    except (TypeError, ValueError):
        per_page = 30
    per_page = max(1, min(per_page, 200))

    if forzar:
        TrasladoService.invalidar_cache_stock(bodega)

    try:
        # `cacheado` es el objeto que vive dentro de _stock_siesa_cache — se
        # arma la respuesta sobre una copia para no mutar lo que otras
        # peticiones (y otros usuarios) van a leer del mismo cache.
        cacheado = TrasladoService.get_stock_disponible(bodega, forzar=forzar)
        items_todos = cacheado.get('items', [])

        if q:
            items_filtrados = [
                it for it in items_todos
                if q in (it.get('nombre') or '').lower()
                or q in (it.get('codigo_siesa') or '').lower()
            ]
        else:
            items_filtrados = items_todos

        total_filtrado = len(items_filtrados)
        total_paginas = max(1, -(-total_filtrado // per_page))  # ceil sin float
        page = min(page, total_paginas)
        inicio = (page - 1) * per_page
        items_pagina = items_filtrados[inicio:inicio + per_page]

        resultado = dict(cacheado)
        resultado['items'] = items_pagina
        resultado['pagina'] = page
        resultado['por_pagina'] = per_page
        resultado['total_filtrado'] = total_filtrado
        resultado['total_paginas'] = total_paginas

        if debug and usuario.rol in ('admin', 'supervisor', 'gerente', 'jefe_almacen'):
            # Expone métricas de diagnóstico sin datos sensibles adicionales
            resultado['_debug'] = {
                'bodega': cacheado.get('bodega'),
                'fuente': cacheado.get('fuente'),
                'actualizado_en': cacheado.get('actualizado_en'),
                # La clave que produce el servicio es `siesa_total_productos`
                # (`traslado_service.py:2239`). Acá se leía `siesa_total_rows`,
                # que NO la escribe nadie — grep sobre todo el repo: aparecía
                # solo en esta línea de lectura.
                #
                # `.get()` devolvía `None`, así que este campo salía SIEMPRE
                # nulo al lado de dos enteros. Y un `null` entre números se lee
                # como «Siesa no devolvió filas» — la confusión entre ausencia
                # y vacío, justo en el campo que existe para explicar por qué
                # faltan ítems.
                'siesa_total_productos': cacheado.get('siesa_total_productos'),
                'siesa_con_stock': cacheado.get('siesa_con_stock'),
                'wms_mapeados': cacheado.get('total'),
                # `sin_mapeo` sale NEGATIVO cuando el fallback `_get_stock_wms`
                # contesta: ese camino no devuelve `siesa_con_stock`, así que
                # la resta es `0 - total`. Un número negativo acá no significa
                # «sobran mapeos»: significa que Siesa no contestó y se está
                # mirando el stock del WMS. Se declara en vez de restar a
                # ciegas.
                'sin_mapeo': (
                    (cacheado.get('siesa_con_stock') - (cacheado.get('total') or 0))
                    if cacheado.get('siesa_con_stock') is not None else None),
                'sin_mapeo_nota': (
                    None if cacheado.get('siesa_con_stock') is not None
                    else 'Siesa no contestó: esta respuesta viene del stock '
                         'del WMS y no hay con qué comparar.'),
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
