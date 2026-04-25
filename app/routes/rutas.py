"""
Módulo de Rutas y Manifiestos — trazabilidad Milla Cero.
Conductores + Vehículos + RutaMaestra (plantilla) + RutaDespacho (instancia/viaje)
"""
import logging
from datetime import datetime, date
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.bulto import Bulto, EstadoBulto
from app.models.conductor import Conductor
from app.models.packing import TareaPacking, EstadoPacking
from app.models.recaudo_entrega import RecaudoEntrega
from app.models.vehiculo import Vehiculo
from app.models.ruta_maestra import RutaMaestra, RutaMaestraParada
from app.models.ruta_despacho import RutaDespacho, EstadoRutaDespacho
from sqlalchemy.orm import selectinload as _sl, joinedload as _jl
from sqlalchemy.exc import IntegrityError as _IntegrityError
from app.routes._auth_helpers import _es_admin_o_jefe, _solo_admin

logger = logging.getLogger(__name__)

rutas_bp = Blueprint('rutas', __name__)


class EstadoEntrega:
    ENTREGADO = 'ENTREGADO'
    PARCIAL   = 'PARCIAL'
    RECHAZADO = 'RECHAZADO'
    VALIDOS   = (ENTREGADO, PARCIAL, RECHAZADO)


class FormaPago:
    EFECTIVO     = 'EFECTIVO'
    TRANSFERENCIA = 'TRANSFERENCIA'
    CHEQUE       = 'CHEQUE'
    CREDITO      = 'CREDITO'
    EXENTO       = 'EXENTO'
    VALIDOS      = (EFECTIVO, TRANSFERENCIA, CHEQUE, CREDITO, EXENTO)


def _procesar_confirmacion_parada(ruta_id, tarea_id, usuario_id, data):
    """Lógica de bultos y recaudo extraída de confirmar_parada para testabilidad."""
    # Lock pesimista — evita double-submit del conductor o reintento de red
    # que podría crear dos RecaudoEntrega para la misma parada
    bultos_tarea = (Bulto.query
                    .filter_by(tarea_id=tarea_id, ruta_despacho_id=ruta_id)
                    .with_for_update()
                    .all())
    if not bultos_tarea:
        raise ValueError('Esta factura no pertenece a la ruta')

    estado_entrega = data.get('estado_entrega', '').upper()
    if estado_entrega not in EstadoEntrega.VALIDOS:
        raise ValueError(f'estado_entrega debe ser {", ".join(EstadoEntrega.VALIDOS)}')

    forma_pago = data.get('forma_pago', '').upper() or None
    if forma_pago and forma_pago not in FormaPago.VALIDOS:
        raise ValueError(f'forma_pago inválido. Válidos: {", ".join(FormaPago.VALIDOS)}')

    foto = data.get('foto_entrega', '') or None
    if foto and len(foto) > 1_150_000:
        raise ValueError('Foto demasiado grande. Máximo ~800KB JPEG.')

    ids_tarea = {b.id for b in bultos_tarea}
    bultos_rechazados_ids = data.get('bultos_rechazados', [])
    if estado_entrega == EstadoEntrega.RECHAZADO and not bultos_rechazados_ids:
        bultos_rechazados_ids = [b.id for b in bultos_tarea]
    if estado_entrega == EstadoEntrega.PARCIAL and not bultos_rechazados_ids:
        raise ValueError('Para entrega parcial debes indicar cuáles bultos fueron rechazados')

    ahora = datetime.utcnow()
    ids_rechazados_set = set(bultos_rechazados_ids)

    for b in bultos_tarea:
        if b.id in ids_rechazados_set:
            b.estado = EstadoBulto.RECHAZADO
            b.motivo_rechazo = data.get('observaciones', 'Rechazado en entrega')[:100]
            b.fecha_entrega = ahora
        else:
            b.estado = EstadoBulto.ENTREGADO
            b.fecha_entrega = ahora

    recaudo = RecaudoEntrega.query.filter_by(ruta_id=ruta_id, tarea_id=tarea_id).first()
    es_edicion = recaudo is not None

    if not recaudo:
        recaudo = RecaudoEntrega(
            ruta_id=ruta_id,
            tarea_id=tarea_id,
            fecha_creacion=ahora,
            confirmado_por=usuario_id,
        )
        db.session.add(recaudo)
    else:
        recaudo.editado_por = usuario_id
        recaudo.editado_en = ahora

    recaudo.estado_entrega        = estado_entrega
    recaudo.forma_pago            = forma_pago
    recaudo.monto_cobrado         = data.get('monto_cobrado', 0) or 0
    recaudo.observaciones         = data.get('observaciones', '') or None
    recaudo.foto_entrega          = foto
    recaudo.bultos_rechazados_ids = list(ids_rechazados_set & ids_tarea)
    recaudo.fecha_confirmacion    = ahora

    db.session.commit()
    return recaudo, es_edicion


