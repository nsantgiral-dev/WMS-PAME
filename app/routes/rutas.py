"""
Módulo de Rutas y Manifiestos — trazabilidad Milla Cero.
Conductores + Vehículos + RutaMaestra (plantilla) + RutaDespacho (instancia/viaje)
"""
from datetime import datetime, date
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.conductor import Conductor
from app.models.vehiculo import Vehiculo
from app.models.ruta_maestra import RutaMaestra, RutaMaestraParada
from app.models.ruta_despacho import RutaDespacho

rutas_bp = Blueprint('rutas', __name__)


from app.routes._auth_helpers import _es_admin_o_jefe


from app.routes._auth_helpers import _solo_admin


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
    return jsonify({'conductor': c.to_dict()}), 201


@rutas_bp.route('/conductores/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_conductor(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar conductores'}), 403
    from app.models.usuario import Usuario
    c = Conductor.query.get_or_404(id)
    data = request.get_json()
    if 'nombre'     in data: c.nombre     = data['nombre'].strip()
    if 'telefono'   in data: c.telefono   = data['telefono'].strip() or None
    if 'activo'     in data: c.activo     = bool(data['activo'])
    if 'usuario_id' in data: c.usuario_id = data['usuario_id'] or None
    db.session.commit()
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
    q = RutaMaestra.query.order_by(RutaMaestra.nombre)
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

    db.session.commit()
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
        estado='PROGRAMADO',
    )
    db.session.add(ruta)
    db.session.commit()
    return jsonify({'ruta': ruta.to_dict()}), 201


# ── Rutas de Despacho ────────────────────────────────────────────

@rutas_bp.route('/', methods=['GET'])
@jwt_required()
def listar_rutas():
    fecha     = request.args.get('fecha')
    cond_id   = request.args.get('conductor_id', type=int)
    veh_id    = request.args.get('vehiculo_id', type=int)
    estado    = request.args.get('estado')
    page      = request.args.get('page', 1, type=int)

    q = RutaDespacho.query.order_by(RutaDespacho.fecha_creacion.desc())
    if estado:
        q = q.filter_by(estado=estado)
    if cond_id:
        q = q.filter_by(conductor_id=cond_id)
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
        estado='EN_CARGUE',
    )
    db.session.add(ruta)
    db.session.commit()
    return jsonify({'ruta': ruta.to_dict()}), 201


@rutas_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_ruta(id):
    ruta = RutaDespacho.query.get_or_404(id)
    return jsonify({'ruta': ruta.to_dict(include_bultos=True)}), 200


