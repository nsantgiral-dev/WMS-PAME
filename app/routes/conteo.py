import uuid
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.conteo import SesionConteo, EstadoConteo
from app.services.conteo_service import ConteoService
from app.services.abc_service import ABCService, RezagoCambioDesdeLaVistaPrevia
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
    # La ruta solo parsea: qué muestra cada pestaña y qué deja pasar cada
    # filtro vive en `app/services/conteo_listado.py`.
    from app.services import conteo_listado
    try:
        filtros = conteo_listado.normalizar_filtros(
            vista=request.args.get('vista'),
            estados=request.args.get('estados'),
            estado=request.args.get('estado'),
            almacen_id=_entero_o_400('almacen_id'),
            clasificacion=request.args.get('clasificacion'),
            operario_id=_entero_o_400('operario_id'),
            marca=request.args.get('marca'),
            categoria=request.args.get('categoria'),
        )
        page = _entero_o_400('page') or 1
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(conteo_listado.listar(filtros, page)), 200


def _entero_o_400(nombre):
    """Un parámetro entero de la query. Vacío es None; ilegible es ValueError
    (400): con `type=int` Flask lo convertía en None y la lista salía sin ese
    filtro, con cara de ser la pedida."""
    crudo = (request.args.get(nombre) or '').strip()
    if not crudo:
        return None
    try:
        return int(crudo)
    except ValueError:
        raise ValueError(f'{nombre} inválido: {crudo!r}')


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

    from app.services.conteo_politica import orden_de_reparto
    from sqlalchemy.orm import selectinload as _sl_mis
    tareas = (SesionConteo.query
              .options(_sl_mis(SesionConteo.ubicacion), _sl_mis(SesionConteo.producto))
              .filter(
                  SesionConteo.operario_id == operario_id,
                  SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO'])
              ).order_by(*orden_de_reparto()).all())

    return jsonify({
        'tareas': [t.to_dict_operario() for t in tareas],
        'total': len(tareas)
    }), 200


@conteo_bp.route('/definitivos', methods=['GET'])
@jwt_required()
def listar_definitivos():
    """
    Cola de "Conteo Definitivo" — sesiones CC1≠CC2 esperando el CC3 que
    rompe el empate. Solo supervisor/admin/jefe_almacén: el CC3 nace sin
    asignar a propósito (`ConteoService._crear_conteo_verificacion`) y este
    es el único lugar donde alguien lo puede tomar.
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso para ver la cola de conteo definitivo'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    pendientes = ConteoService.listar_definitivos(almacen_id=almacen_id)
    return jsonify({'pendientes': pendientes, 'total': len(pendientes)}), 200


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
            return jsonify({'error': 'No puede registrar el conteo de otra persona'}), 403

    try:
        cantidad_fisica = int(data['cantidad_fisica'])
    except (ValueError, TypeError):
        return jsonify({'error': 'cantidad_fisica debe ser un entero válido'}), 400

    try:
        resultado = ConteoService.registrar_conteo(
            sesion_id=id,
            operario_id=operario_id,
            cantidad_fisica=cantidad_fisica,
            lote_id=data.get('lote_id'),
            cero_confirmado=data.get('cero_confirmado') is True,
        )
        return jsonify(ConteoService.respuesta_para_quien_cuenta(resultado, operario_id)), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(f'[CONTEO] Error inesperado en registrar_conteo sesion={id}')
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/<int:id>/ajustar', methods=['PUT'])
@jwt_required()
def confirmar_ajuste(id):
    """
    Aprueba un ajuste de inventario y lo encola a Siesa (motivo 01 entrada / 02
    salida). **Quién aprueba cuánto lo decide el servicio**
    (`ConteoService.motivo_no_puede_aprobar`): supervisor y admin cualquier
    monto, jefe de almacén hasta `CONTEO_TOPE_APROBACION_JEFE`. Acá solo se
    corta temprano a quien no aprueba ningún ajuste
    (`motivo_no_aprueba_ningun_ajuste`, la misma que usa el tablero) y se
    traduce el `PermissionError` del servicio a 403 con su mensaje.
    """
    from app.models.usuario import Usuario
    try:
        supervisor_id = int(get_jwt_identity())
    except (ValueError, TypeError):
        return jsonify({'error': 'Identidad de usuario inválida en el token'}), 422
    usuario = Usuario.query.get(supervisor_id)
    if not usuario or usuario.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Solo un supervisor, admin o jefe de almacén puede aprobar ajustes de inventario'}), 403
    no_aprueba = ConteoService.motivo_no_aprueba_ningun_ajuste(usuario)
    if no_aprueba:
        return jsonify({'error': no_aprueba}), 403
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
    except PermissionError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(f'[CONTEO] Error inesperado en confirmar_ajuste sesion={id}')
        return jsonify({'error': str(e)}), 500


def _adelantar(data: dict) -> bool:
    """`adelantar` — o `forzar_todo`, su nombre anterior (un cliente con la
    PWA vieja en caché lo sigue mandando). Los dos significan lo mismo desde
    2026-09-23: considerar también huecos al día, **dentro del cupo**. Ya no
    existe un modo que cree todo lo elegible sin límite."""
    return bool(data.get('adelantar', data.get('forzar_todo', False)))


@conteo_bp.route('/abc/generar-tareas', methods=['POST'])
@jwt_required()
def generar_tareas_abc():
    """
    Genera los conteos del plan de UNA clase, dentro del cupo diario del
    almacén (ver `conteo_politica`). `adelantar=true` también toma huecos al
    día, los más atrasados primero — nunca más que el cupo.
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
            adelantar=_adelantar(data),
        )
        return jsonify(resultado), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(f'[CONTEO] Error en generar_tareas_conteo_diario almacen={data.get("almacen_id")}')
        return jsonify({'error': str(e)}), 500