# ── Conductores ──────────────────────────────────────────────────

@rutas_bp.route('/conductores', methods=['GET'])
@jwt_required()
def listar_conductores():
    solo_activos = request.args.get('activos', 'true').lower() == 'true'
    q = Conductor.query.order_by(Conductor.nombre)
    if solo_activos:
        q = q.filter_by(activo=True)

    # [23] Cédulas y teléfonos solo visibles para admin/jefe_almacen
    puede_ver_datos_personales = bool(_es_admin_o_jefe())

    def _conductor_safe(c):
        d = c.to_dict()
        if not puede_ver_datos_personales:
            d.pop('cedula', None)
            d.pop('telefono', None)
            d.pop('usuario_email', None)
        return d

    return jsonify({'conductores': [_conductor_safe(c) for c in q.all()]}), 200


@rutas_bp.route('/conductores', methods=['POST'])
@jwt_required()
def crear_conductor():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede registrar conductores'}), 403
    data = request.get_json()
    for campo in ['nombre', 'cedula']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    if Conductor.query.filter_by(cedula=data['cedula']).first():
        return jsonify({'error': 'Ya existe un conductor con esa cédula'}), 409
    c = Conductor(
        nombre=data['nombre'].strip(),
        cedula=data['cedula'].strip(),
        telefono=data.get('telefono', '').strip() or None,
        usuario_id=data.get('usuario_id') or None,
    )
    db.session.add(c)
    db.session.commit()
    db.session.refresh(c)   # recarga relaciones (usuario) expiradas por expire_on_commit
    return jsonify({'conductor': c.to_dict()}), 201


@rutas_bp.route('/conductores/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_conductor(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar conductores'}), 403
    from app.models.usuario import Usuario
    c = Conductor.query.get_or_404(id)
    data = request.get_json()
    if 'nombre'     in data: c.nombre     = (data['nombre'] or '').strip()
    if 'telefono'   in data: c.telefono   = (data['telefono'] or '').strip() or None
    if 'activo'     in data: c.activo     = bool(data['activo'])
    if 'usuario_id' in data: c.usuario_id = data['usuario_id'] or None
    db.session.commit()
    db.session.refresh(c)
    return jsonify({'conductor': c.to_dict()}), 200


@rutas_bp.route('/conductores/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_conductor(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede desactivar conductores'}), 403
    c = Conductor.query.get_or_404(id)
    c.activo = False
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── Vehículos ─────────────────────────────────────────────────────

@rutas_bp.route('/vehiculos', methods=['GET'])
@jwt_required()
def listar_vehiculos():
    solo_activos = request.args.get('activos', 'true').lower() == 'true'
    q = Vehiculo.query.order_by(Vehiculo.placa)
    if solo_activos:
        q = q.filter_by(activo=True)
    return jsonify({'vehiculos': [v.to_dict() for v in q.all()]}), 200


@rutas_bp.route('/vehiculos', methods=['POST'])
@jwt_required()
def crear_vehiculo():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede registrar vehículos'}), 403
    data = request.get_json()
    for campo in ['placa', 'tipo']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    placa = data['placa'].strip().upper()
    if Vehiculo.query.filter_by(placa=placa).first():
        return jsonify({'error': f'Ya existe un vehículo con placa {placa}'}), 409
    v = Vehiculo(placa=placa, tipo=data['tipo'].strip(),
                 capacidad_kg=data.get('capacidad_kg') or None)
    db.session.add(v)
    db.session.commit()
    return jsonify({'vehiculo': v.to_dict()}), 201


@rutas_bp.route('/vehiculos/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_vehiculo(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar vehículos'}), 403
    v = Vehiculo.query.get_or_404(id)
    data = request.get_json()
    if 'tipo'         in data: v.tipo         = data['tipo'].strip()
    if 'capacidad_kg' in data: v.capacidad_kg = data['capacidad_kg'] or None
    if 'activo'       in data: v.activo       = bool(data['activo'])
    db.session.commit()
    return jsonify({'vehiculo': v.to_dict()}), 200


