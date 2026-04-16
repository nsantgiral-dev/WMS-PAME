"""
Rutas Siesa — expone el gateway Connekta a la PWA.

GET  /api/siesa/pedidos               → cola de despacho (Read Model) + estado picking WMS
GET  /api/siesa/ordenes-compra        → cola de recepción desde Siesa
GET  /api/siesa/producto/<codigo>     → lookup producto
POST /api/siesa/iniciar-despacho      → crea picking + packing para un pedido
POST /api/siesa/confirmar-despacho    → dispara RemisionPedido (142945) a Siesa
POST /api/siesa/iniciar-recepcion     → crea e inicia una recepción desde una OC
"""
import json
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.connekta_gateway import connekta
from app.models.producto import Producto
from app.services.picking_service import PickingService
from app.services.packing_service import PackingService
from app.services.recepcion_service import RecepcionService

siesa_bp = Blueprint('siesa', __name__)


def _solo_admin():
    from flask_jwt_extended import get_jwt_identity
    from app.models.usuario import Usuario
    uid = get_jwt_identity()
    u = Usuario.query.get(int(uid))
    return u if u and u.rol == 'admin' else None


# ──────────────────────────────────────────────
# Sync manual (admin)
# ──────────────────────────────────────────────

@siesa_bp.route('/sync-pedidos', methods=['POST'])
@jwt_required()
def sync_pedidos():
    """Dispara sync de pedidos Siesa → DB local en background. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede disparar sync'}), 403
    from flask import current_app
    from app.services.pedidos_sync_service import iniciar_sync_background
    resultado = iniciar_sync_background(current_app._get_current_object(), forzar=True)
    return jsonify(resultado), 202


@siesa_bp.route('/sync-pedidos-estado', methods=['GET'])
@jwt_required()
def sync_pedidos_estado():
    """Estado del último sync de pedidos. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ver estado de sync'}), 403
    from app.services.pedidos_sync_service import estado_sync
    return jsonify(estado_sync()), 200


@siesa_bp.route('/sync-productos', methods=['POST'])
@jwt_required()
def sync_productos():
    """Inicia el sync en background y retorna inmediatamente. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede disparar sync'}), 403
    from flask import current_app
    from app.services.siesa_sync_service import iniciar_sync_background
    resultado = iniciar_sync_background(current_app._get_current_object(), forzar=True)
    return jsonify(resultado), 202


@siesa_bp.route('/sync-estado', methods=['GET'])
@jwt_required()
def sync_estado():
    """Retorna el estado del último sync. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ver estado de sync'}), 403
    from app.services.siesa_sync_service import estado_sync
    return jsonify(estado_sync()), 200


@siesa_bp.route('/cargar-inventario', methods=['POST'])
@jwt_required()
def cargar_inventario():
    """Inicia la carga inicial de stock desde Siesa en background. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede cargar inventario'}), 403
    from flask import current_app
    from app.services.inventario_siesa_service import iniciar_carga_inventario
    resultado = iniciar_carga_inventario(current_app._get_current_object())
    return jsonify(resultado), 202


@siesa_bp.route('/carga-inventario-estado', methods=['GET'])
@jwt_required()
def carga_inventario_estado():
    """Estado de la carga de inventario en curso. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ver estado de carga'}), 403
    from app.services.inventario_siesa_service import estado_carga_inventario
    return jsonify(estado_carga_inventario()), 200


@siesa_bp.route('/setup-inicial', methods=['POST'])
@jwt_required()
def setup_inicial():
    """Catálogo sync + carga de stock en una sola operación. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ejecutar setup inicial'}), 403
    from flask import current_app
    from app.services.inventario_siesa_service import iniciar_setup_inicial
    resultado = iniciar_setup_inicial(current_app._get_current_object())
    return jsonify(resultado), 202


@siesa_bp.route('/setup-inicial-estado', methods=['GET'])
@jwt_required()
def setup_inicial_estado():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    from app.services.inventario_siesa_service import estado_setup_inicial
    return jsonify(estado_setup_inicial()), 200


@siesa_bp.route('/reconciliacion', methods=['POST'])
@jwt_required()
def reconciliacion_iniciar():
    """Inicia la reconciliación en background (puede tardar 2+ min). Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ejecutar reconciliación'}), 403
    from flask import current_app
    from app.services.inventario_siesa_service import iniciar_reconciliacion
    resultado = iniciar_reconciliacion(current_app._get_current_object())
    return jsonify(resultado), 202


