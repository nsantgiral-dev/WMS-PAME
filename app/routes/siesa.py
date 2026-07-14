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
import logging
import os
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.connekta_gateway import connekta
from app.models.producto import Producto
from app.services.picking_service import PickingService
from app.services.packing_service import PackingService
from app.services.recepcion_service import RecepcionService

logger = logging.getLogger(__name__)
siesa_bp = Blueprint('siesa', __name__)


from app.routes._auth_helpers import _solo_admin, Roles


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
    from flask import current_app, request as _req
    from app.services.inventario_siesa_service import iniciar_carga_inventario
    forzar = _req.args.get('forzar', 'false').lower() == 'true'
    resultado = iniciar_carga_inventario(current_app._get_current_object(), forzar=forzar)
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
        # Allowlist: params_raw debe ser tokens tipo campo=valor separados por AND.
        # campo: convención Siesa f<nnn>_<nombre> (ej. f430_id_co).
        # valor: número, ''texto'' (Connekta), o identificador simple.
        import re as _re
        _TOKEN_RE = _re.compile(
            r'^[a-zA-Z][a-zA-Z0-9_]{2,40}='  # campo
            r"(''[^']*''|'[^']*'|\"[^\"]*\"|-?\d+(\.\d+)?|[a-zA-Z0-9_\-\.]+)$"
        )
        if len(params_raw) > 500:
            return jsonify({'error': 'params_raw demasiado largo (máx 500 chars)'}), 400
        tokens = [t.strip() for t in _re.split(r'\bAND\b', params_raw, flags=_re.IGNORECASE)]
        invalidos = [t for t in tokens if t and not _TOKEN_RE.match(t)]
        if invalidos:
            return jsonify({
                'error': 'params_raw contiene tokens inválidos',
                'invalidos': invalidos,
                'formato': 'campo=valor [AND campo=valor] — valor: número, \'\'texto\'\' o identificador',
            }), 400
        params['parametros'] = params_raw
    elif consec:
        filtros = [f'f430_consec_docto={consec}']
        if co:
            filtros.append(f"f430_id_co = ''{co}''")
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