@conteo_bp.route('/abc/generar-todas', methods=['POST'])
@jwt_required()
def generar_todas_las_clases():
    """Watchdog + plan A+B+C dentro del cupo, en una sola llamada — solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede generar tareas de conteo'}), 403
    data = request.get_json() or {}
    if 'almacen_id' not in data:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        resultado = ABCService.generar_todas_las_clases(
            almacen_id=data['almacen_id'],
            adelantar=_adelantar(data),
        )
        return jsonify(resultado), 201
    except Exception as e:
        logger.exception(f'[CONTEO] Error en generar_todas_las_clases almacen={data.get("almacen_id")}')
        return jsonify({'error': str(e)}), 500


def _clase_de(valor):
    clase = (valor or '').strip().upper() or None
    if clase and clase not in ('A', 'B', 'C'):
        raise ValueError('clasificacion debe ser A, B, C o vacía (todas)')
    return clase


@conteo_bp.route('/abc/limpiar-pendientes/preview', methods=['GET'])
@jwt_required()
def preview_limpiar_pendientes_abc():
    """GET — qué cancelaría «Limpiar cola», sin tocar nada: cuántas, por clase,
    por tipo y por antigüedad, y lo que NO se toca con su motivo. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede limpiar la cola de conteo'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        plan = ABCService.plan_cancelar_rezago(
            almacen_id, _clase_de(request.args.get('clasificacion')))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    plan.pop('ids', None)
    plan['ejecutado'] = False
    return jsonify(plan), 200


