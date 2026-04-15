"""
Sincronización masiva de códigos de barras EAN desde Siesa → PostgreSQL local.

Arquitectura (Single Source of Truth):
  Siesa Enterprise es el dueño único de la Data Maestra.
  Este worker vuelca todos los barcodes de API_v2_ItemsBarras (ID 28)
  a la columna productos.codigo_barras mediante upsert.

  El escaneo en bodega consulta SOLO PostgreSQL → cero latencia,
  cero dependencia de Siesa en tiempo real durante la operación.

Cron: diariamente a las 02:00 hora Bogotá.
Trigger manual: POST /api/siesa/sync-barcodes (solo admin).
"""
import logging
import threading
from datetime import datetime
from app.extensions import db
from app.models.producto import Producto
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

_sync_estado = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}


def _run_sync(app):
    global _sync_estado
    with app.app_context():
        actualizados = 0
        sin_producto = 0
        errores = 0
        campo_barras = None   # se detecta en la primera fila válida

        try:
            for pag in range(1, 2001):   # hasta 200 000 barcodes (100/pág)
                resp = connekta._get(connekta.api_barras, {
                    'paginacion': f'numPag={pag}|tamPag=100'
                })
                rows = resp.get('detalle', {}).get('Table', [])

                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break

                for row in rows:
                    try:
                        # Auto-detectar campo de barras en la primera fila válida
                        if campo_barras is None:
                            for candidato in ('f131_id', 'f120_codigo_barras',
                                              'f121_id', 'f131_barras'):
                                if row.get(candidato):
                                    campo_barras = candidato
                                    logger.info(f'[BARCODE SYNC] Campo detectado: {campo_barras}')
                                    break
                            if campo_barras is None:
                                logger.warning(f'[BARCODE SYNC] No se detectó campo de barras. Claves disponibles: {list(row.keys())}')
                                break

                        codigo_barras = (row.get(campo_barras) or '').strip()
                        codigo_siesa  = (row.get('f120_referencia') or '').strip()

                        if not codigo_barras or not codigo_siesa:
                            continue

                        prod = (Producto.query.filter_by(codigo_siesa=codigo_siesa).first() or
                                Producto.query.filter_by(codigo=codigo_siesa).first())

                        if not prod:
                            sin_producto += 1
                            continue

                        # Preferir EAN real (todo dígitos, 8+ caracteres) sobre código interno.
                        # Un producto puede tener múltiples entradas en ItemsBarras;
                        # si ya tiene un EAN real guardado, no lo sobreescribir con un código interno.
                        es_ean_real = codigo_barras.isdigit() and len(codigo_barras) >= 8
                        tiene_ean_actual = (prod.codigo_barras or '').isdigit() and len(prod.codigo_barras or '') >= 8

                        if prod.codigo_barras != codigo_barras:
                            if es_ean_real or not tiene_ean_actual:
                                prod.codigo_barras = codigo_barras
                                actualizados += 1

                    except Exception as e:
                        logger.warning(f'[BARCODE SYNC] Fila inválida: {e}')
                        errores += 1

                db.session.commit()
                logger.info(f'[BARCODE SYNC] Pág {pag}: {len(rows)} filas | actualizados={actualizados}')

            resultado = {
                'campo_detectado': campo_barras,
                'actualizados': actualizados,
                'sin_producto_local': sin_producto,
                'errores': errores,
                'timestamp': datetime.utcnow().isoformat()
            }
            _sync_estado['ultimo_resultado'] = resultado
            _sync_estado['ultimo_error'] = None
            logger.info(f'[BARCODE SYNC] Completado: {resultado}')

        except Exception as e:
            _sync_estado['ultimo_error'] = str(e)
            logger.error(f'[BARCODE SYNC] Error fatal: {e}')
        finally:
            _sync_estado['en_curso'] = False


def ejecutar_sync(app):
    global _sync_estado
    if _sync_estado['en_curso']:
        logger.info('[BARCODE SYNC] Ya hay un sync en curso, se omite')
        return
    _sync_estado['en_curso'] = True
    _sync_estado['ultimo_inicio'] = datetime.utcnow().isoformat()
    t = threading.Thread(target=_run_sync, args=(app,), daemon=True)
    t.start()


def get_estado():
    return dict(_sync_estado)


def init_scheduler(app):
    """Cron diario a las 02:00 hora Bogotá."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[BARCODE SYNC] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=ejecutar_sync,
        trigger=CronTrigger(hour=2, minute=0, timezone='America/Bogota'),
        kwargs={'app': app},
        id='sync_barcodes_siesa',
        name='Sync barcodes EAN Siesa → WMS (02:00 Bogotá)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300
    )
    scheduler.start()
    logger.info('[BARCODE SYNC] Scheduler iniciado — sync diario 02:00 Bogotá')
    return scheduler
