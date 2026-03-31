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

# ──────────────────────────────────────────────
# Sync manual (admin)
# ──────────────────────────────────────────────

@siesa_bp.route('/sync-productos', methods=['POST'])
@jwt_required()
def sync_productos():
    """Inicia el sync en background y retorna inmediatamente (evita timeout gunicorn)."""
    from flask import current_app
    from app.services.siesa_sync_service import iniciar_sync_background
    resultado = iniciar_sync_background(current_app._get_current_object(), forzar=True)
    return jsonify(resultado), 202


@siesa_bp.route('/sync-estado', methods=['GET'])
@jwt_required()
def sync_estado():
    """Retorna el estado del último sync (en_curso, resultado, error)."""
    from app.services.siesa_sync_service import estado_sync
    return jsonify(estado_sync()), 200


@siesa_bp.route('/cargar-inventario', methods=['POST'])
@jwt_required()
def cargar_inventario():
    """Inicia la carga inicial de stock desde Siesa en background."""
    from flask import current_app
    from app.services.inventario_siesa_service import iniciar_carga_inventario
    resultado = iniciar_carga_inventario(current_app._get_current_object())
    return jsonify(resultado), 202


@siesa_bp.route('/carga-inventario-estado', methods=['GET'])
@jwt_required()
def carga_inventario_estado():
    """Estado de la carga de inventario en curso."""
    from app.services.inventario_siesa_service import estado_carga_inventario
    return jsonify(estado_carga_inventario()), 200


@siesa_bp.route('/reconciliacion', methods=['POST'])
@jwt_required()
def reconciliacion_iniciar():
    """Inicia la reconciliación en background (puede tardar 2+ min)."""
    from flask import current_app
    from app.services.inventario_siesa_service import iniciar_reconciliacion
    resultado = iniciar_reconciliacion(current_app._get_current_object())
    return jsonify(resultado), 202


@siesa_bp.route('/reconciliacion-estado', methods=['GET'])
@jwt_required()
def reconciliacion_estado():
    """Retorna el estado de la reconciliación en curso o el último resultado."""
    from app.services.inventario_siesa_service import estado_reconciliacion
    return jsonify(estado_reconciliacion()), 200


@siesa_bp.route('/debug-pedidos-raw', methods=['GET'])
@jwt_required()
def debug_pedidos_raw():
    """
    Debug: devuelve hasta 50 filas de API_v2_Ventas_Pedidos sin filtrar por bodega/CO.
    ?sin_estado=true → incluye pedidos en cualquier estado (no solo aprobados).
    Muestra resumen de bodegas y COs encontrados para diagnosticar filtros.
    """
    sin_estado = request.args.get('sin_estado', '').lower() == 'true'
    params = {'paginacion': 'numPag=1|tamPag=50'}
    if not sin_estado:
        params['parametros'] = 'f430_ind_estado=1'

    resultado = connekta._get(connekta.api_pedidos, params)
    tabla = resultado.get('detalle', {}).get('Table', [])

    # Resumen de bodegas y COs para diagnóstico rápido
    bodegas = {}
    cos = {}
    estados = {}
    for r in tabla:
        b = r.get('f150_id', '?')
        c = r.get('f430_id_co', '?')
        e = str(r.get('f430_ind_estado', '?'))
        bodegas[b] = bodegas.get(b, 0) + 1
        cos[c] = cos.get(c, 0) + 1
        estados[e] = estados.get(e, 0) + 1

    return jsonify({
        'total_filas': len(tabla),
        'bodega_configurada': connekta.bodega,
        'co_configurado': connekta.centro_op,
        'resumen_bodegas': bodegas,
        'resumen_cos': cos,
        'resumen_estados': estados,
        'filas_nb1': [
            {k: r[k] for k in ['f430_consec_docto', 'f430_id_co', 'f150_id',
                                'f430_ind_estado', 'f120_referencia', 'f431_cant1_pedida',
                                'f431_cant1_remisionada', 'f200_razon_social_pedido_fact']
             if k in r}
            for r in tabla if r.get('f150_id') == connekta.bodega
        ],
        'muestra_todas': [
            {k: r[k] for k in ['f430_consec_docto', 'f430_id_co', 'f150_id',
                                'f430_ind_estado', 'f120_referencia']
             if k in r}
            for r in tabla[:20]
        ]
    }), 200


@siesa_bp.route('/debug-inventario-raw', methods=['GET'])
@jwt_required()
def debug_inventario_raw():
    """
    Descubrimiento: devuelve las primeras filas de API_v2_Inventarios_InvFecha
    para bodega NB1 sin filtros adicionales.
    Usar solo para identificar nombres reales de campos de existencias.
    """
    api_inv = 'API_v2_Inventarios_InvFecha'
    resultado = connekta._get(api_inv, {
        'paginacion': 'numPag=1|tamPag=3'
    })
    tabla = resultado.get('detalle', {}).get('Table', [])
    return jsonify({
        'total_filas_pagina': len(tabla),
        'campos_disponibles': list(tabla[0].keys()) if tabla else [],
        'muestra': tabla[:3]
    }), 200


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


@siesa_bp.route('/debug-oc-raw', methods=['GET'])
@jwt_required()
def debug_oc_raw():
    """Debug: devuelve el JSON crudo de Siesa sin procesar."""
    sin_filtros = request.args.get('sin_filtros', '').lower() == 'true'
    parametros_custom = request.args.get('parametros')
    if parametros_custom:
        resultado = connekta._get(connekta.api_ordenes, {
            'paginacion': 'numPag=1|tamPag=10',
            'parametros': parametros_custom
        })
    else:
        resultado = connekta.get_ordenes_compra_aprobadas(sin_filtros=sin_filtros)
    return jsonify(resultado), 200


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
    # Filtrar por CO y bodega en Python (la API no acepta esos campos como filtro)
    items_raw = [
        r for r in items_raw
        if r.get('f420_id_co', '').strip() == connekta.centro_op
        and r.get('f150_id', '').strip() == connekta.bodega
    ]
    ordenes = {}
    for row in items_raw:
        try:
            cant_pedida = float(row.get('f421_cant_pedida', 0))
            cant_recibida = float(row.get('f421_cant_entrada', 0))
            cant_pendiente = cant_pedida - cant_recibida
            if cant_pendiente <= 0:
                continue

            tipo_docto = row.get('f420_id_tipo_docto', '').strip()
            consec_docto = str(row.get('f420_consec_docto', ''))
            numero_oc = f"{tipo_docto}{consec_docto}"
            item_codigo = row.get('f120_referencia', '').strip()

            prod = _buscar_producto(item_codigo)

            if numero_oc not in ordenes:
                ordenes[numero_oc] = {
                    'numero_oc': numero_oc,
                    'tipo_docto': tipo_docto,
                    'consec_docto': consec_docto,
                    'co': row.get('f420_id_co', '').strip(),
                    'proveedor': row.get('f200_razon_social_prov', ''),
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