@conteo_bp.route('/abc/limpiar-pendientes', methods=['POST'])
@jwt_required()
def limpiar_pendientes_abc():
    """
    CANCELA —no borra— el rezago del plan: raíces DIARIO_ABC/WATCHDOG_ABC en
    PENDIENTE, sin dueño y sin hijos. Nunca CC2/CC3, MANUAL ni auditorías por
    faltante. Exige `motivo` y, si se manda, `esperadas` (lo que mostró la
    vista previa): si el rezago cambió, no cancela nada (409). Solo admin.

    Antes era un DELETE físico de TODA pendiente del almacén: se llevaba los
    CC2 (y dejaba su raíz huérfana en SEGUNDO_CONTEO), los conteos manuales y
    las auditorías por faltante, sin rastro de quién ni por qué.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede limpiar la cola de conteo'}), 403
    data = request.get_json() or {}
    almacen_id = data.get('almacen_id')
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    motivo = (data.get('motivo') or '').strip()
    if not motivo:
        return jsonify({'error': 'Se requiere un motivo para cancelar el rezago'}), 400
    try:
        clase = _clase_de(data.get('clasificacion'))
        uid = int(get_jwt_identity())
        resultado = ABCService.cancelar_rezago(
            int(almacen_id), motivo=motivo, usuario_id=uid, clasificacion=clase,
            esperadas=data.get('esperadas'))
    except RezagoCambioDesdeLaVistaPrevia as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@conteo_bp.route('/manual', methods=['POST'])
@jwt_required()
def crear_conteo_manual():
    """
    Admin o supervisor crea una tarea de conteo manual por código de producto.
    Útil para verificar un producto específico o generar conteos por marca
    antes de una OC.

    operario_id (opcional): fuerza el CC1 a un operario específico en vez de
    dejarlo sin asignar para el dispatcher automático — ver
    ConteoService.crear_conteo_manual.
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.LEAD:
        return jsonify({'error': 'Solo admin o supervisor puede crear conteos manuales'}), 403
    data = request.get_json() or {}

    almacen_id = data.get('almacen_id')
    producto_codigo = (data.get('producto_codigo') or '').strip()
    operario_id = data.get('operario_id')
    if not almacen_id or not producto_codigo:
        return jsonify({'error': 'almacen_id y producto_codigo son requeridos'}), 400

    try:
        resultado = ConteoService.crear_conteo_manual(
            almacen_id, producto_codigo,
            operario_id=int(operario_id) if operario_id else None,
        )
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
    Detecta productos B/C con alta rotación real y abre un conteo inmediato,
    dentro del cupo del almacén y nunca sobre un hueco recién contado.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ejecutar el watchdog'}), 403
    data = request.get_json() or {}
    if 'almacen_id' not in data:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        informe = ABCService.watchdog_con_informe(almacen_id=data['almacen_id'])
        return jsonify({'overrides': len(informe['overrides']), 'detalle': informe['overrides'],
                        'omitidos_por_cupo': informe['omitidos_por_cupo'],
                        'omitidos_recien_contados': informe['omitidos_recien_contados'],
                        'cupo': informe['cupo']}), 200
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

    # Corregir cantidad_fisica — dispara re-conciliación si ya tiene
    # existencia_siesa. La política (y el «no con un CC2/CC3 vivo colgando»)
    # vive en el servicio.
    if 'cantidad_fisica' in data:
        nueva_cantidad = data['cantidad_fisica']
        if not isinstance(nueva_cantidad, int) or isinstance(nueva_cantidad, bool) \
                or nueva_cantidad < 0:
            return jsonify({'error': 'cantidad_fisica debe ser un entero >= 0'}), 400
        try:
            cambios.extend(ConteoService.corregir_cantidad(sesion, nueva_cantidad))
        except ValueError as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 409

    # Reasignar operario — la política (qué estados, doble ciego, lo contado
    # en curso) vive en el servicio.
    if 'operario_id' in data:
        from app.services.conteo_service import ConteoNoReasignable
        try:
            cambios.extend(ConteoService.reasignar_operario(sesion, data['operario_id']))
        except LookupError as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 404
        except ConteoNoReasignable as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 409
        except ValueError as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400

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
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403

    try:
        almacen_id = _entero_o_400('almacen_id')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    # Cuenta con las mismas consultas que la lista (`conteo_listado`): el
    # contador de «Acción» es el total de la pestaña Acción.
    from app.services import conteo_listado
    return jsonify(conteo_listado.barra(almacen_id)), 200


