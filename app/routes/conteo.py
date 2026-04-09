from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.conteo import SesionConteo
from app.services.conteo_service import ConteoService
from app.services.abc_service import ABCService

conteo_bp = Blueprint('conteo', __name__)


def _solo_admin():
    from app.models.usuario import Usuario
    uid = get_jwt_identity()
    u = Usuario.query.get(int(uid))
    return u if u and u.rol == 'admin' else None


@conteo_bp.route('/', methods=['GET'])
@jwt_required()
def listar_sesiones():
    estado = request.args.get('estado')
    almacen_id = request.args.get('almacen_id', type=int)
    clasificacion = request.args.get('clasificacion')
    operario_id = request.args.get('operario_id', type=int)
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

    sesiones = query.paginate(page=page, per_page=50, error_out=False)

    return jsonify({
        'sesiones': [s.to_dict() for s in sesiones.items],
        'total': sesiones.total,
        'pagina_actual': page
    }), 200


@conteo_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """
    Endpoint para operario — devuelve sus tareas pendientes.
    Vista ciega — sin cantidades esperadas.
    """
    operario_id = int(get_jwt_identity())

    tareas = SesionConteo.query.filter(
        SesionConteo.operario_id == operario_id,
        SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO'])
    ).order_by(SesionConteo.fecha_creacion.asc()).all()

    return jsonify({
        'tareas': [t.to_dict_operario() for t in tareas],
        'total': len(tareas)
    }), 200


@conteo_bp.route('/<int:id>/tarea', methods=['GET'])
@jwt_required()
def obtener_tarea(id):
    """Vista ciega de una tarea específica para el operario."""
    operario_id = int(get_jwt_identity())
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
    operario_id = int(get_jwt_identity())
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
    supervisor_id = int(get_jwt_identity())
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
    sesiones = (SesionConteo.query
                .filter_by(tipo='EXCEPCION_PICKING')
                .filter(SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO', 'DESCUADRE']))
                .order_by(SesionConteo.fecha_creacion.asc())
                .all())
    return jsonify({
        'auditorias': [s.to_dict() for s in sesiones],
        'total': len(sesiones),
    }), 200


@conteo_bp.route('/abc/generar-tareas', methods=['POST'])
@jwt_required()
def generar_tareas_abc():
    """
    Genera tareas de conteo cíclico automáticamente según clasificación ABC.
    Por defecto genera para clase A (diario). Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede generar tareas de conteo ABC'}), 403
    data = request.get_json() or {}

    if 'almacen_id' not in data:
        return jsonify({'error': 'almacen_id es requerido'}), 400

    try:
        resultado = ABCService.generar_tareas_conteo_diario(
            almacen_id=data['almacen_id'],
            clasificacion=data.get('clasificacion', 'A')
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
        resultado = ABCService.generar_todas_las_clases(almacen_id=data['almacen_id'])
        return jsonify(resultado), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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

    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in ('csv', 'xlsx', 'xls', 'txt'):
        return jsonify({'error': f'Formato no soportado: {ext}. Usar CSV o Excel.'}), 400

    almacen_id = request.form.get('almacen_id', type=int)
    try:
        resultado = ABCService.procesar_csv_abc(f, ext, almacen_id=almacen_id)
        return jsonify(resultado), 200
    except Exception as e:
        current_app.logger.error(f'[ABC CSV] Error procesando archivo: {e}')
        return jsonify({'error': str(e)}), 500