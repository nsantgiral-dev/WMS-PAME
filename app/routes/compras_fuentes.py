"""
Compras: las fuentes de datos — estado y carga mínima (m046compras).

GET  /api/compras/fuentes/estado                     espejo de OCs, en camino, lead time, kardex
POST /api/compras/fuentes/sync-oc                    sincronizar OCs ahora (en segundo plano)
POST /api/compras/fuentes/carga/vista-previa         archivo de origen/marca o fichas: qué pasaría
POST /api/compras/fuentes/carga/aplicar              el mismo archivo: escribe lo válido
PUT  /api/compras/fuentes/producto                   origen / marca de un SKU a mano
POST /api/compras/fuentes/marca-siesa/leer           leer la marca de Siesa (segundo plano)
GET  /api/compras/fuentes/marca-siesa/estado         cómo va / cuándo fue la última lectura
GET  /api/compras/fuentes/marca-siesa/vista-previa   qué cambiaría (de la lectura guardada)
POST /api/compras/fuentes/marca-siesa/aplicar        ídem, escribiendo
GET  /api/compras/fuentes/contenedores/<id>/items    ítems de un contenedor
POST /api/compras/fuentes/contenedores/<id>/items    cargar ítems (código, cantidad)
PUT  /api/compras/fuentes/contenedores/<id>/estado   mover el contenedor (y sus ítems)

Todo con `_es_compras()` (admin, jefe de almacén, gerente, compras).
"""
from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_compras, _get_uid

compras_fuentes_bp = Blueprint('compras_fuentes', __name__)

#: Una vista previa de 20.000 filas no se manda entera: los conteos van
#: completos en `resumen`, las listas se recortan y se dice cuánto.
_TOPE_LISTA = 300

ESTADOS_CONTENEDOR = ('BORRADOR', 'EN_PRODUCCION', 'NAVEGANDO', 'EN_PUERTO',
                      'NACIONALIZACION', 'EN_RUTA_CEDI', 'RECIBIDO')


def _recortar(r: dict) -> dict:
    for k in ('validas', 'invalidas'):
        if isinstance(r.get(k), list) and len(r[k]) > _TOPE_LISTA:
            r[f'{k}_recortadas'] = len(r[k]) - _TOPE_LISTA
            r[k] = r[k][:_TOPE_LISTA]
    return r


def _sin_permiso():
    return jsonify({'error': 'Sin permiso para las fuentes de compras'}), 403


@compras_fuentes_bp.route('/estado', methods=['GET'])
@jwt_required()
def estado():
    if not _es_compras():
        return _sin_permiso()
    from app.services import compras_fuentes, compras_oc_sync, kardex_auto
    obs = compras_fuentes.observaciones_lead_time()
    ec = compras_fuentes.en_camino()
    top = sorted(ec['por_sku'].items(), key=lambda kv: -kv[1])[:20]
    return jsonify({
        'compras_oc': compras_oc_sync.estado(),
        'kardex_auto': kardex_auto.estado(),
        'en_camino': {
            'skus': len(ec['por_sku']),
            'unidades': round(sum(ec['por_sku'].values()), 2),
            'top': [{'referencia': r, 'total': v, **ec['detalle'].get(r, {})}
                    for r, v in top],
            'declaracion': ec['declaracion'],
        },
        'lead_time': {
            'nacional': {k: v for k, v in compras_fuentes.lead_time(
                origen='NACIONAL', observaciones=obs).items() if k != 'lead_times'},
            'china': {k: v for k, v in compras_fuentes.lead_time(
                origen='CHINA', observaciones=obs).items() if k != 'lead_times'},
            'por_proveedor': compras_fuentes.lead_times_por_proveedor(obs),
            'observaciones': len(obs['por_oc']),
            'descartadas': obs['descartadas'],
        },
    }), 200


@compras_fuentes_bp.route('/sync-oc', methods=['POST'])
@jwt_required()
def sync_oc():
    if not _es_compras():
        return _sin_permiso()
    que = (request.get_json(silent=True) or {}).get('que', 'abiertas')
    if que not in ('abiertas', 'historial'):
        return jsonify({'error': "`que` es 'abiertas' o 'historial'"}), 400
    from app.services.compras_oc_sync import disparar_en_segundo_plano
    r = disparar_en_segundo_plano(current_app._get_current_object(), que)
    return jsonify(r), (202 if r.get('ok') else 409)


