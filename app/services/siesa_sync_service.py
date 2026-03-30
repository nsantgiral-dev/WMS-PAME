"""
Sincronización del catálogo de productos Siesa → WMS.

Estrategia: upsert por codigo_siesa.
  - Si el producto no existe → lo crea con clasificacion_abc='C' por defecto.
  - Si ya existe → actualiza nombre y estado activo si cambiaron.

El scheduler llama a `ejecutar_sync()` cada hora de 7am a 8pm (hora Colombia, UTC-5).
En Railway (UTC), eso equivale a horas 12–01 UTC → usamos 12-01 con cron de hora.
"""
import logging
from datetime import datetime, timezone
from app.extensions import db
from app.models.producto import Producto
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

# Último sync exitoso — evita que múltiples workers ejecuten en paralelo
_ultimo_sync: datetime | None = None
_MIN_INTERVALO_SEG = 55 * 60  # 55 minutos entre syncs


def ejecutar_sync(app=None):
    """
    Descarga el catálogo completo de items Siesa y hace upsert en la BD.
    Debe llamarse dentro de un contexto Flask (o pasarle app para crearlo aquí).
    Retorna dict con estadísticas.
    """
    global _ultimo_sync

    # Guard: no ejecutar si corrió hace menos de 55 min (protege contra workers múltiples)
    ahora = datetime.now(timezone.utc)
    if _ultimo_sync and (ahora - _ultimo_sync).total_seconds() < _MIN_INTERVALO_SEG:
        logger.info('[SYNC] Omitido — sync reciente hace menos de 55 min')
        return {'omitido': True, 'razon': 'sync_reciente'}

    def _run():
        if connekta.modo_simulacion:
            logger.warning('[SYNC] Modo simulación — sin acceso real a Siesa')
            return {'simulado': True}

        creados = 0
        actualizados = 0
        errores = 0
        total_procesados = 0

        for pag in range(1, 51):  # hasta 5 000 items (50 páginas × 100)
            try:
                resp = connekta.get_items_catalogo(pag)
                rows = resp.get('detalle', {}).get('Table', [])

                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break

                for row in rows:
                    try:
                        codigo_siesa = (row.get('f120_referencia') or '').strip()
                        nombre = (row.get('f120_descripcion') or '').strip()
                        activo = str(row.get('f120_ind_estado', '1')) == '1'

                        if not codigo_siesa:
                            continue

                        total_procesados += 1

                        prod = (Producto.query.filter_by(codigo_siesa=codigo_siesa).first()
                                or Producto.query.filter_by(codigo=codigo_siesa).first())

                        if prod:
                            changed = False
                            if nombre and prod.nombre != nombre:
                                prod.nombre = nombre
                                changed = True
                            if prod.codigo_siesa != codigo_siesa:
                                prod.codigo_siesa = codigo_siesa
                                changed = True
                            if prod.activo != activo:
                                prod.activo = activo
                                changed = True
                            if changed:
                                actualizados += 1
                        else:
                            prod = Producto(
                                codigo=codigo_siesa,
                                nombre=nombre or f'Producto {codigo_siesa}',
                                codigo_siesa=codigo_siesa,
                                activo=activo,
                                clasificacion_abc='C'
                            )
                            db.session.add(prod)
                            creados += 1

                    except Exception as e:
                        logger.warning(f'[SYNC] Item inválido: {e}')
                        errores += 1

                db.session.commit()
                logger.info(f'[SYNC] Página {pag}: {len(rows)} items procesados')

                if len(rows) < 100:
                    break

            except Exception as e:
                logger.error(f'[SYNC] Error en página {pag}: {e}')
                db.session.rollback()
                break

        resultado = {
            'timestamp': datetime.utcnow().isoformat(),
            'total_procesados': total_procesados,
            'creados': creados,
            'actualizados': actualizados,
            'errores': errores
        }
        logger.info(f'[SYNC] Completado: {resultado}')
        return resultado

    if app:
        with app.app_context():
            resultado = _run()
    else:
        resultado = _run()

    if not resultado.get('simulado') and not resultado.get('omitido'):
        _ultimo_sync = ahora

    return resultado


def init_scheduler(app):
    """
    Inicia el BackgroundScheduler de APScheduler con un cron que corre cada hora
    de 7am a 8pm hora Colombia (UTC-5 → horas UTC 12 a 01 del día siguiente).
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[SYNC] APScheduler no instalado — scheduler no iniciado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=ejecutar_sync,
        trigger=CronTrigger(hour='7-20', minute=0, timezone='America/Bogota'),
        kwargs={'app': app},
        id='sync_productos_siesa',
        name='Sync catálogo Siesa → WMS',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300  # 5 min de gracia si el servidor estaba ocupado
    )
    scheduler.start()
    logger.info('[SYNC] Scheduler iniciado — sync cada hora 7am–8pm (Bogotá)')
    return scheduler
