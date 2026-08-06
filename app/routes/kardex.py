"""
Endpoints del Kardex transaccional — reconstrucción de stock diario.

POST /api/kardex/descargar          — descarga movimientos de Siesa
POST /api/kardex/reconstruir        — reconstruye stock diario hacia atrás
GET  /api/kardex/tasa-censurada     — velocity censurada por SKU
GET  /api/kardex/stock-diario       — stock reconstruido por referencia+bodega
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
import logging

from app.routes._auth_helpers import _es_admin_o_jefe, _es_compras, _get_uid

logger = logging.getLogger(__name__)

kardex_bp = Blueprint('kardex', __name__)


_kardex_descarga_estado = {'en_curso': False, 'resultado': None}

@kardex_bp.route('/descargar', methods=['POST', 'GET'])
@jwt_required()
def descargar_kardex():
    """Lanza descarga de kardex en background thread (no bloquea HTTP)."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede descargar kardex'}), 403

    if _kardex_descarga_estado['en_curso']:
        return jsonify({'ok': True, 'mensaje': 'Descarga ya en curso — revisar logs'}), 200

    if request.method == 'GET':
        fecha_desde = request.args.get('fecha_desde', '20240101')
        fecha_hasta = request.args.get('fecha_hasta')
        pagina_inicial = request.args.get('pagina_inicial', 1, type=int)
        max_minutos = request.args.get('max_minutos', None, type=int)
    else:
        data = request.get_json() or {}
        fecha_desde = data.get('fecha_desde', '20240101')
        fecha_hasta = data.get('fecha_hasta')
        pagina_inicial = data.get('pagina_inicial', 1)
        max_minutos = data.get('max_minutos')

    from flask import current_app
    app = current_app._get_current_object()

    import threading
    def _run():
        _kardex_descarga_estado['en_curso'] = True
        _kardex_descarga_estado['resultado'] = None
        try:
            with app.app_context():
                from app.services.kardex_service import KardexService
                resultado = KardexService.descargar_kardex(
                    fecha_desde, fecha_hasta,
                    pagina_inicial=pagina_inicial, max_minutos=max_minutos)
                _kardex_descarga_estado['resultado'] = resultado
        except Exception as e:
            _kardex_descarga_estado['resultado'] = {'error': str(e)}
        finally:
            _kardex_descarga_estado['en_curso'] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({
        'ok': True,
        'mensaje': ('Descarga iniciada en segundo plano. El resultado dice si quedó '
                    'COMPLETA o PARCIAL — una parcial es un fallo, no un aviso.'),
        'fecha_desde': fecha_desde,
        'pagina_inicial': pagina_inicial,
        'aviso_operativo': (
            'Son ~17.000 peticiones contra el ERP que factura en los puntos de venta. '
            'Correr FUERA DE HORARIO y avisando antes, no después de que alguien no '
            'pueda facturar a media manana.'
        ),
    }), 200


@kardex_bp.route('/descargar/estado', methods=['GET'])
@jwt_required()
def estado_descarga():
    """Verifica si la descarga está en curso o terminó."""
    return jsonify({
        'en_curso': _kardex_descarga_estado['en_curso'],
        'resultado': _kardex_descarga_estado['resultado'],
    }), 200


@kardex_bp.route('/reconstruir', methods=['POST'])
@jwt_required()
def reconstruir_stock():
    """Reconstruye stock diario hacia atrás desde saldo actual - movimientos.

    DENY-BY-DEFAULT: se NIEGA si la última descarga no quedó COMPLETA.

    Una advertencia se ignora bajo presión de agenda; un rechazo con override
    auditado no. Es el mismo patrón del script de reset, aplicado al eslabón
    donde equivocarse es más barato: reconstruir sobre un kardex truncado
    fabrica días sin movimiento que sí lo tuvieron — demanda censurada
    inventada por el descargador, que contamina justo el modelo que existe
    para corregir la censura.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede reconstruir stock'}), 403

    data = request.get_json() or {}
    bodega = data.get('bodega')
    forzar = bool(data.get('forzar'))

    ultima = _kardex_descarga_estado.get('resultado')
    if _kardex_descarga_estado.get('en_curso'):
        return jsonify({
            'error': 'Hay una descarga en curso. Reconstruir ahora usaría datos a medias.',
        }), 409

    # Sin descarga en esta sesión no se puede afirmar que el kardex esté completo.
    # Regla 0: ante estado desconocido, no seguir.
    if not forzar and (not ultima or ultima.get('ok') is not True):
        estado = (ultima or {}).get('estado', 'DESCONOCIDO')
        return jsonify({
            'error': 'RECHAZADO — la última descarga no quedó COMPLETA.',
            'estado_descarga': estado,
            'por_que': (
                'Reconstruir sobre un kardex truncado inventa días sin movimiento. '
                'Eso es demanda censurada fabricada por el descargador, y contamina '
                'la descensura, el ROP y la temporada sin una sola alarma.'
            ),
            'que_hacer': (
                'Completar la descarga (reanudar desde la página indicada) y revisar '
                'el perfil mensual: ningún mes en cero ni anómalamente bajo.'
            ),
            'override': 'Reenviar con {"forzar": true} si se asume el riesgo. Queda auditado.',
        }), 409

    if forzar:
        logger.warning(
            '[KARDEX_RECONSTRUIR] OVERRIDE usuario_id=%s — estado de descarga: %s. '
            'Se reconstruye sobre un kardex posiblemente incompleto.',
            _get_uid(), (ultima or {}).get('estado', 'DESCONOCIDO'),
        )

    from app.services.kardex_service import KardexService, perfil_mensual_kardex
    try:
        resultado = KardexService.reconstruir_stock_diario(bodega)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({
        'ok': True, 'forzado': forzar,
        'perfil_mensual': perfil_mensual_kardex(),
        **resultado,
    }), 200


@kardex_bp.route('/perfil-mensual', methods=['GET'])
@jwt_required()
def perfil_mensual():
    """Filas por mes — el histograma que delata el hueco que el rango esconde."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver el perfil'}), 403
    from app.services.kardex_service import perfil_mensual_kardex
    return jsonify(perfil_mensual_kardex()), 200


