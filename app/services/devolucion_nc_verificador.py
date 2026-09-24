"""
¿La nota crédito de esta devolución ya está APROBADA en Siesa? — verificado.

Hasta el 2026-09-24 la única respuesta era un clic: «Ya la aprobé y crucé en
Siesa» (`marcar_nc_aprobada`), sin mirar nada. Y de esa respuesta dependen dos
cosas que mueven plata: que lo devuelto salga de la zona DEVOLUCION a picking
(vendible) y que el conteo cíclico deje de tratar ese SKU como «mercancía en
proceso».

La consulta que ya está registrada para el motivo DIAN
(`CONNEKTA_CONSULTA_NC_CONSECUTIVO` → `_filas_nc_encabezado`) trae
`f350_ind_estado` de las NCE recientes: 0 = en elaboración, 1 = aprobada
(verificado en vivo con NCE-56 el 2026-07-31), 2 = anulada (el valor estándar
de Siesa; **no verificado en vivo** — se declara, no se actúa sobre él más que
para avisar).

## Reglas

- **Nace apagado** (`DEVOLUCIONES_VERIFICAR_NC=true` lo enciende), como todo
  cron que escribe. Apagado no lee ni escribe.
- **Solo en la ventana de Siesa** (7:00–19:30 Bogotá, Regla 14).
- **Lock del registro** (`LOCK_DEVOLUCIONES_NC`): dos workers no verifican a la
  vez.
- **Solo marca lo que LEYÓ**: `ind_estado == 1` en la fila de ESE consecutivo,
  ese tipo y ese CO. Sin consecutivo conocido, fuera de la ventana de 100 filas
  de la consulta, o con la consulta sin configurar: no se marca nada y se
  cuenta por qué. El botón manual queda como respaldo, con bitácora.
- Un GET por corrida, no uno por devolución.
"""
import logging
import os
from datetime import datetime

from app.extensions import db

logger = logging.getLogger(__name__)

IND_ELABORACION = 0
IND_APROBADA = 1
IND_ANULADA = 2


def encendido() -> bool:
    """`DEVOLUCIONES_VERIFICAR_NC=true`. **Nace apagado.**"""
    return os.getenv('DEVOLUCIONES_VERIFICAR_NC', 'false').strip().lower() == 'true'


def candidatas() -> list:
    """Devoluciones con NC enviada y sin aprobar, con consecutivo conocido."""
    from app.models.devolucion_cliente import DevolucionCliente
    return (DevolucionCliente.query
            .filter(DevolucionCliente.siesa_nc_triggered.is_(True),
                    DevolucionCliente.nc_aprobada_siesa.is_(False))
            .order_by(DevolucionCliente.id)
            .all())


