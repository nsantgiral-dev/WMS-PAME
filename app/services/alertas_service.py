"""
Servicio de Alertas por Email — Gobierno de Datos.

Cron diario a las 06:00 Bogotá. Revisa la tabla ubicaciones_huerfanas
y envía un email al Jefe de Bodega si hay códigos Siesa sin prefijo válido.

Variables de entorno requeridas (Railway):
  RESEND_API_KEY      → API key de resend.com (ej. re_xxxxxxxxxxxxxxxx)
  ALERTA_EMAIL_DEST   → destinatario(s), separados por coma
                        ej. "jefe@papeleria.com,bodega@papeleria.com"
  ALERTA_EMAIL_FROM   → dirección remitente verificada en Resend
                        ej. "WMS Papelería <wms@papeleriamedellin.com.co>"
                        Si no se define usa: "WMS Papelería <onboarding@resend.dev>"

Railway bloquea todo SMTP saliente (puertos 25/465/587).
Resend usa HTTPS (puerto 443) — nunca bloqueado.

Si las variables no están configuradas el cron corre en silencio (solo log).
"""
import logging
import os
import json
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Envío de email via Resend API ─────────────────────────────────────────────

def _config_resend() -> dict | None:
    """Lee env vars de Resend. Retorna None si no están configuradas."""
    api_key = os.getenv('RESEND_API_KEY', '').strip()
    dest    = os.getenv('ALERTA_EMAIL_DEST', '').strip()

    if not all([api_key, dest]):
        return None

    from_addr = os.getenv(
        'ALERTA_EMAIL_FROM',
        'WMS Papelería <onboarding@resend.dev>'
    ).strip()

    return {
        'api_key': api_key,
        'from':    from_addr,
        'dest':    [d.strip() for d in dest.split(',') if d.strip()],
    }


def enviar_email(asunto: str, cuerpo_html: str, cuerpo_texto: str) -> bool:
    """
    Envía un email via Resend API (HTTPS — no SMTP).
    Retorna True si se envió, False si faltó config o hubo error.
    """
    cfg = _config_resend()
    if not cfg:
        logger.warning('[ALERTAS] Resend no configurado — email omitido. '
                       'Agrega RESEND_API_KEY y ALERTA_EMAIL_DEST en Railway.')
        return False

    import requests as _requests

    payload = {
        'from':    cfg['from'],
        'to':      cfg['dest'],
        'subject': asunto,
        'html':    cuerpo_html,
        'text':    cuerpo_texto,
    }

    try:
        resp = _requests.post(
            'https://api.resend.com/emails',
            json=payload,
            headers={'Authorization': f'Bearer {cfg["api_key"]}'},
            timeout=15,
        )
        logger.info(f'[ALERTAS] Resend status={resp.status_code} body={resp.text[:300]}')
        if resp.status_code in (200, 201):
            data = resp.json()
            logger.info(f'[ALERTAS] Email enviado — id={data.get("id")}: {asunto}')
            return True
        raise RuntimeError(f'Resend {resp.status_code}: {resp.text}')
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f'[ALERTAS] Error enviando email via Resend: {e}')
        raise


# ── Alerta de ubicaciones huérfanas ──────────────────────────────────────────

def verificar_y_alertar_huerfanas(app=None):
    """
    Punto de entrada del cron — corre a las 06:00 Bogotá.
    Lee ubicaciones_huerfanas con veces_detectada > 1 y envía email si las hay.
    """
    from flask import current_app as _app
    ctx_app = app or _app._get_current_object()

    with ctx_app.app_context():
        from app.extensions import db as _db
        _lock = _db.session.execute(_db.text('SELECT pg_try_advisory_lock(2004)')).scalar()
        if not _lock:
            logger.info('[ALERTAS] verificar_y_alertar_huerfanas omitida — otro worker ya la ejecuta')
            return
        try:
            from app.models.ubicacion_huerfana import UbicacionHuerfana

            # Solo alertar si fueron detectadas más de una vez (persisten entre syncs)
            huerfanas = UbicacionHuerfana.query.filter(
                UbicacionHuerfana.veces_detectada > 1
            ).order_by(
                UbicacionHuerfana.veces_detectada.desc()
            ).all()

            if not huerfanas:
                logger.info('[ALERTAS] Sin ubicaciones huérfanas persistentes — no se envía email.')
                return

            logger.warning(f'[ALERTAS] {len(huerfanas)} ubicación(es) huérfana(s) — enviando email.')
            _enviar_alerta_huerfanas(huerfanas)

        except Exception as e:
            logger.error(f'[ALERTAS] Error en verificar_y_alertar_huerfanas: {e}', exc_info=True)
        finally:
            _db.session.execute(_db.text('SELECT pg_advisory_unlock(2004)'))
            _db.session.commit()