@kardex_bp.route('/reconciliar', methods=['GET'])
@jwt_required()
def reconciliar():
    """COMPUERTA: cruza kardex vs facturación para verificar completitud."""
    # `_es_compras()` y no `_es_admin_o_jefe()`: la compuerta la lee el semáforo
    # de la pantalla Modelos, que abre el rol `compras`. Es lectura pura, y quien
    # firma la compra tiene derecho a saber si los modelos son confiables.
    if not _es_compras():
        return jsonify({'error': 'Sin permiso para ver la compuerta del kardex'}), 403
    meses = request.args.get('meses', 12, type=int)
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.reconciliar_kardex(meses)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/tasa-servida-corregida', methods=['GET'])
@jwt_required()
def tasa_servida_corregida():
    """Tasa servida corregida: demanda neta / días con stock.
    nivel=bodega para reposición, nivel=red para clasificación S-B."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver tasa servida'}), 403
    meses = request.args.get('meses', 12, type=int)
    nivel = request.args.get('nivel', 'bodega')  # 'bodega' o 'red'
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.calcular_tasa_servida_corregida(meses, nivel)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/clasificacion-sb', methods=['GET'])
@jwt_required()
def clasificacion_sb():
    """Clasificación Syntetos-Boylan a nivel de RED.
    Lee estacionales de tabla ABC (rol=ESTACIONAL). Override adicional
    con ?estacionales_extra=REF1,REF2 para etiquetado provisional."""
    if not _es_compras():
        return jsonify({'error': 'Solo admin puede clasificar'}), 403
    meses = request.args.get('meses', 12, type=int)
    est_raw = request.args.get('estacionales_extra', '')
    extras = [x.strip() for x in est_raw.split(',') if x.strip()] if est_raw else []
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.clasificar_syntetos_boylan(meses, extras)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/stock-diario', methods=['GET'])
@jwt_required()
def stock_diario():
    """Stock reconstruido por referencia + bodega."""
    # Lectura: es la evidencia del «+18% por 62 días sin stock» que la fila de
    # Reposición ya muestra. Negársela a quien decide la compra sería mostrarle
    # el número y esconderle de dónde salió.
    if not _es_compras():
        return jsonify({'error': 'Sin permiso para ver stock diario'}), 403
    ref = request.args.get('referencia')
    bod = request.args.get('bodega')
    if not ref:
        return jsonify({'error': 'referencia es requerido'}), 400
    from app.services.kardex_service import StockDiario
    query = StockDiario.query.filter_by(referencia=ref)
    if bod:
        query = query.filter_by(bodega=bod)
    registros = query.order_by(StockDiario.fecha.desc()).limit(365).all()
    return jsonify({
        'referencia': ref,
        'bodega': bod or 'todas',
        'dias': [{
            'fecha': r.fecha.isoformat(),
            'bodega': r.bodega,
            'stock_cierre': float(r.stock_cierre),
            'tuvo_stock': r.tuvo_stock,
        } for r in registros],
    }), 200


@kardex_bp.route('/pronostico-tsb', methods=['GET'])
@jwt_required()
def pronostico_tsb():
    """TSB (Teunter-Syntetos-Babai) para demanda intermitente/grumosa.
    Spec §2.M0.3: TSB debe ganar a media móvil 8 sem en MASE."""
    if not _es_compras():
        return jsonify({'error': 'Solo admin puede ver pronósticos'}), 403
    meses = request.args.get('meses', 12, type=int)
    alpha = request.args.get('alpha', 0.15, type=float)
    from app.services.kardex_service import KardexService
    try:
        resultado = KardexService.pronostico_tsb(meses, alpha)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/newsvendor', methods=['POST'])
@jwt_required()
def newsvendor():
    """Newsvendor: cantidad óptima de compra para temporada escolar.
    DEADLINE: 7 de agosto 2026."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede calcular newsvendor'}), 403
    data = request.get_json() or {}
    items = data.get('items', [])
    margen = data.get('margen_pct', 0.40)
    costo_exceso = data.get('costo_exceso_pct', 0.60)
    from app.services.kardex_service import KardexService
    resultado = KardexService.newsvendor(items, margen, costo_exceso)
    if 'error' in resultado:
        return jsonify(resultado), 400
    return jsonify(resultado), 200


