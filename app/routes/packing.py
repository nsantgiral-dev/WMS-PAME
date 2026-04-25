from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.packing import TareaPacking
from app.models.picking import TareaPicking
from app.services.packing_service import PackingService
from app.services.connekta_gateway import connekta
from app.routes._auth_helpers import Roles

packing_bp = Blueprint('packing', __name__)


from app.routes._auth_helpers import _solo_admin


def _picking_listo_batch(numeros_pedido: list) -> dict:
    """
    Consulta en UNA sola query si el picking está completo para cada pedido.
    Devuelve {numero_pedido: bool}.
    Evita el N+1 de _enriquecer_picking_listo que hacía 1 query por packing.
    """
    if not numeros_pedido:
        return {}

    pickings = TareaPicking.query.filter(
        TareaPicking.referencia_documento.in_(numeros_pedido),
        TareaPicking.estado != 'CANCELADO'
    ).all()

    # Agrupar por pedido
    por_pedido = {}
    for p in pickings:
        num = p.referencia_documento
        if num not in por_pedido:
            por_pedido[num] = {'total': 0, 'completados': 0}
        por_pedido[num]['total'] += 1
        if p.estado == 'COMPLETADO':
            por_pedido[num]['completados'] += 1

    resultado = {}
    for num in numeros_pedido:
        datos = por_pedido.get(num)
        if not datos:
            resultado[num] = True   # sin picking = creado manual, listo
        else:
            resultado[num] = datos['completados'] == datos['total']
    return resultado


@packing_bp.route('/', methods=['GET'])
@jwt_required()
def listar_tareas():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.PACKING_ROLES:
        return jsonify({'error': 'Sin permiso para listar tareas de packing'}), 403
    estado = request.args.get('estado')
    almacen_id = request.args.get('almacen_id', type=int)
    page = request.args.get('page', 1, type=int)

    query = TareaPacking.query.order_by(TareaPacking.fecha_creacion.desc())

    if estado:
        query = query.filter_by(estado=estado)
    if almacen_id:
        query = query.filter_by(almacen_id=almacen_id)

    tareas = query.paginate(page=page, per_page=50, error_out=False)

    # Una sola query para todos los pickings de la página — sin N+1
    numeros = [t.numero_pedido_siesa for t in tareas.items]
    picking_listo_map = _picking_listo_batch(numeros)

    items = []
    for t in tareas.items:
        d = t.to_dict()
        d['picking_listo'] = picking_listo_map.get(t.numero_pedido_siesa, True)
        items.append(d)

    return jsonify({
        'tareas': items,
        'total': tareas.total,
        'pagina_actual': page
    }), 200


@packing_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_tarea(id):
    tarea = TareaPacking.query.get_or_404(id)
    d = tarea.to_dict()
    d['picking_listo'] = _picking_listo_batch([tarea.numero_pedido_siesa]).get(tarea.numero_pedido_siesa, True)
    return jsonify(d), 200