def _enviar_alerta_huerfanas(huerfanas: list):
    n = len(huerfanas)
    hoy = datetime.now().strftime('%d/%m/%Y %H:%M')
    asunto = f'⚠️ WMS Papelería Medellín — {n} ubicación{"es" if n > 1 else ""} bloqueada{"s" if n > 1 else ""} en Siesa'

    # ── Texto plano ──
    filas_txt = '\n'.join(
        f'  • {h.codigo_siesa}  (bodega {h.bodega_id}, detectada {h.veces_detectada}x, '
        f'último sync: {h.fecha_ultima_vez.strftime("%d/%m %H:%M") if h.fecha_ultima_vez else "—"})'
        for h in huerfanas
    )
    cuerpo_texto = f"""WMS Papelería Medellín — Alerta de Gobierno de Datos
Fecha: {hoy}

PROBLEMA:
{n} ubicación(es) de Siesa Enterprise tienen nombre sin prefijo válido.
El inventario almacenado allí está BLOQUEADO para el motor de reabastecimiento.
No aparecerá en picking ni en reposición hasta que se corrija.

UBICACIONES AFECTADAS:
{filas_txt}

ACCIÓN REQUERIDA:
Entrar a Siesa Enterprise → Inventarios → Maestros → Ubicaciones
Renombrar cada código usando alguno de los prefijos válidos:
  PIK-  →  zona de picking (piso)
  RES-  →  zona de reserva (estanterías altas)
  AVE-  →  zona de averías

El WMS sincronizará automáticamente esta noche a las 03:00.

Si tienes dudas escribe a sistemas o abre un ticket.
"""

    # ── HTML ──
    filas_html = ''.join(f"""
        <tr>
          <td style="padding:10px 14px;font-family:monospace;font-size:14px;
                     font-weight:700;color:#fbbf24;background:#1c1c1c;">{h.codigo_siesa}</td>
          <td style="padding:10px 14px;font-size:13px;color:#d1d5db;">{h.bodega_id}</td>
          <td style="padding:10px 14px;font-size:13px;color:#d1d5db;">{h.descripcion or '—'}</td>
          <td style="padding:10px 14px;text-align:center;font-size:13px;
                     font-weight:700;color:#f87171;">{h.veces_detectada}x</td>
          <td style="padding:10px 14px;font-size:12px;color:#6b7280;">
            {h.fecha_ultima_vez.strftime('%d/%m %H:%M') if h.fecha_ultima_vez else '—'}
          </td>
        </tr>""" for h in huerfanas)

    cuerpo_html = f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:system-ui,-apple-system,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;
                padding:20px 24px;margin-bottom:16px;border-left:4px solid #f59e0b;">
      <div style="font-size:11px;font-weight:700;color:#f59e0b;
                  text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px;">
        WMS Papelería Medellín · Gobierno de Datos · {hoy}
      </div>
      <div style="font-size:22px;font-weight:800;color:#fff;margin-bottom:8px;">
        ⚠️ {n} ubicación{"es" if n > 1 else ""} bloqueada{"s" if n > 1 else ""} en Siesa
      </div>
      <div style="font-size:14px;color:#9ca3af;line-height:1.5;">
        {"Estos códigos" if n > 1 else "Este código"} no tienen prefijo válido (<code style="color:#f59e0b;">PIK-</code>,
        <code style="color:#4ade80;">RES-</code>, <code style="color:#f87171;">AVE-</code>).
        El inventario almacenado allí <strong style="color:#f87171;">está invisible</strong>
        para el motor de reabastecimiento y picking.
      </div>
    </div>

    <!-- Tabla de huérfanas -->
    <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;
                overflow:hidden;margin-bottom:16px;">
      <table style="width:100%;border-collapse:collapse;">
        <thead>
          <tr style="background:#111;">
            <th style="padding:10px 14px;text-align:left;font-size:11px;
                       color:#6b7280;font-weight:600;text-transform:uppercase;">Código Siesa</th>
            <th style="padding:10px 14px;text-align:left;font-size:11px;
                       color:#6b7280;font-weight:600;text-transform:uppercase;">Bodega</th>
            <th style="padding:10px 14px;text-align:left;font-size:11px;
                       color:#6b7280;font-weight:600;text-transform:uppercase;">Descripción</th>
            <th style="padding:10px 14px;text-align:center;font-size:11px;
                       color:#6b7280;font-weight:600;text-transform:uppercase;">Veces</th>
            <th style="padding:10px 14px;text-align:left;font-size:11px;
                       color:#6b7280;font-weight:600;text-transform:uppercase;">Último sync</th>
          </tr>
        </thead>
        <tbody>{filas_html}
        </tbody>
      </table>
    </div>

    <!-- Acción requerida -->
    <div style="background:#1c1417;border:1px solid #7c2d12;border-radius:12px;
                padding:18px 20px;margin-bottom:16px;">
      <div style="font-size:13px;font-weight:700;color:#fbbf24;margin-bottom:10px;">
        Acción requerida en Siesa Enterprise
      </div>
      <div style="font-size:13px;color:#d1d5db;line-height:1.6;">
        <strong>Inventarios → Maestros → Ubicaciones</strong><br>
        Renombrar cada código con uno de estos prefijos:<br><br>
        <code style="background:#111;padding:2px 8px;border-radius:4px;color:#60a5fa;">PIK-</code>
        &nbsp;zona de picking (piso, unidades sueltas)<br>
        <code style="background:#111;padding:2px 8px;border-radius:4px;color:#4ade80;">RES-</code>
        &nbsp;zona de reserva (estanterías altas, pacas)<br>
        <code style="background:#111;padding:2px 8px;border-radius:4px;color:#f87171;">AVE-</code>
        &nbsp;zona de averías (productos dañados)<br><br>
        El WMS sincronizará automáticamente <strong>esta noche a las 03:00</strong>.
      </div>
    </div>

    <!-- Footer -->
    <div style="text-align:center;font-size:11px;color:#4b5563;padding:8px;">
      WMS Papelería Medellín · Alerta automática generada el {hoy}<br>
      Este email se envía solo cuando hay ubicaciones pendientes de corrección.
    </div>

  </div>