@siesa_bp.route('/debug-items-raw', methods=['GET'])
@jwt_required()
def debug_items_raw():
    """
    Inspecciona los campos reales que devuelve API_v2_Items.
    Úsalo para confirmar el nombre exacto del factor de conversión
    (f421_factor, f121_factor, factor u otro alias Connekta).
    Acepta ?pagina=1 (default: 1).
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    pagina = request.args.get('pagina', 1, type=int)
    resultado = connekta.get_items_catalogo(pagina)
    tabla = resultado.get('detalle', {}).get('Table', [])
    if not tabla:
        return jsonify({'error': 'Sin datos — verifica credenciales Connekta', 'raw': resultado}), 200
    campos = list(tabla[0].keys())
    campos_factor = [c for c in campos if 'factor' in c.lower() or 'unidad_emp' in c.lower() or 'empaque' in c.lower()]
    return jsonify({
        'api': connekta.api_items if hasattr(connekta, 'api_items') else os.getenv('CONNEKTA_API_ITEMS', 'API_v2_Items'),
        'total_filas_pagina': len(tabla),
        'campos_relevantes_empaque': campos_factor,
        'todos_los_campos': campos,
        'muestra_primeros_3': tabla[:3]
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
        logger.exception('[DEBUG] Error en debug_monitor_facturas')
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
    """Busca un producto por código WMS, código Siesa, barcode de unidad o barcode de empaque."""
    return (Producto.query.filter_by(codigo=codigo).first() or
            Producto.query.filter_by(codigo_siesa=codigo).first() or
            Producto.query.filter_by(codigo_barras=codigo).first() or
            Producto.query.filter_by(codigo_barras_empaque=codigo).first())


# ──────────────────────────────────────────────
# GETs — exponen el gateway a la PWA
# ──────────────────────────────────────────────

@siesa_bp.route('/pedidos', methods=['GET'])
@jwt_required()
def pedidos_aprobados():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.DESPACHO:
        return jsonify({'error': 'Sin permiso para ver la cola de despacho'}), 403

    # Cola de despacho: Read Model local enriquecido con estado del picking WMS.
    # Estado por pedido:
    #   picking_iniciado=False                       → mostrar botón "Despachar"
    #   picking_iniciado=True, completado=False      → picking en curso (progreso X/Y)
    #   picking_iniciado=True, completado=True,
    #     siesa_triggered=False                      → mostrar botón "Confirmar en Siesa"
    #   siesa_triggered=True                         → despachado ✓
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

    # Enriquecer con estado del picking/packing en WMS — bulk-load en 2 queries (evita N+1)
    nums = list(pedidos.keys())
    packings_map = {}
    if nums:
        for pk in TareaPacking.query.filter(
            TareaPacking.numero_pedido_siesa.in_(nums),
            TareaPacking.estado != 'CANCELADO'
        ).all():
            packings_map.setdefault(pk.numero_pedido_siesa, pk)  # primer activo por pedido

        pickings_por_pedido: dict = {}
        for p in TareaPicking.query.filter(
            TareaPicking.referencia_documento.in_(nums),
            TareaPicking.estado != 'CANCELADO'
        ).all():
            pickings_por_pedido.setdefault(p.referencia_documento, []).append(p)

    for num, pedido in pedidos.items():
        packing = packings_map.get(num)

        if not packing:
            pedido.update({'picking_iniciado': False, 'picking_completado': False,
                           'picking_progreso': '0/0', 'packing_id': None, 'siesa_triggered': False})
        else:
            pickings = pickings_por_pedido.get(num, [])
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
    GET /api/siesa/debug-barras-raw?referencia=ARTESA898 — filtra por f120_referencia
    en vez de f131_id, para inspeccionar los campos crudos que trae Siesa para
    un ítem puntual (ej. confirmar cuál campo es el EAN real).
    Sin parámetros: muestra los primeros 5 registros del conector completo.
    Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403
    codigo = request.args.get('codigo', '').strip()
    referencia = request.args.get('referencia', '').strip()
    if codigo:
        resultado = connekta._get(connekta.api_barras, {
            'paginacion': 'numPag=1|tamPag=5',
            'parametros': f"f131_id = ''{codigo}''"
        })
    elif referencia:
        resultado = connekta._get(connekta.api_barras, {
            'paginacion': 'numPag=1|tamPag=5',
            'parametros': f"f120_referencia = ''{referencia}''"
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


@siesa_bp.route('/debug-empaques-raw', methods=['GET'])
@jwt_required()
def debug_empaques_raw():
    """
    Debug: muestra los campos exactos que devuelven las queries de empaques.
    GET /api/siesa/debug-empaques-raw?query=35   → API_v2_ItemsUnidadesMedida
    GET /api/siesa/debug-empaques-raw?query=28   → API_v2_ItemsBarras
    Retorna las primeras 3 filas con todos sus campos para mapeo.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403

    query = request.args.get('query', '35')

    if query == '35':
        resp = connekta.get_items_unidades_medida(1)
        rows = resp.get('detalle', {}).get('Table', [])
        return jsonify({
            'query': 'API_v2_ItemsUnidadesMedida (35)',
            'total_filas_pagina_1': len(rows),
            'campos_disponibles': list(rows[0].keys()) if rows else [],
            'muestra_3_filas': rows[:3],
            'respuesta_completa_raw': resp,
        }), 200

    if query == '28':
        resp = connekta._get(connekta.api_barras, {'paginacion': 'numPag=1|tamPag=3'})
        rows = resp.get('detalle', {}).get('Table', [])
        return jsonify({
            'query': 'API_v2_ItemsBarras (28)',
            'total_filas_pagina_1': len(rows),
            'campos_disponibles': list(rows[0].keys()) if rows else [],
            'muestra_3_filas': rows[:3],
            'respuesta_completa_raw': resp,
        }), 200

    return jsonify({'error': 'query debe ser 28 o 35'}), 400


