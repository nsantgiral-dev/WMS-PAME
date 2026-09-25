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
from app.utils.fecha import dia_operativo as _dia_operativo

logger = logging.getLogger(__name__)


# ── Envío de email via Resend API ─────────────────────────────────────────────

#: La inyecta Railway en cada proceso: `QA` o `production`. No se configura a
#: mano — por eso sirve para distinguir, y por eso no hay default que inventar.
_VAR_AMBIENTE = 'RAILWAY_ENVIRONMENT_NAME'


def prefijo_ambiente() -> str:
    """El prefijo que lleva el asunto de TODO correo que sale de este proceso.

    QA y producción tienen la **misma** `ALERTA_EMAIL_DEST`. Sin esto, una
    alerta de QA llega idéntica a una de producción y la bandeja no dice cuál
    es cuál: o se atiende un problema que no existe, o —peor— se aprende a
    ignorar el asunto y el día que es de producción tampoco se abre.

    - `production` → `''`. Producción no se marca: es lo que se espera leer.
    - ausente (local, tests, un script) → `''`. Regla 0: no se inventa un
      ambiente que nadie declaró.
    - cualquier otro nombre → `'[<nombre>] '`, tal cual lo da Railway.

    **Se aplica en un solo sitio: `enviar_email`.** Ningún llamador lo pone a
    mano — un prefijo que depende de que cada alerta se acuerde es un prefijo
    que la alerta nueva no lleva. Trinquete:
    `tests/test_alertas_prefijo_ambiente.py`.
    """
    nombre = (os.getenv(_VAR_AMBIENTE) or '').strip()
    if not nombre or nombre.lower() == 'production':
        return ''
    return f'[{nombre}] '


def _config_resend(dest_override: str | None = None) -> dict | None:
    """Lee env vars de Resend. Retorna None si no están configuradas.

    `dest_override` permite que un remitente distinto declare SUS destinatarios
    en vez de heredar la lista global. Ver `enviar_email`.
    """
    api_key = os.getenv('RESEND_API_KEY', '').strip()
    dest    = (dest_override or os.getenv('ALERTA_EMAIL_DEST', '')).strip()

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


def enviar_email(asunto: str, cuerpo_html: str, cuerpo_texto: str,
                 dest: str | None = None) -> bool:
    """
    Envía un email via Resend API (HTTPS — no SMTP).
    Retorna True si se envió, False si faltó config o hubo error.

    ## `dest` — quién lo recibe, y por qué hizo falta el parámetro

    Hasta el 2026-09-04 los destinatarios salían **siempre** de
    `ALERTA_EMAIL_DEST`, la lista global cuyo destinatario documentado es el
    Jefe de Bodega (ver el encabezado de este módulo). Con cuatro alertas de
    inventario eso era correcto: todas van a la misma persona.

    El reporte semanal de flota va a otras dos —control de flota y quien decide
    el gasto—, y sin este parámetro se habría mandado **con éxito, a quien no
    es**: `resp.status_code == 200`, log de «email enviado», y el reporte en la
    bandeja equivocada durante meses. Es la falla que devuelve algo
    indistinguible del éxito.

    `dest=None` conserva el comportamiento de los cuatro llamadores existentes
    —heredan la lista global— y **no es un default peligroso**: la lista global
    es la respuesta correcta para ellos, no un relleno. Quien necesite otra la
    declara; quien no pase nada, sigue igual.

    Coma-separado, igual formato que la variable de entorno.
    """
    cfg = _config_resend(dest)
    if not cfg:
        logger.warning('[ALERTAS] Resend no configurado — email omitido. '
                       'Agrega RESEND_API_KEY y ALERTA_EMAIL_DEST en Railway.')
        return False

    import requests as _requests

    # El prefijo va acá y en ningún otro lado: es la única puerta a Resend.
    asunto = prefijo_ambiente() + asunto

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