</body>
</html>"""

    enviar_email(asunto, cuerpo_html, cuerpo_texto)


# ── Alerta: Job Siesa FALLIDO ─────────────────────────────────────────────────

def alertar_job_fallido(job, app=None):
    """
    Envía email inmediato cuando un job Siesa alcanza estado=FALLIDO (3 intentos).
    Llamar desde siesa_job_service._crear_alerta_admin().
    """
    hoy = datetime.now().strftime('%d/%m/%Y %H:%M')
    asunto = f'🚨 WMS — Job Siesa FALLIDO #{job.id} ({job.tipo})'

    ref = f'{job.referencia_tipo} #{job.referencia_id}' if job.referencia_tipo else '—'
    error = (job.error_ultimo or 'Sin detalle')[:300]

    cuerpo_texto = f"""WMS Papelería Medellín — Job Siesa FALLIDO
Fecha: {hoy}

Job #{job.id} | Tipo: {job.tipo} | Referencia: {ref}
Error: {error}

El movimiento físico ya ocurrió en la bodega pero NO se registró en Siesa.
El inventario del ERP está desactualizado para este movimiento.

ACCIÓN REQUERIDA:
1. Verificar en Siesa que el período contable esté abierto
2. En el WMS admin → Reposición → Jobs Siesa → reintentar el job
3. Si sigue fallando, registrar el movimiento manualmente en Siesa

