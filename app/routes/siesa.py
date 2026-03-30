"""
Rutas Siesa — expone el gateway Connekta a la PWA.

GET  /api/siesa/pedidos           → cola de despacho desde Siesa (enriquecida con producto_id)
GET  /api/siesa/ordenes-compra    → cola de recepción desde Siesa (enriquecida con producto_id)
GET  /api/siesa/producto/<codigo> → lookup producto por código WMS, código Siesa o código de barras
POST /api/siesa/iniciar-despacho  → crea picking + packing para un pedido completo
POST /api/siesa/iniciar-recepcion → crea e inicia una recepción desde una OC de Siesa
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.connekta_gateway import connekta
from app.models.producto import Producto
from app.services.picking_service import PickingService
from app.services.packing_service import PackingService
from app.services.recepcion_service import RecepcionService

siesa_bp = Blueprint('siesa', __name__)


def _buscar_producto(codigo):
    """Busca un producto por código WMS o código Siesa."""
    return (Producto.query.filter_by(codigo=codigo).first() or
            Producto.query.filter_by(codigo_siesa=codigo).first())


# ──────────────────────────────────────────────
# GETs — exponen el gateway a la PWA
# ──────────────────────────────────────────────

@siesa_bp.route('/pedidos', methods=['GET'])
@jwt_required()
def pedidos_aprobados():
    """
    Cola de despacho: pedidos aprobados en Siesa con cantidad pendiente > 0.
    Enriquece cada ítem con producto_id interno y agrupa por número de pedido.
    ?sin_filtros=true  → barrido general sin filtrar por bodega/CO (solo en modo_ensayo).
    """
    sin_filtros = request.args.get('sin_filtros', '').lower() == 'true'
    # El barrido general solo se permite en modo ensayo para evitar exponer
    # pedidos de otras bodegas en producción accidentalmente.
    if sin_filtros and not connekta.modo_ensayo:
        sin_filtros = False

    resultado = connekta.get_pedidos_aprobados(sin_filtros=sin_filtros)

    if resultado.get('simulado'):
        return jsonify(resultado), 200

    pedidos = {}
    for item in resultado.get('items', []):
        prod = _buscar_producto(item['item_codigo'])
        item['producto_id'] = prod.id if prod else None
        item['producto_nombre_wms'] = prod.nombre if prod else None

        num = item['numero_pedido']
        if num not in pedidos:
            pedidos[num] = {
                'numero_pedido': num,
                'tipo_docto': item['tipo_docto'],
                'consec_docto': item['consec_docto'],
                'centro_op': item['centro_op'],
                'cliente': item.get('cliente', ''),
                'fecha_entrega': item.get('fecha_entrega', ''),
                'items': []
            }
        pedidos[num]['items'].append(item)

    lista = sorted(pedidos.values(), key=lambda x: x['fecha_entrega'] or '')
    return jsonify({'pedidos': lista, 'total': len(lista)}), 200


@siesa_bp.route('/ordenes-compra', methods=['GET'])
@jwt_required()
def ordenes_compra():
    """
    Cola de recepción: OCs aprobadas en Siesa con cantidad pendiente > 0.
    Enriquece con producto_id interno y agrupa por número de OC.
    """
    sin_filtros = request.args.get('sin_filtros', '').lower() == 'true'
    resultado = connekta.get_ordenes_compra_aprobadas(sin_filtros=sin_filtros)

    if resultado.get('simulado'):
        return jsonify(resultado), 200

    items_raw = resultado.get('detalle', {}).get('Table', [])
    ordenes = {}
    for row in items_raw:
        try:
            cant_pedida = float(row.get('f431_cant1_pedida', 0))
            cant_recibida = float(row.get('f431_cant1_remisionada', 0))
            cant_pendiente = cant_pedida - cant_recibida
            if cant_pendiente <= 0:
                continue

            tipo_docto = row.get('f430_id_tipo_docto', '').strip()
            consec_docto = str(row.get('f430_consec_docto', ''))
            numero_oc = f"{tipo_docto}{consec_docto}"
            item_codigo = row.get('f120_referencia', '').strip()

            prod = _buscar_producto(item_codigo)

            if numero_oc not in ordenes:
                ordenes[numero_oc] = {
                    'numero_oc': numero_oc,
                    'tipo_docto': tipo_docto,
                    'consec_docto': consec_docto,
                    'co': row.get('f430_id_co', '').strip(),
                    'proveedor': row.get('f200_razon_social', ''),
                    'items': []
                }
            ordenes[numero_oc]['items'].append({
                'item_codigo': item_codigo,
                'item_descripcion': row.get('f120_descripcion', ''),
                'producto_id': prod.id if prod else None,
                'producto_nombre_wms': prod.nombre if prod else None,
                'cantidad_ordenada': cant_pedida,
                'cantidad_pendiente': cant_pendiente,
            })
        except (ValueError, TypeError):
            continue

    lista = list(ordenes.values())
    return jsonify({'ordenes': lista, 'total': len(lista)}), 200


@siesa_bp.route('/producto/<codigo>', methods=['GET'])
@jwt_required()
def buscar_producto(codigo):
    """
    Lookup de producto por código WMS, código Siesa o código de barras (EAN).
    Usado por la PWA para traducir el beep del escáner a un producto_id.
    """
    prod = _buscar_producto(codigo)

    # Si no lo encontramos localmente y Connekta está activo, intentar por barras
    if not prod and not connekta.modo_simulacion:
        try:
            resp = connekta.get_item_por_barras(codigo)
            tabla = resp.get('detalle', {}).get('Table', [])
            if tabla:
                codigo_siesa = tabla[0].get('f120_referencia', '').strip()
                prod = Producto.query.filter_by(codigo_siesa=codigo_siesa).first()
        except Exception:
            pass

    if not prod:
        return jsonify({'error': f"Producto '{codigo}' no encontrado en WMS"}), 404

    return jsonify({
        'producto_id': prod.id,
        'codigo': prod.codigo,
        'nombre': prod.nombre,
        'codigo_siesa': prod.codigo_siesa,
        'clasificacion_abc': prod.clasificacion_abc
    }), 200


# ──────────────────────────────────────────────
# POSTs — arrancan los flujos operativos
# ──────────────────────────────────────────────

@siesa_bp.route('/iniciar-despacho', methods=['POST'])
@jwt_required()
def iniciar_despacho():
    """
    El admin selecciona un pedido de Siesa → este endpoint crea:
      1. Tareas de picking (PENDIENTE) → entran a la cola del operario automáticamente.
      2. Tarea de packing (PENDIENTE) → espera a que el picking esté completo.
    Los campos tipo_docto y consec_docto se guardan en el packing
    para que trigger_despacho los inyecte en Connekta al confirmar.
    """
    data = request.get_json()
    for campo in ['numero_pedido', 'tipo_docto', 'consec_docto', 'almacen_id', 'items']:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    almacen_id = data['almacen_id']
    numero_pedido = data['numero_pedido']
    tipo_docto = data['tipo_docto']
    consec_docto = data['consec_docto']
    items = [i for i in data['items'] if i.get('producto_id')]

    if not items:
        return jsonify({'error': 'Ningún producto está registrado en el WMS — agrégalos primero'}), 400

    tareas_picking_ids = []
    errores = []

    for item in items:
        try:
            tareas = PickingService.crear_tareas(
                producto_id=item['producto_id'],
                cantidad=int(item['cantidad_pendiente']),
                almacen_id=almacen_id,
                referencia_documento=numero_pedido,
                tipo_documento='PEDIDO_SIESA',
                prioridad=2
            )
            tareas_picking_ids.extend([t.id for t in tareas])
        except ValueError as e:
            errores.append(str(e))

    if not tareas_picking_ids:
        return jsonify({'error': 'Stock insuficiente para todos los ítems', 'detalle': errores}), 400

    # Crear packing con las llaves del documento Siesa — usar crear_manual porque
    # el picking aún está PENDIENTE (los operarios lo completarán después).
    items_packing = [{'producto_id': i['producto_id'], 'cantidad': int(i['cantidad_pendiente'])} for i in items]

    try:
        packing = PackingService.crear_manual(
            numero_pedido_siesa=numero_pedido,
            almacen_id=almacen_id,
            items=items_packing,
            tipo_docto_pedido_siesa=tipo_docto,
            consec_docto_pedido_siesa=consec_docto
        )
    except ValueError as e:
        return jsonify({
            'advertencia': str(e),
            'picking_creado': True,
            'tareas_picking': tareas_picking_ids,
            'errores': errores
        }), 207

    return jsonify({
        'mensaje': f'{len(tareas_picking_ids)} tarea(s) de picking en cola — packing listo para empacador',
        'numero_pedido': numero_pedido,
        'tareas_picking': tareas_picking_ids,
        'packing_id': packing.id,
        'packing_codigo': packing.codigo,
        'errores': errores
    }), 201


@siesa_bp.route('/iniciar-recepcion', methods=['POST'])
@jwt_required()
def iniciar_recepcion():
    """
    El recepcionista selecciona una OC de Siesa → este endpoint crea la RecepcionMercancia
    con los tres campos del documento origen (co, tipo_docto, consec_docto) y la inicia
    de inmediato. La PWA pasa directamente a la pantalla de escaneo ciego.
    """
    recepcionista_id = int(get_jwt_identity())
    data = request.get_json()

    for campo in ['numero_oc', 'tipo_docto', 'consec_docto', 'almacen_id', 'items']:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    items_validos = [i for i in data['items'] if i.get('producto_id')]
    if not items_validos:
        return jsonify({'error': 'Ningún producto de la OC está registrado en el WMS'}), 400

    items_recepcion = [{
        'producto_id': i['producto_id'],
        'cantidad_ordenada': int(i['cantidad_ordenada']),
        'tolerancia_exceso_pct': 0.0
    } for i in items_validos]

    try:
        recepcion = RecepcionService.crear_recepcion(
            numero_oc_siesa=data['numero_oc'],
            almacen_id=data['almacen_id'],
            proveedor_codigo=data.get('proveedor_codigo', ''),
            proveedor_nombre=data.get('proveedor', ''),
            items=items_recepcion,
            co_oc_siesa=data.get('co', ''),
            tipo_docto_oc_siesa=data['tipo_docto'],
            consec_docto_oc_siesa=data['consec_docto']
        )
        recepcion = RecepcionService.iniciar(recepcion.id, recepcionista_id)
        return jsonify({
            'mensaje': 'Recepción iniciada — listo para escanear',
            'recepcion': recepcion.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