@packing_bp.route('/crear-desde-picking', methods=['POST'])
@jwt_required()
def crear_desde_picking():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear tareas de packing'}), 403
    data = request.get_json()
    requeridos = ['tareas_picking_ids', 'numero_pedido_siesa', 'almacen_id']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        tarea = PackingService.crear_desde_picking(
            tareas_picking_ids=data['tareas_picking_ids'],
            numero_pedido_siesa=data['numero_pedido_siesa'],
            almacen_id=data['almacen_id'],
            tipo_docto_pedido_siesa=data.get('tipo_docto_pedido_siesa', ''),
            consec_docto_pedido_siesa=data.get('consec_docto_pedido_siesa', '')
        )
        return jsonify({
            'mensaje': 'Tarea de packing creada',
            'tarea': tarea.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/crear-manual', methods=['POST'])
@jwt_required()
def crear_manual():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear tareas de packing'}), 403
    data = request.get_json()
    requeridos = ['numero_pedido_siesa', 'almacen_id', 'items']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        tarea = PackingService.crear_manual(
            numero_pedido_siesa=data['numero_pedido_siesa'],
            almacen_id=data['almacen_id'],
            items=data['items'],
            tipo_docto_pedido_siesa=data.get('tipo_docto_pedido_siesa', ''),
            consec_docto_pedido_siesa=data.get('consec_docto_pedido_siesa', '')
        )
        return jsonify({
            'mensaje': 'Tarea de packing creada manualmente',
            'tarea': tarea.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/iniciar', methods=['PUT'])
@jwt_required()
def iniciar_tarea(id):
    from app.models.usuario import Usuario
    try:
        empacador_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(empacador_id)
    if not usuario or usuario.rol not in Roles.PACKING_ROLES:
        return jsonify({'error': 'No autorizado — se requiere rol empacador, supervisor o admin'}), 403
    try:
        tarea = PackingService.iniciar(id, empacador_id)
        return jsonify(tarea.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/escanear', methods=['POST'])
@jwt_required()
def escanear_item(id):
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(uid)
    if not usuario or usuario.rol not in Roles.PACKING_ROLES:
        return jsonify({'error': 'No autorizado — se requiere rol empacador, supervisor o admin'}), 403
    data = request.get_json()
    requeridos = ['producto_id', 'cantidad_real']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        resultado = PackingService.escanear_item(
            tarea_id=id,
            producto_id=data['producto_id'],
            cantidad_real=data['cantidad_real'],
            lote=data.get('lote')
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/confirmar', methods=['PUT'])
@jwt_required()
def confirmar_packing(id):
    """Paso 1: verifica ítems → estado VERIFICADO. NO dispara Siesa."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.PACKING_ROLES:
        return jsonify({'error': 'Sin permiso para confirmar packing'}), 403
    data = request.get_json() or {}
    try:
        PackingService.confirmar_packing(
            tarea_id=id,
            observaciones=data.get('observaciones'),
            forzar=data.get('forzar', False)
        )
        from app.models.packing import TareaPacking
        tarea = TareaPacking.query.get(id)
        return jsonify({
            'mensaje': 'Ítems verificados — declara las piezas físicas para cerrar',
            'tarea': tarea.to_dict(),
        }), 200
    except ValueError as e:
        error = e.args[0]
        if isinstance(error, dict):
            return jsonify(error), 409
        return jsonify({'error': error}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/<int:id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_packing(id):
    """
    Paso 2: declara bultos físicos, genera códigos de barras y dispara Siesa.
    Body: {"bultos": [{"tipo": "Caja", "cantidad": 2}, {"tipo": "Bolsa", "cantidad": 1}]}
    """
    from app.models.usuario import Usuario
    uid = get_jwt_identity()
    usuario = Usuario.query.get(int(uid))
    if not usuario or usuario.rol not in Roles.PACKING_ROLES:
        return jsonify({'error': 'No autorizado'}), 403
    data = request.get_json() or {}
    bultos_data = data.get('bultos', [])
    # Permitir bultos_data vacío solo si ya existen bultos (retry Siesa)
    if not bultos_data:
        from app.models.bulto import Bulto
        hay_bultos = Bulto.query.filter_by(tarea_id=id).count() > 0
        if not hay_bultos:
            return jsonify({'error': 'Debes declarar al menos una pieza'}), 400
    try:
        bultos = PackingService.cerrar_packing(tarea_id=id, bultos_data=bultos_data)
        from app.models.packing import TareaPacking
        tarea = TareaPacking.query.get(id)
        return jsonify({
            'ok': True,
            'mensaje': f'{len(bultos)} pieza(s) registradas — Siesa generó la remisión',
            'siesa_triggered': tarea.siesa_triggered,
            'numero_pedido': tarea.numero_pedido_siesa,
            'cliente': tarea.cliente or '',
            'municipio': tarea.municipio or '',
            'bultos': [b.to_dict() for b in bultos]
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/<int:id>/cancelar', methods=['PUT'])
@jwt_required()
def cancelar_tarea(id):
    from app.models.usuario import Usuario
    uid = get_jwt_identity()
    usuario = Usuario.query.get(int(uid))
    if not usuario or usuario.rol not in Roles.LEAD:
        return jsonify({'error': 'No autorizado — se requiere rol admin o supervisor'}), 403
    data = request.get_json() or {}
    try:
        tarea = PackingService.cancelar(id, motivo=data.get('motivo'))
        return jsonify({'mensaje': 'Tarea cancelada', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/reiniciar-conteo', methods=['PUT'])
@jwt_required()
def reiniciar_conteo(id):
    """Resetea todos los items a 0 y vuelve el estado a EN_PROCESO."""
    from app.extensions import db
    from app.models.usuario import Usuario
    uid = get_jwt_identity()
    usuario = Usuario.query.get(int(uid))
    if not usuario or usuario.rol not in Roles.PACKING_ROLES:
        return jsonify({'error': 'No autorizado'}), 403
    tarea = TareaPacking.query.get_or_404(id)
    if tarea.estado not in ('EN_PROCESO', 'PENDIENTE', 'VERIFICADO'):
        return jsonify({'error': 'No se puede reiniciar una tarea en este estado'}), 400
    for item in tarea.items:
        item.cantidad_real = 0
        item.verificado = False
    tarea.estado = 'EN_PROCESO'
    tarea.verificacion_exitosa = False
    tarea.fecha_verificado = None
    db.session.commit()
    return jsonify({'ok': True, 'mensaje': 'Conteo reiniciado', 'tarea': tarea.to_dict()}), 200


@packing_bp.route('/<int:id>/resetear-siesa', methods=['POST'])
@jwt_required()
def resetear_siesa(id):
    """Elimina bultos y vuelve a VERIFICADO para reintentar Siesa desde cero."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede resetear el estado de Siesa'}), 403
    try:
        tarea = PackingService.resetear_siesa(id)
        return jsonify({'ok': True, 'mensaje': 'Packing reseteado — declara las piezas de nuevo', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/forzar-siesa', methods=['POST'])
@jwt_required()
def forzar_retry_siesa(id):
    """
    Fuerza el retry de Siesa aunque siesa_triggered=True. Solo admin.
    Útil cuando el packing se cerró en MODO_ENSAYO y nunca llegó a Siesa real.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede forzar retry de Siesa'}), 403
    from app.models.packing import TareaPacking
    from app.extensions import db
    tarea = TareaPacking.query.get_or_404(id)
    if tarea.estado != 'DESPACHADO':
        return jsonify({'error': f'La tarea debe estar DESPACHADO, está {tarea.estado}'}), 400
    # Forzar siesa_triggered=False para que cerrar_packing lo reintente
    tarea.siesa_triggered = False
    db.session.commit()
    try:
        # Pasar bultos existentes como bultos_data para saltarse la validación de "sin piezas"
        from app.models.bulto import Bulto as BultoModel
        bultos_existentes = BultoModel.query.filter_by(tarea_id=id).all()
        bultos_data_dummy = [{'tipo': b.tipo, 'cantidad': 1} for b in bultos_existentes] if bultos_existentes else [{'tipo': 'Caja', 'cantidad': 1}]
        bultos = PackingService.cerrar_packing(tarea_id=id, bultos_data=bultos_data_dummy)
        tarea = TareaPacking.query.get(id)
        return jsonify({
            'ok': True,
            'siesa_triggered': tarea.siesa_triggered,
            'siesa_response': tarea.siesa_response,
            'bultos': [b.to_dict() for b in bultos]
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/connekta/estado', methods=['GET'])
@jwt_required()
def estado_connekta():
    """Verifica el estado de la integración con Siesa/Connekta."""
    return jsonify(connekta.estado()), 200