def _filas_del_request():
    """(tipo, filas) del archivo subido. Levanta ValueError con el motivo."""
    from app.services.maestro_compras_carga import leer_archivo, TIPOS
    tipo = (request.form.get('tipo') or '').strip().upper()
    if tipo not in TIPOS:
        raise ValueError(f'`tipo` es uno de {", ".join(TIPOS)}')
    f = request.files.get('archivo')
    if f is None or not f.filename:
        raise ValueError('Falta el archivo')
    return tipo, leer_archivo(f.filename, f.read()), f.filename


@compras_fuentes_bp.route('/carga/vista-previa', methods=['POST'])
@jwt_required()
def carga_vista_previa():
    if not _es_compras():
        return _sin_permiso()
    from app.services.maestro_compras_carga import vista_previa
    try:
        tipo, filas, _ = _filas_del_request()
        return jsonify(_recortar(vista_previa(tipo, filas))), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@compras_fuentes_bp.route('/carga/aplicar', methods=['POST'])
@jwt_required()
def carga_aplicar():
    if not _es_compras():
        return _sin_permiso()
    from app.services.maestro_compras_carga import aplicar
    try:
        tipo, filas, nombre = _filas_del_request()
        r = aplicar(tipo, filas, usuario_id=_get_uid(),
                    motivo=f'Carga del archivo {nombre}'[:200])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(_recortar(r)), (200 if r.get('ok') else 400)


@compras_fuentes_bp.route('/producto', methods=['PUT'])
@jwt_required()
def editar_producto():
    if not _es_compras():
        return _sin_permiso()
    from app.services.maestro_compras_carga import editar_producto as _editar
    data = request.get_json(silent=True) or {}
    codigo = (data.get('codigo') or '').strip()
    if not codigo:
        return jsonify({'error': 'Falta el código del producto'}), 400
    if data.get('origen') in (None, '') and data.get('marca') in (None, ''):
        return jsonify({'error': 'Nada que cambiar: envíe origen o marca'}), 400
    r = _editar(codigo, origen=data.get('origen') or None,
                marca=data.get('marca') or None, usuario_id=_get_uid())
    if r.get('invalidas'):
        return jsonify({'error': '; '.join(r['invalidas'][0]['errores']), **r}), 400
    return jsonify(r), 200


@compras_fuentes_bp.route('/marca-siesa/leer', methods=['POST'])
@jwt_required()
def marca_siesa_leer():
    """Arranca la lectura en un hilo y vuelve ya: ~5 minutos de Siesa no caben
    en un request (gunicorn corta a los 60 s)."""
    if not _es_compras():
        return _sin_permiso()
    from app.services.maestro_compras_carga import disparar_lectura_marca
    r = disparar_lectura_marca(current_app._get_current_object())
    codigo = r.pop('codigo', 202 if r.get('ok') else 409)
    return jsonify(r), codigo


@compras_fuentes_bp.route('/marca-siesa/estado', methods=['GET'])
@jwt_required()
def marca_siesa_estado():
    if not _es_compras():
        return _sin_permiso()
    from app.services.maestro_compras_carga import estado_lectura_marca
    return jsonify(estado_lectura_marca()), 200


@compras_fuentes_bp.route('/marca-siesa/vista-previa', methods=['GET'])
@jwt_required()
def marca_siesa_vista_previa():
    """Lee la lectura GUARDADA, no Siesa: no pide ventana."""
    if not _es_compras():
        return _sin_permiso()
    from app.services.maestro_compras_carga import marca_desde_siesa
    return jsonify(_recortar(marca_desde_siesa())), 200


@compras_fuentes_bp.route('/marca-siesa/aplicar', methods=['POST'])
@jwt_required()
def marca_siesa_aplicar():
    if not _es_compras():
        return _sin_permiso()
    from app.services.maestro_compras_carga import marca_desde_siesa
    return jsonify(_recortar(marca_desde_siesa(aplicar_=True, usuario_id=_get_uid()))), 200


# ── Contenedores: carga mínima de ítems y estado ─────────────────────────────

