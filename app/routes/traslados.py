from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.traslado import SolicitudTraslado
from app.models.usuario import Usuario
from app.services.traslado_service import TrasladoService

traslados_bp = Blueprint('traslados', __name__)


@traslados_bp.route('/', methods=['GET'])
@jwt_required()
def listar_solicitudes():
    """Lista solicitudes — admin ve todas, tienda solo las suyas."""
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)

    estado = request.args.get('estado')
    page = request.args.get('page', 1, type=int)

    query = SolicitudTraslado.query.order_by(SolicitudTraslado.fecha_creacion.desc())

    if usuario and usuario.rol == 'tienda':
        query = query.filter_by(solicitante_id=usuario_id)
    if estado:
        query = query.filter_by(estado=estado)

    pag = query.paginate(page=page, per_page=30, error_out=False)
    return jsonify({
        'solicitudes': [s.to_dict() for s in pag.items],
        'total': pag.total,
        'pagina': page,
    }), 200


@traslados_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_solicitud(id):
    s = SolicitudTraslado.query.get_or_404(id)
    return jsonify(s.to_dict()), 200


@traslados_bp.route('/', methods=['POST'])
@jwt_required()
def crear_solicitud():
    """Tienda crea solicitud en BORRADOR."""
    usuario_id = int(get_jwt_identity())
    data = request.get_json() or {}

    usuario = Usuario.query.get(usuario_id)
    bodega_destino = data.get('bodega_destino_siesa') or (usuario.bodega_siesa_id if usuario else None)
    nombre_pv = data.get('nombre_punto_venta') or (usuario.nombre_punto_venta if usuario else None)

    if not bodega_destino:
        return jsonify({'error': 'bodega_destino_siesa es requerida (o configurar en perfil de usuario)'}), 400
    if not data.get('items'):
        return jsonify({'error': 'items es requerido'}), 400

    try:
        s = TrasladoService.crear_solicitud(
            solicitante_id=usuario_id,
            bodega_destino=bodega_destino,
            nombre_punto_venta=nombre_pv,
            items=data['items'],
            observaciones=data.get('observaciones'),
        )
        return jsonify(s.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/enviar', methods=['POST'])
@jwt_required()
def enviar_solicitud(id):
    try:
        s = TrasladoService.enviar_solicitud(id)
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/<int:id>/aprobar', methods=['POST'])
@jwt_required()
def aprobar_solicitud(id):
    usuario_id = int(get_jwt_identity())
    data = request.get_json() or {}
    try:
        s = TrasladoService.aprobar_solicitud(
            solicitud_id=id,
            aprobador_id=usuario_id,
            items_aprobados=data.get('items_aprobados')
        )
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/rechazar', methods=['POST'])
@jwt_required()
def rechazar_solicitud(id):
    usuario_id = int(get_jwt_identity())
    data = request.get_json() or {}
    motivo = data.get('motivo', 'Sin motivo especificado')
    try:
        s = TrasladoService.rechazar_solicitud(id, usuario_id, motivo)
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/<int:id>/despachar', methods=['POST'])
@jwt_required()
def despachar(id):
    try:
        s = TrasladoService.despachar(id)
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/recibir', methods=['POST'])
@jwt_required()
def confirmar_recepcion(id):
    usuario_id = int(get_jwt_identity())
    data = request.get_json() or {}
    try:
        s = TrasladoService.confirmar_recepcion(
            solicitud_id=id,
            usuario_id=usuario_id,
            items_recibidos=data.get('items_recibidos')
        )
        return jsonify(s.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/<int:id>/reintentar-siesa', methods=['POST'])
@jwt_required()
def reintentar_siesa(id):
    """Admin: reintenta la llamada a Siesa 174646 sin volver a aprobar.
    ?debug=true devuelve el payload sin llamar a Siesa."""
    from app.models.traslado import SolicitudTraslado
    from app.extensions import db
    s = SolicitudTraslado.query.get_or_404(id)
    body = request.get_json(silent=True) or {}
    debug = body.get('debug', False) or request.args.get('debug', '').lower() == 'true'

    items_payload = [
        {
            'codigo_siesa': item.producto_codigo_siesa,
            'codigo': item.producto.codigo if item.producto else '',
            'cantidad': item.cantidad_aprobada or item.cantidad_solicitada,
            'unidad_medida': item.producto.unidad_medida if item.producto else '',
        }
        for item in s.items if (item.cantidad_aprobada or item.cantidad_solicitada)
    ]

    from app.services.connekta_gateway import connekta

    if debug:
        # Construir payload sin llamar a Siesa — solo para inspección
        from datetime import datetime as _dt
        fecha_hoy = _dt.utcnow().strftime('%Y%m%d')
        payload_preview = {
            'Inicial': [{'F_CIA': connekta.id_cia_siesa}],
            'Documentos': [{
                'F_CIA': connekta.id_cia_siesa,
                'f440_id_co': connekta.centro_op,
                'f440_id_tipo_docto': connekta.tipo_docto_req_traslado,
                'f440_id_solicitante': connekta.req_solicitante,
                'f440_fecha': fecha_hoy,
                'f440_fecha_entrega': fecha_hoy,
                'f440_id_bodega_salida': s.bodega_origen_siesa,
                'f440_id_bodega_entrada': s.bodega_destino_siesa,
            }],
            'Movimientos': [
                {
                    'F_CIA': connekta.id_cia_siesa,
                    'f441_id_co': connekta.centro_op,
                    'f441_id_tipo_docto': connekta.tipo_docto_req_traslado,
                    'f441_consec_docto': 0,
                    'f441_nro_registro': idx + 1,
                    'f441_id_item': '',
                    'f441_referencia_item': item.get('codigo_siesa') or item.get('codigo'),
                    'f441_codigo_barras': '',
                    'f441_id_ext1_detalle': '',
                    'f441_id_ext2_detalle': '',
                    'f441_id_bodega': s.bodega_origen_siesa,
                    'f441_id_motivo': connekta.motivo_traslado,
                    'f441_id_unidad_medida': item.get('unidad_medida') or '',
                    'f441_cant_base': f'{abs(item.get("cantidad", 0)):020.4f}',
                    'f441_cant_2': f'{0:020.4f}',
                    'f441_fecha_entrega': fecha_hoy,
                    'f441_num_dias_entrega': 0,
                    'f441_id_co_movto': connekta.centro_op,
                    'f441_id_ccosto_movto': '',
                    'f441_id_proyecto': '',
                    'f441_notas': '',
                    'f441_id_un_movto': connekta.centro_op,
                    'f441_precio_unitario': f'{0:020.4f}',
                    'f441_id_ubicacion_sal': '',
                    'f441_id_proy_etapa': '',
                    'f441_id_rubro_pof': '',
                }
                for idx, item in enumerate(items_payload)
            ],
            'Final': [{'F_CIA': connekta.id_cia_siesa}],
        }
        return jsonify({'debug': True, 'payload': payload_preview, 'items': items_payload}), 200

    try:
        from app.services.traslado_service import TrasladoService
        res = connekta.crear_requisicion_traslado(
            bodega_origen=s.bodega_origen_siesa,
            bodega_destino=s.bodega_destino_siesa,
            items=items_payload,
            codigo_solicitud=s.codigo
        )
        if not res.get('simulado') and not res.get('modo_ensayo'):
            consec = TrasladoService._extraer_consec(res)
            if consec:
                s.siesa_requisicion_consec = consec
        s.siesa_error = None
        db.session.commit()
        return jsonify({'ok': True, 'siesa_response': res, 'solicitud': s.to_dict()}), 200
    except Exception as e:
        s.siesa_error = f'174646: {str(e)}'
        db.session.commit()
        return jsonify({'error': str(e)}), 400


@traslados_bp.route('/stock-disponible', methods=['GET'])
@jwt_required()
def stock_disponible():
    """Stock disponible en bodega principal para armar solicitud."""
    bodega = request.args.get('bodega')
    try:
        resultado = TrasladoService.get_stock_disponible(bodega)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@traslados_bp.route('/bodegas-siesa', methods=['GET'])
@jwt_required()
def bodegas_siesa():
    """Lista bodegas configuradas en Siesa (para seleccionar punto de venta)."""
    try:
        resultado = TrasladoService.get_bodegas_disponibles()
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