@siesa_bp.route('/debug-pedidos-compromisos-raw', methods=['GET'])
@jwt_required()
def debug_pedidos_compromisos_raw():
    """
    Auditoría del JSON crudo de API_v2_Ventas_Pedidos_Compromisos (ID 103).
    Permite ver exactamente qué campos expone Connekta antes de construir
    el motor dinámico de reposición.

    Parámetros opcionales:
      ?pagina=1           → página de resultados (default 1)
      ?tam=5              → registros por página (default 5, max 50)
      ?parametros=...     → filtros Connekta crudos (ej. "f461_id_co=001")
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403

    pagina  = request.args.get('pagina', '1')
    try:
        tam = min(int(request.args.get('tam', '5')), 50)
    except (ValueError, TypeError):
        return jsonify({'error': 'tam debe ser un número entero'}), 400
    params_custom = request.args.get('parametros', '')

    params = {
        'paginacion': f'numPag={pagina}|tamPag={tam}',
    }
    if params_custom:
        params['parametros'] = params_custom

    resultado = connekta._get('API_v2_Ventas_Pedidos_Compromisos', params)
    return jsonify(resultado), 200


@siesa_bp.route('/ordenes-compra', methods=['GET'])
@jwt_required()
def ordenes_compra():
    """
    Cola de recepción: OCs aprobadas en Siesa con cantidad pendiente > 0.
    Enriquece con producto_id interno y agrupa por número de OC.
    """
    # [41] NIT de proveedores solo visible para admin y jefe_almacen
    from app.models.usuario import Usuario
    try:
        _uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    _u = Usuario.query.get(_uid)
    if not _u or _u.rol not in Roles.RECEPCION_ROLES:
        return jsonify({'error': 'Sin permiso — se requiere rol admin, jefe_almacen o recepcionista'}), 403

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
                    'proveedor_codigo': (row.get('f200_nit_prov', '') or row.get('f200_id_prov', '')).strip(),  # NIT proveedor (campo oficial API_v2_Compras_Ordenes)
                    'sucursal_prov': row.get('f202_id_sucursal_prov', '').strip(),  # sucursal proveedor (campo oficial API_v2_Compras_Ordenes)
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
                'factor_conversion': int(float(row.get('f421_factor') or 1)),
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

    lista = sorted(ordenes.values(), key=lambda o: int(o.get('consec_docto') or 0), reverse=True)
    return jsonify({'ordenes': lista, 'total': len(lista)}), 200


@siesa_bp.route('/debug-oc/<consec>', methods=['GET'])
@jwt_required()
def debug_oc(consec):
    """
    Diagnóstico temporal: campos raw de Siesa para una OC específica
    y cuál filtro la excluye del módulo de recepción. Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403

    resultado = connekta.get_ordenes_compra_aprobadas(consec=consec)
    rows = resultado.get('detalle', {}).get('Table', [])
    if not rows:
        return jsonify({
            'mensaje': f'OC consec={consec} no retornada por Siesa (sin líneas o estado≠1)',
            'tip': 'Si la OC existe en Siesa, su f420_ind_estado puede ser distinto de 1 '
                   '(Aprobado en UI ≠ 1 en API). Verificar con consultor.',
            'raw': resultado,
        }), 404

    lineas = []
    for r in rows:
        co_oc     = r.get('f420_id_co', '').strip()
        bodega_oc = r.get('f150_id', '').strip()
        estado    = r.get('f420_ind_estado')
        cant_ped  = float(r.get('f421_cant_pedida') or 0)
        cant_rec  = float(r.get('f421_cant_entrada') or 0)
        cant_pend = cant_ped - cant_rec
        ref       = r.get('f120_referencia', '').strip()
        prod      = _buscar_producto(ref) if ref else None
        lineas.append({
            'f120_referencia':     ref,
            'f120_descripcion':    r.get('f120_descripcion', ''),
            'producto_wms_id':     prod.id if prod else None,
            'f420_ind_estado':     estado,
            'f420_id_co':          co_oc,
            'f150_id':             bodega_oc,
            'cant_pedida':         cant_ped,
            'cant_recibida':       cant_rec,
            'cant_pendiente':      cant_pend,
            'co_wms':              connekta.centro_op,
            'bodega_wms':          connekta.bodega,
            'pasa_co':             co_oc == connekta.centro_op,
            'pasa_bodega':         bodega_oc == connekta.bodega,
            'pasa_pendiente':      cant_pend > 0,
            'pasa_producto_wms':   prod is not None,
            'apareceria_en_lista': (
                co_oc == connekta.centro_op
                and bodega_oc == connekta.bodega
                and cant_pend > 0
            ),
        })
    return jsonify({'consec': consec, 'total_lineas': len(lineas), 'lineas': lineas}), 200


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

    factor = prod.factor_conversion or 1
    # es_empaque = barcode explícito de empaque, O factor>1 (la OC ya cargó el factor)
    es_empaque = bool(
        (prod.codigo_barras_empaque and codigo == prod.codigo_barras_empaque)
        or factor > 1
    )

    return jsonify({
        'producto_id': prod.id,
        'codigo': prod.codigo,
        'nombre': prod.nombre,
        'codigo_siesa': prod.codigo_siesa,
        'codigo_barras': prod.codigo_barras,
        'clasificacion_abc': prod.clasificacion_abc,
        'es_empaque': es_empaque,
        'factor_conversion': factor,
        'unidad_empaque': prod.unidad_empaque or None,
    }), 200