@rutas_bp.route('/vehiculos/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_vehiculo(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede desactivar vehículos'}), 403
    v = Vehiculo.query.get_or_404(id)
    v.activo = False
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── Rutas Maestras ───────────────────────────────────────────────

@rutas_bp.route('/maestras', methods=['GET'])
@jwt_required()
def listar_maestras():
    solo_activas = request.args.get('activas', 'true').lower() == 'true'
    q = RutaMaestra.query.options(_sl(RutaMaestra.paradas)).order_by(RutaMaestra.nombre)
    if solo_activas:
        q = q.filter_by(activa=True)
    return jsonify({'maestras': [m.to_dict() for m in q.all()]}), 200


@rutas_bp.route('/maestras/<int:id>', methods=['GET'])
@jwt_required()
def obtener_maestra(id):
    m = RutaMaestra.query.get_or_404(id)
    return jsonify({'maestra': m.to_dict()}), 200


@rutas_bp.route('/maestras', methods=['POST'])
@jwt_required()
def crear_maestra():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear rutas maestras'}), 403
    data = request.get_json()
    for campo in ['nombre', 'tipo_ruta']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    if data['tipo_ruta'] not in ('Urbana', 'Municipal'):
        return jsonify({'error': 'tipo_ruta debe ser Urbana o Municipal'}), 400
    if RutaMaestra.query.filter_by(nombre=data['nombre'].strip()).first():
        return jsonify({'error': 'Ya existe una ruta maestra con ese nombre'}), 409

    m = RutaMaestra(nombre=data['nombre'].strip(), tipo_ruta=data['tipo_ruta'])
    db.session.add(m)
    db.session.flush()   # obtener m.id antes de agregar paradas

    paradas = data.get('paradas', [])
    for i, municipio in enumerate(paradas):
        nombre = municipio['municipio'] if isinstance(municipio, dict) else municipio
        nombre = (nombre or '').strip()
        if nombre:
            db.session.add(RutaMaestraParada(
                ruta_maestra_id=m.id,
                municipio=nombre,
                orden=i + 1
            ))

    try:
        db.session.commit()
    except _IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Ya existe una ruta maestra con ese nombre'}), 409
    db.session.refresh(m)   # recarga paradas expiradas por expire_on_commit
    return jsonify({'maestra': m.to_dict()}), 201


@rutas_bp.route('/maestras/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_maestra(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar rutas maestras'}), 403
    m = RutaMaestra.query.get_or_404(id)
    data = request.get_json()
    if 'nombre'    in data: m.nombre    = data['nombre'].strip()
    if 'tipo_ruta' in data: m.tipo_ruta = data['tipo_ruta']
    if 'activa'    in data: m.activa    = bool(data['activa'])

    if 'paradas' in data:
        # Reemplazar paradas completas
        for p in m.paradas:
            db.session.delete(p)
        db.session.flush()
        for i, municipio in enumerate(data['paradas']):
            nombre = municipio['municipio'] if isinstance(municipio, dict) else municipio
            nombre = (nombre or '').strip()
            if nombre:
                db.session.add(RutaMaestraParada(
                    ruta_maestra_id=m.id,
                    municipio=nombre,
                    orden=i + 1
                ))

    db.session.commit()
    db.session.refresh(m)
    return jsonify({'maestra': m.to_dict()}), 200


@rutas_bp.route('/maestras/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_maestra(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede desactivar rutas maestras'}), 403
    m = RutaMaestra.query.get_or_404(id)
    m.activa = False
    db.session.commit()
    return jsonify({'ok': True}), 200


# ── Programar viaje desde plantilla ─────────────────────────────