@compras_fuentes_bp.route('/contenedores/<int:contenedor_id>/items', methods=['GET'])
@jwt_required()
def items_contenedor(contenedor_id):
    if not _es_compras():
        return _sin_permiso()
    from app.extensions import db
    from app.models.importacion import Contenedor, ItemEnTransito
    c = db.session.get(Contenedor, contenedor_id)
    if c is None:
        return jsonify({'error': 'Contenedor no encontrado'}), 404
    items = ItemEnTransito.query.filter_by(contenedor_id=contenedor_id).all()
    return jsonify({'contenedor': c.to_dict(), 'items': [{
        'id': i.id, 'codigo': i.producto.codigo_siesa if i.producto else None,
        'nombre': i.producto.nombre if i.producto else None,
        'cantidad': i.cantidad, 'estado': i.estado,
        'oc_referencia': i.oc_referencia, 'bodega_destino': i.bodega_destino,
    } for i in items]}), 200


@compras_fuentes_bp.route('/contenedores/<int:contenedor_id>/items', methods=['POST'])
@jwt_required()
def cargar_items_contenedor(contenedor_id):
    """Cuerpo: {filas: [{codigo, cantidad}], oc_referencia?, bodega_destino?}.
    Todo o nada: una fila inválida no deja el contenedor a medias."""
    if not _es_compras():
        return _sin_permiso()
    from app.extensions import db
    from app.models.importacion import Contenedor, ItemEnTransito
    from app.services.inventario_siesa_service import _BODEGAS_PV
    from app.services.maestro_compras_carga import _productos_por_codigo
    c = db.session.get(Contenedor, contenedor_id)
    if c is None:
        return jsonify({'error': 'Contenedor no encontrado'}), 404
    data = request.get_json(silent=True) or {}
    filas = data.get('filas') or []
    oc_ref = (data.get('oc_referencia') or '').strip()[:40] or None
    bodega = (data.get('bodega_destino') or '').strip().upper() or None
    if bodega and bodega not in _BODEGAS_PV:
        return jsonify({'error': f'La bodega {bodega} no es una bodega operada'}), 400
    if not isinstance(filas, list) or not filas:
        return jsonify({'error': 'Sin filas (código y cantidad)'}), 400
    productos = _productos_por_codigo(str(f.get('codigo') or '').strip() for f in filas)
    errores, nuevos = [], []
    for n, f in enumerate(filas, start=1):
        codigo = str(f.get('codigo') or '').strip()
        try:
            cant = int(str(f.get('cantidad')).strip())
        except (TypeError, ValueError):
            cant = 0
        if codigo not in productos:
            errores.append(f'fila {n}: «{codigo}» no existe en el catálogo')
        elif cant <= 0:
            errores.append(f'fila {n}: cantidad inválida')
        else:
            nuevos.append(ItemEnTransito(
                producto_id=productos[codigo].id, contenedor_id=c.id, cantidad=cant,
                estado=c.estado if c.estado in ESTADOS_CONTENEDOR else 'BORRADOR',
                oc_referencia=oc_ref, bodega_destino=bodega))
    if errores:
        return jsonify({'error': 'No se cargó nada', 'errores': errores[:50]}), 400
    db.session.add_all(nuevos)
    db.session.commit()
    return jsonify({'ok': True, 'cargados': len(nuevos)}), 201


@compras_fuentes_bp.route('/contenedores/<int:contenedor_id>/estado', methods=['PUT'])
@jwt_required()
def estado_contenedor(contenedor_id):
    """Mueve el contenedor y sus ítems. RECIBIDO saca los ítems de «en camino»
    y fecha la recepción en el CDI (día Bogotá) si no la tenía: esa fecha es la
    observación de lead time de China."""
    if not _es_compras():
        return _sin_permiso()
    from app.extensions import db
    from app.models.importacion import Contenedor, ItemEnTransito
    from app.utils.fecha import dia_operativo
    c = db.session.get(Contenedor, contenedor_id)
    if c is None:
        return jsonify({'error': 'Contenedor no encontrado'}), 404
    nuevo = ((request.get_json(silent=True) or {}).get('estado') or '').strip().upper()
    if nuevo not in ESTADOS_CONTENEDOR:
        return jsonify({'error': f'Estado inválido. Válidos: {", ".join(ESTADOS_CONTENEDOR)}'}), 400
    c.estado = nuevo
    if nuevo == 'RECIBIDO' and c.fecha_recepcion_cedi is None:
        c.fecha_recepcion_cedi = dia_operativo()
    n = 0
    for i in ItemEnTransito.query.filter_by(contenedor_id=c.id).all():
        i.estado = nuevo
        n += 1
    db.session.commit()
    return jsonify({'ok': True, 'contenedor': c.to_dict(), 'items_movidos': n}), 200
