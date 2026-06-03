import uuid
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.conteo import SesionConteo, EstadoConteo
from app.services.conteo_service import ConteoService
from app.services.abc_service import ABCService
from app.routes._auth_helpers import Roles, _es_personal_almacen

conteo_bp = Blueprint('conteo', __name__)
logger = logging.getLogger(__name__)


from app.routes._auth_helpers import _solo_admin


@conteo_bp.route('/', methods=['GET'])
@jwt_required()
def listar_sesiones():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso para listar sesiones de conteo'}), 403
    estado = request.args.get('estado')
    almacen_id = request.args.get('almacen_id', type=int)
    clasificacion = request.args.get('clasificacion')
    operario_id = request.args.get('operario_id', type=int)
    categoria = request.args.get('categoria', request.args.get('marca', '')).strip()
    page = request.args.get('page', 1, type=int)

    from sqlalchemy.orm import joinedload as _jl
    query = (SesionConteo.query
             .options(
                 _jl(SesionConteo.producto),
                 _jl(SesionConteo.ubicacion),
                 _jl(SesionConteo.almacen),      # evita N+1 en to_dict() almacen_nombre/bodega
                 _jl(SesionConteo.operario),
                 _jl(SesionConteo.aprobador),
                 _jl(SesionConteo.editor),
                 _jl(SesionConteo.hijo_conteo)
                 .joinedload(SesionConteo.operario),
             )
             .order_by(SesionConteo.fecha_creacion.desc()))

    # Soporte para múltiples estados separados por coma (ej: "SEGUNDO_CONTEO,DESCUADRE")
    estados_multi = request.args.get('estados', '')
    if estados_multi:
        estados_list = [e.strip() for e in estados_multi.split(',') if e.strip()]
        query = query.filter(SesionConteo.estado.in_(estados_list))
    elif estado:
        query = query.filter_by(estado=estado)
    if almacen_id:
        query = query.filter_by(almacen_id=almacen_id)
    if clasificacion:
        query = query.filter_by(clasificacion_abc=clasificacion)
    if operario_id:
        query = query.filter_by(operario_id=operario_id)
    if categoria:
        from app.models.producto import Producto
        categoria_safe = categoria.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        query = (query
                 .join(Producto, SesionConteo.producto_id == Producto.id)
                 .filter(Producto.categoria.ilike(f'%{categoria_safe}%', escape='\\')))

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
    from sqlalchemy.orm import selectinload as _sl_mis
    tareas = (SesionConteo.query
              .options(_sl_mis(SesionConteo.ubicacion), _sl_mis(SesionConteo.producto))
              .filter(
                  SesionConteo.operario_id == operario_id,
                  SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO'])
              ).order_by(prioridad_abc, SesionConteo.fecha_creacion.asc()).all())

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
    Dispara conciliación en tiempo real contra stock WMS.
    """
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para registrar conteos'}), 403
    try:
        operario_id = int(get_jwt_identity())
    except (ValueError, TypeError):
        return jsonify({'error': 'Identidad de usuario inválida en el token'}), 422
    data = request.get_json()

    if 'cantidad_fisica' not in data:
        return jsonify({'error': 'cantidad_fisica es requerida'}), 400

    # Verificar ownership antes de delegar al servicio — un operario solo puede
    # registrar sus propias sesiones (supervisores pueden acceder a cualquiera).
    _sesion_chk = SesionConteo.query.get(id)
    if _sesion_chk is None:
        return jsonify({'error': 'Sesión de conteo no encontrada'}), 404
    from app.models.usuario import Usuario
    _u_chk = Usuario.query.get(operario_id)
    if _u_chk and _u_chk.rol not in Roles.SUPERVISION:
        if _sesion_chk.operario_id != operario_id:
            return jsonify({'error': 'No puedes registrar el conteo de otra persona'}), 403

    try:
        cantidad_fisica = int(data['cantidad_fisica'])
    except (ValueError, TypeError):
        return jsonify({'error': 'cantidad_fisica debe ser un entero válido'}), 400

    try:
        resultado = ConteoService.registrar_conteo(
            sesion_id=id,
            operario_id=operario_id,
            cantidad_fisica=cantidad_fisica,
            lote_id=data.get('lote_id')
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(f'[CONTEO] Error inesperado en registrar_conteo sesion={id}')
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
    if not usuario or usuario.rol not in Roles.LEAD:
        return jsonify({'error': 'Solo un supervisor o admin puede aprobar ajustes de inventario'}), 403
    try:
        sesion = ConteoService.confirmar_ajuste(id, supervisor_id)
        # [A22] 202 cuando el ajuste está encolado en DLQ (AJUSTANDO) — el supervisor
        # sabe que no completó todavía y no cierra la pantalla prematuramente.
        # 200 solo cuando siesa_triggered=True (Siesa ya confirmó el ajuste).
        http_status = 200 if sesion.siesa_triggered else 202
        return jsonify({
            'mensaje': (
                f'Ajuste {sesion.motivo_codigo} confirmado — Siesa ya procesó'
                if sesion.siesa_triggered
                else f'Ajuste {sesion.motivo_codigo} encolado — pendiente de sincronización con Siesa'
            ),
            'diferencia': sesion.diferencia,
            'motivo_codigo': sesion.motivo_codigo,
            'siesa_triggered': sesion.siesa_triggered,
            'sesion': sesion.to_dict()
        }), http_status
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(f'[CONTEO] Error inesperado en confirmar_ajuste sesion={id}')
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/auditorias-urgentes', methods=['GET'])
@jwt_required()
def auditorias_urgentes():
    """
    Auditorías EXCEPCION_PICKING pendientes de resolución.
    Solo visibles para admin/supervisor — aparecen en "Auditorías Urgentes".
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(uid)
    if not usuario or usuario.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Solo admin o supervisor puede ver las auditorías urgentes'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    from sqlalchemy.orm import selectinload as _sl
    q = (SesionConteo.query
         .options(
             _sl(SesionConteo.producto),
             _sl(SesionConteo.ubicacion),
             _sl(SesionConteo.operario),
             _sl(SesionConteo.segundo_operario),
         )
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
        logger.exception(f'[CONTEO] Error en generar_tareas_conteo_diario almacen={data.get("almacen_id")}')
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
        logger.exception(f'[CONTEO] Error en generar_todas_las_clases almacen={data.get("almacen_id")}')
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
    producto_codigo = (data.get('producto_codigo') or '').strip()
    if not almacen_id or not producto_codigo:
        return jsonify({'error': 'almacen_id y producto_codigo son requeridos'}), 400

    try:
        resultado = ConteoService.crear_conteo_manual(almacen_id, producto_codigo)
        return jsonify(resultado), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 404


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
        logger.exception(f'[CONTEO] Error en sincronizar_abc')
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
        logger.exception(f'[CONTEO] Error en watchdog_anomalias almacen={data.get("almacen_id")}')
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/abc/resumen', methods=['GET'])
@jwt_required()
def resumen_abc():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso — se requiere admin, supervisor o jefe_almacen'}), 403
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
        logger.exception('[ABC CSV] Error procesando archivo')
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
    try:
        editor_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(editor_id)
    if not usuario or usuario.rol not in Roles.LEAD:
        return jsonify({'error': 'Solo admin o supervisor puede editar conteos'}), 403

    sesion = SesionConteo.query.get(id)
    if not sesion:
        return jsonify({'error': 'Conteo no encontrado'}), 404

    if sesion.estado in (EstadoConteo.AJUSTADO, EstadoConteo.AJUSTANDO):
        return jsonify({'error': 'No se puede editar un conteo que ya fue enviado o está en vuelo a Siesa'}), 409

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
            resultado = ConteoService.reconciliar_cantidad(sesion, nueva_cantidad)
            if resultado['es_match']:
                sesion.estado = EstadoConteo.MATCH
                sesion.fecha_cierre = datetime.utcnow()
                cambios.append('estado → MATCH')
            else:
                diferencia = resultado['diferencia']
                # Si estaba en MATCH pero ahora no cuadra, volver a DESCUADRE
                if sesion.estado == EstadoConteo.MATCH:
                    sesion.estado = EstadoConteo.DESCUADRE
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
        if sesion.estado == EstadoConteo.PENDIENTE and nuevo_op:
            sesion.estado = EstadoConteo.PENDIENTE  # mantener — asignación no cambia estado

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


@conteo_bp.route('/stats', methods=['GET'])
@jwt_required()
def stats_conteo():
    """
    Estadísticas rápidas de conteo para el dashboard del admin.
    Retorna contadores: pendientes, en_proceso, hoy_completados, atrasados.
    """
    from app.models.usuario import Usuario
    from sqlalchemy import func as _fn, case as _case
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403

    almacen_id = request.args.get('almacen_id', type=int)

    # Un solo query — COUNT por estado agrupado
    q = db.session.query(
        SesionConteo.estado,
        _fn.count(SesionConteo.id)
    ).group_by(SesionConteo.estado)
    if almacen_id:
        q = q.filter(SesionConteo.almacen_id == almacen_id)
    counts = {estado: cnt for estado, cnt in q.all()}

    pendientes = counts.get('PENDIENTE', 0)
    en_proceso = counts.get('EN_PROCESO', 0)
    segundo_conteo = counts.get('SEGUNDO_CONTEO', 0)
    descuadre = counts.get('DESCUADRE', 0)
    match = counts.get('MATCH', 0)
    ajustado = counts.get('AJUSTADO', 0)
    ajustando = counts.get('AJUSTANDO', 0)

    # Completados hoy (MATCH + AJUSTADO con fecha_cierre de hoy)
    hoy_inicio = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q_hoy = SesionConteo.query.filter(
        SesionConteo.estado.in_(['MATCH', 'AJUSTADO']),
        SesionConteo.fecha_cierre >= hoy_inicio
    )
    if almacen_id:
        q_hoy = q_hoy.filter(SesionConteo.almacen_id == almacen_id)
    hoy_completados = q_hoy.count()

    # Atrasados: PENDIENTE con fecha_creacion > 2 días
    umbral_atraso = datetime.utcnow() - __import__('datetime').timedelta(days=2)
    q_atraso = SesionConteo.query.filter(
        SesionConteo.estado == 'PENDIENTE',
        SesionConteo.fecha_creacion < umbral_atraso
    )
    if almacen_id:
        q_atraso = q_atraso.filter(SesionConteo.almacen_id == almacen_id)
    atrasados = q_atraso.count()

    # Pendientes sin operario asignado
    q_sin_asignar = SesionConteo.query.filter(
        SesionConteo.estado == 'PENDIENTE',
        SesionConteo.operario_id.is_(None)
    )
    if almacen_id:
        q_sin_asignar = q_sin_asignar.filter(SesionConteo.almacen_id == almacen_id)
    sin_asignar = q_sin_asignar.count()

    # Jobs DLQ fallidos (ajustes que Siesa rechazó después de 3 reintentos)
    from app.models.siesa_job import SiesaJob
    fallos_dlq = SiesaJob.query.filter(
        SiesaJob.tipo == 'AJUSTE_CONTEO',
        SiesaJob.estado == 'FALLIDO',
    ).count()

    return jsonify({
        'pendientes': pendientes,
        'en_proceso': en_proceso,
        'segundo_conteo': segundo_conteo,
        'descuadre': descuadre,
        'hoy_completados': hoy_completados,
        'atrasados_2d': atrasados,
        'sin_asignar': sin_asignar,
        'accion_requerida': segundo_conteo + descuadre,
        'resueltos': match + ajustado + ajustando,
        'fallos_dlq': fallos_dlq,
    }), 200


@conteo_bp.route('/asignar-lote', methods=['POST'])
@jwt_required()
def asignar_lote():
    """
    Admin asigna todas las tareas PENDIENTE sin operario a un operario específico.
    Body: { operario_id, almacen_id, limite (opcional, default 20) }
    """
    from app.models.usuario import Usuario
    try:
        admin_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    admin = Usuario.query.get(admin_id)
    if not admin or admin.rol not in Roles.LEAD:
        return jsonify({'error': 'Solo admin o supervisor puede asignar conteos'}), 403

    data = request.get_json() or {}
    operario_id = data.get('operario_id')
    almacen_id = data.get('almacen_id')
    limite = data.get('limite', 20)

    if not operario_id:
        return jsonify({'error': 'operario_id es requerido'}), 400

    operario = Usuario.query.get(operario_id)
    if not operario or not operario.activo:
        return jsonify({'error': 'Operario no encontrado o inactivo'}), 404

    q = SesionConteo.query.filter(
        SesionConteo.estado == 'PENDIENTE',
        SesionConteo.operario_id.is_(None)
    ).order_by(SesionConteo.fecha_creacion.asc())

    if almacen_id:
        q = q.filter(SesionConteo.almacen_id == almacen_id)

    tareas = q.limit(limite).all()

    asignadas = 0
    for t in tareas:
        t.operario_id = operario_id
        asignadas += 1

    if asignadas > 0:
        db.session.commit()
        logger.info(
            f'[CONTEO] {asignadas} tareas asignadas a operario #{operario_id} '
            f'por admin #{admin_id} (almacen={almacen_id})'
        )

    return jsonify({
        'asignadas': asignadas,
        'operario_id': operario_id,
        'operario_nombre': operario.nombre,
        'almacen_id': almacen_id,
    }), 200


@conteo_bp.route('/<int:id>/cancelar', methods=['PUT'])
@jwt_required()
def cancelar_conteo(id):
    """Cancela un conteo que no ha sido ajustado ni está en vuelo a Siesa."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403

    sesion = SesionConteo.query.filter_by(id=id).with_for_update().first()
    if not sesion:
        return jsonify({'error': 'Sesión no encontrada'}), 404

    estados_cancelables = [
        EstadoConteo.PENDIENTE, EstadoConteo.EN_PROCESO,
        EstadoConteo.SEGUNDO_CONTEO, EstadoConteo.DESCUADRE,
    ]
    if sesion.estado not in estados_cancelables:
        return jsonify({
            'error': f'No se puede cancelar en estado {sesion.estado}'
        }), 409

    motivo = (request.get_json() or {}).get('motivo', '').strip()
    if not motivo:
        return jsonify({'error': 'Se requiere un motivo de cancelación'}), 400

    sesion.estado = EstadoConteo.CANCELADO
    sesion.fecha_cierre = datetime.utcnow()
    sesion.motivo_edicion = f'CANCELADO: {motivo}'
    sesion.editado_por = uid
    sesion.editado_en = datetime.utcnow()

    # Cancelar hijo también si existe
    if sesion.hijo_conteo and sesion.hijo_conteo.estado in estados_cancelables:
        sesion.hijo_conteo.estado = EstadoConteo.CANCELADO
        sesion.hijo_conteo.fecha_cierre = datetime.utcnow()
        sesion.hijo_conteo.motivo_edicion = f'CANCELADO (padre): {motivo}'
        sesion.hijo_conteo.editado_por = uid
        sesion.hijo_conteo.editado_en = datetime.utcnow()

    db.session.commit()
    logger.info(f'[CONTEO] #{id} cancelado por usuario #{uid}: {motivo}')
    return jsonify({'mensaje': 'Conteo cancelado', 'sesion': sesion.to_dict()}), 200


@conteo_bp.route('/reintentar-fallos', methods=['POST'])
@jwt_required()
def reintentar_fallos_dlq():
    """Re-encola todos los jobs AJUSTE_CONTEO FALLIDO para un nuevo intento."""
    from app.models.usuario import Usuario
    from app.models.siesa_job import SiesaJob, EstadoSiesaJob
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.LEAD:
        return jsonify({'error': 'Sin permiso'}), 403

    fallidos = SiesaJob.query.filter(
        SiesaJob.tipo == 'AJUSTE_CONTEO',
        SiesaJob.estado == EstadoSiesaJob.FALLIDO,
    ).all()

    reencolados = 0
    for job in fallidos:
        job.estado = EstadoSiesaJob.PENDIENTE
        job.intentos = 0
        job.proximo_intento = None
        job.error_ultimo = None
        reencolados += 1

    if reencolados:
        db.session.commit()
        logger.info(f'[CONTEO DLQ] {reencolados} jobs FALLIDO re-encolados por usuario #{uid}')

    return jsonify({'reencolados': reencolados}), 200


@conteo_bp.route('/exportar', methods=['GET'])
@jwt_required()
def exportar_conteos():
    """
    Exporta historial de conteos en CSV.
    Query params: desde (YYYY-MM-DD), hasta (YYYY-MM-DD), almacen_id.
    Incluye: accuracy por operario, varianza por clase, ajustes totales.
    """
    import csv
    import io
    from flask import Response
    from app.models.usuario import Usuario
    from sqlalchemy.orm import joinedload

    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403

    desde_str = request.args.get('desde')
    hasta_str = request.args.get('hasta')
    almacen_id = request.args.get('almacen_id', type=int)

    q = (SesionConteo.query
         .options(
             joinedload(SesionConteo.producto),
             joinedload(SesionConteo.ubicacion),
             joinedload(SesionConteo.operario),
             joinedload(SesionConteo.almacen),
             joinedload(SesionConteo.aprobador),
         )
         .filter(SesionConteo.estado.in_([
             'MATCH', 'AJUSTADO', 'AJUSTANDO', 'DESCUADRE', 'CANCELADO'
         ]))
         .order_by(SesionConteo.fecha_creacion.desc()))

    if desde_str:
        try:
            desde = datetime.strptime(desde_str, '%Y-%m-%d')
            q = q.filter(SesionConteo.fecha_creacion >= desde)
        except ValueError:
            pass
    if hasta_str:
        try:
            hasta = datetime.strptime(hasta_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
            q = q.filter(SesionConteo.fecha_creacion <= hasta)
        except ValueError:
            pass
    if almacen_id:
        q = q.filter(SesionConteo.almacen_id == almacen_id)

    sesiones = q.limit(5000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Codigo', 'Tipo', 'ABC', 'Estado', 'Producto', 'Producto Nombre',
        'Ubicacion', 'Almacen', 'Operario', 'Stock WMS', 'Conteo Fisico',
        'Diferencia', 'Motivo', '2do Conteo', 'Aprobador',
        'Creacion', 'Cierre', 'Editado', 'Motivo Edicion',
    ])

    for s in sesiones:
        writer.writerow([
            s.codigo,
            s.tipo,
            s.clasificacion_abc or '',
            s.estado,
            s.producto.codigo if s.producto else '',
            s.producto.nombre if s.producto else '',
            s.ubicacion.codigo if s.ubicacion else '',
            s.almacen.nombre if s.almacen else '',
            s.operario.nombre if s.operario else '',
            s.existencia_siesa if s.existencia_siesa is not None else '',
            s.cantidad_fisica if s.cantidad_fisica is not None else '',
            s.diferencia if s.diferencia is not None else '',
            s.motivo_codigo or '',
            'Si' if s.es_segundo_conteo else 'No',
            s.aprobador.nombre if s.aprobador else '',
            s.fecha_creacion.strftime('%Y-%m-%d %H:%M') if s.fecha_creacion else '',
            s.fecha_cierre.strftime('%Y-%m-%d %H:%M') if s.fecha_cierre else '',
            s.editado_en.strftime('%Y-%m-%d %H:%M') if s.editado_en else '',
            s.motivo_edicion or '',
        ])

    # Resumen al final
    writer.writerow([])
    writer.writerow(['=== RESUMEN ==='])

    # Accuracy por operario
    from collections import defaultdict
    op_stats = defaultdict(lambda: {'total': 0, 'match': 0})
    for s in sesiones:
        if not s.operario or s.es_segundo_conteo:
            continue
        nombre = s.operario.nombre
        op_stats[nombre]['total'] += 1
        if s.estado == 'MATCH':
            op_stats[nombre]['match'] += 1

    writer.writerow([])
    writer.writerow(['Operario', 'Total Conteos', 'Match', 'Accuracy %'])
    for nombre, stats in sorted(op_stats.items()):
        acc = round(stats['match'] / stats['total'] * 100, 1) if stats['total'] else 0
        writer.writerow([nombre, stats['total'], stats['match'], f'{acc}%'])

    # Varianza por clase ABC
    clase_stats = defaultdict(lambda: {'total': 0, 'match': 0, 'ajustado': 0, 'sum_dif': 0})
    for s in sesiones:
        if s.es_segundo_conteo:
            continue
        cls = s.clasificacion_abc or '?'
        clase_stats[cls]['total'] += 1
        if s.estado == 'MATCH':
            clase_stats[cls]['match'] += 1
        if s.estado in ('AJUSTADO', 'AJUSTANDO'):
            clase_stats[cls]['ajustado'] += 1
            clase_stats[cls]['sum_dif'] += abs(s.diferencia or 0)

    writer.writerow([])
    writer.writerow(['Clase ABC', 'Total', 'Match', 'Ajustados', 'Accuracy %', 'Varianza Total (uds)'])
    for cls in ['A', 'B', 'C', '?']:
        if cls not in clase_stats:
            continue
        st = clase_stats[cls]
        acc = round(st['match'] / st['total'] * 100, 1) if st['total'] else 0
        writer.writerow([cls, st['total'], st['match'], st['ajustado'], f'{acc}%', st['sum_dif']])

    resp = Response(output.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = f'attachment; filename=conteos_{desde_str or "all"}_{hasta_str or "all"}.csv'
    return resp