def _enviar_email_con_dlq(asunto: str, cuerpo_html: str, cuerpo_texto: str,
                          tipo_alerta: str, dest: str | None = None):
    """
    Wrapper sobre enviar_email que encola un SiesaJob(ALERTA_EMAIL) cuando Resend falla.
    El job queda visible en la queue del WMS y dispara logger.critical en el próximo DLQ run,
    evitando que la falla de alertas pase completamente desapercibida (SF_JOB_SILENCIOSO).
    """
    try:
        # **El valor de retorno importa tanto como la excepción.**
        #
        # `enviar_email` devuelve `False` —sin lanzar— cuando falta
        # `RESEND_API_KEY` o cuando la API contesta un error. Este wrapper solo
        # miraba las excepciones, así que ese `False` se descartaba: no se
        # encolaba nada en el DLQ, no salía ningún `critical`, y el llamador
        # seguía como si hubiera mandado.
        #
        # El caso que lo destapó (2026-09-21): el reporte semanal de flota
        # registraba «[FLOTA_REPORTE] enviado a …» y devolvía
        # `{'enviado': True}` sobre un correo que nunca salió. Pero no era de
        # ese archivo — es de acá, y lo heredaban los 15 llamadores, entre
        # ellos las cuatro alertas de inventario y la de ruta entregada sin
        # liquidar.
        #
        # Una alerta apagada no falla: se calla. Y callarse es indistinguible
        # de «no hubo nada que avisar».
        if not enviar_email(asunto, cuerpo_html, cuerpo_texto, dest):
            raise RuntimeError(
                'enviar_email devolvió False — falta configuración de Resend '
                'o la API rechazó el envío')
    except Exception as e:
        logger.critical(
            f'[ALERTAS] enviar_email falló para "{tipo_alerta}" — encolando en DLQ: {e}'
        )
        try:
            from app.models.siesa_job import SiesaJob as _SJ
            from app.extensions import db as _db
            from app.utils.fecha import dia_operativo as _dia
            # Deduplicar por tipo_alerta + DÍA para no acumular N jobs idénticos
            # durante un downtime de Resend que dura varios ciclos del scheduler.
            #
            # La clave con la fecha ya estaba escrita acá y **la consulta la
            # ignoraba**: filtraba por `payload.contains(tipo_alerta)` sin
            # ninguna cota temporal. Como la lista de estados incluye FALLIDO
            # —reintentos agotados, la fila queda para siempre—, el primer
            # fallo definitivo de un tipo de alerta SILENCIABA ESE TIPO PARA
            # SIEMPRE: meses después, con Resend caído otra vez, el job no se
            # encolaba porque "ya había uno".
            #
            # El wrapper existe justamente para que una alerta perdida quede
            # visible en la cola (SF_JOB_SILENCIOSO). Al mes de vida, dejaba de
            # cumplir su única función.
            #
            # Con la fecha DENTRO de la comparación, un FALLIDO viejo no bloquea
            # el de hoy y sigue evitando duplicados dentro del mismo día.
            _idem = f'ALERTA-{tipo_alerta}-{_dia().isoformat()}'
            _ya_en_cola = _SJ.query.filter(
                _SJ.tipo == 'ALERTA_EMAIL',
                _SJ.estado.in_(['PENDIENTE', 'PROCESANDO', 'FALLIDO']),
                _SJ.payload.contains(f'"idem_key": "{_idem}"'),
            ).first()
            if not _ya_en_cola:
                _SJ.encolar(
                    'ALERTA_EMAIL',
                    {'tipo_alerta': tipo_alerta, 'asunto': asunto, 'error': str(e),
                     'idem_key': _idem},
                )
                _db.session.commit()
        except Exception as e2:
            logger.critical(
                f'[ALERTAS] DLQ también falló — alerta "{tipo_alerta}" perdida: {e2}. '
                'Revisar RESEND_API_KEY y conexión BD.'
            )


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
        from app.utils.lock import LOCK_ALERTA_HUERFANAS, tomar_lock_de_sesion
        _lock = tomar_lock_de_sesion(LOCK_ALERTA_HUERFANAS, 'alerta_huerfanas')
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
            try:
                _enviar_email_con_dlq(
                    '[WMS] Scheduler verificar_huerfanas falló',
                    f'<p><b>Error:</b> {str(e)[:400]}</p>',
                    f'Error: {str(e)[:400]}',
                    'alertas_scheduler_huerfanas_fallo',
                )
            except Exception:
                pass
        finally:
            try:
                _db.session.rollback()
            except Exception as _fe:
                logger.error(f'[ALERTAS] rollback al cerrar: {_fe}')
            _lock.liberar()


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

    _enviar_email_con_dlq(asunto, cuerpo_html, cuerpo_texto, 'huerfanas')


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

    _enviar_email_con_dlq(asunto, cuerpo_html, cuerpo_texto, f'job_fallido_{job.tipo}_{job.id}')


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
        from app.utils.lock import LOCK_ALERTA_STOCK_CRITICO, tomar_lock_de_sesion
        _lock = tomar_lock_de_sesion(LOCK_ALERTA_STOCK_CRITICO, 'alerta_stock_critico')
        if not _lock:
            logger.info('[ALERTAS] verificar_y_alertar_stock_critico omitida — otro worker ya la ejecuta')
            return
        try:
            from app.models.ubicacion import Ubicacion
            from app.models.inventario import UbicacionProducto
            from app.models.lpn import LPN, EstadoLPN
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
                    LPN.estado == EstadoLPN.ACTIVO,
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
            try:
                _enviar_email_con_dlq(
                    '[WMS] Scheduler stock_critico falló',
                    f'<p><b>Error:</b> {str(e)[:400]}</p>',
                    f'Error: {str(e)[:400]}',
                    'alertas_scheduler_stock_critico_fallo',
                )
            except Exception:
                pass
        finally:
            try:
                _db.session.rollback()
            except Exception as _fe:
                logger.error(f'[ALERTAS] rollback al cerrar: {_fe}')
            _lock.liberar()


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

    _enviar_email_con_dlq(asunto, cuerpo_html, cuerpo_texto, 'stock_critico')


