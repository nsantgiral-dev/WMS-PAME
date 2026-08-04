"""
Endpoints del Vigía CUSUM — monitoreo de corrimientos operativos.

POST /api/vigia/cargar-txt         — carga export TXT de ventas Siesa
POST /api/vigia/ejecutar/<serie>   — corre CUSUM sobre una serie
GET  /api/vigia/alarmas            — lista alarmas abiertas
POST /api/vigia/alarmas/<id>/cerrar — cierra alarma con causa + responsable
GET  /api/vigia/series             — lista series disponibles con stats
GET  /api/vigia/backtest/florencia — test canónico C.O. 006
"""
import logging
import os
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_admin_o_jefe, _es_gestion, _get_uid
from app.extensions import db

logger = logging.getLogger(__name__)

vigia_bp = Blueprint('vigia', __name__)


@vigia_bp.route('/cargar-txt', methods=['POST'])
@jwt_required()
def cargar_txt():
    """Carga un export TXT de facturación de Siesa — herramienta de backfill admin.

    Bloqueada por defecto: requiere VIGIA_CARGAR_TXT=true. NO es un camino
    operativo recurrente, es el único cargador de la línea base histórica
    (26 semanas de mu_ref/sigma_ref) que Connekta no puede alimentar porque
    solo produce datos hacia adelante.

    Ciclo de vida previsto: se habilita para la carga inicial, se verifica con
    verificar_carga_historica(), y se devuelve a false. Se conserva habilitable
    para backfills puntuales, siempre con registro de auditoría.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin/gerente puede cargar datos'}), 403

    # Guard: bloqueado salvo override explícito
    if not os.environ.get('VIGIA_CARGAR_TXT', '').lower() == 'true':
        return jsonify({
            'error': 'Carga manual de TXT deshabilitada en este entorno. '
                     'Es una herramienta de backfill, no un camino operativo. '
                     'Para habilitar temporalmente: VIGIA_CARGAR_TXT=true'
        }), 403

    if 'archivo' not in request.files:
        return jsonify({'error': 'Se requiere archivo TXT'}), 400

    file = request.files['archivo']
    if not file.filename:
        return jsonify({'error': 'Nombre de archivo vacío'}), 400

    uid = _get_uid()

    # Guardar temporalmente
    tmp_path = f'/tmp/vigia_{file.filename}'
    file.save(tmp_path)

    # Cadena de custodia: el hash identifica el archivo exacto que produjo la
    # línea base. Es lo que permite despues distinguir un fallo de tuberia de
    # un cambio de insumos en la prueba de reproduccion.
    from app.services.vigia_service import VigiaService, sha256_archivo
    try:
        sha = sha256_archivo(tmp_path)
    except OSError:
        sha = 'no-calculable'
    logger.warning('[VIGIA_BACKFILL] usuario_id=%s archivo=%s sha256=%s — carga manual habilitada',
                   uid, file.filename, sha)

    try:
        resultado = VigiaService.cargar_ventas_desde_txt(tmp_path)
    except Exception as e:
        logger.exception('[VIGIA_BACKFILL] usuario_id=%s archivo=%s FALLO: %s',
                         uid, file.filename, e)
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    logger.warning('[VIGIA_BACKFILL] usuario_id=%s archivo=%s sha256=%s OK — %s series nuevas, %s registros',
                   uid, file.filename, sha, resultado.get('series_creadas'),
                   resultado.get('registros_procesados'))

    return jsonify({'ok': True, 'sha256': sha, **resultado}), 200


@vigia_bp.route('/ejecutar/<nombre_serie>', methods=['POST'])
@jwt_required()
def ejecutar_cusum(nombre_serie):
    """Ejecuta CUSUM tabular bilateral sobre una serie."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin/gerente puede ejecutar CUSUM'}), 403

    from app.services.vigia_service import VigiaService
    resultado = VigiaService.ejecutar_cusum(nombre_serie)

    if 'error' in resultado:
        return jsonify(resultado), 400

    return jsonify(resultado), 200