Ref interna: {ref}
"""
    cuerpo_html = f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:system-ui,sans-serif;">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">
    <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;
                padding:20px 24px;margin-bottom:16px;border-left:4px solid #ef4444;">
      <div style="font-size:11px;font-weight:700;color:#ef4444;text-transform:uppercase;
                  letter-spacing:0.08em;margin-bottom:6px;">WMS Papelería Medellín · {hoy}</div>
      <div style="font-size:22px;font-weight:800;color:#fff;margin-bottom:8px;">🚨 Job Siesa FALLIDO</div>
      <div style="font-size:14px;color:#9ca3af;">
        El movimiento físico ocurrió pero <strong style="color:#ef4444;">no se registró en Siesa</strong>.
        El inventario del ERP está desactualizado.
      </div>
    </div>
    <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;padding:18px 20px;margin-bottom:16px;">
      <table style="width:100%;font-size:13px;border-collapse:collapse;">
        <tr><td style="color:#6b7280;padding:6px 0;width:120px;">Job ID</td>
            <td style="color:#fff;font-weight:700;font-family:monospace;">#{job.id}</td></tr>
        <tr><td style="color:#6b7280;padding:6px 0;">Tipo</td>
            <td style="color:#fbbf24;font-weight:700;">{job.tipo}</td></tr>
        <tr><td style="color:#6b7280;padding:6px 0;">Referencia</td>
            <td style="color:#d1d5db;">{ref}</td></tr>
        <tr><td style="color:#6b7280;padding:6px 0;">Error</td>
            <td style="color:#f87171;font-family:monospace;font-size:12px;">{error}</td></tr>
      </table>
    </div>
    <div style="background:#1c1417;border:1px solid #7c2d12;border-radius:12px;padding:18px 20px;">
      <div style="font-size:13px;font-weight:700;color:#fbbf24;margin-bottom:8px;">Acción requerida</div>
      <div style="font-size:13px;color:#d1d5db;line-height:1.7;">
        1. Verificar que el <strong>período contable esté abierto</strong> en Siesa<br>
        2. WMS Admin → Reposición → Jobs Siesa → <strong>Reintentar</strong><br>
        3. Si sigue fallando: registrar el movimiento <strong>manualmente en Siesa</strong>
      </div>
    </div>
  </div>
</body></html>"""

    try:
        enviar_email(asunto, cuerpo_html, cuerpo_texto)
    except Exception as e:
        logger.error(f'[ALERTAS] No se pudo enviar alerta job fallido: {e}')


# ── Alerta: Stock crítico sin reserva ─────────────────────────────────────────

def verificar_y_alertar_stock_critico(app=None):
    """
    Cron diario a las 07:00 Bogotá.
    Detecta ubicaciones PICKING bajo mínimo sin LPN disponible en RESERVA.
    Esos productos necesitan reabastecimiento desde compras — el motor no puede actuar.
    """
    from flask import current_app as _app
    ctx_app = app or _app._get_current_object()

    with ctx_app.app_context():
        from app.extensions import db as _db
        _lock = _db.session.execute(_db.text('SELECT pg_try_advisory_lock(2005)')).scalar()
        if not _lock:
            logger.info('[ALERTAS] verificar_y_alertar_stock_critico omitida — otro worker ya la ejecuta')
            return
        try:
            from app.models.ubicacion import Ubicacion
            from app.models.inventario import UbicacionProducto
            from app.models.lpn import LPN
            from app.models.producto import Producto
            from sqlalchemy import func as _func

            # [27] Consolidar en queries bulk para evitar N+1 triple anidado
            picking_ubs = Ubicacion.query.filter(
                Ubicacion.tipo_zona == 'PICKING',
                Ubicacion.stock_minimo.isnot(None),
                Ubicacion.activo == True,
            ).all()

            if not picking_ubs:
                logger.info('[ALERTAS] Sin ubicaciones PICKING con stock mínimo configurado.')
                return

            picking_ub_ids = [ub.id for ub in picking_ubs]
            picking_ub_map = {ub.id: ub for ub in picking_ubs}

            # Stock disponible por (ubicacion_id, producto_id) en una sola query
            inv_rows = UbicacionProducto.query.filter(
                UbicacionProducto.ubicacion_id.in_(picking_ub_ids)
            ).all()

            # Candidatos bajo mínimo
            candidatos = []
            for i in inv_rows:
                ub = picking_ub_map[i.ubicacion_id]
                stock = (i.cantidad or 0) - (getattr(i, 'reservado', 0) or 0)
                if stock < ub.stock_minimo:
                    candidatos.append((ub, i.producto_id, stock))

            if not candidatos:
                logger.info('[ALERTAS] Sin stock crítico sin reserva — no se envía email.')
                return

            # Productos con LPN en RESERVA — una sola query para todos los candidatos
            candidatos_almacen = {(ub.almacen_id, prod_id) for ub, prod_id, _ in candidatos}
            almacen_ids_cands = list({a for a, _ in candidatos_almacen})
            prod_ids_cands = list({p for _, p in candidatos_almacen})
            lpns_reserva_rows = (
                LPN.query
                .join(Ubicacion, Ubicacion.id == LPN.ubicacion_id)
                .filter(
                    LPN.producto_id.in_(prod_ids_cands),
                    LPN.almacen_id.in_(almacen_ids_cands),
                    LPN.estado == 'ACTIVO',
                    Ubicacion.tipo_zona == 'RESERVA',
                )
                .with_entities(LPN.almacen_id, LPN.producto_id)
                .all()
            )
            lpns_en_reserva = {(a, p) for a, p in lpns_reserva_rows}

            # Pre-cargar productos en bulk
            prod_ids = list({prod_id for _, prod_id, _ in candidatos})
            prods_map = {p.id: p for p in Producto.query.filter(Producto.id.in_(prod_ids)).all()}

            criticos = []
            for ub, prod_id, stock in candidatos:
                if (ub.almacen_id, prod_id) in lpns_en_reserva:
                    continue  # Motor puede actuar — no alertar
                prod = prods_map.get(prod_id)
                criticos.append({
                    'ubicacion': ub.codigo,
                    'producto_codigo': prod.codigo if prod else str(prod_id),
                    'producto_nombre': prod.nombre if prod else '—',
                    'stock_actual': stock,
                    'stock_minimo': ub.stock_minimo,
                })

            if not criticos:
                logger.info('[ALERTAS] Sin stock crítico sin reserva — no se envía email.')
                return

            logger.warning(f'[ALERTAS] {len(criticos)} slot(s) críticos sin reserva.')
            _enviar_alerta_stock_critico(criticos)

        except Exception as e:
            logger.error(f'[ALERTAS] Error en verificar_y_alertar_stock_critico: {e}', exc_info=True)
        finally:
            _db.session.execute(_db.text('SELECT pg_advisory_unlock(2005)'))
            _db.session.commit()


