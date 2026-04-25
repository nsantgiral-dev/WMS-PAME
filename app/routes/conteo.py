import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.conteo import SesionConteo
from app.services.conteo_service import ConteoService
from app.services.abc_service import ABCService

conteo_bp = Blueprint('conteo', __name__)


from app.routes._auth_helpers import _solo_admin


@conteo_bp.route('/', methods=['GET'])
@jwt_required()
def listar_sesiones():
    estado = request.args.get('estado')
    almacen_id = request.args.get('almacen_id', type=int)
    clasificacion = request.args.get('clasificacion')
    operario_id = request.args.get('operario_id', type=int)
    categoria = request.args.get('categoria', request.args.get('marca', '')).strip()
    page = request.args.get('page', 1, type=int)

    query = SesionConteo.query.order_by(SesionConteo.fecha_creacion.desc())

    if estado:
        query = query.filter_by(estado=estado)
    if almacen_id:
        query = query.filter_by(almacen_id=almacen_id)
    if clasificacion:
        query = query.filter_by(clasificacion_abc=clasificacion)
    if operario_id:
        query = query.filter_by(operario_id=operario_id)
    if categoria:
        from app.models.producto import Producto
        query = (query
                 .join(Producto, SesionConteo.producto_id == Producto.id)
                 .filter(Producto.categoria.ilike(f'%{categoria}%')))

    sesiones = query.paginate(page=page, per_page=30, error_out=False)

    return jsonify({
        'sesiones': [s.to_dict() for s in sesiones.items],
        'total': sesiones.total,
        'pagina_actual': page,
        'total_paginas': sesiones.pages,
        'por_pagina': 30,
    }), 200


@conteo_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """
    Endpoint para operario — devuelve sus tareas pendientes.
    Vista ciega — sin cantidades esperadas.
    """
    try:
        operario_id = int(get_jwt_identity())
    except (ValueError, TypeError):
        return jsonify({'error': 'Identidad de usuario inválida en el token'}), 422

    from sqlalchemy import case as sa_case
    prioridad_abc = sa_case(
        {'A': 1, 'B': 2, 'C': 3},
        value=SesionConteo.clasificacion_abc,
        else_=4
    )
    tareas = SesionConteo.query.filter(
        SesionConteo.operario_id == operario_id,
        SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO'])
    ).order_by(prioridad_abc, SesionConteo.fecha_creacion.asc()).all()

    return jsonify({
        'tareas': [t.to_dict_operario() for t in tareas],
        'total': len(tareas)
    }), 200


@conteo_bp.route('/<int:id>/tarea', methods=['GET'])
@jwt_required()
def obtener_tarea(id):
    """Vista ciega de una tarea específica para el operario."""
    try:
        operario_id = int(get_jwt_identity())
    except (ValueError, TypeError):
        return jsonify({'error': 'Identidad de usuario inválida en el token'}), 422
    try:
        tarea = ConteoService.obtener_tarea_operario(id, operario_id)
        return jsonify(tarea), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@conteo_bp.route('/<int:id>/registrar', methods=['POST'])