@rutas_bp.route('/programar', methods=['POST'])
@jwt_required()
def programar_viaje():
    """
    Crea un RutaDespacho en estado PROGRAMADO a partir de una RutaMaestra.
    Solo admin o jefe de almacén.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede programar viajes'}), 403
    data = request.get_json()
    for campo in ['ruta_maestra_id', 'fecha_programada', 'conductor_id', 'vehiculo_id']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    maestra = RutaMaestra.query.get(data['ruta_maestra_id'])
    if not maestra or not maestra.activa:
        return jsonify({'error': 'Ruta maestra no encontrada o inactiva'}), 404

    conductor = Conductor.query.get(data['conductor_id'])
    if not conductor or not conductor.activo:
        return jsonify({'error': 'Conductor no encontrado o inactivo'}), 404

    vehiculo = Vehiculo.query.get(data['vehiculo_id'])
    if not vehiculo or not vehiculo.activo:
        return jsonify({'error': 'Vehículo no encontrado o inactivo'}), 404

    try:
        fecha = date.fromisoformat(data['fecha_programada'])
    except ValueError:
        return jsonify({'error': 'fecha_programada debe ser YYYY-MM-DD'}), 400

    ruta = RutaDespacho(
        ruta_maestra_id=maestra.id,
        conductor_id=conductor.id,
        vehiculo_id=vehiculo.id,
        tipo_ruta=maestra.tipo_ruta,
        fecha_programada=fecha,
        notas=data.get('notas', '').strip() or None,
        estado=EstadoRutaDespacho.PROGRAMADO,
    )
    db.session.add(ruta)
    db.session.commit()
    return jsonify({'ruta': ruta.to_dict()}), 201


# ── Rutas de Despacho ────────────────────────────────────────────

@rutas_bp.route('/', methods=['GET'])
@jwt_required()
def listar_rutas():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(uid)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 401

    fecha     = request.args.get('fecha')
    cond_id   = request.args.get('conductor_id', type=int)
    veh_id    = request.args.get('vehiculo_id', type=int)
    estado    = request.args.get('estado')
    page      = request.args.get('page', 1, type=int)

    q = (RutaDespacho.query
         .options(
             _jl(RutaDespacho.conductor),
             _jl(RutaDespacho.vehiculo),
             _jl(RutaDespacho.ruta_maestra),
             _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
         )
         .order_by(RutaDespacho.fecha_creacion.desc()))

    # Conductores solo ven sus propias rutas
    if usuario.rol == 'conductor':
        conductor = Conductor.query.filter_by(usuario_id=uid).first()
        if not conductor:
            return jsonify({'rutas': [], 'total': 0}), 200
        q = q.filter_by(conductor_id=conductor.id)
    else:
        if cond_id:
            q = q.filter_by(conductor_id=cond_id)

    if estado:
        q = q.filter_by(estado=estado)
    if veh_id:
        q = q.filter_by(vehiculo_id=veh_id)
    if fecha:
        from sqlalchemy import func
        q = q.filter(func.date(RutaDespacho.fecha_creacion) == fecha)

    rutas = q.paginate(page=page, per_page=50, error_out=False)
    return jsonify({'rutas': [r.to_dict() for r in rutas.items], 'total': rutas.total}), 200


@rutas_bp.route('/', methods=['POST'])
@jwt_required()
def crear_ruta():
    """Crea una RutaDespacho ad-hoc (sin plantilla) directo en EN_CARGUE. Solo admin/jefe."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede crear rutas'}), 403
    data = request.get_json()
    for campo in ['conductor_id', 'vehiculo_id', 'tipo_ruta']:
        if not data.get(campo):
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    if data['tipo_ruta'] not in ('Urbana', 'Municipal'):
        return jsonify({'error': 'tipo_ruta debe ser Urbana o Municipal'}), 400

    conductor = Conductor.query.get(data['conductor_id'])
    if not conductor or not conductor.activo:
        return jsonify({'error': 'Conductor no encontrado o inactivo'}), 404
    vehiculo = Vehiculo.query.get(data['vehiculo_id'])
    if not vehiculo or not vehiculo.activo:
        return jsonify({'error': 'Vehículo no encontrado o inactivo'}), 404

    ruta = RutaDespacho(
        conductor_id=data['conductor_id'],
        vehiculo_id=data['vehiculo_id'],
        tipo_ruta=data['tipo_ruta'],
        notas=data.get('notas', '').strip() or None,
        estado=EstadoRutaDespacho.EN_CARGUE,
    )
    db.session.add(ruta)
    db.session.commit()
    return jsonify({'ruta': ruta.to_dict()}), 201


