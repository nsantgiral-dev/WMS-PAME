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
        # Advisory lock de PostgreSQL — protege contra ejecución simultánea entre workers
        from sqlalchemy import text as _text
        lock_adquirido = db.session.execute(
            _text('SELECT pg_try_advisory_lock(:key)'), {'key': 2013}
        ).scalar()
        if not lock_adquirido:
            logger.warning('[BARCODE SYNC] Otro worker ya ejecuta — omitido')
            _sync_estado['en_curso'] = False
            return

        from app.services import registro_sync_service as _reg
        _reg_id = _reg.abrir('barcodes')

        actualizados = 0
        sin_producto = 0
        errores = 0
        campo_barras = None   # se detecta en la primera fila válida

        try:
            # Cargar todos los productos en memoria una sola vez → evita N+1 queries
            # (28k productos × 2 queries/fila = 56k queries sin esto)
            prods_por_siesa = {}
            prods_por_codigo = {}
            for p in Producto.query.all():
                if p.codigo_siesa:
                    prods_por_siesa[p.codigo_siesa] = p
                if p.codigo:
                    prods_por_codigo[p.codigo] = p
            logger.info(f'[BARCODE SYNC] Productos en memoria: {len(prods_por_siesa)} (por siesa), {len(prods_por_codigo)} (por codigo)')

            errores_consecutivos = 0
            for pag in range(1, 2001):   # hasta 200 000 barcodes (100/pág)
                try:
                    resp = connekta._get(connekta.api_barras, {
                        'paginacion': f'numPag={pag}|tamPag=100'
                    })
                    errores_consecutivos = 0
                except Exception as _e:
                    errores_consecutivos += 1
                    logger.warning(f'[BARCODE SYNC] Error en pág {pag}: {_e} (consecutivos: {errores_consecutivos})')
                    if errores_consecutivos >= 3:
                        logger.error('[BARCODE SYNC] 3 errores consecutivos — abortando sync')
                        break
                    continue
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

                        prod = prods_por_siesa.get(codigo_siesa) or prods_por_codigo.get(codigo_siesa)

                        if not prod:
                            sin_producto += 1
                            continue

                        changed = False
                        es_ean_real = codigo_barras.isdigit() and len(codigo_barras) >= 8

                        # Clasificar si es barcode de unidad suelta o de empaque
                        # Si la unidad del barcode difiere de la unidad de inventario → es empaque
                        unidad_barra = (row.get('f131_id_unidad_medida') or '').strip()
                        unidad_inv   = (prod.unidad_medida or 'UND').strip()
                        es_empaque_barcode = bool(unidad_barra and unidad_barra != unidad_inv)

                        if es_empaque_barcode:
                            # Guardar en codigo_barras_empaque — el scan aplicará factor
                            if prod.codigo_barras_empaque != codigo_barras and es_ean_real:
                                prod.codigo_barras_empaque = codigo_barras
                                prod.unidad_empaque = unidad_barra
                                changed = True
                        else:
                            # Barcode de unidad suelta — preferir EAN real
                            tiene_ean_actual = (prod.codigo_barras or '').isdigit() and len(prod.codigo_barras or '') >= 8
                            if prod.codigo_barras != codigo_barras:
                                if es_ean_real or not tiene_ean_actual:
                                    prod.codigo_barras = codigo_barras
                                    changed = True

                        if changed:
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
            _reg.cerrar_ok(_reg_id, resultado)
            logger.info(f'[BARCODE SYNC] Completado: {resultado}')

        except Exception as e:
            _sync_estado['ultimo_error'] = str(e)
            _reg.cerrar_error(_reg_id, e)
            logger.error(f'[BARCODE SYNC] Error fatal: {e}')
            try:
                db.session.rollback()
            except Exception:
                pass
        finally:
            _sync_estado['en_curso'] = False
            if lock_adquirido:
                try:
                    db.session.execute(_text('SELECT pg_advisory_unlock(:key)'), {'key': 2013})
                    db.session.commit()
                except Exception as _e:
                    logger.error('[BARCODE SYNC] Error liberando advisory lock: %s', _e)


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
    """Estado del sync + **cobertura real**, que es la pregunta de verdad.

    `_sync_estado` dice cómo fue la última corrida y se borra en cada deploy.
    `cobertura` dice cuántos productos tienen código de barras hoy — sale de la
    base y no se evapora. Un sync exitoso que actualizó 3 de 12.000 productos se
    veía igual que uno que los cubrió todos: ambos `ultimo_error: null`.
    """
    salida = dict(_sync_estado)
    try:
        from app.services.inventario_siesa_service import cobertura_catalogo
        from app.services import registro_sync_service as _reg
        salida['cobertura'] = cobertura_catalogo()
        salida['persistido'] = _reg.estado_persistido(
            'barcodes', bool(_sync_estado.get('ultimo_resultado')))
    except Exception as e:
        # Declarado, no omitido: un campo que desaparece se lee como "no aplica".
        salida['cobertura'] = {'_error_lectura': str(e)[:200]}
    return salida


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