@jwt_required()
def registrar_conteo(id):
    """
    Operario registra su conteo físico.
    Dispara conciliación en tiempo real contra Siesa.
    """
    try:
        operario_id = int(get_jwt_identity())
    except (ValueError, TypeError):
        return jsonify({'error': 'Identidad de usuario inválida en el token'}), 422
    data = request.get_json()

    if 'cantidad_fisica' not in data:
        return jsonify({'error': 'cantidad_fisica es requerida'}), 400

    try:
        resultado = ConteoService.registrar_conteo(
            sesion_id=id,
            operario_id=operario_id,
            cantidad_fisica=data['cantidad_fisica'],
            lote_id=data.get('lote_id')
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/<int:id>/ajustar', methods=['PUT'])
@jwt_required()
def confirmar_ajuste(id):
    """
    Solo admin o supervisor pueden aprobar ajustes de inventario.
    Dispara POST a Siesa con motivo 01 (entrada) o 02 (salida).
    """
    from app.models.usuario import Usuario
    try:
        supervisor_id = int(get_jwt_identity())
    except (ValueError, TypeError):
        return jsonify({'error': 'Identidad de usuario inválida en el token'}), 422
    usuario = Usuario.query.get(supervisor_id)
    if not usuario or usuario.rol not in ('admin', 'supervisor'):
        return jsonify({'error': 'Solo un supervisor o admin puede aprobar ajustes de inventario'}), 403
    try:
        sesion = ConteoService.confirmar_ajuste(id, supervisor_id)
        return jsonify({
            'mensaje': f'Ajuste {sesion.motivo_codigo} enviado a Siesa',
            'diferencia': sesion.diferencia,
            'motivo_codigo': sesion.motivo_codigo,
            'siesa_triggered': sesion.siesa_triggered,
            'sesion': sesion.to_dict()
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/auditorias-urgentes', methods=['GET'])
@jwt_required()
def auditorias_urgentes():
    """
    Auditorías EXCEPCION_PICKING pendientes de resolución.
    Solo visibles para admin/supervisor — aparecen en "Auditorías Urgentes".
    """
    from app.models.usuario import Usuario
    uid = int(get_jwt_identity())
    usuario = Usuario.query.get(uid)
    if not usuario or usuario.rol not in ('admin', 'supervisor', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin o supervisor puede ver las auditorías urgentes'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    q = (SesionConteo.query
         .filter_by(tipo='EXCEPCION_PICKING')
         .filter(SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO', 'DESCUADRE'])))
    if almacen_id:
        q = q.filter_by(almacen_id=almacen_id)
    sesiones = q.order_by(SesionConteo.fecha_creacion.asc()).all()
    return jsonify({
        'auditorias': [s.to_dict() for s in sesiones],
        'total': len(sesiones),
    }), 200


@conteo_bp.route('/abc/generar-tareas', methods=['POST'])
@jwt_required()
def generar_tareas_abc():
    """
    Genera el lote diario de conteo para una clase.
    forzar_todo=true genera para todos los elegibles sin límite de batch.
    Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede generar tareas de conteo ABC'}), 403
    data = request.get_json() or {}

    if 'almacen_id' not in data:
        return jsonify({'error': 'almacen_id es requerido'}), 400

    try:
        resultado = ABCService.generar_tareas_conteo_diario(
            almacen_id=data['almacen_id'],
            clasificacion=data.get('clasificacion', 'A'),
            forzar_todo=bool(data.get('forzar_todo', False)),
        )
        return jsonify(resultado), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/abc/generar-todas', methods=['POST'])
@jwt_required()
def generar_todas_las_clases():
    """Genera tareas A+B+C en una sola llamada — solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede generar tareas de conteo'}), 403
    data = request.get_json() or {}
    if 'almacen_id' not in data:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        resultado = ABCService.generar_todas_las_clases(
            almacen_id=data['almacen_id'],
            forzar_todo=bool(data.get('forzar_todo', False)),
        )
        return jsonify(resultado), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/abc/limpiar-pendientes', methods=['POST'])
@jwt_required()
def limpiar_pendientes_abc():
    """
    Elimina tareas PENDIENTE para reiniciar el ciclo de conteo de una clase.
    Útil cuando se generó todo de golpe y se quiere empezar gradualmente.
    Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede limpiar la cola de conteo'}), 403
    data = request.get_json() or {}
    almacen_id = data.get('almacen_id')
    clasificacion = data.get('clasificacion')  # A, B, C o None (todas)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400

    query = SesionConteo.query.filter_by(estado='PENDIENTE', almacen_id=almacen_id)
    if clasificacion and clasificacion in ('A', 'B', 'C'):
        query = query.filter_by(clasificacion_abc=clasificacion)

    count = query.count()
    query.delete(synchronize_session=False)
    db.session.commit()
    return jsonify({
        'eliminadas': count,
        'almacen_id': almacen_id,
        'clasificacion': clasificacion or 'todas',
    }), 200


@conteo_bp.route('/manual', methods=['POST'])
@jwt_required()
def crear_conteo_manual():
    """
    Admin crea una tarea de conteo manual por código de producto.
    Útil para verificar un producto específico o generar conteos por marca
    antes de una OC.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear conteos manuales'}), 403
    data = request.get_json() or {}

    almacen_id = data.get('almacen_id')
    producto_codigo = (data.get('producto_codigo') or '').strip().upper()
    if not almacen_id or not producto_codigo:
        return jsonify({'error': 'almacen_id y producto_codigo son requeridos'}), 400

    from app.models.producto import Producto
    from app.models.inventario import UbicacionProducto
    from app.models.ubicacion import Ubicacion

    producto = Producto.query.filter(
        db.or_(
            Producto.codigo_siesa == producto_codigo,
            Producto.codigo == producto_codigo
        )
    ).first()
    if not producto:
        return jsonify({'error': f'Producto {producto_codigo} no encontrado'}), 404

    registros = (
        UbicacionProducto.query
        .join(Ubicacion)
        .filter(
            UbicacionProducto.producto_id == producto.id,
            Ubicacion.almacen_id == almacen_id
        ).all()
    )
    if not registros:
        return jsonify({'error': 'El producto no tiene stock registrado en este almacén'}), 404

    creadas = []
    omitidas = 0
    for reg in registros:
        ya = SesionConteo.query.filter(
            SesionConteo.producto_id == producto.id,
            SesionConteo.ubicacion_id == reg.ubicacion_id,
            SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
        ).first()
        if ya:
            omitidas += 1
            continue

        codigo = f'CC-MANUAL-{datetime.utcnow().strftime("%Y%m%d")}-{str(uuid.uuid4())[:6].upper()}'
        sesion = SesionConteo(
            codigo=codigo,
            tipo='MANUAL',
            clasificacion_abc=producto.clasificacion_abc or 'C',
            ubicacion_id=reg.ubicacion_id,
            almacen_id=almacen_id,
            producto_id=producto.id,
            producto_codigo_siesa=producto.codigo_siesa,
            maneja_lote=False,
            estado='PENDIENTE'
        )
        db.session.add(sesion)
        creadas.append(codigo)

    db.session.commit()
    return jsonify({
        'tareas_creadas': len(creadas),
        'omitidas_ya_activas': omitidas,
        'producto': producto_codigo,
        'producto_nombre': producto.nombre or '',
        'codigos': creadas,
    }), 201


@conteo_bp.route('/abc/sincronizar', methods=['POST'])
@jwt_required()
def sincronizar_abc():
    """
    Sincroniza clasificación ABC desde Siesa. Solo admin.
    Body opcional: {"api_abc": "API_custom_ABC_Rotacion"}
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede sincronizar ABC'}), 403
    data = request.get_json() or {}
    try:
        resultado = ABCService.sincronizar_clasificacion_desde_siesa(
            api_abc=data.get('api_abc')
        )
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/abc/watchdog', methods=['POST'])
@jwt_required()
def watchdog_anomalias():
    """
    Ejecuta el AI Watchdog manualmente para un almacén. Solo admin.
    Detecta productos B/C con alta rotación real y fuerza conteo inmediato.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ejecutar el watchdog'}), 403
    data = request.get_json() or {}
    if 'almacen_id' not in data:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        overrides = ABCService.watchdog_anomalias(almacen_id=data['almacen_id'])
        return jsonify({'overrides': len(overrides), 'detalle': overrides}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/abc/resumen', methods=['GET'])
@jwt_required()
def resumen_abc():
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    resultado = ABCService.resumen_abc(almacen_id)
    return jsonify(resultado), 200


@conteo_bp.route('/abc/cargar-csv', methods=['POST'])
@jwt_required()
def cargar_csv_abc():
    """
    Recibe el CSV/Excel del reporte "Recalculo de rotación ABC" de Siesa
    y actualiza clasificacion_abc en la tabla productos. Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede cargar el CSV de ABC'}), 403
    if 'archivo' not in request.files:
        return jsonify({'error': 'Se requiere el campo "archivo" en el form'}), 400

    f = request.files['archivo']
    if not f.filename:
        return jsonify({'error': 'Archivo vacío'}), 400

    from werkzeug.utils import secure_filename
    safe_name = secure_filename(f.filename)
    if not safe_name:
        return jsonify({'error': 'Nombre de archivo inválido'}), 400

    ext = safe_name.rsplit('.', 1)[-1].lower() if '.' in safe_name else ''
    if ext not in ('csv', 'xlsx', 'xls', 'txt'):
        return jsonify({'error': f'Formato no soportado: {ext}. Usar CSV o Excel.'}), 400

    almacen_id = request.form.get('almacen_id', type=int)
    try:
        resultado = ABCService.procesar_csv_abc(f, ext, almacen_id=almacen_id)
        return jsonify(resultado), 200
    except Exception as e:
        current_app.logger.error(f'[ABC CSV] Error procesando archivo: {e}')
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/<int:id>/editar', methods=['PUT'])
@jwt_required()
def editar_conteo(id):
    """
    Admin corrige datos de un conteo (cantidad_fisica, operario_id).
    Requiere motivo_edicion obligatorio — queda en auditoría.
    No aplica a conteos AJUSTADOS (Siesa ya procesó el ajuste).
    """
    from app.models.usuario import Usuario
    editor_id = int(get_jwt_identity())
    usuario = Usuario.query.get(editor_id)
    if not usuario or usuario.rol not in ('admin', 'supervisor'):
        return jsonify({'error': 'Solo admin o supervisor puede editar conteos'}), 403

    sesion = SesionConteo.query.get(id)
    if not sesion:
        return jsonify({'error': 'Conteo no encontrado'}), 404

    if sesion.estado == 'AJUSTADO':
        return jsonify({'error': 'No se puede editar un conteo ya ajustado en Siesa'}), 409

    data = request.get_json() or {}
    motivo = (data.get('motivo_edicion') or '').strip()
    if not motivo:
        return jsonify({'error': 'motivo_edicion es obligatorio'}), 400

    cambios = []

    # Corregir cantidad_fisica — dispara re-conciliación si ya tiene existencia_siesa
    if 'cantidad_fisica' in data:
        nueva_cantidad = data['cantidad_fisica']
        if not isinstance(nueva_cantidad, int) or nueva_cantidad < 0:
            return jsonify({'error': 'cantidad_fisica debe ser un entero >= 0'}), 400
        sesion.cantidad_fisica = nueva_cantidad
        cambios.append(f'cantidad_fisica → {nueva_cantidad}')

        # Re-conciliar si ya tenemos referencia Siesa
        if sesion.existencia_siesa is not None:
            diferencia = nueva_cantidad - sesion.existencia_siesa
            sesion.diferencia = diferencia
            if diferencia == 0:
                sesion.estado = 'MATCH'
                sesion.fecha_cierre = datetime.utcnow()
                cambios.append('estado → MATCH')
            else:
                # Si estaba en MATCH pero ahora no cuadra, volver a DESCUADRE
                if sesion.estado == 'MATCH':
                    sesion.estado = 'DESCUADRE'
                    sesion.fecha_cierre = None
                    cambios.append(f'estado → DESCUADRE (dif={diferencia})')

    # Reasignar operario
    if 'operario_id' in data:
        nuevo_op = data['operario_id']
        if nuevo_op is not None:
            op_usr = Usuario.query.get(nuevo_op)
            if not op_usr:
                return jsonify({'error': f'Operario {nuevo_op} no encontrado'}), 404
        sesion.operario_id = nuevo_op
        cambios.append(f'operario_id → {nuevo_op}')
        if sesion.estado == 'PENDIENTE' and nuevo_op:
            sesion.estado = 'PENDIENTE'  # mantener — asignación no cambia estado

    if not cambios:
        return jsonify({'error': 'No se enviaron campos a modificar'}), 400

    sesion.editado_por = editor_id
    sesion.editado_en = datetime.utcnow()
    sesion.motivo_edicion = motivo

    db.session.commit()
    current_app.logger.info(
        f'[CONTEO EDIT] #{id} editado por usuario #{editor_id}: {"; ".join(cambios)}. Motivo: {motivo}'
    )
    return jsonify({'mensaje': 'Conteo actualizado', 'cambios': cambios, 'sesion': sesion.to_dict()}), 200