# ── Resumen operativo diario ──────────────────────────────────────────────────

def despachados_del_dia(dia) -> int:
    """Pedidos despachados ese día Bogotá, en todos los almacenes —
    `metricas.pedidos_despachados`, la misma del tablero."""
    from app.models.almacen import Almacen
    from app.services.metricas.pedidos_despachados import calcular_pedidos_despachados
    return sum(calcular_pedidos_despachados(a.id, dia, dia)['pedidos']
               for a in Almacen.query.all())


def avisos_sin_canal(ayer_inicio, ayer_fin) -> list:
    """Líneas del resumen diario para lo que no tenía canal (2026-09-25).

    Cada fuente es la función que ya decide —no una consulta nueva—; una que
    revienta se declara en su propia línea, no tumba las demás."""
    lineas = []

    def _seguro(nombre, fn):
        try:
            fn()
        except Exception as e:     # noqa: BLE001
            logger.error('[RESUMEN] aviso %s falló: %s', nombre, e, exc_info=True)
            lineas.append(f'⚠ No se pudo revisar {nombre}: {str(e)[:120]}')

    def _auditoria():
        from app.services.auditoria import auditar
        rep = auditar()
        bloq = [r for r in rep['resultados'] if r['severidad'] == 'BLOQUEA' and r['total']]
        if not bloq:
            return
        nuevos = 0
        for r in bloq:
            for h in r['hallazgos']:
                if h.get('fecha') and ayer_inicio.isoformat() <= h['fecha'] < ayer_fin.isoformat():
                    nuevos += 1
        detalle = ', '.join(f"{r['codigo']} ({r['total']})" for r in bloq[:8])
        lineas.append(f'🚨 Auditoría: {rep["bloqueantes"]} hallazgo(s) que BLOQUEAN '
                      f'({nuevos} nuevos ayer): {detalle}')

    def _cartera():
        from app.services import cartera_service
        viejas = cartera_service.salud().get('alertas') or []
        if viejas:
            lineas.append(f'⚠ {len(viejas)} pedido(s) retenidos por cartera hace más de 3 días: '
                          + ', '.join(str(a.get('pedido')) for a in viejas[:10]))

    def _traslados():
        from app.services.traslado_monitor_service import _traslados_en_riesgo
        crit = _traslados_en_riesgo()['criticos']
        if crit:
            lineas.append(f'🚨 {len(crit)} traslado(s) en tránsito hace más de 24 h (mercancía '
                          'en la bodega puente): ' + ', '.join(t['codigo'] for t in crit[:10]))

    def _carga_fisica():
        from app.services.inventario_siesa_service import estado_carga_fisica
        for c in estado_carga_fisica():
            if c['no_escribio'] or (c['ok'] is False and c['de_hoy']):
                lineas.append(f"⚠ Carga física de {c['bodega']} NO escribió inventario: "
                              f"{str(c['error'])[:160]}. Se reintenta desde Siesa → Cargar inventario.")

    def _crons():
        from app.services import cron_latido
        lat = cron_latido.estado()
        if lat.get('fallando'):
            lineas.append('⚠ Crons cuya última corrida falló: ' + ', '.join(lat['fallando']))
        if lat.get('callados'):
            lineas.append('⚠ Crons que no corren hace más de lo esperado: '
                          + ', '.join(lat['callados']))

    def _stock_viejo():
        from datetime import timedelta as _td
        from app.services.inventario_siesa_service import _BODEGAS_PV, frescura_stock_siesa
        f = frescura_stock_siesa(_BODEGAS_PV)
        viejas = sorted(b for b, v in f['por_bodega'].items()
                        if v['actualizada'] and datetime.utcnow() - v['actualizada'] > _td(hours=24))
        if viejas:
            lineas.append('⚠ Existencias de Siesa sin refrescar hace más de 24 h en: '
                          + ', '.join(viejas) + ' (el Armador y los traslados las usan).')

    def _sello():
        from app.services import sello_ambiente
        e = sello_ambiente.estado()
        if e.get('bloquea'):
            lineas.append(f"🚨 {e['texto']}")

    _seguro('la auditoría', _auditoria)
    _seguro('la cartera retenida', _cartera)
    _seguro('los traslados en tránsito', _traslados)
    _seguro('la carga física', _carga_fisica)
    _seguro('los crons', _crons)
    _seguro('las existencias de Siesa', _stock_viejo)
    _seguro('el sello de ambiente', _sello)
    return lineas