@siesa_bp.route('/producto-siesa-vivo/<codigo>', methods=['GET'])
@jwt_required()
def buscar_producto_siesa_vivo(codigo):
    """
    Lookup EN VIVO contra Connekta (API_v2_Items) — respaldo exclusivo de la
    herramienta de Etiquetas cuando el catálogo local todavía no sincronizó
    un ítem recién creado en Siesa. No usar en picking/packing: puede tardar
    hasta 30s (ver /producto/<codigo>, que es la ruta local usada en caliente).
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede consultar Siesa en vivo'}), 403
    item = connekta.buscar_item_por_referencia(codigo)
    if not item:
        return jsonify({'error': f"'{codigo}' no existe en el maestro de ítems de Siesa"}), 404
    barras = connekta.buscar_barras_por_referencia(item['codigo_siesa'])
    item['codigo_barras'] = barras[0] if barras else None
    return jsonify(item), 200


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
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede iniciar despacho'}), 403
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
        except (ValueError, TypeError) as e:
            errores.append(str(e))

    if not tareas_picking_ids:
        return jsonify({'error': 'Stock insuficiente para todos los ítems', 'detalle': errores}), 400

    # Crear packing con las llaves del documento Siesa — usar crear_manual porque
    # el picking aún está PENDIENTE (los operarios lo completarán después).
    #
    # NORMALIZACIÓN A UND: Siesa devuelve f431_cant1_pedida en la unidad1 del pedido,
    # que puede ser PQ (empaque) o UND según cómo se parametrizó el pedido.
    # El WMS maneja cantidad_esperada siempre en UND para que el escaneo por empaque
    # (frontend envía cantidad = factor) funcione correctamente.
    # Heurístico: si cant_raw < factor_conversion → la cantidad está en unidades de empaque.
    from app.models.producto import Producto as _ProdPack
    items_packing = []
    for _ip in items:
        _pp = _ProdPack.query.get(_ip['producto_id'])
        _fc = (_pp.factor_conversion or 1) if _pp else 1
        _cq = int(_ip['cantidad_pendiente'])
        # Si la cantidad es menor que el factor, Siesa la envió en unidades de empaque → convertir a UND
        _cq_und = _cq * _fc if (_fc > 1 and _cq < _fc) else _cq
        items_packing.append({'producto_id': _ip['producto_id'], 'cantidad': _cq_und})

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
    # [C1] Solo recepcionistas, jefe_almacen y admin pueden crear recepciones.
    # Sin este check cualquier usuario autenticado (ej. conductor, tienda) podría
    # crear entradas de inventario en Siesa — riesgo crítico de integridad.
    from app.models.usuario import Usuario as _Usr
    try:
        recepcionista_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    _usr = _Usr.query.get(recepcionista_id)
    if not _usr or _usr.rol not in Roles.RECEPCION_ROLES:
        return jsonify({'error': 'Sin permiso — se requiere rol recepcionista, jefe_almacen o admin'}), 403
    data = request.get_json()

    for campo in ['numero_oc', 'tipo_docto', 'consec_docto', 'almacen_id', 'items']:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    items_validos = [i for i in data['items'] if i.get('producto_id')]
    if not items_validos:
        return jsonify({'error': 'Ningún producto de la OC está registrado en el WMS'}), 400

    # Validar tipo de proveedor antes de crear la recepción.
    # Solo 0001 (Nacionales) y 0002 (Extranjero) tienen Aux. Pasivo Estimado
    # configurado en Siesa — cualquier otro tipo revienta el conector 142948.
    nit_proveedor = (data.get('proveedor_codigo') or '').strip()
    if nit_proveedor:
        val = connekta.validar_tipo_proveedor(nit_proveedor)
        if val.get('configurado') is False:
            return jsonify({'error': val['mensaje']}), 422
        tipo = val.get('tipo_proveedor') or ''
        if tipo and tipo not in ('0001', '0002'):
            return jsonify({
                'error': (
                    f'El proveedor NIT {nit_proveedor} tiene Tipo de Proveedor {tipo} '
                    f'({tipo}), el cual no tiene cuenta de Pasivo Estimado configurada. '
                    f'Solo los tipos 0001 (Nacionales) y 0002 (Extranjero) pueden '
                    f'generar entradas de inventario. Corrige en Siesa: Maestros → '
                    f'Terceros → [este NIT] → pestaña Compras → Tipo de Proveedor.'
                )
            }), 422

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

    # Actualizar factor_conversion desde f421_factor de la OC (fuente de verdad Siesa)
    from app.models.producto import Producto as _Prod
    for i in items_validos:
        fc = int(float(i.get('factor_conversion') or 1))
        if fc > 1 and i.get('producto_id'):
            prod = _Prod.query.get(i['producto_id'])
            if prod and (prod.factor_conversion or 1) != fc:
                prod.factor_conversion = fc
    from app.extensions import db as _db
    _db.session.flush()

    items_recepcion = [{
        'producto_id': i['producto_id'],
        'cantidad_ordenada': int(i['cantidad_ordenada']),
        'tolerancia_exceso_pct': 0.0
    } for i in items_validos]

    try:
        recepcion = RecepcionService.crear_recepcion(
            numero_oc_siesa=data['numero_oc'],
            almacen_id=data['almacen_id'],
            proveedor_codigo=(data.get('proveedor_codigo') or '').strip(),
            proveedor_nombre=data.get('proveedor', ''),
            items=items_recepcion,
            co_oc_siesa=data.get('co', ''),
            tipo_docto_oc_siesa=data['tipo_docto'],
            consec_docto_oc_siesa=data['consec_docto'],
            cond_pago_siesa=data.get('cond_pago', ''),
            sucursal_prov_siesa=data.get('sucursal_prov', ''),
            num_remision_prov=data.get('num_remision_prov', '')
        )
        recepcion = RecepcionService.iniciar(recepcion.id, recepcionista_id)
        return jsonify({
            'mensaje': 'Recepción iniciada — listo para escanear',
            'recepcion': recepcion.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ──────────────────────────────────────────────
# Jobs DLQ — gestión de jobs fallidos
# ──────────────────────────────────────────────

@siesa_bp.route('/jobs', methods=['GET'])
@jwt_required()
def listar_jobs():
    """
    Lista jobs filtrando por estado y/o tipo. Solo admin.
    ?estado=REINTENTANDO&tipo=DESPACHO_F470&ref_id=62
    Si no se pasa estado, devuelve todos los estados activos (PENDIENTE, PROCESANDO, REINTENTANDO, FALLIDO).
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ver jobs'}), 403

    from app.models.siesa_job import SiesaJob, EstadoSiesaJob

    estado = request.args.get('estado')
    tipo = request.args.get('tipo')
    ref_id = request.args.get('ref_id', type=int)

    query = SiesaJob.query
    if estado:
        query = query.filter_by(estado=estado)
    else:
        query = query.filter(SiesaJob.estado.in_([
            EstadoSiesaJob.PENDIENTE,
            EstadoSiesaJob.PROCESANDO,
            EstadoSiesaJob.REINTENTANDO,
            EstadoSiesaJob.FALLIDO,
        ]))
    if tipo:
        query = query.filter_by(tipo=tipo)
    if ref_id:
        query = query.filter_by(referencia_id=ref_id)

    jobs = query.order_by(SiesaJob.fecha_creacion.desc()).limit(100).all()
    return jsonify({'total': len(jobs), 'jobs': [j.to_dict() for j in jobs]}), 200


