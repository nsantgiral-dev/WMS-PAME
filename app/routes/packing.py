import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.packing import TareaPacking, ItemPacking, EstadoPacking
from app.models.picking import TareaPicking, EstadoPicking
from app.services.packing_service import PackingService
from app.services.connekta_gateway import connekta
from app.routes._auth_helpers import Roles, _puede_empacar

packing_bp = Blueprint('packing', __name__)
logger = logging.getLogger(__name__)


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
        TareaPicking.estado != EstadoPicking.CANCELADO
    ).all()

    # Agrupar por pedido
    por_pedido = {}
    for p in pickings:
        num = p.referencia_documento
        if num not in por_pedido:
            por_pedido[num] = {'total': 0, 'completados': 0}
        por_pedido[num]['total'] += 1
        if p.estado == EstadoPicking.COMPLETADO or (
            p.estado == EstadoPicking.BLOQUEADO and (p.cantidad_recogida or 0) > 0
        ):
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
    if not u or not _puede_empacar(u):
        return jsonify({'error': 'Sin permiso para listar tareas de packing'}), 403
    estado = request.args.get('estado')
    almacen_id = request.args.get('almacen_id', type=int)
    page = request.args.get('page', 1, type=int)

    from sqlalchemy.orm import selectinload as _sl, joinedload as _jl
    query = (TareaPacking.query
             .options(
                 _sl(TareaPacking.items).selectinload(ItemPacking.producto),  # evita N+1 items.producto
                 _sl(TareaPacking.bultos),
                 _jl(TareaPacking.empacador),
             )
             .order_by(TareaPacking.fecha_creacion.desc()))

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
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or not _puede_empacar(u):
        return jsonify({'error': 'Sin permiso para ver tareas de packing'}), 403
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
    if not usuario or not _puede_empacar(usuario):
        return jsonify({'error': 'No autorizado — se requiere rol empacador, supervisor o admin'}), 403
    try:
        from app.services.packing_picking_sync_service import PackingPickingSyncService
        try:
            PackingPickingSyncService.sincronizar(id)
        except Exception:
            pass  # Sin picking asociado → se trabaja con cantidades de Siesa
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
    if not usuario or not _puede_empacar(usuario):
        return jsonify({'error': 'No autorizado — se requiere rol empacador, supervisor o admin'}), 403
    # Ownership: empacador solo opera su propia tarea; supervisores/admin pueden cualquiera
    if usuario.rol not in (Roles.ADMIN, Roles.SUPERVISOR, Roles.JEFE_ALMACEN):
        tarea_chk = TareaPacking.query.get(id)
        if tarea_chk and tarea_chk.empacador_id and tarea_chk.empacador_id != uid:
            return jsonify({'error': 'Esta tarea pertenece a otro empacador'}), 403
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
    if not u or not _puede_empacar(u):
        return jsonify({'error': 'Sin permiso para confirmar packing'}), 403
    # Ownership: empacador solo confirma su propia tarea; supervisores/admin pueden cualquiera
    if u.rol not in (Roles.ADMIN, Roles.SUPERVISOR, Roles.JEFE_ALMACEN):
        tarea_chk = TareaPacking.query.get(id)
        if tarea_chk and tarea_chk.empacador_id and tarea_chk.empacador_id != uid:
            return jsonify({'error': 'Esta tarea pertenece a otro empacador'}), 403
    data = request.get_json() or {}
    try:
        PackingService.confirmar_packing(
            tarea_id=id,
            observaciones=data.get('observaciones'),
            forzar=data.get('forzar', False)
        )
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
        logger.exception(f'[PACKING] Error inesperado en confirmar_packing id={id}')
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/<int:id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_packing(id):
    """
    Paso 2: declara bultos físicos, genera códigos de barras y dispara Siesa.
    Body: {"bultos": [{"tipo": "Caja", "cantidad": 2}, {"tipo": "Bolsa", "cantidad": 1}]}
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(uid)
    if not usuario or not _puede_empacar(usuario):
        return jsonify({'error': 'No autorizado'}), 403
    # [A1] Ownership check: empacador solo puede cerrar su propia tarea.
    # Supervisión puede cerrar cualquier tarea.
    _tarea_chk = TareaPacking.query.get(id)
    if _tarea_chk and _tarea_chk.empacador_id and _tarea_chk.empacador_id != uid:
        if usuario.rol not in Roles.SUPERVISION:
            return jsonify({'error': 'No puedes cerrar una tarea asignada a otro empacador'}), 403
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
        from app.models.bulto import Bulto as _Bulto
        from sqlalchemy.orm import selectinload as _sl_b
        tarea = TareaPacking.query.get(id)
        # Re-query bultos con eager load — expire_on_commit invalida los objetos retornados
        # por cerrar_packing; b.to_dict() accede b.tarea (lazy) sin esto → N+1
        bultos_resp = (_Bulto.query
                       .options(_sl_b(_Bulto.tarea))
                       .filter_by(tarea_id=id).all())
        return jsonify({
            'ok': True,
            'mensaje': (
                f'{len(bultos)} pieza(s) registradas — Siesa confirmó la remisión'
                if tarea.siesa_triggered else
                f'{len(bultos)} pieza(s) registradas — Siesa procesando (se confirma en segundos)'
            ),
            'siesa_triggered': tarea.siesa_triggered,
            'numero_pedido': tarea.numero_pedido_siesa,
            'cliente': tarea.cliente or '',
            'municipio': tarea.municipio or '',
            'bultos': [b.to_dict() for b in bultos_resp]
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(f'[PACKING] Error inesperado en cerrar_packing id={id}')
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/<int:id>/cancelar', methods=['PUT'])
@jwt_required()
def cancelar_tarea(id):
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(uid)
    if not usuario or usuario.rol not in Roles.LEAD:
        return jsonify({'error': 'No autorizado — se requiere rol admin o supervisor'}), 403
    data = request.get_json() or {}
    try:
        tarea = PackingService.cancelar(id, motivo=data.get('motivo'))
        return jsonify({'mensaje': 'Tarea cancelada', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/reconciliar', methods=['POST'])
@jwt_required()
def reconciliar_manual(id):
    """
    Fuerza reconciliación inmediata de una tarea: verifica en Siesa si ya existe
    factura o estado==4 y, si aplica, la marca DESPACHADO + siesa_triggered=True.
    Solo admin. Úsalo cuando el sweep aún no ha corrido y necesitas resolver ya.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede reconciliar tareas Siesa'}), 403
    tarea = TareaPacking.query.get_or_404(id)
    if tarea.siesa_triggered:
        return jsonify({'ok': True, 'mensaje': 'Ya estaba reconciliada (siesa_triggered=True)', 'tarea': tarea.to_dict()}), 200
    from app.services.reconciliacion_service import ReconciliacionService
    resultado = ReconciliacionService.reconciliar_despacho(
        tarea,
        tipo_docto=tarea.tipo_docto_pedido_siesa,
        consec_docto=tarea.consec_docto_pedido_siesa,
    )
    tarea = TareaPacking.query.get(id)
    if resultado.get('reconciliado'):
        return jsonify({'ok': True, 'mensaje': 'Reconciliada — tarea marcada DESPACHADO', 'tarea': tarea.to_dict()}), 200
    return jsonify({'ok': False, 'mensaje': 'Siesa aún no tiene la factura o no se pudo consultar', 'tarea': tarea.to_dict()}), 200


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
        logger.exception(f'[PACKING] Error inesperado en forzar_retry_siesa id={id}')
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/<int:id>/etiqueta-canasto', methods=['GET'])
@jwt_required()
def etiqueta_canasto(id):
    """HTML imprimible para identificar el canasto en el área de picking."""
    from app.models.usuario import Usuario
    from flask import Response
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or not _puede_empacar(u):
        return jsonify({'error': 'Sin permiso'}), 403
    tarea = TareaPacking.query.get_or_404(id)
    if tarea.estado == EstadoPacking.PENDIENTE:
        return jsonify({'error': 'Tarea aún no iniciada'}), 409
    try:
        copias = max(1, min(10, int(request.args.get('copias', 1))))
    except (ValueError, TypeError):
        copias = 1
    html = _html_etiqueta_canasto(tarea, copias)
    return Response(html, mimetype='text/html; charset=utf-8'), 200