@rutas_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_ruta(id):
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    ruta = (RutaDespacho.query
            .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea))
            .get_or_404(id))
    # Conductores solo pueden ver sus propias rutas
    usuario = Usuario.query.get(uid)
    if usuario and usuario.rol == 'conductor':
        conductor = Conductor.query.filter_by(usuario_id=uid).first()
        if not conductor or ruta.conductor_id != conductor.id:
            return jsonify({'error': 'Sin permiso para ver esta ruta'}), 403
    return jsonify({'ruta': ruta.to_dict(include_bultos=True)}), 200


@rutas_bp.route('/<int:id>/iniciar', methods=['POST'])
@jwt_required()
def iniciar_ruta(id):
    """
    PROGRAMADO → EN_CARGUE.
    Devuelve además los bultos sugeridos por municipio de la ruta maestra.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede iniciar el cargue'}), 403
    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != EstadoRutaDespacho.PROGRAMADO:
        return jsonify({'error': f'La ruta debe estar PROGRAMADO, está {ruta.estado}'}), 400

    ruta.estado = EstadoRutaDespacho.EN_CARGUE
    db.session.commit()

    sugeridos_ids = []
    if ruta.ruta_maestra:
        municipios = {p.municipio.lower() for p in ruta.ruta_maestra.paradas}
        from app.models.packing import TareaPacking
        bultos_libres = (Bulto.query
            .options(_sl(Bulto.tarea))
            .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
            .filter(
                TareaPacking.siesa_triggered == True,
                TareaPacking.estado != EstadoPacking.CANCELADO,
                Bulto.estado == EstadoBulto.PENDIENTE,
                Bulto.ruta_despacho_id == None,
            ).all())
        sugeridos_ids = [b.id for b in bultos_libres
                         if (b.tarea.municipio or '').lower() in municipios]

    return jsonify({
        'ok': True,
        'ruta': ruta.to_dict(),
        'sugeridos_count': len(sugeridos_ids),
        'sugeridos_ids': sugeridos_ids,
    }), 200


@rutas_bp.route('/<int:id>/sugeridos', methods=['GET'])
@jwt_required()
def sugeridos_ruta(id):
    """Bultos sin asignar que coinciden con los municipios de la ruta maestra."""
    ruta = RutaDespacho.query.get_or_404(id)
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    conductor_ruta = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    if not _es_admin_o_jefe() and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a los sugeridos de esta ruta'}), 403

    if not ruta.ruta_maestra:
        return jsonify({'sugeridos': [], 'total': 0}), 200

    municipios = {p.municipio.lower() for p in ruta.ruta_maestra.paradas}
    bultos_libres = (Bulto.query
        .options(_sl(Bulto.tarea))
        .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
        .filter(
            TareaPacking.siesa_triggered == True,
            TareaPacking.estado != 'CANCELADO',
            Bulto.estado == EstadoBulto.PENDIENTE,
            Bulto.ruta_despacho_id == None,
        ).all())

    sugeridos = [b.to_dict() for b in bultos_libres
                 if (b.tarea.municipio or '').lower() in municipios]

    return jsonify({'sugeridos': sugeridos, 'total': len(sugeridos)}), 200


@rutas_bp.route('/<int:id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_ruta(id):
    """EN_CARGUE → EN_TRANSITO. Solo admin/jefe."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede cerrar rutas'}), 403
    ruta = (RutaDespacho.query
            .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea))
            .get_or_404(id))
    if ruta.estado != EstadoRutaDespacho.EN_CARGUE:
        return jsonify({'error': f'La ruta ya está en estado {ruta.estado}'}), 400
    if not ruta.bultos:
        return jsonify({'error': 'No hay bultos asignados a esta ruta'}), 400

    sin_confirmar = Bulto.query.filter_by(ruta_despacho_id=ruta.id, estado=EstadoBulto.PENDIENTE).count()
    if sin_confirmar > 0:
        return jsonify({
            'error': f'Faltan {sin_confirmar} bulto{"s" if sin_confirmar != 1 else ""} por confirmar. '
                     f'Escanéalos en el muelle antes de cerrar la ruta.'
        }), 400

    _n_bultos = len(ruta.bultos)  # capturar antes del commit
    ruta.estado = EstadoRutaDespacho.EN_TRANSITO
    ruta.fecha_cierre = datetime.utcnow()
    db.session.commit()
    logger.info(f'[RUTAS] Ruta {ruta.id} EN_CARGUE → EN_TRANSITO ({_n_bultos} bultos)')
    return jsonify({'ok': True, 'ruta': ruta.to_dict(include_bultos=True)}), 200