@siesa_bp.route('/jobs-fallidos', methods=['GET'])
@jwt_required()
def listar_jobs_fallidos():
    """Lista jobs FALLIDO agrupados por tipo. Solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede ver jobs fallidos'}), 403

    from app.models.siesa_job import SiesaJob, EstadoSiesaJob

    tipo = request.args.get('tipo')
    query = SiesaJob.query.filter_by(estado=EstadoSiesaJob.FALLIDO)
    if tipo:
        query = query.filter_by(tipo=tipo)

    jobs = query.order_by(SiesaJob.fecha_creacion.desc()).limit(200).all()

    por_tipo = {}
    for j in jobs:
        por_tipo.setdefault(j.tipo, 0)
        por_tipo[j.tipo] += 1

    return jsonify({
        'total': len(jobs),
        'por_tipo': por_tipo,
        'jobs': [j.to_dict() for j in jobs]
    }), 200


@siesa_bp.route('/resetear-jobs-fallidos', methods=['POST'])
@jwt_required()
def resetear_jobs_fallidos():
    """
    Resetea jobs FALLIDO → PENDIENTE y dispara DLQ inmediatamente.
    Usar después de corregir CONNEKTA_IKEY/ITOKEN en Railway.
    ?tipo=DESPACHO_F470  (default)
    Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede resetear jobs'}), 403

    from app.models.siesa_job import SiesaJob, EstadoSiesaJob
    from app.extensions import db

    tipo = request.args.get('tipo', 'DESPACHO_F470')

    jobs = SiesaJob.query.filter_by(
        tipo=tipo,
        estado=EstadoSiesaJob.FALLIDO
    ).all()

    if not jobs:
        return jsonify({
            'mensaje': f'No hay jobs {tipo} en estado FALLIDO',
            'reseteados': 0
        }), 200

    for job in jobs:
        job.estado = EstadoSiesaJob.PENDIENTE
        job.intentos = 0
        job.proximo_intento = None
        job.error_ultimo = None

    db.session.commit()

    from app.services.siesa_job_service import disparar_dlq_inmediato
    disparar_dlq_inmediato()

    return jsonify({
        'mensaje': f'{len(jobs)} job(s) reseteados a PENDIENTE — DLQ disparado',
        'reseteados': len(jobs),
        'tipo': tipo,
        'job_ids': [j.id for j in jobs]
    }), 200