def _html_etiqueta_canasto(tarea: TareaPacking, copias: int = 1) -> str:
    from datetime import datetime
    empacador_nombre = tarea.empacador.nombre if tarea.empacador else '—'
    ahora = datetime.now().strftime('%d/%m/%Y %H:%M')
    total_unidades = sum(i.cantidad_real or i.cantidad_esperada for i in tarea.items)
    filas_items = ''
    for item in tarea.items:
        cant = item.cantidad_real or item.cantidad_esperada
        desc = (item.producto.descripcion[:40] if item.producto and item.producto.descripcion else '') or ''
        filas_items += f'<tr><td>{item.referencia}</td><td>{desc}</td><td style="text-align:right;font-weight:700">{cant:,}</td></tr>'

    bloque = f"""<div class="etiqueta">
  <div class="header">
    <div class="pedido">&#128230; {tarea.numero_pedido_siesa}</div>
    <div class="cliente">{tarea.cliente or '—'}</div>
    <div class="municipio">{tarea.municipio or ''}</div>
  </div>
  <div class="meta">
    <span><span class="lbl">Empacador</span><span class="val">{empacador_nombre}</span></span>
    <span><span class="lbl">Referencias</span><span class="val">{tarea.total_items()}</span></span>
    <span><span class="lbl">Total uds</span><span class="val">{total_unidades:,}</span></span>
    <span><span class="lbl">Generado</span><span class="val">{ahora}</span></span>
  </div>
  <table>
    <thead><tr><th>Referencia</th><th>Descripción</th><th style="text-align:right">Cant.</th></tr></thead>
    <tbody>{filas_items}</tbody>
  </table>
  <div class="totales">{tarea.total_items()} ref · {total_unidades:,} uds</div>
  <div class="footer">WMS-PAME · Tarea #{tarea.id}</div>
</div>"""

    bloques = bloque * copias

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Etiqueta Canasto — {tarea.numero_pedido_siesa}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: Arial, sans-serif; font-size: 12px; }}
  .etiqueta {{ padding: 12px; page-break-after: always; }}
  .etiqueta:last-child {{ page-break-after: avoid; }}
  .header {{ border: 3px solid #000; padding: 10px 14px; margin-bottom: 10px; }}
  .pedido {{ font-size: 36px; font-weight: 900; letter-spacing: 2px; color: #000; }}
  .cliente {{ font-size: 16px; font-weight: 700; margin-top: 4px; }}
  .municipio {{ font-size: 13px; color: #444; }}
  .meta {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 8px 0; font-size: 12px; }}
  .meta span {{ display: flex; flex-direction: column; }}
  .meta .lbl {{ font-size: 10px; color: #666; text-transform: uppercase; }}
  .meta .val {{ font-weight: 700; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 11px; }}
  th {{ background: #000; color: #fff; padding: 4px 6px; text-align: left; }}
  td {{ padding: 3px 6px; border-bottom: 1px solid #ddd; }}
  .totales {{ margin-top: 8px; text-align: right; font-size: 13px; font-weight: 700; border-top: 2px solid #000; padding-top: 6px; }}
  .footer {{ margin-top: 10px; font-size: 10px; color: #666; border-top: 1px solid #ccc; padding-top: 6px; }}
</style>
</head>
<body>
{bloques}
<script>window.onload = function(){{ window.print(); }}</script>
</body>
</html>"""


@packing_bp.route('/<int:id>/remision', methods=['GET'])
@jwt_required()
def imprimir_remision(id):
    """Devuelve el HTML imprimible de la remisión. Solo disponible en estado DESPACHADO."""
    from app.models.usuario import Usuario
    from flask import Response
    from app.services.remision_service import RemisionService
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or not _puede_empacar(u):
        return jsonify({'error': 'Sin permiso para ver la remisión'}), 403
    tarea = TareaPacking.query.get_or_404(id)
    disponible, motivo = RemisionService.puede_generar(tarea)
    if not disponible:
        return jsonify({'error': motivo}), 409
    html = RemisionService.generar_html(tarea)
    return Response(html, mimetype='text/html; charset=utf-8'), 200


@packing_bp.route('/<int:id>/factura', methods=['GET'])
@jwt_required()
def imprimir_factura(id):
    """Devuelve el HTML de factura electrónica con datos reales de Siesa."""
    from app.models.usuario import Usuario
    from flask import Response
    from app.services.factura_fe_service import FacturaFEService
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or not _puede_empacar(u):
        return jsonify({'error': 'Sin permiso para ver la factura'}), 403
    tarea = TareaPacking.query.get_or_404(id)
    disponible, motivo = FacturaFEService.puede_generar(tarea)
    if not disponible:
        return jsonify({'error': motivo}), 409
    lineas_fe = FacturaFEService.obtener_lineas(tarea)
    html = FacturaFEService.generar_html(tarea, lineas_fe)
    return Response(html, mimetype='text/html; charset=utf-8'), 200


@packing_bp.route('/connekta/estado', methods=['GET'])
@jwt_required()
def estado_connekta():
    """Verifica el estado de la integración con Siesa/Connekta."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in (Roles.ADMIN, Roles.SUPERVISOR, Roles.JEFE_ALMACEN):
        return jsonify({'error': 'Sin permiso para ver estado de Connekta'}), 403
    return jsonify(connekta.estado()), 200


# ─────────────────────────────────────────────────────────────────────────────
# SINCRONIZACIÓN PICKING → PACKING
# Ajusta cantidad_esperada de los ítems del packing con lo que el picker
# realmente recogió. Se llama antes de que el empacador empiece a contar.
# ─────────────────────────────────────────────────────────────────────────────
@packing_bp.route('/<int:id>/sincronizar-picking', methods=['POST'])
@jwt_required()
def sincronizar_picking(id):
    """
    Recalcula cantidad_esperada de cada ítem del packing usando la suma real
    de cantidad_recogida de las tareas de picking del mismo pedido.

    Solo actúa si hay picking COMPLETADO o BLOQUEADO con unidades recogidas.
    Idempotente: se puede llamar varias veces sin efecto secundario.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede sincronizar picking con packing'}), 403
    from app.services.packing_picking_sync_service import PackingPickingSyncService
    try:
        resultado = PackingPickingSyncService.sincronizar(id)
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400