@rutas_bp.route('/<int:id>/entregar', methods=['POST'])
@jwt_required()
def entregar_ruta(id):
    """
    Confirmación de entrega bulto a bulto.

    Payload: { bultos: [ { id, entregado: true/false, motivo_rechazo: '' } ] }

    - Bultos entregados: estado → ENTREGADO
    - Bultos rechazados: estado → RECHAZADO + motivo_rechazo guardado
    - Si no se manda payload (flujo legacy sin bultos): cierra directo.
    - Ruta → ENTREGADA al final.
    """

    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != EstadoRutaDespacho.EN_TRANSITO:
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO, está {ruta.estado}'}), 400

    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    conductor_ruta = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    if not _es_admin_o_jefe() and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a esta ruta'}), 403

    data = request.get_json() or {}
    ahora = datetime.utcnow()
    rechazados = 0

    confirmaciones = data.get('bultos', [])
    if confirmaciones:
        ids_payload = {c['id'] for c in confirmaciones}
        bultos_ruta = Bulto.query.filter_by(ruta_despacho_id=id).with_for_update().all()

        for bulto in bultos_ruta:
            conf = next((c for c in confirmaciones if c['id'] == bulto.id), None)
            entregado = conf.get('entregado', True) if conf else True

            if entregado:
                bulto.estado = EstadoBulto.ENTREGADO
                bulto.fecha_entrega = ahora
            else:
                bulto.estado = EstadoBulto.RECHAZADO
                bulto.fecha_entrega = ahora
                bulto.motivo_rechazo = conf.get('motivo_rechazo', 'Sin especificar') if conf else 'Sin especificar'
                rechazados += 1

    ruta.estado = EstadoRutaDespacho.ENTREGADA
    ruta.fecha_entregada = ahora
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f'[RUTAS] Error en entregar_ruta {id}: {e}', exc_info=True)
        return jsonify({'error': 'Error registrando entrega de ruta — reintenta'}), 500

    return jsonify({
        'ok': True,
        'entregados': len(confirmaciones) - rechazados if confirmaciones else 0,
        'rechazados': rechazados,
        'ruta': ruta.to_dict()
    }), 200


@rutas_bp.route('/mis-rutas', methods=['GET'])
@jwt_required()
def mis_rutas():
    """
    Para el conductor autenticado: devuelve sus rutas EN_TRANSITO.
    Usa la vinculación Conductor.usuario_id → JWT identity.
    """
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    conductor = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    if not conductor:
        return jsonify({'error': 'Tu cuenta no está vinculada a ningún conductor'}), 404

    rutas = (RutaDespacho.query
             .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea))
             .filter_by(conductor_id=conductor.id, estado=EstadoRutaDespacho.EN_TRANSITO)
             .order_by(RutaDespacho.fecha_cierre.desc())
             .all())
    return jsonify({
        'conductor': conductor.to_dict(),
        'rutas': [r.to_dict(include_bultos=True) for r in rutas]
    }), 200


@rutas_bp.route('/usuarios-conductores', methods=['GET'])
@jwt_required()
def usuarios_conductores():
    """Lista de usuarios con rol conductor — solo para admin/jefe_almacen al registrar flota."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere admin o jefe_almacen'}), 403
    from app.models.usuario import Usuario
    usuarios = (Usuario.query
                .filter_by(rol='conductor', activo=True)
                .order_by(Usuario.nombre)
                .all())
    return jsonify({'usuarios': [{'id': u.id, 'nombre': u.nombre, 'email': u.email} for u in usuarios]}), 200


@rutas_bp.route('/bultos-rechazados', methods=['GET'])
@jwt_required()
def bultos_rechazados():
    """Bultos rechazados en entrega — auditoría para admin/jefe_almacen."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere admin o jefe_almacen'}), 403
    bultos = (Bulto.query
              .filter_by(estado=EstadoBulto.RECHAZADO)
              .order_by(Bulto.fecha_entrega.desc())
              .all())
    return jsonify({'bultos': [b.to_dict() for b in bultos], 'total': len(bultos)}), 200


# ── Última Milla: paradas y recaudos ────────────────────────────────