@siesa_bp.route('/reconciliacion-estado', methods=['GET'])
@jwt_required()
def reconciliacion_estado():
    """Retorna el estado de la reconciliación en curso o el último resultado. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ver estado de reconciliación'}), 403
    from app.services.inventario_siesa_service import estado_reconciliacion
    return jsonify(estado_reconciliacion()), 200


@siesa_bp.route('/monitor', methods=['GET'])
@jwt_required()
def monitor_sincronizacion():
    """
    Semáforo de integración Siesa — verde/rojo por módulo.
    Permite al admin ver de un vistazo qué módulos están al día.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ver el monitor de sync'}), 403

    try:
        from app.services.siesa_sync_service import estado_sync as estado_productos
        productos = estado_productos()
    except Exception:
        productos = {}

    try:
        from app.services.pedidos_sync_service import estado_sync as estado_pedidos
        pedidos = estado_pedidos()
    except Exception:
        pedidos = {}

    try:
        from app.services.inventario_siesa_service import estado_reconciliacion, estado_carga_inventario
        reconciliacion = estado_reconciliacion()
        inventario = estado_carga_inventario()
    except Exception:
        reconciliacion = {}
        inventario = {}

    def semaforo(estado_dict):
        """Verde si terminó sin error, amarillo si en curso, rojo si hay error."""
        if estado_dict.get('en_curso'):
            return 'AMARILLO'
        if estado_dict.get('error'):
            return 'ROJO'
        if estado_dict.get('resultado') or estado_dict.get('ultima_sync'):
            return 'VERDE'
        return 'GRIS'

    return jsonify({
        'modulos': {
            'productos':      {'estado': semaforo(productos),      'detalle': productos},
            'pedidos':        {'estado': semaforo(pedidos),        'detalle': pedidos},
            'inventario':     {'estado': semaforo(inventario),     'detalle': inventario},
            'reconciliacion': {'estado': semaforo(reconciliacion), 'detalle': reconciliacion},
        },
        'connekta': connekta.estado(),
    }), 200


@siesa_bp.route('/debug-pedidos-raw', methods=['GET'])
@jwt_required()
def debug_pedidos_raw():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403
    """
    Debug: devuelve hasta 50 filas de API_v2_Ventas_Pedidos sin filtrar por bodega/CO.
    ?sin_estado=true → incluye pedidos en cualquier estado (no solo aprobados).
    Muestra resumen de bodegas y COs encontrados para diagnosticar filtros.
    """
    sin_estado = request.args.get('sin_estado', '').lower() == 'true'
    consec = request.args.get('consec_docto', '').strip()
    co = request.args.get('co', '').strip()
    params_raw = request.args.get('params_raw', '').strip()
    echo = request.args.get('echo', '').lower() == 'true'
    num_pag = request.args.get('num_pag', '1').strip()
    tam_pag = request.args.get('tam_pag', '50').strip()
    # ?api=API_v2_Ventas_Pedidos_Compromisos → explorar APIs alternativas
    api_nombre = request.args.get('api', connekta.api_pedidos).strip()

    params = {'paginacion': f'numPag={num_pag}|tamPag={tam_pag}'}

    if params_raw:
        params['parametros'] = params_raw
    elif consec:
        filtros = [f'f430_consec_docto={consec}']
        if co:
            filtros.append(f'f430_id_co="{co}"')
        if not sin_estado:
            filtros.append('f430_ind_estado=1')
        params['parametros'] = ' AND '.join(filtros)
    elif not sin_estado:
        params['parametros'] = 'f430_ind_estado=2'  # 2=Aprobado (listo para despacho)

    # Modo echo: muestra la URL exacta que se mandaría a Connekta
    if echo:
        import requests as req_lib
        req = req_lib.Request(
            'GET',
            connekta.url_get,
            headers=connekta.headers,
            params={
                'idCompania': connekta.id_compania,
                'descripcion': connekta.api_pedidos,
                **params
            }
        )
        prepared = req.prepare()
        return jsonify({
            'url_exacta_connekta': prepared.url,
            'parametros_enviado': params.get('parametros', '(ninguno)'),
            'modo_simulacion': connekta.modo_simulacion
        }), 200

    resultado = connekta._get(api_nombre, params)
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

    primera_fila_completa = tabla[0] if tabla else {}
    campos_ciudad = {k: v for k, v in primera_fila_completa.items()
                     if any(x in k.lower() for x in ['ciudad', 'munici', 'depart', 'dir', 'entrega', 'ciudad'])}

    return jsonify({
        'total_filas': len(tabla),
        'bodega_configurada': connekta.bodega,
        'co_configurado': connekta.centro_op,
        'resumen_bodegas': bodegas,
        'resumen_cos': cos,
        'resumen_estados': estados,
        'todos_los_campos_primera_fila': list(primera_fila_completa.keys()),
        'campos_posible_ciudad': campos_ciudad,
        'primera_fila_completa': primera_fila_completa,
        'filas_nb1': [
            {k: r[k] for k in ['f430_consec_docto', 'f430_id_co', 'f150_id',
                                'f430_ind_estado', 'f120_referencia', 'f431_cant1_pedida',
                                'f431_cant1_remisionada', 'f200_razon_social_pedido_fact']
             if k in r}
            for r in tabla if r.get('f150_id') == connekta.bodega
        ],
    }), 200


