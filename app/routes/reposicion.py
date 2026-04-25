"""
Rutas de Reposición — Abastecedor + Admin.

/api/reposicion/tarea-actual          GET   → próxima tarea para el abastecedor
/api/reposicion/mis-tareas            GET   → todas las tareas activas del abastecedor
/api/reposicion/confirmar             POST  → romper paca y abastecer zona PICKING
/api/reposicion/verificar-stock       POST  → dispara verificación manual (admin)
/api/reposicion/pendientes            GET   → admin — todas las tareas PENDIENTE/EN_PROCESO
/api/reposicion/cancelar/<id>         POST  → admin — cancelar tarea

/api/reposicion/sync-ubicaciones      POST  → dispara sync nocturno desde Siesa (admin)
/api/reposicion/sync-ubicaciones/estado GET → estado del último sync
/api/siesa/debug-ubicaciones-raw      GET   → raw de la API 43 para certificar campos
"""

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.inventario import UbicacionProducto
from app.models.producto import Producto
from app.models.siesa_job import SiesaJob
from app.models.tarea_reposicion import TareaReposicion
from app.models.ubicacion import Ubicacion
from app.models.ubicacion_huerfana import UbicacionHuerfana
from app.models.usuario import Usuario
from app.routes._auth_helpers import _es_admin_o_jefe
from app.services.alertas_service import enviar_email, _config_resend
from app.services.connekta_gateway import connekta
from app.services.ola_predictiva_service import pre_verificar_ola as _verificar
from app.services.reposicion_service import confirmar_reposicion, get_tarea_abastecedor, get_tareas_abastecedor, verificar_stock_picking
from app.services.siesa_job_service import get_jobs_fallidos, reintentar_job as _reintentar
from app.services.ubicaciones_sync_service import get_estado, sync_ubicaciones_desde_siesa

reposicion_bp = Blueprint('reposicion', __name__)


# ──────────────────────────────────────────────────────────────────────────────
# Abastecedor — Mobile
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/tarea-actual', methods=['GET'])
@jwt_required()
def tarea_actual():
    """Próxima tarea de reposición para el abastecedor."""
    from app.models.usuario import Usuario
    abastecedor_id = int(get_jwt_identity())
    u = Usuario.query.get(abastecedor_id)
    if not u or (not u.puede_abastecer and u.rol not in ('admin', 'supervisor', 'jefe_almacen')):
        return jsonify({'error': 'Sin permiso — se requiere permiso de abastecedor'}), 403
    tarea = get_tarea_abastecedor(abastecedor_id)
    if not tarea:
        return jsonify({'sin_tareas': True, 'mensaje': 'No hay reposiciones pendientes'}), 200
    return jsonify(tarea), 200


@reposicion_bp.route('/mis-tareas', methods=['GET'])
@jwt_required()
def mis_tareas():
    """Todas las tareas activas del abastecedor."""
    from app.models.usuario import Usuario
    abastecedor_id = int(get_jwt_identity())
    u = Usuario.query.get(abastecedor_id)
    if not u or (not u.puede_abastecer and u.rol not in ('admin', 'supervisor', 'jefe_almacen')):
        return jsonify({'error': 'Sin permiso — se requiere permiso de abastecedor'}), 403
    tareas = get_tareas_abastecedor(abastecedor_id)
    return jsonify({'tareas': tareas, 'total': len(tareas)}), 200