@rutas_bp.route('/<int:id>/paradas', methods=['GET'])
@jwt_required()
def listar_paradas(id):
    """
    Devuelve las facturas (TareaPacking) de la ruta con sus bultos y recaudo.
    Accesible por admin/jefe y por el conductor dueño de la ruta.
    """

    ruta = (RutaDespacho.query
            .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea))
            .get_or_404(id))
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401

    # Verificar acceso: admin/jefe o conductor vinculado
    conductor_ruta = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    es_admin = _es_admin_o_jefe()
    if not es_admin and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a esta ruta'}), 403

    # Agrupar bultos por tarea
    tareas_map = {}
    for b in ruta.bultos:
        if not b.tarea:
            continue
        tid = b.tarea_id
        if tid not in tareas_map:
            t = b.tarea
            tareas_map[tid] = {
                'tarea_id':       tid,
                'numero_pedido':  t.numero_pedido_siesa,
                'cliente':        t.cliente or '',
                'municipio':      t.municipio or '',
                'bultos':         [],
                'recaudo':        None,
            }
        tareas_map[tid]['bultos'].append({
            'id':            b.id,
            'codigo_barras': b.codigo_barras,
            'tipo':          b.tipo,
            'numero':        b.numero,
            'total':         b.total,
            'estado':        b.estado,
        })

    # Adjuntar recaudos
    recaudos = RecaudoEntrega.query.filter_by(ruta_id=id).all()
    for r in recaudos:
        if r.tarea_id in tareas_map:
            tareas_map[r.tarea_id]['recaudo'] = r.to_dict()

    paradas = sorted(tareas_map.values(), key=lambda x: (x['municipio'], x['cliente']))
    return jsonify({
        'paradas':             paradas,
        'total_paradas':       len(paradas),
        'paradas_gestionadas': sum(1 for p in paradas if p['recaudo']),
    }), 200


@rutas_bp.route('/<int:id>/paradas/<int:tarea_id>/confirmar', methods=['POST'])
@jwt_required()
def confirmar_parada(id, tarea_id):
    """
    Registra el resultado de entrega de una factura (parada).
    Payload: {
        estado_entrega: ENTREGADO|PARCIAL|RECHAZADO,
        forma_pago: EFECTIVO|TRANSFERENCIA|CHEQUE|CREDITO|EXENTO,
        monto_cobrado: 150000,
        observaciones: '',
        foto_entrega: 'base64...',   # opcional, máx ~800KB JPEG
        bultos_rechazados: [id, ...] # requerido si PARCIAL o RECHAZADO
    }
    """
    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != EstadoRutaDespacho.EN_TRANSITO:
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO, está {ruta.estado}'}), 400

    TareaPacking.query.get_or_404(tarea_id)

    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    conductor_ruta = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    if not _es_admin_o_jefe() and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a esta ruta'}), 403

    data = request.get_json() or {}
    try:
        recaudo, es_edicion = _procesar_confirmacion_parada(id, tarea_id, usuario_id, data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({
        'ok':         True,
        'recaudo':    recaudo.to_dict(),
        'es_edicion': es_edicion,
    }), 200


@rutas_bp.route('/<int:id>/planilla', methods=['GET'])
@jwt_required()
def planilla_ruta(id):
    """
    Vista de planilla completa para admin: todas las paradas con recaudos y totales.
    """

    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede ver la planilla'}), 403

    ruta = (RutaDespacho.query
            .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
                     _sl(RutaDespacho.recaudos))
            .get_or_404(id))
    tareas = ruta.tareas_unicas()
    recaudos_map = {r.tarea_id: r for r in ruta.recaudos}

    paradas = []
    totales = {'EFECTIVO': 0, 'TRANSFERENCIA': 0, 'CHEQUE': 0, 'CREDITO': 0, 'EXENTO': 0}
    sin_gestionar = 0

    for t in tareas:
        r = recaudos_map.get(t.id)
        bultos_t = [b for b in ruta.bultos if b.tarea_id == t.id]
        parada = {
            'tarea_id':       t.id,
            'numero_pedido':  t.numero_pedido_siesa,
            'cliente':        t.cliente or '',
            'municipio':      t.municipio or '',
            'bultos_total':   len(bultos_t),
            'bultos_entregados': sum(1 for b in bultos_t if b.estado == EstadoBulto.ENTREGADO),
            'bultos_rechazados': sum(1 for b in bultos_t if b.estado == EstadoBulto.RECHAZADO),
            'recaudo':        r.to_dict() if r else None,
        }
        paradas.append(parada)
        if r:
            fp = (r.forma_pago or '').upper()
            if fp in totales:
                totales[fp] += float(r.monto_cobrado or 0)
        else:
            sin_gestionar += 1

    return jsonify({
        'ruta':            ruta.to_dict(),
        'paradas':         sorted(paradas, key=lambda x: (x['municipio'], x['cliente'])),
        'total_paradas':   len(paradas),
        'sin_gestionar':   sin_gestionar,
        'total_recaudado': ruta.total_recaudado(),
        'totales_por_forma': totales,
        'estado_financiero': ruta.estado_financiero or 'PENDIENTE',
    }), 200