def enviar_resumen_diario(app=None):
    """
    Cron diario a las 08:00 Bogotá.
    Consolida lo que pasó ayer: pedidos, packing, reposición, jobs Siesa.
    """
    from flask import current_app as _app
    ctx_app = app or _app._get_current_object()

    with ctx_app.app_context():
        from app.extensions import db as _db
        from app.utils.lock import LOCK_RESUMEN_DIARIO, tomar_lock_de_sesion
        _lock = tomar_lock_de_sesion(LOCK_RESUMEN_DIARIO, 'resumen_diario')
        if not _lock:
            logger.info('[ALERTAS] enviar_resumen_diario omitido — otro worker ya lo ejecuta')
            return
        try:
            from datetime import date, timedelta
            from app.extensions import db
            from sqlalchemy import func

            # El día de AYER en Bogotá (Regla 5). Antes eran medianoches naive
            # comparadas contra columnas UTC: la ventana quedaba corrida 5 h.
            from app.utils.fecha import rango_dia_operativo_utc
            ayer = _dia_operativo() - timedelta(days=1)
            ayer_inicio, ayer_fin = rango_dia_operativo_utc(ayer, ayer)

            # Pedidos despachados ayer: la métrica de siempre (`metricas`), por
            # almacén. Antes importaba `PedidoPicking`, que no existe: salía
            # «N/D» todos los días.
            try:
                pedidos_despachados = despachados_del_dia(ayer)
            except Exception as _e:
                logger.warning(f'[RESUMEN] No se pudo calcular pedidos_despachados: {_e}')
                pedidos_despachados = 'N/D'

            # Bultos empacados ayer
            try:
                from app.models.bulto import Bulto
                bultos_empacados = Bulto.query.filter(
                    Bulto.fecha_creacion >= ayer_inicio,
                    Bulto.fecha_creacion < ayer_fin,
                ).count()
            except Exception as _e:
                logger.warning(f'[RESUMEN] No se pudo calcular bultos_empacados: {_e}')
                bultos_empacados = 'N/D'

            # Tareas reposición completadas ayer
            try:
                from app.models.tarea_reposicion import TareaReposicion
                tareas_ok = TareaReposicion.query.filter(
                    TareaReposicion.estado == 'COMPLETADA',
                    TareaReposicion.fecha_completada >= ayer_inicio,
                    TareaReposicion.fecha_completada < ayer_fin,
                ).count()
            except Exception as _e:
                logger.warning(f'[RESUMEN] No se pudo calcular tareas_reposicion: {_e}')
                tareas_ok = 'N/D'

            # Jobs Siesa ayer — crítico: jobs_fallidos='N/D' oculta movimientos que no llegaron al ERP
            try:
                from app.models.siesa_job import SiesaJob, EstadoSiesaJob
                jobs_ok      = SiesaJob.query.filter(
                    SiesaJob.estado == EstadoSiesaJob.COMPLETADO,
                    SiesaJob.fecha_creacion >= ayer_inicio,
                    SiesaJob.fecha_creacion < ayer_fin,
                ).count()
                # Trabados de verdad (`fallidos_vigentes`): un FALLIDO que un
                # reintento posterior superó ya no es un movimiento perdido.
                from app.services.siesa_job_service import fallidos_vigentes
                jobs_fallidos = len(fallidos_vigentes(
                    desde=ayer_inicio, hasta=ayer_fin, excluir_tipos=())['jobs'])
            except Exception as _e:
                logger.error(f'[RESUMEN] No se pudo calcular jobs Siesa: {_e}', exc_info=True)
                jobs_ok = jobs_fallidos = 'N/D'

            # Lo que ningún otro canal avisa (P1-14 / alertas sin canal): la
            # auditoría (con el corte) en vez de consultas propias —la de
            # «DESPACHADO sin bultos» reescribía VTA-30 sin corte—, cartera
            # retenida > 3 días, traslados en limbo > 24 h, la carga física que
            # no escribió, crons que fallan o callan y el sello de ambiente.
            anomalias = []
            try:
                anomalias.extend(avisos_sin_canal(ayer_inicio, ayer_fin))
            except Exception:
                logger.error('[RESUMEN] avisos sin canal fallaron', exc_info=True)
                anomalias.append('⚠ No se pudieron calcular los avisos del día (ver el log)')
            try:
                # Jobs FALLIDO >24h (no solo ayer)
                # Desde el corte (FECHA_INICIO_AUDITORIA): el ensayo no es
                # «sin resolver» de nadie.
                from app.services import corte as _corte
                from app.services.siesa_job_service import fallidos_vigentes
                _fallidos_viejos = len(fallidos_vigentes(
                    desde=_corte.inicio_auditoria(), hasta=ayer_inicio,
                    excluir_tipos=())['jobs'])
                if _fallidos_viejos:
                    anomalias.append(f'⚠ {_fallidos_viejos} job(s) Siesa FALLIDO >24h sin resolver')
            except Exception:
                logger.error('[RESUMEN] Sweep jobs FALLIDO >24h falló', exc_info=True)
            try:
                # INV_BULTO_UNA_RUTA: bultos cuyo codigo_barras aparece en >1 ruta activa.
                # Bulto.ruta_despacho_id es FK escalar (1 ruta por bulto) y codigo_barras es UNIQUE,
                # así que la anomalía real es un bulto reasignado a otra ruta sin liberar la anterior.
                # Detectamos bultos en rutas activas que comparten tarea_id con bultos en OTRA ruta activa.
                from app.models.ruta_despacho import RutaDespacho
                from sqlalchemy import func as _fn
                _estados_activos = ('PROGRAMADO', 'EN_CARGUE', 'EN_TRANSITO')
                # Tareas con bultos en >1 ruta activa (señal de asignación incorrecta)
                _subq = (
                    db.session.query(Bulto.tarea_id)
                    .join(RutaDespacho, Bulto.ruta_despacho_id == RutaDespacho.id)
                    .filter(RutaDespacho.estado.in_(_estados_activos))
                    .group_by(Bulto.tarea_id)
                    .having(_fn.count(_fn.distinct(RutaDespacho.id)) > 1)
                    .count()
                )
                if _subq:
                    anomalias.append(f'🚨 {_subq} tarea(s) con bultos en >1 ruta activa')
            except Exception:
                logger.error('[RESUMEN] Sweep INV_BULTO_UNA_RUTA falló', exc_info=True)
            try:
                # Devoluciones (m045devol): sin contar 24/48 h, NC sin aprobar
                # 3 días o anulada, RC esperando su NC 48 h. La misma función
                # que pinta el tablero de recepción — un universo, no dos.
                from app.services import devolucion_ruta as _dr_res
                anomalias.extend(_dr_res.lineas_de_aviso())
            except Exception:
                logger.error('[RESUMEN] Sweep de devoluciones falló', exc_info=True)
            if anomalias:
                logger.warning(f'[RESUMEN] Anomalías detectadas: {anomalias}')

            ayer_str = (_dia_operativo() - timedelta(days=1)).strftime('%d/%m/%Y')
            _enviar_resumen_diario(ayer_str, pedidos_despachados, bultos_empacados,
                                   tareas_ok, jobs_ok, jobs_fallidos, anomalias=anomalias)

        except Exception as e:
            logger.error(f'[ALERTAS] Error en enviar_resumen_diario: {e}', exc_info=True)
            try:
                _enviar_email_con_dlq(
                    '[WMS] Scheduler resumen_diario falló',
                    f'<p><b>Error:</b> {str(e)[:400]}</p>',
                    f'Error: {str(e)[:400]}',
                    'alertas_scheduler_resumen_fallo',
                )
            except Exception:
                pass
        finally:
            try:
                _db.session.rollback()
            except Exception as _fe:
                logger.error(f'[ALERTAS] rollback al cerrar: {_fe}')
            _lock.liberar()