def _enviar_alerta_stock_critico(criticos: list):
    n = len(criticos)
    hoy = datetime.now().strftime('%d/%m/%Y %H:%M')
    asunto = f'⚠️ WMS — {n} producto{"s" if n > 1 else ""} crítico{"s" if n > 1 else ""} sin stock en RESERVA'

    filas_txt = '\n'.join(
        f'  • {c["ubicacion"]} | {c["producto_codigo"]} {c["producto_nombre"]} '
        f'— stock {c["stock_actual"]} / mín {c["stock_minimo"]}'
        for c in criticos
    )
    cuerpo_texto = f"""WMS Papelería Medellín — Stock Crítico Sin Reserva
Fecha: {hoy}

{n} ubicación(es) PICKING están bajo el stock mínimo y NO HAY
stock en RESERVA para reabastecer. El motor de reposición no puede actuar.
Se requiere orden de compra o traslado de mercancía.

PRODUCTOS AFECTADOS:
{filas_txt}

ACCIÓN REQUERIDA:
Compras debe ordenar o trasladar mercancía para estos SKUs.
"""
    filas_html = ''.join(f"""
      <tr>
        <td style="padding:10px 14px;font-family:monospace;font-size:13px;color:#fbbf24;">{c['ubicacion']}</td>
        <td style="padding:10px 14px;font-size:12px;color:#60a5fa;">{c['producto_codigo']}</td>
        <td style="padding:10px 14px;font-size:13px;color:#d1d5db;">{c['producto_nombre'][:40]}</td>
        <td style="padding:10px 14px;text-align:center;font-weight:700;color:#f87171;">{c['stock_actual']}</td>
        <td style="padding:10px 14px;text-align:center;color:#6b7280;">{c['stock_minimo']}</td>
      </tr>""" for c in criticos)

    cuerpo_html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:system-ui,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;">
    <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;
                padding:20px 24px;margin-bottom:16px;border-left:4px solid #f59e0b;">
      <div style="font-size:11px;font-weight:700;color:#f59e0b;text-transform:uppercase;
                  letter-spacing:0.08em;margin-bottom:6px;">WMS Papelería Medellín · {hoy}</div>
      <div style="font-size:22px;font-weight:800;color:#fff;margin-bottom:8px;">
        ⚠️ {n} producto{"s" if n > 1 else ""} crítico{"s" if n > 1 else ""} — sin reserva
      </div>
      <div style="font-size:14px;color:#9ca3af;">
        El motor de reposición <strong style="color:#ef4444;">no puede actuar</strong>.
        Se requiere orden de compra o traslado de mercancía.
      </div>
    </div>
    <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;overflow:hidden;margin-bottom:16px;">
      <table style="width:100%;border-collapse:collapse;">
        <thead><tr style="background:#111;">
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#6b7280;">Ubicación</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#6b7280;">Código</th>
          <th style="padding:10px 14px;text-align:left;font-size:11px;color:#6b7280;">Producto</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#6b7280;">Stock</th>
          <th style="padding:10px 14px;text-align:center;font-size:11px;color:#6b7280;">Mín</th>
        </tr></thead>
        <tbody>{filas_html}</tbody>
      </table>
    </div>
    <div style="text-align:center;font-size:11px;color:#4b5563;padding:8px;">
      WMS Papelería Medellín · Alerta automática · {hoy}
    </div>
  </div>