@siesa_bp.route('/debug-clasificacion-raw', methods=['GET'])
@jwt_required()
def debug_clasificacion_raw():
    """
    Explora los campos que devuelve el conector dinámico 238920 (CLASIFICACION DE ITEMS).
    Devuelve las primeras 3 filas para mapear nombres de campos antes de activar
    el sync automático de ABC.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403
    resultado = connekta.get_clasificacion_items(pagina=1)
    tabla = resultado.get('detalle', {}).get('Table', [])
    return jsonify({
        'conector': connekta.api_clasificacion,
        'total_filas_pagina': len(tabla),
        'campos_disponibles': list(tabla[0].keys()) if tabla else [],
        'muestra': tabla[:3]
    }), 200


@siesa_bp.route('/debug-monitor-facturas', methods=['GET'])
@jwt_required()
def debug_monitor_facturas():
    """
    Explora el response crudo de papeleriamedellin_monitos_facturas_wms.
    Acepta ?fecha=AAAAMMDD (default: hoy).
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403
    fecha = request.args.get('fecha')
    try:
        resultado = connekta.get_monitor_facturas_raw(fecha=fecha, pagina=1)
        detalle = resultado.get('detalle', {})
        tabla = detalle.get('Datos', detalle.get('Table', []))
        return jsonify({
            'consulta': 'papeleriamedellin_monitos_facturas_wms',
            'fecha_filtro': fecha or 'hoy',
            'total_filas': len(tabla),
            'campos': list(tabla[0].keys()) if tabla else [],
            'muestra': tabla[:5],
            'raw': resultado
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@siesa_bp.route('/debug-inventario-raw', methods=['GET'])
@jwt_required()
def debug_inventario_raw():
    """
    Descubrimiento: devuelve las primeras filas de API_v2_Inventarios_InvFecha.
    Usar solo para identificar nombres reales de campos de existencias.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403
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
    """Busca un producto por código WMS, código Siesa o código de barras EAN."""
    return (Producto.query.filter_by(codigo=codigo).first() or
            Producto.query.filter_by(codigo_siesa=codigo).first() or
            Producto.query.filter_by(codigo_barras=codigo).first())


# ──────────────────────────────────────────────
# GETs — exponen el gateway a la PWA
# ──────────────────────────────────────────────

@siesa_bp.route('/pedidos', methods=['GET'])
@jwt_required()
def pedidos_aprobados():
    """
    Cola de despacho: Read Model local enriquecido con estado del picking WMS.
    Respuesta instantánea — nunca toca Connekta en tiempo real.

    Estado por pedido:
      picking_iniciado=False                         → mostrar botón "Despachar"
      picking_iniciado=True, completado=False        → picking en curso (progreso X/Y)
      picking_iniciado=True, completado=True,
        siesa_triggered=False                        → mostrar botón "Confirmar en Siesa"
      siesa_triggered=True                           → despachado ✓
    """
    from app.models.pedido_siesa import PedidoSiesa
    from app.models.picking import TareaPicking
    from app.models.packing import TareaPacking

    if connekta.modo_simulacion:
        resultado = connekta.get_pedidos_aprobados()
        return jsonify(resultado), 200

    rows = PedidoSiesa.query.order_by(PedidoSiesa.fecha_entrega.desc()).all()

    pedidos = {}
    for row in rows:
        num = row.numero_pedido
        if num not in pedidos:
            pedidos[num] = {
                'numero_pedido': num,
                'tipo_docto':    row.tipo_docto,
                'consec_docto':  row.consec_docto,
                'centro_op':     row.centro_op,
                'cliente':       row.cliente or '',
                'fecha_entrega': row.fecha_entrega or '',
                'items':         []
            }
        pedidos[num]['items'].append(row.to_dict())

    # Enriquecer con estado del picking/packing en WMS
    for num, pedido in pedidos.items():
        packing = TareaPacking.query.filter(
            TareaPacking.numero_pedido_siesa == num,
            TareaPacking.estado != 'CANCELADO'
        ).first()

        if not packing:
            pedido.update({'picking_iniciado': False, 'picking_completado': False,
                           'picking_progreso': '0/0', 'packing_id': None, 'siesa_triggered': False})
        else:
            pickings = TareaPicking.query.filter(
                TareaPicking.referencia_documento == num,
                TareaPicking.estado != 'CANCELADO'
            ).all()
            total = len(pickings)
            completados = sum(1 for p in pickings if p.estado == 'COMPLETADO')
            pedido.update({
                'picking_iniciado':   True,
                'picking_completado': total > 0 and completados == total,
                'picking_progreso':   f'{completados}/{total}',
                'packing_id':         packing.id,
                'packing_estado':     packing.estado,
                'siesa_triggered':    packing.siesa_triggered,
                'siesa_triggered_at': packing.siesa_triggered_at.isoformat() if packing.siesa_triggered_at else None,
            })

    lista = sorted(pedidos.values(), key=lambda x: x['fecha_entrega'] or '', reverse=True)
    return jsonify({'pedidos': lista, 'total': len(lista)}), 200


@siesa_bp.route('/debug-barras-raw', methods=['GET'])
@jwt_required()
def debug_barras_raw():
    """
    Debug: prueba API_v2_ItemsBarras con un código de barras específico.
    GET /api/siesa/debug-barras-raw?codigo=49218787
    Sin código: muestra los primeros 5 registros del conector completo.
    Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403
    codigo = request.args.get('codigo', '').strip()
    if codigo:
        resultado = connekta._get(connekta.api_barras, {
            'paginacion': 'numPag=1|tamPag=5',
            'parametros': f"f131_id = ''{codigo}''"
        })
    else:
        resultado = connekta._get(connekta.api_barras, {
            'paginacion': 'numPag=1|tamPag=5'
        })
    return jsonify(resultado), 200


@siesa_bp.route('/sync-barcodes', methods=['POST'])
@jwt_required()
def sync_barcodes():
    """Dispara sync manual de barcodes EAN Siesa → DB local. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    from flask import current_app
    from app.services.siesa_barcode_sync_service import ejecutar_sync, get_estado
    estado = get_estado()
    if estado['en_curso']:
        return jsonify({'mensaje': 'Sync ya en curso', 'estado': estado}), 200
    ejecutar_sync(current_app._get_current_object())
    return jsonify({'mensaje': 'Sync iniciado en background', 'estado': get_estado()}), 200


@siesa_bp.route('/sync-barcodes-estado', methods=['GET'])
@jwt_required()
def sync_barcodes_estado():
    """Estado del último sync de barcodes. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    from app.services.siesa_barcode_sync_service import get_estado
    return jsonify(get_estado()), 200


@siesa_bp.route('/debug-oc-raw', methods=['GET'])
@jwt_required()
def debug_oc_raw():
    """Debug: devuelve el JSON crudo de Siesa sin procesar. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403
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
                    'proveedor_codigo': row.get('f420_id_tercero', '').strip(),   # NIT proveedor → f451_id_tercero
                    'cond_pago': row.get('f420_id_cond_pago', '').strip(),        # Condición de pago
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

    # Enriquecer con estado WMS — evita mostrar OCs ya confirmadas como pendientes
    from app.models.recepcion import RecepcionMercancia
    numeros_oc = list(ordenes.keys())
    recepciones_wms = RecepcionMercancia.query.filter(
        RecepcionMercancia.numero_oc_siesa.in_(numeros_oc)
    ).filter(RecepcionMercancia.estado.notin_(['CANCELADA'])).all()
    estado_por_oc = {r.numero_oc_siesa: r.estado for r in recepciones_wms}

    for numero_oc, oc in ordenes.items():
        oc['recepcion_wms_estado'] = estado_por_oc.get(numero_oc)

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

    # Arquitectura Single Source of Truth: consulta SOLO DB local.
    # Los barcodes se mantienen actualizados por el sync nocturno (02:00).
    # No se consulta Connekta en tiempo real — evita timeouts de 30s durante operación.
    if not prod:
        return jsonify({
            'error': f"Código '{codigo}' no encontrado. Si es un EAN nuevo, ejecuta Sync EAN en Admin → Siesa."
        }), 404

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

    # Idempotencia: si ya existe packing no cancelado para este pedido → rechazar
    from app.models.packing import TareaPacking as _TP
    existing_packing = _TP.query.filter(
        _TP.numero_pedido_siesa == numero_pedido,
        _TP.estado != 'CANCELADO'
    ).first()
    if existing_packing:
        if existing_packing.siesa_triggered:
            return jsonify({'error': f'{numero_pedido} ya fue despachado a Siesa.'}), 409
        completado = existing_packing.estado in ['VERIFICADO', 'DESPACHADO']
        if completado:
            return jsonify({
                'error': f'{numero_pedido} ya tiene picking completo — '
                         f'usa "Confirmar en Siesa" para enviar la remisión.',
                'packing_id': existing_packing.id
            }), 409
        return jsonify({
            'error': f'{numero_pedido} ya está en proceso de picking '
                     f'(packing {existing_packing.codigo}).'
        }), 409

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

    # Obtener cliente/destino del pedido Siesa para el Monitor de Muelle
    from app.models.pedido_siesa import PedidoSiesa as _PS
    pedido_row = _PS.query.filter_by(numero_pedido=numero_pedido).first()
    cliente_destino  = pedido_row.cliente   if pedido_row else data.get('cliente', '')
    municipio_destino = pedido_row.municipio if pedido_row else ''

    try:
        packing = PackingService.crear_manual(
            numero_pedido_siesa=numero_pedido,
            almacen_id=almacen_id,
            items=items_packing,
            tipo_docto_pedido_siesa=tipo_docto,
            consec_docto_pedido_siesa=consec_docto,
            cliente=cliente_destino,
            municipio=municipio_destino
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

    # Idempotente: si ya existe una recepción activa para esta OC, redirigir a ella
    from app.models.recepcion import RecepcionMercancia
    existente = RecepcionMercancia.query.filter_by(
        numero_oc_siesa=data['numero_oc']
    ).filter(RecepcionMercancia.estado.notin_(['CANCELADA'])).first()

    if existente:
        if existente.estado == 'EN_PROCESO':
            return jsonify({
                'mensaje': 'Recepción ya en proceso — continuando escaneo',
                'recepcion': existente.to_dict()
            }), 200
        if existente.estado == 'CONFIRMADA':
            return jsonify({
                'error': f'La OC {data["numero_oc"]} ya fue recepcionada y confirmada (recepción {existente.codigo}). Si necesitas una corrección, cancela la recepción desde Admin.'
            }), 409
        if existente.estado == 'ABIERTA':
            existente = RecepcionService.iniciar(existente.id, recepcionista_id)
            return jsonify({
                'mensaje': 'Recepción retomada — listo para escanear',
                'recepcion': existente.to_dict()
            }), 200

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