@conteo_bp.route('/estadisticas', methods=['GET'])
@jwt_required()
def estadisticas_conteo():
    """Estadísticas del conteo cíclico: carga y cobertura del plan ABC, flujo,
    ajustes (unidades y valor estimado), exactitud y productos problema.

    Query: `desde`, `hasta` (YYYY-MM-DD, días operativos Bogotá; por defecto las
    últimas 4 semanas), `almacen_id`, `clase` (A/B/C), `tipo`.

    La ruta solo parsea: toda la política vive en
    `app/services/metricas/conteo.py`. Un parámetro ilegible es 400 — ignorarlo
    en silencio devolvería el reporte de otro rango con cara de ser el pedido.
    """
    from app.models.usuario import Usuario
    from app.services.metricas.conteo import calcular_estadisticas_conteo
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403

    fechas = {}
    for nombre in ('desde', 'hasta'):
        crudo = (request.args.get(nombre) or '').strip()
        if not crudo:
            fechas[nombre] = None
            continue
        try:
            fechas[nombre] = datetime.strptime(crudo, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': f'{nombre} inválida: {crudo!r} (formato YYYY-MM-DD)'}), 400

    almacen_crudo = (request.args.get('almacen_id') or '').strip()
    almacen_id = None
    if almacen_crudo:
        try:
            almacen_id = int(almacen_crudo)
        except ValueError:
            return jsonify({'error': f'almacen_id inválido: {almacen_crudo!r}'}), 400

    clase = (request.args.get('clase') or '').strip().upper() or None
    tipo = (request.args.get('tipo') or '').strip().upper() or None
    try:
        reporte = calcular_estadisticas_conteo(
            fechas['desde'], fechas['hasta'],
            almacen_id=almacen_id, clase=clase, tipo=tipo)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(reporte), 200


@conteo_bp.route('/lider/tablero', methods=['GET'])
@jwt_required()
def tablero_lider():
    """El tablero del líder de bodega: lo que espera su decisión, lo urgente
    primero y cada fila con su acción. Query: `almacen_id` (obligatorio).

    La ruta solo parsea: la política vive en
    `app/services/tablero_lider_conteo.py`. Cero Siesa.
    """
    from app.models.usuario import Usuario
    from app.services.tablero_lider_conteo import tablero
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    crudo = (request.args.get('almacen_id') or '').strip()
    if not crudo:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        almacen_id = int(crudo)
    except ValueError:
        return jsonify({'error': f'almacen_id inválido: {crudo!r}'}), 400
    try:
        return jsonify(tablero(almacen_id, rol=u.rol)), 200
    except LookupError as e:
        return jsonify({'error': str(e)}), 404


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

    # Sin almacén explícito, el del operario. Antes, sin almacén se asignaban
    # conteos de cualquier bodega; y sin filtro de cadena se asignaban también
    # CC3 (que son de supervisión) y CC2 cuyo CC1 había hecho el mismo operario.
    almacen_id = almacen_id or ConteoService.almacen_de_usuario(operario)
    if not almacen_id:
        return jsonify({'error': 'Indicar almacen_id: el operario no tiene almacén asignado'}), 400

    from app.services.conteo_politica import orden_de_reparto
    q = (SesionConteo.query
         .filter(*ConteoService.filtros_pool_sin_dueno(operario.id, almacen_id))
         .order_by(*orden_de_reparto()))

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
    """Cancela un conteo **y toda su cadena** (CC1, CC2, CC3).

    La política vive en `ConteoService.cancelar_cadena`: cancelar solo el
    eslabón elegido dejaba a la raíz esperando un conteo que ya no iba a
    llegar, y el hueco trabado para siempre.
    """
    from app.models.usuario import Usuario
    from app.services.conteo_service import CadenaNoCancelable
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    # El servicio lo vuelve a exigir: esto protege la ruta, aquello la operación.
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    try:
        sesion = ConteoService.cancelar_cadena(
            id, uid, (request.get_json() or {}).get('motivo', ''))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except CadenaNoCancelable as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    logger.info(f'[CONTEO] #{id} cancelado con su cadena por usuario #{uid}')
    return jsonify({'mensaje': 'Conteo cancelado con su cadena', 'sesion': sesion.to_dict()}), 200


# `GET /bloqueados` se retiró el 2026-09-24: su única pantalla era la copia de
# la lista en la pestaña 🎯 Definitivo, que se fundió con 🧭 Por decidir. La
# lista vive en el tablero (`tablero_lider_conteo._bloqueados`, misma
# `ConteoService.listar_bloqueados`): una lista, un lugar.


@conteo_bp.route('/<int:id>/reabrir', methods=['POST'])
@jwt_required()
def reabrir_bloqueado(id):
    """El líder devuelve un conteo BLOQUEADO a la cola (PENDIENTE, desde
    cero). Opcional: `operario_id` para dárselo a alguien en particular."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    # El servicio lo vuelve a exigir: esto protege la ruta, aquello la operación.
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    data = request.get_json() or {}
    try:
        sesion = ConteoService.reabrir_bloqueado(
            id, uid, nota=data.get('nota'), operario_id=data.get('operario_id'))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'mensaje': 'Conteo reabierto — vuelve a la cola desde cero',
                    'sesion': sesion.to_dict()}), 200


@conteo_bp.route('/recogido-sin-despachar', methods=['GET'])
@jwt_required()
def listar_recogido_sin_despachar():
    """Pedidos con mercancía recogida que no salió ni está saliendo (empaque
    cancelado sin remisión, o nunca empacado). Mientras nadie declare que
    volvió al estante, sus productos quedan fuera del plan de conteo."""
    from app.models.usuario import Usuario
    from app.services.picking_service import PickingService
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    filas = PickingService.recogido_sin_despachar(
        almacen_id=request.args.get('almacen_id', type=int))
    return jsonify({'pedidos': filas, 'total': len(filas)}), 200


@conteo_bp.route('/recogido-sin-despachar/devuelto', methods=['POST'])
@jwt_required()
def declarar_devuelto_al_estante():
    """El líder declara que la mercancía recogida de un pedido volvió al
    estante. Body: `{pedido, almacen_id, nota?}`. No mueve inventario."""
    from app.models.usuario import Usuario
    from app.services.picking_service import PickingService
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    # El servicio lo vuelve a exigir: esto protege la ruta, aquello la operación.
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    data = request.get_json() or {}
    try:
        r = PickingService.declarar_devuelto_al_estante(
            data.get('pedido'), data.get('almacen_id'), uid, nota=data.get('nota'))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, **r}), 200


# `GET /novedades` se retiró el 2026-09-24 por la misma razón que
# `GET /bloqueados`: la lista vive en el tablero (`_novedades`).


@conteo_bp.route('/novedades/<int:nid>/resolver', methods=['POST'])
@jwt_required()
def resolver_novedad(nid):
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    try:
        nov = ConteoService.resolver_novedad(nid, uid, (request.get_json() or {}).get('nota'))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'novedad': nov.to_dict()}), 200


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

    # Un ajuste que quedó sin verificar (pudo haber entrado) no se reencola:
    # la guarda lo cerraría como hecho sin que nadie mire Siesa (tanda 2).
    from app.services.siesa_job_service import preflag_sin_verificar
    sin_verificar = [j.id for j in fallidos if preflag_sin_verificar(j)]
    fallidos = [j for j in fallidos if j.id not in set(sin_verificar)]

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

    return jsonify({'reencolados': reencolados, 'sin_verificar': sin_verificar}), 200


@conteo_bp.route('/<int:id>/omitir-segundo', methods=['POST'])
@jwt_required()
def omitir_segundo_conteo(id):
    """
    Admin omite el CC2 (o CC3) pendiente y mueve el padre directo a DESCUADRE.
    Útil cuando no hay segundo operario disponible o en entornos de prueba.
    El ajuste queda pendiente de aprobación manual del admin.
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.LEAD:
        return jsonify({'error': 'Sin permiso'}), 403

    sesion = SesionConteo.query.get(id)
    if not sesion:
        return jsonify({'error': 'Sesión no encontrada'}), 404
    if sesion.estado not in ('SEGUNDO_CONTEO', 'TERCER_CONTEO'):
        return jsonify({'error': f'Solo se puede omitir en estado SEGUNDO_CONTEO o TERCER_CONTEO, actual: {sesion.estado}'}), 400

    # Cancelar TODO conteo vivo de la cadena, cada uno por su estado. Antes el
    # CC3 solo se cancelaba si su padre CC2 seguía vivo, pero con la raíz en
    # TERCER_CONTEO el CC2 ya está en DESCUADRE: el CC3 quedaba vivo y huérfano
    # en la cola de Conteo Definitivo, y contarlo no llegaba a ningún lado
    # (la propagación exige la raíz en TERCER_CONTEO).
    # BLOQUEADO también: un CC2/CC3 bloqueado que sobrevive a la omisión
    # queda para siempre en la cola de bloqueados de una cadena resuelta. La
    # lista de «vivo» es la de la cancelación (`descendientes_vivos`).
    ahora = datetime.utcnow()
    for descendiente in ConteoService.descendientes_vivos(sesion):
        descendiente.estado = EstadoConteo.CANCELADO
        descendiente.fecha_cierre = ahora

    sesion.estado = 'DESCUADRE'
    db.session.commit()
    logger.info(f'[CONTEO] Sesión {sesion.id} omitió CC2/CC3 — movida a DESCUADRE por usuario #{uid}')
    return jsonify({'ok': True, 'sesion_id': id, 'estado': 'DESCUADRE'}), 200


#: Qué se le hace a la sesión de cada job descartado. El nombre es lo que
#: aparece en el preview, así que dice la consecuencia, no el mecanismo.
_RESET = 'reset_a_descuadre'
_YA_AJUSTADA = 'sin_tocar_ya_ajustada'
_HUERFANA = 'SE_CONSERVA_POSIBLE_ENVIO'
_SIN_SESION = 'job_sin_sesion_en_payload'
_NO_EXISTE = 'sesion_no_existe'
_OTRO_ESTADO = 'sin_tocar_otro_estado'


def _plan_descarte_ajustes():
    """Qué haría un descarte de AJUSTE_CONTEO — **sin hacer nada**.

    Fuente única del preview y del POST. Que sean el mismo cálculo es el punto:
    un preview que estima por su cuenta lo que el POST hace por la suya es un
    preview que miente el día que uno de los dos cambie. Hoy mismo costó 93
    jobs tener una política escrita dos veces.

    Devuelve el plan completo, incluida **la parte que no se toca**. El endpoint
    viejo respondía `{descartados, sesiones_reset}`: con 103 jobs y 12 sesiones
    reseteadas, nada decía dónde quedaron las otras 91. Una acción masiva que
    solo reporta lo que hizo obliga a confiar; una que reporta lo que dejó atrás
    se puede auditar.
    """
    from app.models.siesa_job import SiesaJob, EstadoSiesaJob
    from app.models.conteo import SesionConteo, EstadoConteo

    fallidos = (SiesaJob.query
                .filter(SiesaJob.tipo == 'AJUSTE_CONTEO',
                        SiesaJob.estado == EstadoSiesaJob.FALLIDO)
                .all())

    items = []
    for job in fallidos:
        sid = (job.get_payload() or {}).get('sesion_id')
        if not sid:
            items.append({'job_id': job.id, 'sesion_id': None,
                          'accion': _SIN_SESION, 'estado_sesion': None,
                          'por_que': 'el payload no dice a qué sesión pertenece'})
            continue

        s = db.session.get(SesionConteo, int(sid))
        if s is None:
            items.append({'job_id': job.id, 'sesion_id': int(sid),
                          'accion': _NO_EXISTE, 'estado_sesion': None,
                          'por_que': 'la sesión ya no está en la base'})
        elif s.estado != EstadoConteo.AJUSTANDO:
            items.append({'job_id': job.id, 'sesion_id': s.id,
                          'accion': (_YA_AJUSTADA if s.estado == EstadoConteo.AJUSTADO
                                     else _OTRO_ESTADO),
                          'estado_sesion': s.estado,
                          'por_que': f'la sesión ya está en {s.estado}, no en AJUSTANDO'})
        elif s.siesa_triggered:
            # El caso que hay que ver ANTES de apretar el botón.
            #
            # El descarte solo resetea sesiones `AJUSTANDO and not
            # siesa_triggered`. Y el barrido de `siesa_job_service` que rescata
            # sesiones atascadas filtra por `siesa_triggered == False`. Una
            # sesión AJUSTANDO CON el flag puesto no la toca ninguno de los dos
            # y se queda sin job: nadie la vuelve a mirar, y no aparece en
            # ningún contador.
            items.append({'job_id': job.id, 'sesion_id': s.id,
                          'accion': _HUERFANA,
                          'estado_sesion': s.estado,
                          'por_que': (
                              'AJUSTANDO con siesa_triggered=True: el ajuste pudo haber '
                              'llegado a Siesa. Su job NO se descarta: sin job, ni el '
                              'barrido de sesiones atascadas ni nadie la volvería a '
                              'mirar y la cadena quedaría sin salida. Queda FALLIDO '
                              'sin verificar: búsquelo en Siesa y diga si llegó '
                              '(Siesa → Recuperación, «¿Está en Siesa?»). Si llegó, '
                              'se cierra como AJUSTADO sin volver a llamar a Siesa.')})
        else:
            items.append({'job_id': job.id, 'sesion_id': s.id,
                          'accion': _RESET, 'estado_sesion': s.estado,
                          'por_que': 'AJUSTANDO sin marca de envío: vuelve a DESCUADRE'})

    resumen = {}
    for it in items:
        resumen[it['accion']] = resumen.get(it['accion'], 0) + 1

    return {
        'jobs_fallidos': len(fallidos),
        'resumen': resumen,
        'huerfanas': sorted({it['sesion_id'] for it in items
                             if it['accion'] == _HUERFANA and it['sesion_id']}),
        'items': items,
    }


@conteo_bp.route('/descartar-fallos/preview', methods=['GET'])
@jwt_required()
def preview_descartar_fallos():
    """GET — qué pasaría si se descartaran los AJUSTE_CONTEO fallidos.

    No escribe nada. Existe porque la acción es masiva e irreversible en la
    práctica (un job descartado no se vuelve a intentar), y porque su parte
    peligrosa —las sesiones que quedan huérfanas— no se ve en ningún contador
    después de ejecutarla.
    """
    from app.models.usuario import Usuario

    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.LEAD:
        return jsonify({'error': 'Sin permiso'}), 403

    plan = _plan_descarte_ajustes()
    plan['ejecutado'] = False
    if plan['huerfanas']:
        plan['advertencia'] = (
            f'{len(plan["huerfanas"])} sesión(es) en AJUSTANDO con el ajuste '
            f'posiblemente enviado: {plan["huerfanas"]}. Sus jobs NO se descartan '
            f'(quedan FALLIDO para reintentar). Verifique en Siesa si el ajuste llegó.'
        )
    return jsonify(plan), 200


@conteo_bp.route('/descartar-fallos', methods=['POST'])
@jwt_required()
def descartar_fallos_dlq():
    """
    Descarta todos los jobs AJUSTE_CONTEO FALLIDO y resetea sus sesiones
    de AJUSTANDO → DESCUADRE para que el admin pueda re-aprobar o cancelar.

    Los jobs quedan en **DESCARTADO**, no en COMPLETADO. Antes se escribía
    COMPLETADO con una marca `descartado: true` dentro del JSON de `resultado`:
    toda consulta que filtrara por estado los contaba como envíos exitosos a
    Siesa. Nada de esto llegó a Siesa.

    Devuelve el mismo plan que `/descartar-fallos/preview`, con `ejecutado:
    true` — incluida la parte que NO se tocó, que es la que hay que mirar.
    """
    import json as _json
    from app.models.siesa_job import SiesaJob, EstadoSiesaJob
    from app.models.conteo import SesionConteo, EstadoConteo
    from app.models.usuario import Usuario

    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.LEAD:
        return jsonify({'error': 'Sin permiso'}), 403

    plan = _plan_descarte_ajustes()
    ahora = datetime.utcnow()

    for it in plan['items']:
        if it['accion'] == _HUERFANA:
            # Descartarle el job la dejaba AJUSTANDO sin job para siempre: una
            # cadena sin salida (ningún barrido la recoge, cancelar y editar
            # rechazan AJUSTANDO). Su job queda FALLIDO, visible y reintentable.
            continue
        job = db.session.get(SiesaJob, it['job_id'])
        if job is None:
            continue
        job.estado = EstadoSiesaJob.DESCARTADO
        job.resultado = _json.dumps({
            'descartado': True,
            'por_usuario': uid,
            'fecha': ahora.isoformat(),
            'accion_sobre_sesion': it['accion'],
        })
        job.fecha_completado = ahora
        if it['accion'] == _RESET:
            s = db.session.get(SesionConteo, it['sesion_id'])
            if s and s.estado == EstadoConteo.AJUSTANDO and not s.siesa_triggered:
                s.estado = EstadoConteo.DESCUADRE

    if plan['items']:
        db.session.commit()
        logger.info(
            '[CONTEO DLQ] %s jobs DESCARTADOS por usuario #%s — plan: %s%s',
            plan['jobs_fallidos'], uid, plan['resumen'],
            (f' — HUÉRFANAS: {plan["huerfanas"]}' if plan['huerfanas'] else ''),
        )

    plan['ejecutado'] = True
    # Compatibilidad con quien ya leía estas dos claves. Se conservan con el
    # mismo significado; lo que cambia es que ahora no son lo único que hay.
    plan['descartados'] = plan['jobs_fallidos'] - plan['resumen'].get(_HUERFANA, 0)
    plan['sesiones_reset'] = plan['resumen'].get(_RESET, 0)
    return jsonify(plan), 200


#: Filas máximas del CSV de `/exportar`. Si hay más, el CSV lo declara en una
#: línea `TRUNCADO` — un corte silencioso se lee como «esto es todo».
LIMITE_EXPORTAR = 5000


@conteo_bp.route('/exportar', methods=['GET'])
@jwt_required()
def exportar_conteos():
    """
    Exporta historial de conteos en CSV.
    Query params: desde (YYYY-MM-DD), hasta (YYYY-MM-DD), almacen_id.
    Incluye el resumen por clase con el veredicto por cadena
    (`veredicto_cadena`), ajustes y varianza.

    **Ya no trae exactitud por operario** (2026-09-23, decisión del usuario):
    contaba filas CC1 sueltas, con muestra chica y con el sesgo de que el CC2
    solo le toca a quien le mandan los huecos que no cuadraron. Ver
    `app/services/metricas/conteo.py::_por_operario`.
    """
    import csv
    import io
    from flask import Response
    from app.models.usuario import Usuario
    from sqlalchemy.orm import joinedload, selectinload
    from app.services.metricas.conteo import (MIN_N_EXACTITUD, SIN_VEREDICTO,
                                              VEREDICTOS_ERROR, VEREDICTOS_OK,
                                              veredicto_cadena)

    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403

    from app.services import conteo_listado
    desde_str = (request.args.get('desde') or '').strip()
    hasta_str = (request.args.get('hasta') or '').strip()
    try:
        desde_utc, hasta_utc = conteo_listado.rango_de_dias(desde_str, hasta_str)
        almacen_id = _entero_o_400('almacen_id')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    q = (SesionConteo.query
         .options(
             joinedload(SesionConteo.producto),
             joinedload(SesionConteo.ubicacion),
             joinedload(SesionConteo.operario),
             joinedload(SesionConteo.almacen),
             joinedload(SesionConteo.aprobador),
             # El veredicto se lee de la cadena entera: sin esto, un N+1 de
             # hasta dos consultas por raíz sobre 5.000 filas.
             selectinload(SesionConteo.hijo_conteo).selectinload(SesionConteo.hijo_conteo),
         )
         .filter(SesionConteo.estado.in_([
             'MATCH', 'AJUSTADO', 'AJUSTANDO', 'DESCUADRE', 'CANCELADO'
         ]))
         .order_by(SesionConteo.fecha_creacion.desc()))

    if desde_utc:
        q = q.filter(SesionConteo.fecha_creacion >= desde_utc)
    if hasta_utc:
        q = q.filter(SesionConteo.fecha_creacion < hasta_utc)
    if almacen_id:
        q = q.filter(SesionConteo.almacen_id == almacen_id)

    LIMITE = LIMITE_EXPORTAR
    sesiones = q.limit(LIMITE + 1).all()
    # Una fila de más dice si hubo corte. Un CSV truncado sin decirlo se lee
    # como «esto es todo lo que hubo».
    truncado = len(sesiones) > LIMITE
    sesiones = sesiones[:LIMITE]

    def _num(v):
        return v if v is not None else ''

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Codigo', 'Tipo', 'ABC', 'Estado', 'Producto', 'Producto Nombre',
        'Ubicacion', 'Almacen', 'Operario', 'Existencia Siesa', 'Teorico Siesa',
        'POS', 'Fuente', 'Costo unit.', 'Conteo Fisico',
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
            _num(s.existencia_siesa),
            _num(s.teorico_siesa),
            _num(s.cant_pos_siesa),
            s.fuente_existencia or '',
            _num(s.costo_prom_uni_siesa),
            _num(s.cantidad_fisica),
            _num(s.diferencia),
            s.motivo_codigo or '',
            'Si' if s.es_segundo_conteo else 'No',
            s.aprobador.nombre if s.aprobador else '',
            s.fecha_creacion.strftime('%Y-%m-%d %H:%M') if s.fecha_creacion else '',
            s.fecha_cierre.strftime('%Y-%m-%d %H:%M') if s.fecha_cierre else '',
            s.editado_en.strftime('%Y-%m-%d %H:%M') if s.editado_en else '',
            s.motivo_edicion or '',
        ])

    if truncado:
        writer.writerow([f'TRUNCADO: se exportaron las {LIMITE} sesiones más recientes; '
                         'hay más en el rango. Acotar desde/hasta o almacén.'])

    # Resumen al final
    writer.writerow([])
    writer.writerow(['=== RESUMEN ==='])

    # Resumen por clase — por CADENA, con el veredicto de `veredicto_cadena`
    # (la misma función que las estadísticas). Antes contaba filas raíz por
    # su estado: una raíz MATCH por un CC2 que cuadró salía «acierto» igual
    # que una que cuadró sola, y una raíz AJUSTADO no contaba como error.
    from collections import defaultdict
    clase_stats = defaultdict(lambda: {'total': 0, 'ok': 0, 'error': 0, 'sin': 0,
                                       'ajustado': 0, 'sum_dif': 0})
    for s in sesiones:
        if s.es_segundo_conteo:
            continue
        cls = s.clasificacion_abc or '?'
        v = veredicto_cadena(s)
        st = clase_stats[cls]
        st['total'] += 1
        if v in VEREDICTOS_OK:
            st['ok'] += 1
        elif v in VEREDICTOS_ERROR:
            st['error'] += 1
        elif v == SIN_VEREDICTO:
            st['sin'] += 1
        if s.estado in ('AJUSTADO', 'AJUSTANDO'):
            st['ajustado'] += 1
            st['sum_dif'] += abs(s.diferencia or 0)

    writer.writerow([])
    writer.writerow(['Clase ABC', 'Cadenas', 'OK', 'Error', 'Sin veredicto', 'Ajustados',
                     'Exactitud %', 'Varianza Total (uds)'])
    for cls in ['A', 'B', 'C', '?']:
        if cls not in clase_stats:
            continue
        st = clase_stats[cls]
        n = st['ok'] + st['error']
        acc = (f'{round(st["ok"] / n * 100, 1)}%' if n >= MIN_N_EXACTITUD
               else f'n={n} < {MIN_N_EXACTITUD}')
        writer.writerow([cls, st['total'], st['ok'], st['error'], st['sin'],
                         st['ajustado'], acc, st['sum_dif']])

    resp = Response(output.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = f'attachment; filename=conteos_{desde_str or "all"}_{hasta_str or "all"}.csv'
    return resp