</body></html>"""

    enviar_email(asunto, cuerpo_html, cuerpo_texto)


# ── Resumen operativo diario ──────────────────────────────────────────────────

def enviar_resumen_diario(app=None):
    """
    Cron diario a las 08:00 Bogotá.
    Consolida lo que pasó ayer: pedidos, packing, reposición, jobs Siesa.
    """
    from flask import current_app as _app
    ctx_app = app or _app._get_current_object()

    with ctx_app.app_context():
        from app.extensions import db as _db
        _lock = _db.session.execute(_db.text('SELECT pg_try_advisory_lock(2006)')).scalar()
        if not _lock:
            logger.info('[ALERTAS] enviar_resumen_diario omitido — otro worker ya lo ejecuta')
            return
        try:
            from datetime import date, timedelta
            from app.extensions import db
            from sqlalchemy import func

            ayer_inicio = datetime.combine(date.today() - timedelta(days=1),
                                           datetime.min.time())
            ayer_fin    = datetime.combine(date.today(), datetime.min.time())

            # Pedidos despachados ayer (picking completado)
            try:
                from app.models.picking import PedidoPicking
                pedidos_despachados = PedidoPicking.query.filter(
                    PedidoPicking.estado == 'COMPLETADO',
                    PedidoPicking.fecha_completado >= ayer_inicio,
                    PedidoPicking.fecha_completado < ayer_fin,
                ).count()
            except Exception:
                pedidos_despachados = 'N/D'

            # Bultos empacados ayer
            try:
                from app.models.bulto import Bulto
                bultos_empacados = Bulto.query.filter(
                    Bulto.fecha_creacion >= ayer_inicio,
                    Bulto.fecha_creacion < ayer_fin,
                ).count()
            except Exception:
                bultos_empacados = 'N/D'

            # Tareas reposición completadas ayer
            try:
                from app.models.tarea_reposicion import TareaReposicion
                tareas_ok = TareaReposicion.query.filter(
                    TareaReposicion.estado == 'COMPLETADA',
                    TareaReposicion.fecha_completada >= ayer_inicio,
                    TareaReposicion.fecha_completada < ayer_fin,
                ).count()
            except Exception:
                tareas_ok = 'N/D'

            # Jobs Siesa ayer
            try:
                from app.models.siesa_job import SiesaJob
                jobs_ok      = SiesaJob.query.filter(
                    SiesaJob.estado == 'COMPLETADO',
                    SiesaJob.fecha_creacion >= ayer_inicio,
                    SiesaJob.fecha_creacion < ayer_fin,
                ).count()
                jobs_fallidos = SiesaJob.query.filter(
                    SiesaJob.estado == 'FALLIDO',
                    SiesaJob.fecha_creacion >= ayer_inicio,
                    SiesaJob.fecha_creacion < ayer_fin,
                ).count()
            except Exception:
                jobs_ok = jobs_fallidos = 'N/D'

            ayer_str = (date.today() - timedelta(days=1)).strftime('%d/%m/%Y')
            _enviar_resumen_diario(ayer_str, pedidos_despachados, bultos_empacados,
                                   tareas_ok, jobs_ok, jobs_fallidos)

        except Exception as e:
            logger.error(f'[ALERTAS] Error en enviar_resumen_diario: {e}', exc_info=True)
        finally:
            _db.session.execute(_db.text('SELECT pg_advisory_unlock(2006)'))
            _db.session.commit()


def _enviar_resumen_diario(fecha, pedidos, bultos, tareas_rep, jobs_ok, jobs_fallidos):
    hoy = datetime.now().strftime('%d/%m/%Y %H:%M')
    asunto = f'📊 WMS Papelería Medellín — Resumen operativo {fecha}'

    alerta_jobs = jobs_fallidos not in ('N/D', 0) and jobs_fallidos > 0

    cuerpo_texto = f"""WMS Papelería Medellín — Resumen Operativo {fecha}