@siesa_bp.route('/trigger-dlq', methods=['POST'])
@jwt_required()
def trigger_dlq_manual():
    """
    Fuerza la ejecución inmediata del DLQ en hilo daemon.
    Útil cuando el daemon perdió el advisory lock contra el scheduler.
    Devuelve cuántos jobs PENDIENTE/REINTENTANDO hay elegibles. Solo admin.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede disparar el DLQ'}), 403

    from app.models.siesa_job import SiesaJob, EstadoSiesaJob
    from datetime import datetime, timezone

    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    elegibles = SiesaJob.query.filter(
        SiesaJob.estado.in_([EstadoSiesaJob.PENDIENTE, EstadoSiesaJob.REINTENTANDO]),
        (SiesaJob.proximo_intento == None) | (SiesaJob.proximo_intento <= ahora)
    ).count()

    from app.services.siesa_job_service import disparar_dlq_inmediato
    disparar_dlq_inmediato()

    return jsonify({
        'mensaje': 'DLQ disparado — procesando jobs en hilo daemon',
        'jobs_elegibles': elegibles
    }), 200


@siesa_bp.route('/terceros-contacto', methods=['GET'])
@jwt_required()
def terceros_contacto():
    """
    Prueba API_custom_TercerosContacto (8232).
    ?nit=900123456  — filtra por NIT exacto
    ?pagina=1&tam=10 — paginación
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    nit = request.args.get('nit')
    pagina = request.args.get('pagina', 1, type=int)
    tam = request.args.get('tam', 10, type=int)
    rows = connekta.get_terceros_contacto(nit=nit, pagina=pagina, tam_pagina=tam)
    return jsonify({
        'total': len(rows),
        'pagina': pagina,
        'datos': rows,
    }), 200