def _enviar_resumen_diario(fecha, pedidos, bultos, tareas_rep, jobs_ok, jobs_fallidos, anomalias=None):
    hoy = datetime.now().strftime('%d/%m/%Y %H:%M')
    asunto = f'📊 WMS Papelería Medellín — Resumen operativo {fecha}'

    # N/D en jobs_fallidos = fallo de query DB → también es alerta (no mostrar en gris neutro)
    alerta_jobs = jobs_fallidos == 'N/D' or (jobs_fallidos not in (0,) and jobs_fallidos > 0)

    _anomalias_texto = (
        '\nANOMALÍAS DETECTADAS:\n' + '\n'.join(f'  {a}' for a in anomalias) + '\n'
        if anomalias else ''
    )
    _filas_anomalias = ''.join(
        f'<div style="font-size:14px;color:#fecaca;margin:4px 0;">{a}</div>'
        for a in (anomalias or [])
    )
    _bloque_anomalias_html = (
        f'<div style="background:#7c2d12;border:1px solid #991b1b;border-radius:12px;'
        f'padding:16px 20px;margin-bottom:16px;">'
        f'<div style="font-size:12px;font-weight:700;color:#fca5a5;text-transform:uppercase;'
        f'letter-spacing:0.06em;margin-bottom:8px;">⚠ Anomalías detectadas</div>'
        f'{_filas_anomalias}</div>'
        if anomalias else ''
    )
    cuerpo_texto = f"""WMS Papelería Medellín — Resumen Operativo {fecha}

Pedidos despachados:      {pedidos}
Bultos empacados:         {bultos}
Reposiciones completadas: {tareas_rep}
Transferencias Siesa OK:  {jobs_ok}
Transferencias fallidas:  {jobs_fallidos}{'  ← REVISAR' if alerta_jobs else ''}
{_anomalias_texto}
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
    {_bloque_anomalias_html}
    <div style="text-align:center;font-size:11px;color:#4b5563;padding:8px;">
      WMS Papelería Medellín · Resumen automático generado el {hoy}
    </div>
  </div>
</body></html>"""

    _enviar_email_con_dlq(asunto, cuerpo_html, cuerpo_texto, 'resumen_diario')