@reposicion_bp.route('/confirmar', methods=['POST'])
@jwt_required()
def confirmar():
    """
    Confirma la reposición — "rompe la paca".

    Payload: { tarea_id, lpn_codigo (opcional — para validar escaneo) }
    """
    from app.models.usuario import Usuario
    abastecedor_id = int(get_jwt_identity())
    u = Usuario.query.get(abastecedor_id)
    if not u or (not u.puede_abastecer and u.rol not in ('admin', 'supervisor', 'jefe_almacen')):
        return jsonify({'error': 'Sin permiso — se requiere permiso de abastecedor'}), 403
    data = request.get_json() or {}

    tarea_id = data.get('tarea_id')
    if not tarea_id:
        return jsonify({'error': 'tarea_id es requerido'}), 400

    try:
        resultado = confirmar_reposicion(
            tarea_id=tarea_id,
            abastecedor_id=abastecedor_id,
            lpn_codigo_escaneado=data.get('lpn_codigo'),
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Admin
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/verificar-stock', methods=['POST'])
@jwt_required()
def verificar_stock():
    """Fuerza una verificación de stock en todas las zonas PICKING."""
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in ('admin', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin o jefe de almacén puede verificar stock'}), 403
    data = request.get_json() or {}
    almacen_id = data.get('almacen_id')
    try:
        generadas = verificar_stock_picking(almacen_id=almacen_id)
        return jsonify({'ok': True, 'tareas_generadas': generadas}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reposicion_bp.route('/pendientes', methods=['GET'])
@jwt_required()
def pendientes():
    """Lista tareas filtradas por estado (admin / jefe de almacén)."""
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in ('admin', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin o jefe de almacén puede ver todas las tareas'}), 403
    estado = request.args.get('estado', '').upper()
    estados_validos = {'PENDIENTE', 'EN_PROCESO', 'COMPLETADA', 'CANCELADA'}

    if estado in estados_validos:
        q = TareaReposicion.query.filter(TareaReposicion.estado == estado)
    else:
        q = TareaReposicion.query.filter(
            TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        )
    tareas = q.order_by(TareaReposicion.fecha_creacion.desc()).limit(100).all()
    return jsonify({'tareas': [t.to_dict() for t in tareas], 'total': len(tareas)}), 200


@reposicion_bp.route('/cancelar/<int:tarea_id>', methods=['POST'])
@jwt_required()
def cancelar(tarea_id):
    """Cancela una tarea de reposición (admin)."""
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in ('admin', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin o jefe de almacén puede cancelar tareas'}), 403
    data = request.get_json() or {}

    tarea = TareaReposicion.query.get(tarea_id)
    if not tarea:
        return jsonify({'error': f'Tarea {tarea_id} no encontrada'}), 404
    if tarea.estado in ('COMPLETADA', 'CANCELADA'):
        return jsonify({'error': f'Tarea ya está en estado {tarea.estado}'}), 400

    tarea.estado = 'CANCELADA'
    tarea.notas = (tarea.notas or '') + f' | Cancelada: {data.get("motivo", "sin motivo")}'
    db.session.commit()
    return jsonify({'ok': True, 'tarea': tarea.to_dict()}), 200


# ──────────────────────────────────────────────────────────────────────────────
# Sync ubicaciones desde Siesa
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/sync-ubicaciones', methods=['POST'])
@jwt_required()
def sync_ubicaciones():
    """
    Dispara la sincronización de ubicaciones desde Siesa (API_v2_Ubicaciones ID 43).
    Corre en hilo de fondo — retorna inmediatamente.
    """
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in ('admin', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin o jefe de almacén puede disparar el sync de ubicaciones'}), 403
    data = request.get_json() or {}
    bodega_id = data.get('bodega_id')
    try:
        resultado = sync_ubicaciones_desde_siesa(
            app=current_app._get_current_object(),
            bodega_id=bodega_id,
            en_hilo=True,
        )
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reposicion_bp.route('/sync-ubicaciones/estado', methods=['GET'])
@jwt_required()
def sync_ubicaciones_estado():
    return jsonify(get_estado()), 200


@reposicion_bp.route('/ubicacion/<int:ubicacion_id>/limites', methods=['PATCH'])
@jwt_required()
def configurar_limites(ubicacion_id):
    """
    Configura stock_minimo, stock_maximo y secuencia_ruteo de una ubicación PICKING.

    Siesa solo expone código + descripción + estado en la API.
    Los límites de reabastecimiento son configuración local del WMS.
    El jefe de bodega los ajusta aquí.

    Payload: { stock_minimo, stock_maximo, secuencia_ruteo (opcional) }
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén pueden configurar límites'}), 403

    ub = Ubicacion.query.get(ubicacion_id)
    if not ub:
        return jsonify({'error': f'Ubicación {ubicacion_id} no encontrada'}), 404

    data = request.get_json() or {}
    try:
        if 'stock_minimo' in data:
            ub.stock_minimo = int(data['stock_minimo']) if data['stock_minimo'] is not None else None
        if 'stock_maximo' in data:
            ub.stock_maximo = int(data['stock_maximo']) if data['stock_maximo'] is not None else None
        if 'secuencia_ruteo' in data:
            ub.secuencia_ruteo = int(data['secuencia_ruteo']) if data['secuencia_ruteo'] is not None else None
    except (TypeError, ValueError) as e:
        return jsonify({'error': f'Valor numérico inválido: {e}'}), 400

    db.session.commit()
    return jsonify({'ok': True, 'ubicacion': ub.to_dict()}), 200


@reposicion_bp.route('/ubicaciones', methods=['GET'])
@jwt_required()
def listar_ubicaciones_picking():
    """
    Lista ubicaciones PICKING con sus límites configurados.
    El admin ve aquí qué zonas tienen min/max y cuáles faltan por configurar.
    """

    almacen_id = request.args.get('almacen_id', type=int)
    q = Ubicacion.query.filter(Ubicacion.tipo_zona == 'PICKING', Ubicacion.activo == True)
    if almacen_id:
        q = q.filter(Ubicacion.almacen_id == almacen_id)

    ubicaciones = q.order_by(Ubicacion.codigo).all()

    resultado = []
    for ub in ubicaciones:
        inventarios = UbicacionProducto.query.filter_by(ubicacion_id=ub.id).all()
        stock_actual = sum((i.cantidad or 0) for i in inventarios)
        d = ub.to_dict()
        d['stock_actual'] = stock_actual
        d['productos_count'] = len(inventarios)
        d['limites_configurados'] = ub.stock_minimo is not None and ub.stock_maximo is not None

        # SKU dominante (el que tiene más stock en esta ubicación)
        if inventarios:
            inv_top = max(inventarios, key=lambda i: i.cantidad or 0)
            prod = Producto.query.get(inv_top.producto_id)
            d['sku_asignado'] = {
                'id': inv_top.producto_id,
                'codigo': prod.codigo if prod else None,
                'nombre': prod.nombre if prod else None,
            }
        else:
            d['sku_asignado'] = None

        resultado.append(d)

    sin_limites = sum(1 for u in resultado if not u['limites_configurados'])
    return jsonify({
        'ubicaciones': resultado,
        'total': len(resultado),
        'sin_limites': sin_limites,
        'advertencia': f'{sin_limites} ubicaciones PICKING sin límites — no generarán tareas de reposición' if sin_limites else None,
    }), 200


# ──────────────────────────────────────────────────────────────────────────────
# Ubicaciones Huérfanas — cuarentena Fail-Fast
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/ubicaciones-huerfanas', methods=['GET'])
@jwt_required()
def ubicaciones_huerfanas():
    """Lista ubicaciones en cuarentena (prefijo inválido detectado en sync Siesa)."""
    items = UbicacionHuerfana.query.order_by(
        UbicacionHuerfana.veces_detectada.desc(),
        UbicacionHuerfana.fecha_ultima_vez.desc(),
    ).all()
    return jsonify({'huerfanas': [h.to_dict() for h in items], 'total': len(items)}), 200


# ──────────────────────────────────────────────────────────────────────────────
# Dead Letter Queue — admin
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/siesa-jobs/fallidos', methods=['GET'])
@jwt_required()
def jobs_fallidos():
    """
    Alerta roja — jobs que fallaron 3 veces y necesitan intervención manual.
    El admin verifica el periodo contable en Siesa y luego reintenta.
    """
    from app.models.usuario import Usuario
    uid = int(get_jwt_identity())
    u = Usuario.query.get(uid)
    if not u or u.rol not in ('admin', 'supervisor', 'jefe_almacen'):
        return jsonify({'error': 'Sin permiso'}), 403
    jobs = get_jobs_fallidos()
    return jsonify({
        'total': len(jobs),
        'jobs': [j.to_dict() for j in jobs],
        'alerta': f'{len(jobs)} jobs fallidos — verificar periodo contable en Siesa' if jobs else None,
    }), 200


@reposicion_bp.route('/siesa-jobs/<int:job_id>/reintentar', methods=['POST'])
@jwt_required()
def reintentar_job(job_id):
    """Admin fuerza un reintento de un job FALLIDO."""
    from app.models.usuario import Usuario
    uid = int(get_jwt_identity())
    u = Usuario.query.get(uid)
    if not u or u.rol not in ('admin', 'supervisor', 'jefe_almacen'):
        return jsonify({'error': 'Sin permiso — solo admin/supervisor/jefe_almacen puede reintentar jobs'}), 403
    try:
        resultado = _reintentar(job_id)
        return jsonify({'ok': True, 'job': resultado}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@reposicion_bp.route('/siesa-jobs', methods=['GET'])
@jwt_required()
def listar_jobs():
    """Lista todos los jobs (filtrable por estado)."""
    estado = request.args.get('estado')
    q = SiesaJob.query.order_by(SiesaJob.fecha_creacion.desc())
    if estado:
        q = q.filter(SiesaJob.estado == estado.upper())
    jobs = q.limit(100).all()
    return jsonify({'jobs': [j.to_dict() for j in jobs], 'total': len(jobs)}), 200


# ──────────────────────────────────────────────────────────────────────────────
# Ola Predictiva — admin / endpoint de pre-verificación manual
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/pre-verificar-ola', methods=['POST'])
@jwt_required()
def pre_verificar_ola():
    """
    Pre-verificación de ola — cruza demanda vs stock PICKING y pre-dispara reposición.

    Útil para llamar manualmente antes de liberar una ola de pedidos grande.
    PickingService.crear_tareas() lo llama automáticamente en cada tarea.

    Payload: { almacen_id, items: [{ producto_id, cantidad }, ...] }
    """
    data = request.get_json() or {}
    items = data.get('items', [])
    almacen_id = data.get('almacen_id')

    if not items or not almacen_id:
        return jsonify({'error': 'items y almacen_id son requeridos'}), 400

    try:
        resultado = _verificar(items=items, almacen_id=almacen_id)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@reposicion_bp.route('/debug-ubicaciones-raw', methods=['GET'])
@jwt_required()
def debug_ubicaciones_raw():
    """
    Descarga la primera página de API_v2_Ubicaciones sin procesar.
    Usar para certificar nombres exactos de campos antes del primer sync real.
    """
    bodega_id = request.args.get('bodega_id', connekta.bodega)
    try:
        resp = connekta.get_ubicaciones_siesa(bodega_id=bodega_id, pagina=1)
        rows = resp.get('detalle', {}).get('Table', [])
        return jsonify({
            'total_filas': len(rows),
            'primera_fila': rows[0] if rows else None,
            'muestra_5': rows[:5],
            'raw': resp,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Alertas — admin
# ──────────────────────────────────────────────────────────────────────────────

@reposicion_bp.route('/alertas/test-email', methods=['POST'])
@jwt_required()
def test_alerta_email():
    """Envía un email de prueba via Resend. Muestra el error real si falla."""
    usuario_id = int(get_jwt_identity())
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in ('admin', 'jefe_almacen'):
        return jsonify({'error': 'Solo admin o jefe de almacén puede enviar emails de prueba'}), 403
    from datetime import datetime

    if not _config_resend():
        return jsonify({'ok': False, 'error': 'Resend no configurado — agrega RESEND_API_KEY y ALERTA_EMAIL_DEST en Railway'}), 400

    # Usar huérfanas reales si las hay
    huerfanas_reales = UbicacionHuerfana.query.limit(3).all()
    if huerfanas_reales:
        from app.services.alertas_service import _enviar_alerta_huerfanas
        try:
            _enviar_alerta_huerfanas(huerfanas_reales)
            return jsonify({'ok': True, 'mensaje': f'Email enviado con {len(huerfanas_reales)} huérfana(s) real(es)'}), 200
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    # Sin huérfanas → email de prueba
    asunto = '✅ WMS Papelería Medellín — Email de prueba'
    cuerpo_texto = (
        'Este es un email de prueba del WMS de Papelería Medellín.\n\n'
        'Si recibes este mensaje, Resend está configurado correctamente.\n\n'
        'El cron de alertas corre todos los días a las 06:00 Bogotá.'
    )
    cuerpo_html = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;background:#0f0f0f;color:#e5e7eb;padding:24px;">
  <div style="max-width:560px;margin:0 auto;background:#1c1c1c;border:1px solid #333;
              border-radius:12px;padding:24px;border-left:4px solid #22c55e;">
    <div style="font-size:11px;color:#4ade80;font-weight:700;text-transform:uppercase;
                letter-spacing:0.08em;margin-bottom:8px;">WMS Papelería Medellín · {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>
    <div style="font-size:22px;font-weight:800;margin-bottom:12px;">✅ Email de prueba recibido</div>
    <div style="font-size:14px;color:#9ca3af;line-height:1.6;">
      Resend está configurado correctamente en Railway.<br><br>
      El cron corre todos los días a las <strong style="color:#fff;">06:00 hora Bogotá</strong>
      y enviará alertas cuando haya ubicaciones con prefijo inválido en Siesa.
    </div>
  </div>
</body></html>"""

    try:
        enviar_email(asunto, cuerpo_html, cuerpo_texto)
        return jsonify({'ok': True, 'mensaje': 'Email de prueba enviado — revisa la bandeja'}), 200
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@reposicion_bp.route('/alertas/smtp-check', methods=['GET'])
@jwt_required()
def smtp_check():
    """Diagnóstico de configuración Resend — no envía email."""
    import os
    api_key = os.getenv('RESEND_API_KEY', '')
    dest    = os.getenv('ALERTA_EMAIL_DEST', '')
    from_   = os.getenv('ALERTA_EMAIL_FROM', 'WMS Papelería <onboarding@resend.dev>')

    return jsonify({
        'vars': {
            'RESEND_API_KEY':    '✅ configurado' if api_key else '❌ no configurado',
            'ALERTA_EMAIL_DEST': dest or '❌ no configurado',
            'ALERTA_EMAIL_FROM': from_,
        },
        'listo': bool(api_key and dest),
    }), 200