@siesa_bp.route('/debug-stock-bodega', methods=['GET'])
@jwt_required()
def debug_stock_bodega():
    """
    Diagnóstico directo del stock Siesa para una bodega.
    ?bodega=NC1    — bodega a consultar (default: BODEGA_ORIGEN_DEFAULT)
    ?pag=1         — página de la API a leer (default 1)
    ?sin_filtro=true — sin filtro f150_id (descarga todo y filtra en Python)
    ?buscar=PAPELSP9218 — busca referencia exacta en TODAS las páginas (hasta 200)

    Muestra las primeras 5 filas y estadísticas para confirmar qué devuelve Siesa.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede usar endpoints de debug'}), 403

    from app.services.traslado_service import BODEGA_ORIGEN_DEFAULT
    bodega = request.args.get('bodega') or BODEGA_ORIGEN_DEFAULT
    pag = request.args.get('pag', 1, type=int)
    sin_filtro = request.args.get('sin_filtro', '').lower() == 'true'
    buscar = (request.args.get('buscar') or '').strip().upper()

    # Modo buscar: recorre TODAS las páginas buscando una referencia específica
    if buscar:
        api = connekta.api_inventario
        tam = 100
        encontrado = None
        paginas_revisadas = 0
        for p in range(1, 201):
            try:
                resp = connekta._get(api, {
                    'paginacion': f'numPag={p}|tamPag={tam}',
                    'parametros': f"f150_id = ''{bodega}'' AND f400_cant_existencia_1 > 0",
                })
                rows = resp.get('detalle', {}).get('Table', []) or []
                paginas_revisadas += 1
                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break
                for r in rows:
                    ref = (r.get('f120_referencia') or '').strip().upper()
                    if ref == buscar:
                        encontrado = {k: v for k, v in r.items()}
                        break
                if encontrado or len(rows) < tam:
                    break
            except Exception as _e:
                return jsonify({'error': str(_e), 'pagina_error': p}), 502
        return jsonify({
            'buscar': buscar,
            'bodega': bodega,
            'paginas_revisadas': paginas_revisadas,
            'encontrado': encontrado,
            'en_siesa': encontrado is not None,
            'disponible_calculado': (
                max(0,
                    int(encontrado.get('f400_cant_existencia_1') or 0)
                    - int(encontrado.get('f400_cant_comprometida_1') or 0)
                    - int(encontrado.get('f400_cant_salida_sin_conf_1') or 0)
                ) if encontrado else None
            ),
        }), 200

    api = connekta.api_inventario
    tam = 100

    try:
        if sin_filtro:
            # Descarga sin filtro y filtra en Python (igual que _descargar_inventario_siesa)
            resp = connekta._get(api, {'paginacion': f'numPag={pag}|tamPag={tam}'})
        else:
            # Con filtro f150_id (igual que get_stock_bodega)
            resp = connekta._get(api, {
                'paginacion': f'numPag={pag}|tamPag={tam}',
                'parametros': f"f150_id = ''{bodega}'' AND f400_cant_existencia_1 > 0",
            })
    except Exception as exc:
        return jsonify({'error': str(exc), 'bodega': bodega, 'modo': 'sin_filtro' if sin_filtro else 'con_filtro_f150_id'}), 502

    rows = resp.get('detalle', {}).get('Table', []) or []

    # Estadísticas
    bodegas_vistas = {}
    refs_con_stock = []
    for r in rows:
        bid = (r.get('f150_id') or '').strip()
        bodegas_vistas[bid] = bodegas_vistas.get(bid, 0) + 1

    if sin_filtro:
        rows_bodega = [r for r in rows if (r.get('f150_id') or '').strip() == bodega]
    else:
        rows_bodega = rows

    for r in rows_bodega:
        ref = (r.get('f120_referencia') or '').strip()
        if ref:
            existencia = int(r.get('f400_cant_existencia_1') or 0)
            comprometida = int(r.get('f400_cant_comprometida_1') or 0)
            salida = int(r.get('f400_cant_salida_sin_conf_1') or 0)
            refs_con_stock.append({
                'referencia': ref,
                'descripcion': (r.get('f120_descripcion') or '').strip(),
                'existencia': existencia,
                'comprometida': comprometida,
                'salida_sin_conf': salida,
                'disponible': max(0, existencia - comprometida - salida),
            })

    # Campos disponibles en la primera fila (para saber qué devuelve la API)
    campos = list(rows[0].keys()) if rows else []

    return jsonify({
        'bodega': bodega,
        'modo': 'sin_filtro_python' if sin_filtro else 'con_filtro_f150_id',
        'api': api,
        'pagina': pag,
        'total_filas_retornadas': len(rows),
        'filas_para_bodega': len(rows_bodega),
        'refs_con_stock_en_bodega': len(refs_con_stock),
        'bodegas_en_respuesta': bodegas_vistas,
        'campos_disponibles': campos,
        'muestra_5': refs_con_stock[:5],
    }), 200


@siesa_bp.route('/debug-cache-status', methods=['GET'])
@jwt_required()
def debug_cache_status():
    """Estado de los caches de inventario Siesa."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    from app.services.inventario_siesa_service import (
        _cache_inventario_multibodega, _cache_inventario_siesa,
        _descarga_multibodega_en_curso,
    )
    multi = _cache_inventario_multibodega
    mono = _cache_inventario_siesa
    return jsonify({
        'multibodega': {
            'tiene_data': multi['data'] is not None,
            'bodegas': sorted(multi['data'].keys()) if multi['data'] else [],
            'productos_por_bodega': {k: len(v) for k, v in (multi['data'] or {}).items()},
            'ts': str(multi['ts']) if multi['ts'] else None,
        },
        'monobodega': {
            'tiene_data': mono['data'] is not None,
            'productos': len(mono['data']) if mono['data'] else 0,
            'ts': str(mono['ts']) if mono['ts'] else None,
        },
        'descarga_en_curso': _descarga_multibodega_en_curso,
    }), 200


@siesa_bp.route('/debug-cache-ref', methods=['GET'])
@jwt_required()
def debug_cache_ref():
    """Busca una referencia en el cache multi-bodega. ?ref=PAPELSP9218&bodega=NS1"""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403
    from app.services.inventario_siesa_service import _cache_inventario_multibodega
    ref = (request.args.get('ref') or '').strip().upper()
    bodega = (request.args.get('bodega') or '').strip().upper()
    if not ref:
        return jsonify({'error': 'Param ref requerido'}), 400

    multi = _cache_inventario_multibodega.get('data') or {}
    resultado = {}
    bodegas_buscar = [bodega] if bodega else sorted(multi.keys())
    for bod in bodegas_buscar:
        inv = multi.get(bod, {})
        if ref in inv:
            resultado[bod] = inv[ref]
        ref_lower = ref.lower()
        for cod, datos in inv.items():
            if cod.upper() == ref:
                resultado[bod] = datos
                break
    return jsonify({
        'ref': ref,
        'bodega_filtro': bodega or 'todas',
        'encontrado_en': list(resultado.keys()),
        'datos': resultado,
    }), 200


