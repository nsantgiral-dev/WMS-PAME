"""
Servicio de Alertas por Email — Gobierno de Datos.

Cron diario a las 06:00 Bogotá. Revisa la tabla ubicaciones_huerfanas
y envía un email al Jefe de Bodega si hay códigos Siesa sin prefijo válido.

Variables de entorno requeridas (Railway):
  ALERTA_EMAIL_DEST   → destinatario(s), separados por coma
                        ej. "jefe@papeleria.com,bodega@papeleria.com"
  SMTP_HOST           → servidor SMTP (ej. smtp.gmail.com)
  SMTP_PORT           → puerto (587 para TLS, 465 para SSL, 25 sin cifrado)
  SMTP_USER           → usuario / dirección remitente
  SMTP_PASS           → contraseña o App Password
  SMTP_FROM           → dirección "De:" (opcional, usa SMTP_USER si no se define)

Si las variables no están configuradas el cron corre en silencio (solo log).
Eso permite desplegarlo en Railway sin romper nada — se activa cuando
el jefe de infraestructura agrega las variables.
"""
import logging
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


# ── Envío de email ────────────────────────────────────────────────────────────

def _config_smtp() -> dict | None:
    """Lee env vars de SMTP. Retorna None si no están configuradas."""
    host = os.getenv('SMTP_HOST', '').strip()
    user = os.getenv('SMTP_USER', '').strip()
    password = os.getenv('SMTP_PASS', '').strip()
    dest = os.getenv('ALERTA_EMAIL_DEST', '').strip()

    if not all([host, user, password, dest]):
        return None

    return {
        'host': host,
        'port': int(os.getenv('SMTP_PORT', '587')),
        'user': user,
        'password': password,
        'from': os.getenv('SMTP_FROM', user).strip(),
        'dest': [d.strip() for d in dest.split(',') if d.strip()],
    }


def enviar_email(asunto: str, cuerpo_html: str, cuerpo_texto: str) -> bool:
    """
    Envía un email via SMTP.
    Detecta automáticamente el modo según el puerto:
      465 → SSL directo (cPanel/Banahosting)
      587 → STARTTLS (Gmail, Outlook)
      25  → sin cifrado (solo redes internas)
    Retorna True si se envió, False si faltó config o hubo error.
    """
    cfg = _config_smtp()
    if not cfg:
        logger.warning('[ALERTAS] SMTP no configurado — email omitido. '
                       'Agrega SMTP_HOST, SMTP_USER, SMTP_PASS, ALERTA_EMAIL_DEST en Railway.')
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = asunto
    msg['From'] = cfg['from']
    msg['To'] = ', '.join(cfg['dest'])

    msg.attach(MIMEText(cuerpo_texto, 'plain', 'utf-8'))
    msg.attach(MIMEText(cuerpo_html,  'html',  'utf-8'))

    try:
        context = ssl.create_default_context()
        if cfg['port'] == 465:
            # SSL directo — cPanel/Banahosting
            with smtplib.SMTP_SSL(cfg['host'], cfg['port'], context=context) as srv:
                srv.login(cfg['user'], cfg['password'])
                srv.sendmail(cfg['from'], cfg['dest'], msg.as_string())
        else:
            # STARTTLS — puerto 587 (Gmail, Outlook) o 25
            with smtplib.SMTP(cfg['host'], cfg['port']) as srv:
                srv.ehlo()
                if cfg['port'] != 25:
                    srv.starttls(context=context)
                srv.login(cfg['user'], cfg['password'])
                srv.sendmail(cfg['from'], cfg['dest'], msg.as_string())

        logger.info(f'[ALERTAS] Email enviado a {cfg["dest"]}: {asunto}')
        return True
    except Exception as e:
        logger.error(f'[ALERTAS] Error enviando email (host={cfg["host"]} port={cfg["port"]}): {e}')
        return False


# ── Alerta de ubicaciones huérfanas ──────────────────────────────────────────

def verificar_y_alertar_huerfanas(app=None):
    """
    Punto de entrada del cron — corre a las 06:00 Bogotá.
    Lee ubicaciones_huerfanas con veces_detectada > 1 y envía email si las hay.
    """
    from flask import current_app as _app
    ctx_app = app or _app._get_current_object()

    with ctx_app.app_context():
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


# ── Scheduler ─────────────────────────────────────────────────────────────────

def init_scheduler(app):
    """Cron diario a las 06:00 hora Bogotá — después del sync nocturno (03:00)."""
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
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=600,
    )
    scheduler.start()
    logger.info('[ALERTAS] Scheduler iniciado — alerta email 06:00 Bogotá')
    return scheduler
