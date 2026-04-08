"""
Sincronización del catálogo de productos Siesa → WMS.

El sync puede tardar minutos (múltiples páginas × llamadas HTTP).
Para evitar timeout de gunicorn (30 seg), el sync manual corre en hilo de fondo
y el endpoint retorna inmediatamente con el estado.
"""
import logging
import threading
from datetime import datetime, timezone
from app.extensions import db
from app.models.producto import Producto
from app.models.siesa_mapeo_unidades import SiesaMapeоUnidades
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

# Estado global del sync (acceso desde ruta y scheduler)
_sync_estado = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,   # dict con estadísticas del último sync exitoso
    'ultimo_error': None,
}
_MIN_INTERVALO_SEG = 4 * 60  # 4 min — deja margen al scheduler de 5 min


def _run_sync(app):
    """Lógica real del sync — se ejecuta en un hilo separado con su propio app_context."""
    global _sync_estado

    with app.app_context():
        creados = 0
        actualizados = 0
        errores = 0
        total_procesados = 0

        try:
            # Cargar tabla de mapeo en memoria para evitar N queries por item
            mapeo_unidades = {
                m.tipo_inv_siesa: m.unidad_negocio_id
                for m in SiesaMapeоUnidades.query.all()
            }
            tipos_sin_mapeo = set()  # tipos que Siesa devuelve pero no están en nuestra tabla

            for pag in range(1, 501):  # hasta 50 000 items (500 págs × 100) — catálogo 28k+
                resp = connekta.get_items_catalogo(pag)
                rows = resp.get('detalle', {}).get('Table', [])

                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break

                for row in rows:
                    try:
                        codigo_siesa = (row.get('f120_referencia') or '').strip()
                        nombre = (row.get('f120_descripcion') or '').strip()
                        activo = str(row.get('f120_ind_estado', '1')) == '1'
                        unidad_medida = (row.get('f120_id_unidad_medida_inventario') or '').strip() or None
                        tipo_inv = (row.get('f120_id_tipo_inv_serv') or '').strip()
                        unidad_negocio = mapeo_unidades.get(tipo_inv) if tipo_inv else None

                        if tipo_inv and unidad_negocio is None:
                            tipos_sin_mapeo.add(tipo_inv)

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
                            if unidad_medida and prod.unidad_medida != unidad_medida:
                                prod.unidad_medida = unidad_medida
                                changed = True
                            # Solo sobreescribir si el mapeo existe; si no, no borrar lo que haya
                            if unidad_negocio and prod.unidad_negocio_id != unidad_negocio:
                                prod.unidad_negocio_id = unidad_negocio
                                changed = True
                            if changed:
                                actualizados += 1
                        else:
                            prod = Producto(
                                codigo=codigo_siesa,
                                nombre=nombre or f'Producto {codigo_siesa}',
                                codigo_siesa=codigo_siesa,
                                activo=activo,
                                clasificacion_abc='C',
                                unidad_medida=unidad_medida or 'UND',
                                unidad_negocio_id=unidad_negocio,
                            )
                            db.session.add(prod)
                            creados += 1

                    except Exception as e:
                        logger.warning(f'[SYNC] Item inválido: {e}')
                        errores += 1

                db.session.commit()
                logger.info(f'[SYNC] Página {pag}: {len(rows)} items · creados={creados}')

                if len(rows) < 100:
                    break

        except Exception as e:
            logger.error(f'[SYNC] Error durante sync: {e}')
            db.session.rollback()
            _sync_estado['en_curso'] = False
            _sync_estado['ultimo_error'] = str(e)
            return

        if tipos_sin_mapeo:
            lista = sorted(tipos_sin_mapeo)
            logger.warning(
                f'[SYNC] ALERTA: {len(lista)} tipo(s) de inventario Siesa sin mapeo de '
                f'Unidad de Negocio — productos quedarán con unidad_negocio_id=NULL. '
                f'Configura en /api/config/mapeo-unidades: {lista}'
            )

        resultado = {
            'timestamp': datetime.utcnow().isoformat(),
            'total_procesados': total_procesados,
            'creados': creados,
            'actualizados': actualizados,
            'errores': errores,
            'tipos_sin_mapeo': sorted(tipos_sin_mapeo) if tipos_sin_mapeo else [],
        }
        logger.info(f'[SYNC] Completado: {resultado}')
        _sync_estado['ultimo_resultado'] = resultado
        _sync_estado['ultimo_error'] = None
        _sync_estado['en_curso'] = False


def iniciar_sync_background(app, forzar=False):
    """
    Arranca el sync en un hilo de fondo y retorna inmediatamente.
    forzar=True: el admin dispara manualmente — ignora el guard de intervalo.
    forzar=False: llamada automática del scheduler — respeta el guard.
    """
    global _sync_estado

    ahora = datetime.now(timezone.utc)

    if _sync_estado['en_curso']:
        return {'en_curso': True, 'mensaje': 'Sync ya en proceso — espera que termine'}

    if not forzar:
        ultimo = _sync_estado.get('ultimo_inicio')
        if ultimo and (ahora - ultimo).total_seconds() < _MIN_INTERVALO_SEG:
            return {'omitido': True, 'mensaje': 'Sync reciente — scheduler omite esta vuelta'}

    if connekta.modo_simulacion:
        return {'simulado': True, 'mensaje': 'Modo simulación — conecta credenciales Siesa'}

    _sync_estado['en_curso'] = True
    _sync_estado['ultimo_inicio'] = ahora

    hilo = threading.Thread(target=_run_sync, args=(app,), daemon=True)
    hilo.start()

    return {'iniciado': True, 'mensaje': 'Sync iniciado en background — refresca en ~30 seg'}


def estado_sync():
    """Retorna el estado actual del último sync."""
    return {
        'en_curso': _sync_estado['en_curso'],
        'ultimo_inicio': _sync_estado['ultimo_inicio'].isoformat() if _sync_estado['ultimo_inicio'] else None,
        'ultimo_resultado': _sync_estado['ultimo_resultado'],
        'ultimo_error': _sync_estado['ultimo_error'],
    }


def ejecutar_sync(app=None):
    """
    Compatibilidad con el scheduler de APScheduler (corre en su propio hilo).
    Llama directamente a _run_sync() ya que el scheduler maneja el threading.
    """
    if app:
        _run_sync(app)
    # Si no hay app, no puede correr (necesita contexto)


def init_scheduler(app):
    """Scheduler cada 5 min entre 7am y 8pm hora Bogotá."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[SYNC] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=ejecutar_sync,
        trigger=CronTrigger(hour='7-20', minute='*/5', timezone='America/Bogota'),
        kwargs={'app': app},
        id='sync_productos_siesa',
        name='Sync catálogo Siesa → WMS cada 5 min',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60
    )
    scheduler.start()
    logger.info('[SYNC] Scheduler iniciado — sync cada 5 min 7am–8pm (Bogotá)')
    return scheduler
