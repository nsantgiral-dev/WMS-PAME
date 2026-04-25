"""
Monitor de Muelle — lectura exclusiva desde DB local, cero peticiones a Connekta.
Flujo: siesa_triggered → bultos PENDIENTE aparecen en muelle → scan-to-truck → CARGADO.
"""
from datetime import datetime, date
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import selectinload
from app.extensions import db
from app.models.bulto import Bulto, EstadoBulto
from app.models.packing import TareaPacking, EstadoPacking

muelle_bp = Blueprint('muelle', __name__)


from app.routes._auth_helpers import _es_admin_o_jefe


@muelle_bp.route('/listos', methods=['GET'])
@jwt_required()
def listos():
    """
    Bultos físicos pendientes de cargue, agrupados por municipio (destino).
    Orden LIFO dentro de cada grupo: el primer bulto creado va al fondo del camión.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso para ver bultos listos en muelle'}), 403
    bultos = (
        Bulto.query
        .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
        .filter(
            TareaPacking.siesa_triggered == True,
            TareaPacking.estado != EstadoPacking.CANCELADO,
            Bulto.estado == EstadoBulto.PENDIENTE,
            Bulto.ruta_despacho_id.is_(None)
        )
        .options(selectinload(Bulto.tarea))
        .order_by(TareaPacking.fecha_despachado.asc(), Bulto.numero.asc())
        .all()
    )

    # Agrupar por municipio (fallback: cliente)
    grupos = {}
    for b in bultos:
        destino = b.tarea.municipio or b.tarea.cliente or 'Sin destino'
        if destino not in grupos:
            grupos[destino] = []
        grupos[destino].append(b.to_dict())

    resultado = [
        {'destino': destino, 'bultos': lista, 'total': len(lista)}
        for destino, lista in sorted(grupos.items())
    ]

    return jsonify({
        'grupos': resultado,
        'total_bultos': len(bultos)
    }), 200


@muelle_bp.route('/asignar', methods=['POST'])
@jwt_required()
def asignar_a_ruta():
    """
    Planificación: asigna bultos o un pedido completo a una ruta.
    Payload: {"ruta_id": 1, "bultos_ids": [10, 11]} O {"ruta_id": 1, "pedido_siesa": "PD1126"}
    """
    # [21] Solo admin o jefe_almacen pueden asignar bultos a rutas
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere rol admin o jefe_almacen'}), 403

    from app.models.ruta_despacho import RutaDespacho
    data = request.get_json()
    ruta_id = data.get('ruta_id')
    bultos_ids = data.get('bultos_ids', [])
    pedido_siesa = data.get('pedido_siesa')

    if not ruta_id:
        return jsonify({'error': 'ruta_id es requerido'}), 400

    ruta = RutaDespacho.query.get(ruta_id)
    if not ruta:
        return jsonify({'error': 'Ruta no encontrada'}), 404
    if ruta.estado != 'EN_CARGUE':
        return jsonify({'error': f'La ruta ya está {ruta.estado}'}), 400

    query = Bulto.query.filter(Bulto.estado == 'PENDIENTE')
    if pedido_siesa:
        query = query.join(TareaPacking).filter(TareaPacking.numero_pedido_siesa == pedido_siesa)
    elif bultos_ids:
        query = query.filter(Bulto.id.in_(bultos_ids))
    else:
        return jsonify({'error': 'Debe enviar bultos_ids o pedido_siesa'}), 400

    # Bloqueo para evitar doble asignación
    bultos = query.with_for_update().all()

    for b in bultos:
        b.ruta_despacho_id = ruta_id

    db.session.commit()
    return jsonify({
        'ok': True,
        'mensaje': f'{len(bultos)} bultos asignados a la ruta {ruta_id}'
    }), 200


@muelle_bp.route('/desasignar/<int:id>', methods=['DELETE'])
@jwt_required()
def desasignar_de_ruta(id):
    """Quita un bulto de la planificación de una ruta."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'No autorizado'}), 403
    bulto = Bulto.query.get_or_404(id)
    if bulto.estado == EstadoBulto.CARGADO:
        return jsonify({'error': 'No se puede desasignar un bulto que ya fue cargado físicamente'}), 400

    bulto.ruta_despacho_id = None
    db.session.commit()
    return jsonify({'ok': True, 'mensaje': 'Bulto desasignado de la ruta'}), 200