@kardex_bp.route('/temporada/pedido', methods=['GET'])
@jwt_required()
def pedido_temporada():
    """Q* del pedido escolar — el llamador del newsvendor.

    A diferencia de POST /newsvendor (calculadora pura que recibe items), este
    identifica los SKUs de temporada solo, trae su demanda descensurada,
    excluye lista negra y costo fantasma, y reporta qué fracción de la decisión
    alcanza a cubrir el modelo.

    DEADLINE: comité del 7 de agosto 2026.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver el pedido de temporada'}), 403

    margen = request.args.get('margen_pct', 0.40, type=float)
    capital = request.args.get('tasa_capital', 0.30, type=float)
    liquidacion = request.args.get('tasa_liquidacion', 0.60, type=float)
    umbral = request.args.get('umbral', 0.40, type=float)

    from app.services.temporada_service import TemporadaService
    try:
        resultado = TemporadaService.preparar_pedido_temporada(
            margen_pct=margen, tasa_capital=capital,
            tasa_liquidacion=liquidacion, umbral=umbral)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(resultado), 200


@kardex_bp.route('/temporada/juicios', methods=['GET'])
@jwt_required()
def listar_juicios():
    """Lista paralela registrada para una temporada."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede ver juicios'}), 403
    from app.models.juicio_temporada import JuicioTemporada
    temporada = request.args.get('temporada', '2026-27')
    js = JuicioTemporada.query.filter_by(temporada=temporada).all()
    return jsonify({
        'temporada': temporada,
        'juicios': {j.referencia: j.to_dict() for j in js},
        'total': len(js),
    }), 200


@kardex_bp.route('/temporada/juicios', methods=['POST'])
@jwt_required()
def guardar_juicio():
    """Registra un juicio humano — NO es un cálculo del sistema.

    Guarda el Q* del modelo del momento junto al juicio: sin esa foto,
    comparar en enero de 2027 sería contra un modelo que ya cambió.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede registrar juicios'}), 403

    from app.models.juicio_temporada import JuicioTemporada
    from app.extensions import db

    d = request.get_json() or {}
    ref = (d.get('referencia') or '').strip()
    if not ref:
        return jsonify({'error': 'referencia es requerida'}), 400

    temporada = d.get('temporada', '2026-27')
    cantidad = d.get('cantidad_juicio')

    j = JuicioTemporada.query.filter_by(temporada=temporada, referencia=ref).first()

    # Cantidad nula = borrar el juicio (el comité se retractó)
    if cantidad is None or cantidad == '':
        if j:
            db.session.delete(j)
            db.session.commit()
        return jsonify({'ok': True, 'borrado': True}), 200

    try:
        cantidad = int(cantidad)
    except (TypeError, ValueError):
        return jsonify({'error': 'cantidad_juicio debe ser entero'}), 400

    if j is None:
        j = JuicioTemporada(temporada=temporada, referencia=ref)
        db.session.add(j)

    j.cantidad_juicio = cantidad
    j.autor_id = _get_uid()
    j.autor_nombre = d.get('autor_nombre') or j.autor_nombre
    j.nota = d.get('nota') or j.nota
    # Foto del modelo en el momento del juicio
    if d.get('q_modelo') is not None:
        j.q_modelo = d.get('q_modelo')
    if d.get('costo_unitario') is not None:
        j.costo_unitario = d.get('costo_unitario')
    if d.get('distribucion'):
        j.distribucion = d.get('distribucion')

    db.session.commit()
    return jsonify({'ok': True, 'juicio': j.to_dict()}), 200


@kardex_bp.route('/probar-paginacion', methods=['POST'])
@jwt_required()
def probar_paginacion():
    """Dos peticiones para saber si el orden de la consulta es estable.

    Cuesta dos llamadas en vez de diecisiete mil, y responde la pregunta de la
    que depende que reanudar sea seguro.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede probar paginación'}), 403
    d = request.get_json() or {}
    from app.services.kardex_service import KardexService
    try:
        r = KardexService.probar_estabilidad_paginacion(
            pagina=int(d.get('pagina', 50)), espera_s=int(d.get('espera_s', 90)))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(r), 200