@vigia_bp.route('/alarmas', methods=['GET'])
@jwt_required()
def listar_alarmas():
    """Lista alarmas, opcionalmente filtradas por serie y estado."""
    # Cerrar una alarma es SILENCIAR EL DETECTOR. El endpoint tenía el guard
    # anti-silencio (causa de 20 chars mínimo) y no tenía guard de QUIÉN: el
    # autor pensó en que no se cerrara sin explicación, no en quién podía.
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para ver alarmas del Vigía'}), 403
    serie = request.args.get('serie')
    solo_abiertas = request.args.get('solo_abiertas', 'true').lower() != 'false'

    from app.services.vigia_service import VigiaService
    alarmas = VigiaService.listar_alarmas(serie, solo_abiertas)
    return jsonify({'alarmas': alarmas, 'total': len(alarmas)}), 200


@vigia_bp.route('/alarmas/<int:alarma_id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_alarma(alarma_id):
    """Cierra una alarma con causa + responsable (anti-silencio: min 20 chars)."""
    # Cerrar una alarma es SILENCIAR EL DETECTOR. El endpoint tenía el guard
    # anti-silencio (causa de 20 chars mínimo) y no tenía guard de QUIÉN: el
    # autor pensó en que no se cerrara sin explicación, no en quién podía.
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para cerrar alarmas del Vigía'}), 403
    uid = _get_uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401

    data = request.get_json() or {}
    causa = data.get('causa', '')
    # responsable_id = quien queda dueño del plan (no necesariamente quien cierra)
    responsable_id = data.get('responsable_id')

    # Para ALARMA, responsable explícito es obligatorio — sin fallback
    from app.services.vigia_service import AlarmaVigia
    alarma_check = db.session.get(AlarmaVigia, alarma_id)
    if alarma_check and alarma_check.severidad == 'ALARMA' and not responsable_id:
        return jsonify({'error': 'ALARMA requiere responsable_id explícito — quien queda dueño del plan'}), 400

    # AVISO puede usar el usuario que cierra como fallback
    if not responsable_id:
        responsable_id = uid

    from app.services.vigia_service import VigiaService
    try:
        resultado = VigiaService.cerrar_alarma(alarma_id, causa, responsable_id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    return jsonify(resultado), 200


@vigia_bp.route('/series', methods=['GET'])
@jwt_required()
def listar_series():
    """Lista series disponibles con estadísticas básicas."""
    # Cerrar una alarma es SILENCIAR EL DETECTOR. El endpoint tenía el guard
    # anti-silencio (causa de 20 chars mínimo) y no tenía guard de QUIÉN: el
    # autor pensó en que no se cerrara sin explicación, no en quién podía.
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para ver las series del Vigía'}), 403
    from app.services.vigia_service import SerieVigia
    from app.extensions import db
    from sqlalchemy import func

    stats = (
        db.session.query(
            SerieVigia.serie,
            func.count(SerieVigia.id).label('semanas'),
            func.min(SerieVigia.semana).label('desde'),
            func.max(SerieVigia.semana).label('hasta'),
        )
        .group_by(SerieVigia.serie)
        .all()
    )

    return jsonify({
        'series': [{
            'serie': s.serie,
            'semanas': s.semanas,
            'desde': s.desde.isoformat() if s.desde else None,
            'hasta': s.hasta.isoformat() if s.hasta else None,
        } for s in stats],
    }), 200


@vigia_bp.route('/resumen', methods=['GET'])
@jwt_required()
def resumen_vigia():
    """Resumen para el panel: series agrupadas por CO + alarmas abiertas."""
    # Cerrar una alarma es SILENCIAR EL DETECTOR. El endpoint tenía el guard
    # anti-silencio (causa de 20 chars mínimo) y no tenía guard de QUIÉN: el
    # autor pensó en que no se cerrara sin explicación, no en quién podía.
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para ver el panel del Vigía'}), 403
    from app.services.vigia_service import SerieVigia, AlarmaVigia
    from app.extensions import db
    from sqlalchemy import func

    # Series agrupadas por CO con stats
    stats = (
        db.session.query(
            SerieVigia.serie,
            func.count(SerieVigia.id).label('semanas'),
            func.min(SerieVigia.semana).label('desde'),
            func.max(SerieVigia.semana).label('hasta'),
        )
        .group_by(SerieVigia.serie)
        .all()
    )

    # Organizar por CO con jerarquía: facturas > despachos > facturacion
    cos = {}
    for s in stats:
        nombre = s.serie
        # Extraer tipo y CO del nombre (ej: 'facturas_006' → tipo='facturas', co='006')
        parts = nombre.split('_', 1)
        if len(parts) == 2:
            tipo, co = parts
        else:
            tipo, co = nombre, '???'

        if co not in cos:
            cos[co] = {}
        cos[co][tipo] = {
            'serie': nombre,
            'semanas': s.semanas,
            'desde': s.desde.isoformat() if s.desde else None,
            'hasta': s.hasta.isoformat() if s.hasta else None,
        }

    # Alarmas abiertas
    alarmas = AlarmaVigia.query.filter_by(cerrada=False).order_by(
        AlarmaVigia.semana.desc()).all()

    alarmas_por_sev = {'ALARMA': 0, 'AVISO': 0}
    alarmas_list = []
    for a in alarmas:
        sev = a.severidad or 'ALARMA'
        alarmas_por_sev[sev] = alarmas_por_sev.get(sev, 0) + 1
        alarmas_list.append({
            'id': a.id,
            'serie': a.serie,
            'semana': a.semana.isoformat(),
            'tipo': a.tipo,
            'severidad': sev,
            's_valor': float(a.s_valor),
        })

    return jsonify({
        'cos': cos,
        'alarmas': alarmas_list,
        'alarmas_resumen': alarmas_por_sev,
        'total_series': len(stats),
    }), 200


@vigia_bp.route('/salud', methods=['GET'])
@jwt_required()
def salud_conectores():
    """G0: Latencia de conectores. Bandera roja si >24h sin datos."""
    # Cerrar una alarma es SILENCIAR EL DETECTOR. El endpoint tenía el guard
    # anti-silencio (causa de 20 chars mínimo) y no tenía guard de QUIÉN: el
    # autor pensó en que no se cerrara sin explicación, no en quién podía.
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para ver la salud de conectores'}), 403
    from app.services.vigia_service import VigiaService
    resultado = VigiaService.salud_conectores()
    return jsonify(resultado), 200


@vigia_bp.route('/alimentar-picking', methods=['POST'])
@jwt_required()
def alimentar_picking():
    """Alimenta la serie adopcion_picking + brecha_picking desde tareas de picking."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin puede alimentar series'}), 403
    from app.services.vigia_service import VigiaService
    resultado = VigiaService.alimentar_adopcion_picking()
    return jsonify({'ok': True, **resultado}), 200


@vigia_bp.route('/verificar-carga', methods=['GET'])
@jwt_required()
def verificar_carga():
    """Arnés de verificación de la carga histórica — las tres pruebas.

    Valores de referencia como query params (opcionales):
      ?semanas=53&cos=6&semana_alarma=2025-12-29&s_minus=6.30

    Sin ellos, la prueba 2 reporta lo observado y queda pendiente en vez de
    inventar un canon.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin/gerente puede verificar la carga'}), 403

    def _int(n):
        v = request.args.get(n)
        return int(v) if v else None

    def _float(n):
        v = request.args.get(n)
        return float(v) if v else None

    from app.services.vigia_service import verificar_carga_historica
    resultado = verificar_carga_historica(
        semanas=_int('semanas'),
        cos=_int('cos'),
        semana_alarma=request.args.get('semana_alarma'),
        s_minus=_float('s_minus'),
    )
    return jsonify(resultado), 200


@vigia_bp.route('/backtest/florencia', methods=['POST'])
@jwt_required()
def backtest_florencia():
    """Test canónico: CUSUM sobre C.O. 006 (Florencia)."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin/gerente puede ejecutar backtest'}), 403

    from app.services.vigia_service import VigiaService
    resultado = VigiaService.backtest_florencia()
    return jsonify(resultado), 200