Pedidos despachados:      {pedidos}
Bultos empacados:         {bultos}
Reposiciones completadas: {tareas_rep}
Transferencias Siesa OK:  {jobs_ok}
Transferencias fallidas:  {jobs_fallidos}{'  ← REVISAR' if alerta_jobs else ''}

Generado: {hoy}
"""
    def _stat(valor, alerta=False):
        color = '#ef4444' if alerta else '#22c55e' if valor not in ('N/D', 0) else '#6b7280'
        return f'<span style="font-size:28px;font-weight:800;color:{color};">{valor}</span>'

    cuerpo_html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f0f0f;font-family:system-ui,sans-serif;">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">
    <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;
                padding:20px 24px;margin-bottom:16px;border-left:4px solid #6366f1;">
      <div style="font-size:11px;font-weight:700;color:#818cf8;text-transform:uppercase;
                  letter-spacing:0.08em;margin-bottom:6px;">WMS Papelería Medellín · Resumen diario</div>
      <div style="font-size:22px;font-weight:800;color:#fff;">📊 Operación del {fecha}</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;">
      <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;padding:18px 20px;">
        <div style="font-size:12px;color:#6b7280;margin-bottom:6px;">Pedidos despachados</div>
        {_stat(pedidos)}
      </div>
      <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;padding:18px 20px;">
        <div style="font-size:12px;color:#6b7280;margin-bottom:6px;">Bultos empacados</div>
        {_stat(bultos)}
      </div>
      <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;padding:18px 20px;">
        <div style="font-size:12px;color:#6b7280;margin-bottom:6px;">Reposiciones RESERVA→PICKING</div>
        {_stat(tareas_rep)}
      </div>
      <div style="background:#1c1c1c;border:1px solid #333;border-radius:12px;padding:18px 20px;
                  {'border-color:#7c2d12;' if alerta_jobs else ''}">
        <div style="font-size:12px;color:#6b7280;margin-bottom:6px;">Jobs Siesa</div>
        <div>{_stat(jobs_ok)} <span style="font-size:13px;color:#6b7280;">OK</span>
        {'&nbsp;&nbsp;' + _stat(jobs_fallidos, alerta=True) + ' <span style="font-size:13px;color:#ef4444;">fallidos</span>' if isinstance(jobs_fallidos, int) and jobs_fallidos > 0 else ''}</div>
      </div>
    </div>
    <div style="text-align:center;font-size:11px;color:#4b5563;padding:8px;">
      WMS Papelería Medellín · Resumen automático generado el {hoy}
    </div>
  </div>
</body></html>"""

    enviar_email(asunto, cuerpo_html, cuerpo_texto)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def init_scheduler(app):
    """
    Crons diarios:
      06:00 → alerta ubicaciones huérfanas
      07:00 → alerta stock crítico sin reserva
      08:00 → resumen operativo diario
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[ALERTAS] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')

    scheduler.add_job(
        func=verificar_y_alertar_huerfanas,
        trigger=CronTrigger(hour=6, minute=0, timezone='America/Bogota'),
        kwargs={'app': app},
        id='alertas_huerfanas_email',
        name='Alerta email ubicaciones huérfanas (06:00 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=600,
    )
    scheduler.add_job(
        func=verificar_y_alertar_stock_critico,
        trigger=CronTrigger(hour=7, minute=0, timezone='America/Bogota'),
        kwargs={'app': app},
        id='alertas_stock_critico',
        name='Alerta email stock crítico sin reserva (07:00 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=600,
    )
    scheduler.add_job(
        func=enviar_resumen_diario,
        trigger=CronTrigger(hour=8, minute=0, timezone='America/Bogota'),
        kwargs={'app': app},
        id='resumen_operativo_diario',
        name='Resumen operativo diario (08:00 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=600,
    )

    scheduler.start()
    logger.info('[ALERTAS] Scheduler iniciado — 06:00 huérfanas | 07:00 stock crítico | 08:00 resumen')
    return scheduler