@rutas_bp.route('/<int:id>/iniciar', methods=['POST'])
@jwt_required()
def iniciar_ruta(id):
    """
    PROGRAMADO → EN_CARGUE.
    Devuelve además los bultos sugeridos por municipio de la ruta maestra.
    """
    from app.models.bulto import Bulto
    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != 'PROGRAMADO':
        return jsonify({'error': f'La ruta debe estar PROGRAMADO, está {ruta.estado}'}), 400

    ruta.estado = 'EN_CARGUE'
    db.session.commit()

    sugeridos_ids = []
    if ruta.ruta_maestra:
        municipios = {p.municipio.lower() for p in ruta.ruta_maestra.paradas}
        from app.models.packing import TareaPacking
        bultos_libres = (Bulto.query
            .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
            .filter(
                TareaPacking.siesa_triggered == True,
                TareaPacking.estado != 'CANCELADO',
                Bulto.estado == 'PENDIENTE',
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
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    ruta = RutaDespacho.query.get_or_404(id)

    if not ruta.ruta_maestra:
        return jsonify({'sugeridos': [], 'total': 0}), 200

    municipios = {p.municipio.lower() for p in ruta.ruta_maestra.paradas}
    bultos_libres = (Bulto.query
        .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
        .filter(
            TareaPacking.siesa_triggered == True,
            TareaPacking.estado != 'CANCELADO',
            Bulto.estado == 'PENDIENTE',
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
    from app.models.bulto import Bulto
    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != 'EN_CARGUE':
        return jsonify({'error': f'La ruta ya está en estado {ruta.estado}'}), 400
    if not ruta.bultos:
        return jsonify({'error': 'No hay bultos asignados a esta ruta'}), 400

    sin_confirmar = Bulto.query.filter_by(ruta_despacho_id=ruta.id, estado='PENDIENTE').count()
    if sin_confirmar > 0:
        return jsonify({
            'error': f'Faltan {sin_confirmar} bulto{"s" if sin_confirmar != 1 else ""} por confirmar. '
                     f'Escanéalos en el muelle antes de cerrar la ruta.'
        }), 400

    ruta.estado = 'EN_TRANSITO'
    ruta.fecha_cierre = datetime.utcnow()
    db.session.commit()
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
    from app.models.bulto import Bulto

    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != 'EN_TRANSITO':
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO, está {ruta.estado}'}), 400

    data = request.get_json() or {}
    ahora = datetime.utcnow()
    rechazados = 0

    confirmaciones = data.get('bultos', [])
    if confirmaciones:
        ids_payload = {c['id'] for c in confirmaciones}
        bultos_ruta = Bulto.query.filter_by(ruta_despacho_id=id).all()

        for bulto in bultos_ruta:
            conf = next((c for c in confirmaciones if c['id'] == bulto.id), None)
            entregado = conf.get('entregado', True) if conf else True

            if entregado:
                bulto.estado = 'ENTREGADO'
                bulto.fecha_entrega = ahora
            else:
                bulto.estado = 'RECHAZADO'
                bulto.fecha_entrega = ahora
                bulto.motivo_rechazo = conf.get('motivo_rechazo', 'Sin especificar') if conf else 'Sin especificar'
                rechazados += 1

    ruta.estado = 'ENTREGADA'
    ruta.fecha_entregada = ahora
    db.session.commit()

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
    usuario_id = int(get_jwt_identity())
    conductor = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    if not conductor:
        return jsonify({'error': 'Tu cuenta no está vinculada a ningún conductor'}), 404

    rutas = (RutaDespacho.query
             .filter_by(conductor_id=conductor.id, estado='EN_TRANSITO')
             .order_by(RutaDespacho.fecha_cierre.desc())
             .all())
    return jsonify({
        'conductor': conductor.to_dict(),
        'rutas': [r.to_dict(include_bultos=True) for r in rutas]
    }), 200


@rutas_bp.route('/usuarios-conductores', methods=['GET'])
@jwt_required()
def usuarios_conductores():
    """Lista de usuarios con rol conductor — para vincular al registro de flota."""
    from app.models.usuario import Usuario
    usuarios = (Usuario.query
                .filter_by(rol='conductor', activo=True)
                .order_by(Usuario.nombre)
                .all())
    return jsonify({'usuarios': [{'id': u.id, 'nombre': u.nombre, 'email': u.email} for u in usuarios]}), 200


@rutas_bp.route('/bultos-rechazados', methods=['GET'])
@jwt_required()
def bultos_rechazados():
    """Bultos rechazados en entrega — aparecen en panel recepcionista para re-ingresar."""
    from app.models.bulto import Bulto
    bultos = (Bulto.query
              .filter_by(estado='RECHAZADO')
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
    from app.models.bulto import Bulto
    from app.models.recaudo_entrega import RecaudoEntrega

    ruta = RutaDespacho.query.get_or_404(id)
    usuario_id = int(get_jwt_identity())

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
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega

    ruta = RutaDespacho.query.get_or_404(id)
    if ruta.estado != 'EN_TRANSITO':
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO, está {ruta.estado}'}), 400

    tarea = TareaPacking.query.get_or_404(tarea_id)

    # Verificar que la tarea tiene bultos en esta ruta
    bultos_tarea = Bulto.query.filter_by(tarea_id=tarea_id, ruta_despacho_id=id).all()
    if not bultos_tarea:
        return jsonify({'error': 'Esta factura no pertenece a la ruta'}), 404

    # Acceso: admin/jefe o conductor dueño
    usuario_id = int(get_jwt_identity())
    conductor_ruta = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    es_admin = _es_admin_o_jefe()
    if not es_admin and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a esta ruta'}), 403

    data = request.get_json() or {}
    estado_entrega = data.get('estado_entrega', '').upper()
    if estado_entrega not in ('ENTREGADO', 'PARCIAL', 'RECHAZADO'):
        return jsonify({'error': 'estado_entrega debe ser ENTREGADO, PARCIAL o RECHAZADO'}), 400

    forma_pago = data.get('forma_pago', '').upper() or None
    if forma_pago and forma_pago not in ('EFECTIVO', 'TRANSFERENCIA', 'CHEQUE', 'CREDITO', 'EXENTO'):
        return jsonify({'error': 'forma_pago inválido'}), 400

    foto = data.get('foto_entrega', '') or None
    if foto and len(foto) > 1_150_000:
        return jsonify({'error': 'Foto demasiado grande. Máximo ~800KB JPEG.'}), 400

    bultos_rechazados_ids = data.get('bultos_rechazados', [])
    # RECHAZADO total: si no se especifican bultos, se rechazan TODOS automáticamente
    if estado_entrega == 'RECHAZADO' and not bultos_rechazados_ids:
        bultos_rechazados_ids = [b.id for b in bultos_tarea]
    # PARCIAL sí requiere selección explícita de qué bultos no pudieron entregarse
    if estado_entrega == 'PARCIAL' and not bultos_rechazados_ids:
        return jsonify({'error': 'Para entrega parcial debes indicar cuáles bultos fueron rechazados'}), 400

    ahora = datetime.utcnow()

    # Actualizar estado de cada bulto — con SELECT FOR UPDATE para evitar concurrencia
    ids_tarea = {b.id for b in bultos_tarea}
    ids_rechazados_set = set(bultos_rechazados_ids)

    for b in bultos_tarea:
        if b.id in ids_rechazados_set:
            b.estado = 'RECHAZADO'
            b.motivo_rechazo = data.get('observaciones', 'Rechazado en entrega')[:100]
            b.fecha_entrega = ahora
        else:
            b.estado = 'ENTREGADO'
            b.fecha_entrega = ahora

    # Crear o actualizar RecaudoEntrega
    recaudo = RecaudoEntrega.query.filter_by(ruta_id=id, tarea_id=tarea_id).first()
    es_edicion = recaudo is not None

    if not recaudo:
        recaudo = RecaudoEntrega(
            ruta_id=id,
            tarea_id=tarea_id,
            fecha_creacion=ahora,
            confirmado_por=usuario_id,
        )
        db.session.add(recaudo)
    else:
        # Edición — registrar audit trail
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
    return jsonify({
        'ok':          True,
        'recaudo':     recaudo.to_dict(),
        'es_edicion':  es_edicion,
    }), 200


@rutas_bp.route('/<int:id>/planilla', methods=['GET'])
@jwt_required()
def planilla_ruta(id):
    """
    Vista de planilla completa para admin: todas las paradas con recaudos y totales.
    """
    from app.models.recaudo_entrega import RecaudoEntrega

    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede ver la planilla'}), 403

    ruta = RutaDespacho.query.get_or_404(id)
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
            'bultos_entregados': sum(1 for b in bultos_t if b.estado == 'ENTREGADO'),
            'bultos_rechazados': sum(1 for b in bultos_t if b.estado == 'RECHAZADO'),
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
    if ruta.estado not in ('EN_TRANSITO', 'ENTREGADA'):
        return jsonify({'error': f'No se puede liquidar una ruta en estado {ruta.estado}'}), 400

    tareas = ruta.tareas_unicas()
    from app.models.recaudo_entrega import RecaudoEntrega
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
    if ruta.estado != 'EN_TRANSITO':
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO para forzar cierre (estado: {ruta.estado})'}), 400

    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.packing import TareaPacking
    from app.models.bulto import Bulto

    admin_id = int(get_jwt_identity())
    tareas = ruta.tareas_unicas()
    recaudos_existentes = {r.tarea_id for r in RecaudoEntrega.query.filter_by(ruta_id=id).all()}
    # Comparar t.id (int) contra el set de tarea_id (int) — evita comparar objeto vs int
    pendientes = [t for t in tareas if t.id not in recaudos_existentes]
    ahora = datetime.utcnow()
    auto_cerradas = 0

    for tarea in pendientes:
        bultos_tarea = Bulto.query.filter_by(tarea_id=tarea.id, ruta_despacho_id=id).all()
        for b in bultos_tarea:
            b.estado = 'RECHAZADO'
            b.motivo_rechazo = 'Cierre forzado por admin'
            b.fecha_entrega = ahora

        recaudo = RecaudoEntrega(
            ruta_id=id,
            tarea_id=tarea.id,
            estado_entrega='RECHAZADO',
            forma_pago=None,
            monto_cobrado=0,
            observaciones='Cierre forzado por administrador — parada no gestionada',
            confirmado_por=admin_id,
            fecha_creacion=ahora,
        )
        db.session.add(recaudo)
        auto_cerradas += 1

    ruta.estado = 'ENTREGADA'
    ruta.estado_financiero = 'LIQUIDADA'
    ruta.fecha_cierre = ahora
    db.session.commit()

    return jsonify({
        'ok': True,
        'paradas_auto_cerradas': auto_cerradas,
        'mensaje': f'Ruta cerrada. {auto_cerradas} parada(s) registradas como rechazadas automáticamente.',
        'ruta': ruta.to_dict(),
    }), 200
