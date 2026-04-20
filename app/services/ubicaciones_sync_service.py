"""
Sincronización de ubicaciones Siesa → WMS  (ID 43: API_v2_Ubicaciones).

Siesa Enterprise es el dueño de la data maestra de ubicaciones:
  - Nomenclatura  (PIK-01-A, RES-02-B, ...)
  - Stock mínimo  (f152_cant_minima)
  - Stock máximo  (f152_cant_maxima)
  - Secuencia de ruteo (f152_secuencia)

El WMS es un espejo: lee cada noche y obedece.
El tipo_zona se deduce del prefijo del código de ubicación:
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
            almacenes = {a.codigo_siesa: a.id for a in Almacen.query.all() if a.codigo_siesa}
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
                            codigo = (
                                row.get('f152_id') or
                                row.get('f152_codigo') or
                                row.get('codigo') or ''
                            ).strip()
                            if not codigo:
                                continue

                            activo = str(row.get('f152_ind_estado', '1')) in ('1', 'true', 'True')

                            # Capacidades — Siesa puede usar distintos aliases
                            def _int_or_none(v):
                                try:
                                    return int(float(v)) if v not in (None, '', 'null') else None
                                except (ValueError, TypeError):
                                    return None

                            stock_minimo = _int_or_none(
                                row.get('f152_cant_minima') or row.get('cant_minima')
                            )
                            stock_maximo = _int_or_none(
                                row.get('f152_cant_maxima') or
                                row.get('f152_capacidad_maxima') or
                                row.get('cant_maxima')
                            )
                            secuencia = _int_or_none(
                                row.get('f152_secuencia') or row.get('secuencia')
                            )
                            descripcion = (
                                row.get('f152_descripcion') or
                                row.get('descripcion') or ''
                            ).strip() or None

                            tipo_zona = _inferir_tipo_zona(codigo)

                            ub = Ubicacion.query.filter_by(
                                codigo=codigo, almacen_id=almacen_id
                            ).first()

                            if ub:
                                # Actualizar campos que Siesa puede cambiar
                                ub.tipo_zona = tipo_zona
                                ub.stock_minimo = stock_minimo
                                ub.stock_maximo = stock_maximo
                                ub.activo = activo
                                if descripcion and not ub.zona:
                                    ub.zona = descripcion
                                if secuencia is not None:
                                    ub.secuencia_ruteo = secuencia
                                actualizadas += 1
                            else:
                                ub = Ubicacion(
                                    codigo=codigo,
                                    almacen_id=almacen_id,
                                    tipo_zona=tipo_zona,
                                    stock_minimo=stock_minimo,
                                    stock_maximo=stock_maximo,
                                    activo=activo,
                                    zona=descripcion,
                                    tipo='estanteria',
                                )
                                if secuencia is not None:
                                    ub.secuencia_ruteo = secuencia
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