@rutas_bp.route('/<int:id>/liquidar', methods=['POST'])
@jwt_required()
def liquidar_ruta(id):
    """
    Marca la ruta como LIQUIDADA financieramente.
    Bloquea si hay paradas sin gestionar.
    Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede liquidar rutas'}), 403

    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado not in (EstadoRutaDespacho.EN_TRANSITO, EstadoRutaDespacho.ENTREGADA):
        return jsonify({'error': f'No se puede liquidar una ruta en estado {ruta.estado}'}), 400

    tareas = ruta.tareas_unicas()
    gestionadas = RecaudoEntrega.query.filter_by(ruta_id=id).count()
    sin_gestionar = len(tareas) - gestionadas

    if sin_gestionar > 0:
        return jsonify({
            'error': f'Faltan {sin_gestionar} parada{"s" if sin_gestionar != 1 else ""} por gestionar antes de liquidar.'
        }), 400

    ruta.estado_financiero = 'LIQUIDADA'
    db.session.commit()

    return jsonify({
        'ok':              True,
        'total_recaudado': ruta.total_recaudado(),
        'ruta':            ruta.to_dict(),
    }), 200


@rutas_bp.route('/<int:id>/forzar-cierre', methods=['POST'])
@jwt_required()
def forzar_cierre_ruta(id):
    """
    Admin fuerza el cierre de una ruta EN_TRANSITO aunque queden paradas sin gestionar.
    Las paradas sin recaudo quedan registradas con RECHAZADO automático (motivo: cierre forzado).
    Útil cuando el conductor tiene paradas inaccesibles o el sistema de pago falló.
    Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede forzar el cierre de rutas'}), 403

    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != EstadoRutaDespacho.EN_TRANSITO:
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO para forzar cierre (estado: {ruta.estado})'}), 400


    try:
        admin_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    tareas = ruta.tareas_unicas()
    recaudos_existentes = {r.tarea_id for r in RecaudoEntrega.query.filter_by(ruta_id=id).all()}
    # Comparar t.id (int) contra el set de tarea_id (int) — evita comparar objeto vs int
    pendientes = [t for t in tareas if t.id not in recaudos_existentes]
    ahora = datetime.utcnow()
    auto_cerradas = 0

    # Pre-cargar todos los bultos de la ruta para evitar N+1 dentro del bucle
    bultos_por_tarea: dict = {}
    for b in Bulto.query.filter_by(ruta_despacho_id=id).all():
        bultos_por_tarea.setdefault(b.tarea_id, []).append(b)

    for tarea in pendientes:
        bultos_tarea = bultos_por_tarea.get(tarea.id, [])
        for b in bultos_tarea:
            b.estado = EstadoBulto.RECHAZADO
            b.motivo_rechazo = 'Cierre forzado por admin'
            b.fecha_entrega = ahora

        recaudo = RecaudoEntrega(
            ruta_id=id,
            tarea_id=tarea.id,
            estado_entrega=EstadoEntrega.RECHAZADO,
            forma_pago=None,
            monto_cobrado=0,
            observaciones='Cierre forzado por administrador — parada no gestionada',
            confirmado_por=admin_id,
            fecha_creacion=ahora,
        )
        db.session.add(recaudo)
        auto_cerradas += 1

    ruta.estado = EstadoRutaDespacho.ENTREGADA
    ruta.estado_financiero = 'LIQUIDADA'
    ruta.fecha_cierre = ahora
    db.session.commit()

    return jsonify({
        'ok': True,
        'paradas_auto_cerradas': auto_cerradas,
        'mensaje': f'Ruta cerrada. {auto_cerradas} parada(s) registradas como rechazadas automáticamente.',
        'ruta': ruta.to_dict(),
    }), 200