def verificar(gateway=None, ahora=None) -> dict:
    """Una pasada. No mira el interruptor ni la ventana (eso es `correr`):
    sirve también para verificar a mano desde el tablero."""
    if gateway is None:
        from app.services.connekta_gateway import connekta as gateway
    from app.services.devolucion_cliente_service import DevolucionClienteService
    resumen = {'candidatas': 0, 'aprobadas': 0, 'en_elaboracion': 0, 'anuladas': 0,
               'sin_consecutivo': 0, 'fuera_de_la_consulta': 0, 'liberadas': 0,
               'errores': []}
    devs = candidatas()
    resumen['candidatas'] = len(devs)
    if not devs:
        return resumen
    if not getattr(gateway, 'consulta_nc_consecutivo', None):
        resumen['omitido'] = ('CONNEKTA_CONSULTA_NC_CONSECUTIVO no está configurada: sin '
                              'ella no hay de dónde leer el estado de la NC')
        return resumen
    filas = gateway._filas_nc_encabezado()
    tipo = (getattr(gateway, 'tipo_docto_nota_credito', None) or 'NCE').strip()
    co = (getattr(gateway, 'centro_op', None) or '').strip()
    por_consec = {}
    for f in filas or []:
        if (f.get('f350_id_tipo_docto') or '').strip() != tipo:
            continue
        if co and (f.get('f350_id_co') or '').strip() != co:
            continue
        try:
            por_consec[int(f.get('f350_consec_docto'))] = int(f.get('f350_ind_estado'))
        except (TypeError, ValueError):
            continue
    ahora = ahora or datetime.utcnow()
    for d in devs:
        if not d.siesa_nc_consec:
            resumen['sin_consecutivo'] += 1
            continue
        try:
            consec = int(d.siesa_nc_consec)
        except (TypeError, ValueError):
            resumen['sin_consecutivo'] += 1
            continue
        if consec not in por_consec:
            resumen['fuera_de_la_consulta'] += 1
            continue
        estado = por_consec[consec]
        d.nc_estado_siesa = estado
        d.nc_estado_leido_at = ahora
        if estado == IND_APROBADA:
            try:
                with db.session.begin_nested():
                    r = DevolucionClienteService.marcar_nc_aprobada_desde_siesa(d)
                resumen['aprobadas'] += 1
                if r and r.get('movidas'):
                    resumen['liberadas'] += 1
            except Exception as e:
                resumen['errores'].append({'devolucion': d.codigo, 'error': str(e)[:300]})
                logger.error('[DEV_NC] %s: aprobada en Siesa pero no se pudo liberar: %s',
                             d.codigo, e)
        elif estado == IND_ANULADA:
            resumen['anuladas'] += 1
        else:
            resumen['en_elaboracion'] += 1
    db.session.commit()
    return resumen


def correr(gateway=None, reloj=None) -> dict:
    """El cron: interruptor + ventana + verificar."""
    if not encendido():
        return {'omitido': 'DEVOLUCIONES_VERIFICAR_NC no está en true — nace apagado'}
    from app.services.fotos_siesa_service import VENTANA, ventana_abierta
    if not ventana_abierta(reloj() if reloj else None):
        return {'omitido': f'fuera de la ventana de Siesa ({VENTANA[0]}–{VENTANA[1]} Bogotá)'}
    return verificar(gateway=gateway)


def estado() -> dict:
    """Para `/api/health/siesa` y el tablero. Nunca levanta."""
    try:
        from app.models.devolucion_cliente import DevolucionCliente
        pend = candidatas()
        return {
            'encendido': encendido(),
            'nc_sin_aprobar': len(pend),
            'sin_consecutivo': sum(1 for d in pend if not d.siesa_nc_consec),
            'anuladas': sum(1 for d in pend if d.nc_estado_siesa == IND_ANULADA),
            'aprobadas_por_siesa': DevolucionCliente.query.filter_by(
                nc_aprobada_fuente='SIESA').count(),
            'aprobadas_a_mano': DevolucionCliente.query.filter_by(
                nc_aprobada_fuente='MANUAL').count(),
        }
    except Exception as e:
        return {'encendido': encendido(), 'error': str(e)[:200]}


def init_scheduler(app):
    """Cada 30 min entre las 7:00 y las 19:30 Bogotá (la ventana la vuelve a
    mirar `correr`). Nace apagado: sin `DEVOLUCIONES_VERIFICAR_NC=true`,
    `correr` devuelve su motivo sin tocar Siesa."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[DEV_NC] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            from app.utils.lock import LOCK_DEVOLUCIONES_NC, advisory_lock
            with advisory_lock(LOCK_DEVOLUCIONES_NC, 'devoluciones_nc') as tomado:
                if not tomado:
                    logger.info('[DEV_NC] otro worker ya verifica las NC')
                    return
                try:
                    r = correr()
                    logger.info('[DEV_NC] %s', r)
                except Exception as e:
                    db.session.rollback()
                    logger.error('[DEV_NC] falló: %s', e, exc_info=True)

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(func=_job,
                      trigger=CronTrigger(hour='7-19', minute='5,35',
                                          timezone='America/Bogota'),
                      id='devoluciones_verificar_nc', replace_existing=True,
                      max_instances=1, misfire_grace_time=900)
    scheduler.start()
    logger.info('[DEV_NC] Scheduler cada 30 min 7–19 h Bogotá (encendido=%s)', encendido())
    return scheduler