@siesa_bp.route('/debug-traza-ref', methods=['GET'])
@jwt_required()
def debug_traza_ref():
    """
    Traza el pipeline completo para una referencia específica.
    Ejecuta get_stock_bodega() (mismo path que get_stock_disponible)
    y muestra en qué punto la referencia aparece o desaparece.
    ?bodega=NC1&ref=PAPELSP9218
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin'}), 403

    from app.services.traslado_service import BODEGA_ORIGEN_DEFAULT
    from app.models.producto import Producto

    bodega = request.args.get('bodega') or BODEGA_ORIGEN_DEFAULT
    ref_buscar = (request.args.get('ref') or '').strip().upper()
    if not ref_buscar:
        return jsonify({'error': 'Param ref requerido (ej: ?ref=PAPELSP9218)'}), 400

    traza = {'ref': ref_buscar, 'bodega': bodega, 'pasos': []}

    # Paso 1: get_stock_bodega (paginación paralela — mismo code path)
    try:
        res_siesa = connekta.get_stock_bodega(bodega)
        rows = res_siesa.get('detalle', {}).get('Table', []) or []
        traza['pasos'].append({
            'paso': '1_get_stock_bodega',
            'total_rows': len(rows),
        })
    except Exception as exc:
        traza['pasos'].append({'paso': '1_get_stock_bodega', 'error': str(exc)})
        return jsonify(traza), 502

    # Paso 2: buscar ref en filas crudas
    filas_ref = []
    for i, r in enumerate(rows):
        raw_ref = r.get('f120_referencia')
        ref_limpio = str(raw_ref or '').strip().upper()
        if ref_limpio == ref_buscar:
            filas_ref.append({
                'indice_fila': i,
                'pagina_estimada': (i // 100) + 1,
                'f120_referencia_raw': raw_ref,
                'f120_referencia_stripped': ref_limpio,
                'f400_cant_existencia_1': r.get('f400_cant_existencia_1'),
                'f400_cant_comprometida_1': r.get('f400_cant_comprometida_1'),
                'f400_cant_salida_sin_conf_1': r.get('f400_cant_salida_sin_conf_1'),
                'f120_descripcion': r.get('f120_descripcion'),
                'f400_id_lote': r.get('f400_id_lote'),
            })
    traza['pasos'].append({
        'paso': '2_buscar_en_rows_crudas',
        'encontrado': len(filas_ref) > 0,
        'ocurrencias': len(filas_ref),
        'filas': filas_ref,
    })

    # Paso 3: simular el procesamiento de get_stock_disponible
    if filas_ref:
        for fila in filas_ref:
            existencia = int(fila['f400_cant_existencia_1'] or 0)
            comprometida = int(fila['f400_cant_comprometida_1'] or 0)
            salida = int(fila['f400_cant_salida_sin_conf_1'] or 0)
            disponible = max(0, existencia - comprometida - salida)
            fila['existencia_int'] = existencia
            fila['comprometida_int'] = comprometida
            fila['salida_int'] = salida
            fila['disponible_calculado'] = disponible
            fila['pasaria_filtro_existencia_gt0'] = existencia > 0
            fila['pasaria_filtro_disponible_gt0'] = disponible > 0

    # Paso 4: buscar en tabla productos WMS
    prod_por_siesa = Producto.query.filter_by(codigo_siesa=ref_buscar).first()
    prod_por_codigo = Producto.query.filter_by(codigo=ref_buscar).first()
    traza['pasos'].append({
        'paso': '4_buscar_producto_wms',
        'por_codigo_siesa': {
            'encontrado': prod_por_siesa is not None,
            'id': prod_por_siesa.id if prod_por_siesa else None,
            'nombre': prod_por_siesa.nombre if prod_por_siesa else None,
            'activo': prod_por_siesa.activo if prod_por_siesa else None,
        } if prod_por_siesa else {'encontrado': False},
        'por_codigo': {
            'encontrado': prod_por_codigo is not None,
            'id': prod_por_codigo.id if prod_por_codigo else None,
            'nombre': prod_por_codigo.nombre if prod_por_codigo else None,
            'codigo_siesa': prod_por_codigo.codigo_siesa if prod_por_codigo else None,
            'activo': prod_por_codigo.activo if prod_por_codigo else None,
        } if prod_por_codigo else {'encontrado': False},
    })

    return jsonify(traza), 200
