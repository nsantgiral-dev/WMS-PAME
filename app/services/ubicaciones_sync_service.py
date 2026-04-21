"""
Sincronización de ubicaciones Siesa → WMS  (ID 43: API_v2_Ubicaciones).

Siesa Enterprise es el dueño de la nomenclatura de ubicaciones.
El WMS sincroniza cada noche:
  - Código     (f155_id)
  - Descripción (f155_descripcion)
  - Estado     (f155_ind_estado → activo/inactivo)

Lo que Siesa NO expone (la API solo tiene 5 campos):
  - stock_minimo / stock_maximo → se configuran en el WMS directamente
    vía endpoint admin PATCH /api/reposicion/ubicacion/<id>/limites
  - secuencia_ruteo → idem, configuración local WMS

El tipo_zona se deduce del prefijo del código:
  PIK* → PICKING   |   RES* → RESERVA   |   resto → GENERAL

El worker nocturno llama a sync_ubicaciones_desde_siesa().
"""

import logging
import threading
from datetime import datetime, timezone
from app.extensions import db
from app.models.ubicacion import Ubicacion
from app.models.almacen import Almacen
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

_sync_estado = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}


def _inferir_tipo_zona(codigo: str) -> str:
    """
    Deduce el tipo de zona del prefijo del código Siesa.
    El jefe de bodega controla el tipo simplemente nombrando:
      PIK-... → zona de picking (floor level, unidades sueltas)
      RES-... → zona de reserva (alto, pacas/LPNs sellados)
      todo lo demás → GENERAL
    """
    c = (codigo or '').upper()
    if c.startswith('PIK'):
        return 'PICKING'
    if c.startswith('RES'):
        return 'RESERVA'
    return 'GENERAL'


def _run_sync(app, bodega_id: str = None):
    """Lógica real — corre en hilo separado con su propio app_context."""
    global _sync_estado

    with app.app_context():
        creadas = 0
        actualizadas = 0
        errores = 0

        try:
            # Necesitamos mapear bodega_id (Siesa) → almacen_id (WMS)
            almacenes = {a.bodega_siesa_id: a.id for a in Almacen.query.all() if a.bodega_siesa_id}
            if not almacenes:
                logger.warning('[UBICACIONES SYNC] No hay almacenes con codigo_siesa — abortando')
                _sync_estado['ultimo_error'] = 'Sin almacenes con codigo_siesa'
                return

            bodegas_a_sincronizar = [bodega_id] if bodega_id else list(almacenes.keys())

            for bid in bodegas_a_sincronizar:
                almacen_id = almacenes.get(bid)
                if not almacen_id:
                    logger.warning(f'[UBICACIONES SYNC] Bodega {bid} no tiene almacen WMS — omitida')
                    continue

                for pag in range(1, 500):
                    try:
                        resp = connekta.get_ubicaciones_siesa(bodega_id=bid, pagina=pag)
                    except Exception as e:
                        logger.error(f'[UBICACIONES SYNC] Error Connekta pag {pag}: {e}')
                        errores += 1
                        break

                    rows = resp.get('detalle', {}).get('Table', [])
                    if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                        break

                    for row in rows:
                        try:
                            # Campos certificados por PDF oficial API_v2_Ubicaciones:
                            # f150_id, f155_id_cia, f155_id, f155_descripcion, f155_ind_estado
                            codigo = (row.get('f155_id') or '').strip()
                            if not codigo:
                                continue

                            activo = int(row.get('f155_ind_estado', 1)) == 1
                            descripcion = (row.get('f155_descripcion') or '').strip() or None
                            tipo_zona = _inferir_tipo_zona(codigo)

                            ub = Ubicacion.query.filter_by(
                                codigo=codigo, almacen_id=almacen_id
                            ).first()

                            if ub:
                                # Siesa manda nomenclatura y estado — solo eso actualizamos.
                                # stock_minimo / stock_maximo / secuencia_ruteo son configuración
                                # local del WMS — NO los tocamos en el sync.
                                ub.tipo_zona = tipo_zona
                                ub.activo = activo
                                if descripcion and not ub.zona:
                                    ub.zona = descripcion
                                actualizadas += 1
                            else:
                                ub = Ubicacion(
                                    codigo=codigo,
                                    almacen_id=almacen_id,
                                    tipo_zona=tipo_zona,
                                    activo=activo,
                                    zona=descripcion,
                                    tipo='estanteria',
                                    # stock_minimo / stock_maximo: el jefe los configura
                                    # en el WMS admin tras el primer sync
                                )
                                db.session.add(ub)
                                creadas += 1

                        except Exception as e:
                            logger.error(f'[UBICACIONES SYNC] Fila {row}: {e}')
                            errores += 1

                    db.session.commit()

                    if len(rows) < 200:
                        break

            resultado = {
                'creadas': creadas,
                'actualizadas': actualizadas,
                'errores': errores,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            }
            _sync_estado['ultimo_resultado'] = resultado
            _sync_estado['ultimo_error'] = None
            logger.info(f'[UBICACIONES SYNC] OK — {resultado}')

        except Exception as e:
            logger.error(f'[UBICACIONES SYNC] Error fatal: {e}', exc_info=True)
            _sync_estado['ultimo_error'] = str(e)
        finally:
            _sync_estado['en_curso'] = False
            _sync_estado['ultimo_inicio'] = None


def sync_ubicaciones_desde_siesa(app=None, bodega_id: str = None, en_hilo: bool = True):
    """
    Punto de entrada principal.
    - app: instancia Flask (requerida si en_hilo=True)
    - bodega_id: si None sincroniza todas las bodegas mapeadas
    - en_hilo: True → dispara hilo y retorna inmediatamente (para endpoints HTTP)
               False → corre en el hilo actual (para scheduler nocturno)
    """
    global _sync_estado

    if _sync_estado['en_curso']:
        return {'advertencia': 'Sync ya en curso — espera que termine'}

    _sync_estado['en_curso'] = True
    _sync_estado['ultimo_inicio'] = datetime.now(timezone.utc).isoformat()

    if en_hilo:
        if app is None:
            raise ValueError('app es requerido cuando en_hilo=True')
        t = threading.Thread(target=_run_sync, args=(app, bodega_id), daemon=True)
        t.start()
        return {'iniciado': True, 'mensaje': 'Sync de ubicaciones iniciado en background'}
    else:
        # Llamada síncrona desde scheduler — el caller ya tiene app_context
        from flask import current_app
        _run_sync(current_app._get_current_object(), bodega_id)
        return _sync_estado['ultimo_resultado']


def get_estado():
    return dict(_sync_estado)


def ejecutar_sync(app=None):
    """Wrapper para el scheduler — corre síncronamente en el hilo del cron."""
    from flask import current_app
    _app = app or current_app._get_current_object()
    return sync_ubicaciones_desde_siesa(app=_app, en_hilo=False)


def init_scheduler(app):
    """Cron diario a las 03:00 hora Bogotá — después de empaques (02:30)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[UBICACIONES SYNC] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=ejecutar_sync,
        trigger=CronTrigger(hour=3, minute=0, timezone='America/Bogota'),
        kwargs={'app': app},
        id='sync_ubicaciones_siesa',
        name='Sync ubicaciones Siesa → WMS (03:00 Bogotá)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info('[UBICACIONES SYNC] Scheduler iniciado — sync diario 03:00 Bogotá')
    return scheduler