@muelle_bp.route('/cargar/<string:codigo_barras>', methods=['POST'])
@jwt_required()
def cargar_bulto(codigo_barras):
    """
    Verificación de Cargue: el operario escanea el bulto físico.
    Requiere ruta_id en el body para validar que el bulto pertenece a esa ruta.
    """
    # [21] Solo admin o jefe_almacen pueden registrar el cargue de bultos
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere rol admin o jefe_almacen'}), 403

    from app.models.ruta_despacho import RutaDespacho
    data = request.get_json(silent=True) or {}
    ruta_id_raw = data.get('ruta_id')

    if not ruta_id_raw:
        return jsonify({'error': 'ruta_id es requerido para verificación'}), 400

    try:
        ruta_id = int(ruta_id_raw)
    except (ValueError, TypeError):
        return jsonify({'error': 'ruta_id debe ser un número entero válido'}), 400

    bulto = Bulto.query.filter_by(codigo_barras=codigo_barras.upper()).with_for_update().first()

    if not bulto:
        return jsonify({'error': f'Bulto {codigo_barras} no encontrado'}), 404
    if not bulto.tarea.siesa_triggered:
        return jsonify({'error': 'Este pedido aún no fue procesado por Siesa'}), 400
    
    # Validar que el bulto ya esté planificado para ESTA ruta
    if bulto.ruta_despacho_id != int(ruta_id):
        if bulto.ruta_despacho_id:
            return jsonify({'error': f'Bulto planificado para ruta #{bulto.ruta_despacho_id}, no para #{ruta_id}'}), 400
        else:
            return jsonify({'error': f'Bulto no ha sido asignado a ninguna ruta. Asígnalo manualmente primero.'}), 400

    if bulto.estado == EstadoBulto.CARGADO:
        return jsonify({
            'mensaje': 'Bulto ya fue verificado y cargado',
            'codigo_barras': codigo_barras,
            'ya_cargado': True,
            'ruta_despacho_id': bulto.ruta_despacho_id
        }), 200

    ruta = RutaDespacho.query.get(ruta_id)
    if not ruta:
        return jsonify({'error': 'Ruta no encontrada'}), 404
    if ruta.estado != 'EN_CARGUE':
        return jsonify({'error': f'La ruta #{ruta_id} ya está {ruta.estado}'}), 400

    _tarea_id = bulto.tarea_id  # capturar antes del commit — expire_on_commit invalida relaciones
    bulto.estado = EstadoBulto.CARGADO
    bulto.fecha_cargado = datetime.utcnow()
    db.session.commit()

    # Pendientes de la misma tarea EN LA MISMA RUTA
    pendientes_ruta = Bulto.query.filter_by(
        tarea_id=_tarea_id,
        ruta_despacho_id=ruta_id,
        estado='PENDIENTE'
    ).count()
    tarea = TareaPacking.query.get(_tarea_id)

    return jsonify({
        'ok': True,
        'id': bulto.id,
        'codigo_barras': codigo_barras,
        'tipo': bulto.tipo,
        'numero': bulto.numero,
        'total': bulto.total,
        'numero_pedido_siesa': tarea.numero_pedido_siesa if tarea else None,
        'cliente': tarea.cliente or '' if tarea else '',
        'municipio': tarea.municipio or '' if tarea else '',
        'ruta_despacho_id': bulto.ruta_despacho_id,
        'pedido_completo_en_ruta': pendientes_ruta == 0,
        'bultos_pendientes_pedido_ruta': pendientes_ruta
    }), 200


@muelle_bp.route('/manifiesto', methods=['GET'])
@jwt_required()
def manifiesto():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso para ver el manifiesto'}), 403
    """
    Manifiesto de ruta: todos los bultos cargados hoy, agrupados por municipio.
    El conductor lleva esto impreso — listado exacto de piezas por cliente.
    """
    from sqlalchemy import func

    hoy = date.today()
    bultos = (
        Bulto.query
        .options(selectinload(Bulto.tarea))
        .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
        .filter(
            Bulto.estado == EstadoBulto.CARGADO,
            func.date(Bulto.fecha_cargado) == hoy
        )
        .order_by(TareaPacking.municipio, TareaPacking.cliente, Bulto.numero)
        .all()
    )

    grupos = {}
    for b in bultos:
        destino = b.tarea.municipio or b.tarea.cliente or 'Sin destino'
        if destino not in grupos:
            grupos[destino] = {}
        pedido = b.tarea.numero_pedido_siesa
        if pedido not in grupos[destino]:
            grupos[destino][pedido] = {
                'numero_pedido': pedido,
                'cliente': b.tarea.cliente or '',
                'bultos': []
            }
        grupos[destino][pedido]['bultos'].append({
            'codigo_barras': b.codigo_barras,
            'tipo': b.tipo,
            'numero': b.numero,
            'total': b.total
        })

    resultado = []
    for destino, pedidos in sorted(grupos.items()):
        parada = {'destino': destino, 'pedidos': []}
        for p in pedidos.values():
            resumen = {}
            for bul in p['bultos']:
                resumen[bul['tipo']] = resumen.get(bul['tipo'], 0) + 1
            p['resumen'] = resumen
            p['total_bultos'] = len(p['bultos'])
            parada['pedidos'].append(p)
        parada['total_bultos'] = sum(p['total_bultos'] for p in parada['pedidos'])
        resultado.append(parada)

    return jsonify({
        'manifiesto': resultado,
        'fecha': hoy.isoformat(),
        'total_bultos': len(bultos)
    }), 200


@muelle_bp.route('/historial', methods=['GET'])
@jwt_required()
def historial():
    """Últimos 100 bultos cargados, para auditoría."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso para ver historial de muelle'}), 403
    bultos = (
        Bulto.query
        .options(selectinload(Bulto.tarea))
        .filter_by(estado=EstadoBulto.CARGADO)
        .order_by(Bulto.fecha_cargado.desc())
        .limit(100).all()
    )
    return jsonify({'bultos': [b.to_dict() for b in bultos]}), 200