# ── Scheduler ─────────────────────────────────────────────────────────────────

def init_scheduler(app):
    """
    Crons diarios:
      05:45 → alerta ubicaciones huérfanas
      06:15 → alerta stock crítico sin reserva (hueco PICKING puntual)
      06:30 → alerta rutas entregadas sin liquidar
      06:45 → resumen operativo diario
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[ALERTAS] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')

    # FM_SCHEDULER_PEAK: alejados del :00 exacto para no competir con el inicio del turno
    # de operarios (6:00am) ni entre sí. Separados 30min para escalonar carga en DB.
    from app.services.cron_latido import con_latido  # P1-11
    scheduler.add_job(
        func=con_latido('alertas_huerfanas_email', verificar_y_alertar_huerfanas),
        trigger=CronTrigger(hour=5, minute=45, timezone='America/Bogota'),
        kwargs={'app': app},
        id='alertas_huerfanas_email',
        name='Alerta email ubicaciones huérfanas (05:45 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=600,
    )
    scheduler.add_job(
        func=con_latido('alertas_stock_critico', verificar_y_alertar_stock_critico),
        trigger=CronTrigger(hour=6, minute=15, timezone='America/Bogota'),
        kwargs={'app': app},
        id='alertas_stock_critico',
        name='Alerta email stock crítico sin reserva (06:15 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=600,
    )
    scheduler.add_job(
        func=con_latido('alertas_rutas_sin_liquidar', verificar_y_alertar_rutas_sin_liquidar),
        trigger=CronTrigger(hour=6, minute=30, timezone='America/Bogota'),
        kwargs={'app': app},
        id='alertas_rutas_sin_liquidar',
        name='Alerta email rutas entregadas sin liquidar (06:30 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=600,
    )
    scheduler.add_job(
        func=con_latido('resumen_operativo_diario', enviar_resumen_diario),
        trigger=CronTrigger(hour=6, minute=45, timezone='America/Bogota'),
        kwargs={'app': app},
        id='resumen_operativo_diario',
        name='Resumen operativo diario (06:45 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=600,
    )

    scheduler.start()
    import atexit
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info('[ALERTAS] Scheduler iniciado — 05:45 huérfanas | 06:15 stock crítico | '
                '06:30 rutas sin liquidar | 06:45 resumen')
    return scheduler


def verificar_y_alertar_rutas_sin_liquidar(app=None):
    """Cron diario — una ruta entregada que nadie liquidó.

    Lo pide BK-OPS-01 v2.1 §4.2: hoy no existe ninguna alerta y una ruta puede
    quedarse sin liquidar indefinidamente. La política de qué cuenta como
    atrasada vive en `services/rezago_liquidacion.py`, la misma que lee el
    desglose — si divergieran, el correo hablaría de un universo y el tablero
    de otro.
    """
    from flask import current_app as _app
    ctx_app = app or _app._get_current_object()

    with ctx_app.app_context():
        from app.extensions import db as _db
        from app.utils.lock import LOCK_ALERTA_RUTAS_SIN_LIQUIDAR, tomar_lock_de_sesion
        _lock = tomar_lock_de_sesion(LOCK_ALERTA_RUTAS_SIN_LIQUIDAR, 'alerta_rutas_sin_liquidar')
        if not _lock:
            logger.info('[ALERTAS] rutas_sin_liquidar omitida — otro worker ya la ejecuta')
            return
        try:
            from app.services import rezago_liquidacion as _rz

            d = _rz.diagnostico()
            atrasadas, cruzan = d['atrasadas'], d['cruzan_mes']
            if not atrasadas and not cruzan:
                logger.info('[ALERTAS] Todas las rutas entregadas están liquidadas.')
                return

            logger.warning(
                '[ALERTAS] %s ruta(s) entregadas sin liquidar (%s cruzan mes) — enviando email.',
                len(atrasadas) + len(cruzan), len(cruzan))
            _enviar_alerta_rutas_sin_liquidar(atrasadas, cruzan,
                                              d.get('recibos_de_otro_mes') or [])

        except Exception as e:
            logger.error(f'[ALERTAS] Error en verificar_y_alertar_rutas_sin_liquidar: {e}',
                         exc_info=True)
            try:
                _enviar_email_con_dlq(
                    '[WMS] Scheduler rutas_sin_liquidar falló',
                    f'<p><b>Error:</b> {str(e)[:400]}</p>',
                    f'Error: {str(e)[:400]}',
                    'alertas_scheduler_rutas_sin_liquidar_fallo',
                )
            except Exception:
                pass
        finally:
            try:
                _db.session.rollback()
            except Exception as _fe:
                logger.error(f'[ALERTAS] rollback al cerrar: {_fe}')
            _lock.liberar()


def _enviar_alerta_rutas_sin_liquidar(atrasadas: list, cruzan_mes: list,
                                      recibos_otro_mes: list = ()):
    """El correo. Las que cruzan mes van primero y con su propia explicación:
    liquidar rápido ya no las arregla, y quien las reciba tiene que saberlo.
    Los recibos que ya salieron fechados en otro mes van al lado (tanda 2)."""
    def _fila(f):
        d = f['dias']
        return f"  · Ruta {f['ruta_id']} — {d if d is not None else '?'} día(s), {f['estado_financiero']}"

    partes = []
    if cruzan_mes:
        partes.append(
            f'CRUZAN EL MES ({len(cruzan_mes)}) — la entrega ocurrió en un mes y el '
            f'recaudo va a quedar registrado en otro. Liquidar ya no lo corrige: '
            f'el período contable no se mueve. El recibo de caja se fechará en el '
            f'mes del envío y llevará el día real del cobro en sus notas.\n'
            + '\n'.join(_fila(f) for f in cruzan_mes))
    if recibos_otro_mes:
        partes.append(
            f'RECIBOS DE CAJA FECHADOS EN OTRO MES ({len(recibos_otro_mes)}) — '
            f'cobrados en un mes y registrados en el siguiente (día real en las notas):\n'
            + '\n'.join(f"  · Ruta {r['ruta_id']} — pedido {r.get('pedido') or '?'}, "
                         f"cobrado el {r['cobrado_el']}" for r in recibos_otro_mes))
    if atrasadas:
        partes.append(
            f'ATRASADAS ({len(atrasadas)}) — debían liquidarse el mismo día.\n'
            + '\n'.join(_fila(f) for f in atrasadas))

    cuerpo = (
        'Rutas entregadas cuyo cierre financiero no ocurrió.\n\n'
        + '\n\n'.join(partes)
        + '\n\nMientras no se liquiden, la factura de cada parada queda abierta: '
          'computa mora, consume cupo y puede frenar el próximo pedido del mismo '
          'cliente. Es el mecanismo que produce la cartera fantasma.\n\n'
          'El recibo de caja de la liquidación es lo que cierra ese saldo.'
    )
    _enviar_email_con_dlq(
        f'[WMS ALERTA] {len(atrasadas) + len(cruzan_mes)} ruta(s) entregadas sin liquidar',
        f'<pre>{cuerpo}</pre>', cuerpo, 'rutas_entregadas_sin_liquidar',
    )
