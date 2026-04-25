"""
Sincronización de empaques Siesa → producto_empaques.

Estrategia JOIN confirmada por inspección directa de APIs:

  Query 28 (API_v2_ItemsBarras):
    - f120_referencia        → código producto en Siesa
    - f131_id                → código de barras (EAN-13, DUN-14, interno)
    - f131_id_unidad_medida  → tipo de unidad al que pertenece ese barcode
                               ('UND ' → unidad base, 'CAJ ', 'PACA' → empaque)

  Query 35 (API_v2_ItemsUnidadesMedida):
    - f120_referencia        → código producto en Siesa
    - f122_id_unidad         → tipo de unidad (mismo valor que f131_id_unidad_medida)
    - f122_factor            → factor de conversión respecto a UND base

  JOIN: f131_id_unidad_medida (q28) == f122_id_unidad (q35)
        agrupando por f120_referencia

Algoritmo:
  Paso A: cargar TODO q35 en memoria → factores[(referencia, unidad)] = factor
  Paso B: paginar q28, para cada barcode:
          unidad = f131_id_unidad_medida.strip()
          si unidad == 'UND' → factor=1 (empaque base)
          si no              → factor = factores.get((referencia, unidad), 1)
          upsert en producto_empaques

Reglas:
  - Registros origen='WMS_LPN' nunca se modifican
  - Solo EAN reales (dígitos, ≥8 chars) — ignora códigos internos tipo 'SC-6182'
  - factor=1 y unidad='UND' → empaque base (UND individual)

Cron: diariamente a las 02:30 hora Bogotá.
Trigger manual: POST /api/empaques/sync (solo admin).
"""
import logging
import threading
from datetime import datetime
from app.extensions import db
from app.models.producto import Producto
from app.models.producto_empaque import ProductoEmpaque
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

_sync_lock = threading.Lock()
_sync_estado = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}


def _cargar_factores_q35():
    """
    Carga TODA la data de query 35 en memoria.
    Retorna dict: {(referencia_strip, unidad_strip): factor_int}
    """
    factores = {}
    total_filas = 0
    for pag in range(1, 2001):
        resp = connekta.get_items_unidades_medida(pag)
        rows = resp.get('detalle', {}).get('Table', [])
        if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
            break

        for row in rows:
            try:
                referencia = (row.get('f120_referencia') or '').strip()
                unidad     = (row.get('f122_id_unidad') or '').strip()
                try:
                    factor = int(float(row.get('f122_factor') or 1))
                except (ValueError, TypeError):
                    factor = 1

                if referencia and unidad and factor > 0:
                    factores[(referencia, unidad)] = factor
                    total_filas += 1
            except Exception as e:
                logger.warning(f'[EMPAQUES SYNC] q35 fila inválida: {e}')

        logger.info(f'[EMPAQUES SYNC] q35 pág {pag}: acumulados={total_filas}')

    logger.info(f'[EMPAQUES SYNC] q35 completo: {len(factores)} combinaciones (ref, unidad)')
    return factores


_ADVISORY_LOCK_EMPAQUES = 2001  # clave única para pg_advisory_lock


def _run_sync(app):
    global _sync_estado
    with app.app_context():
        insertados = 0
        actualizados = 0
        sin_producto = 0
        sin_factor = 0
        errores = 0

        # Advisory lock de PostgreSQL — protege contra ejecución simultánea entre workers
        from sqlalchemy import text as _text
        lock_adquirido = False
        try:
            lock_adquirido = db.session.execute(
                _text('SELECT pg_try_advisory_lock(:key)'), {'key': _ADVISORY_LOCK_EMPAQUES}
            ).scalar()
            if not lock_adquirido:
                logger.info('[EMPAQUES SYNC] Otro worker ya ejecuta el sync — omitido')
                _sync_estado['en_curso'] = False
                return
        except Exception as e:
            logger.warning(f'[EMPAQUES SYNC] Advisory lock no disponible: {e} — continuando sin él')

        try:
            # ── Paso A: cargar productos en memoria ────────────────────────────
            prods_por_siesa = {}
            prods_por_codigo = {}
            for p in Producto.query.filter_by(activo=True).all():
                if p.codigo_siesa:
                    prods_por_siesa[p.codigo_siesa.strip()] = p
                if p.codigo:
                    prods_por_codigo[p.codigo.strip()] = p
            logger.info(f'[EMPAQUES SYNC] Productos en memoria: {len(prods_por_siesa)}')

            # ── Paso B: cargar empaques existentes en memoria ──────────────────
            empaques_existentes = {}
            for e in ProductoEmpaque.query.filter_by(activo=True).all():
                empaques_existentes[(e.producto_id, e.codigo_barras)] = e

            # ── Paso C: cargar factores de q35 en memoria ──────────────────────
            # factores[(referencia, unidad)] = factor_int
            factores_q35 = _cargar_factores_q35()
            if not factores_q35:
                logger.error('[EMPAQUES SYNC] q35 retornó vacío (Connekta caído o sin datos) — sync abortado para evitar corrupción de factores')
                _sync_estado['ultimo_error'] = 'q35 vacío — sync abortado'
                return

            # Liberar la conexión BD antes de las horas de HTTP calls en q28
            # Los dicts en memoria (prods_por_siesa, empaques_existentes) conservan
            # los atributos que necesitamos aunque los objetos queden detached.
            db.session.commit()

            # ── Paso D: paginar q28 y hacer JOIN con factores_q35 ─────────────
            # Campos confirmados:
            #   f120_referencia        → código Siesa
            #   f131_id                → código de barras
            #   f131_id_unidad_medida  → tipo de unidad (JOIN key con q35)
            for pag in range(1, 2001):
                resp = connekta._get(connekta.api_barras, {
                    'paginacion': f'numPag={pag}|tamPag=100'
                })
                rows = resp.get('detalle', {}).get('Table', [])
                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break

                for row in rows:
                    try:
                        codigo_barras = (row.get('f131_id') or '').strip()
                        referencia    = (row.get('f120_referencia') or '').strip()
                        unidad_raw    = (row.get('f131_id_unidad_medida') or 'UND').strip()

                        if not codigo_barras or not referencia:
                            continue

                        # Solo EAN reales (dígitos, ≥8 chars) — ignora 'SC-6182', etc.
                        if not (codigo_barras.isdigit() and len(codigo_barras) >= 8):
                            continue

                        prod = prods_por_siesa.get(referencia) or prods_por_codigo.get(referencia)
                        if not prod:
                            sin_producto += 1
                            continue

                        # JOIN con q35: obtener factor para esta unidad
                        if unidad_raw == 'UND':
                            factor = 1
                        else:
                            factor = factores_q35.get((referencia, unidad_raw))
                            if factor is None:
                                # Intentar con variantes de la referencia
                                factor = factores_q35.get((referencia.upper(), unidad_raw))
                            if factor is None:
                                sin_factor += 1
                                factor = 1  # fallback seguro

                        unidad = unidad_raw if unidad_raw else 'UND'

                        clave = (prod.id, codigo_barras)
                        if clave in empaques_existentes:
                            emp = empaques_existentes[clave]
                            # WMS_LPN nunca se toca
                            if emp.origen == 'WMS_LPN':
                                continue
                            changed = False
                            if emp.factor_conversion != factor:
                                emp.factor_conversion = factor
                                changed = True
                            if emp.unidad_medida != unidad:
                                emp.unidad_medida = unidad
                                changed = True
                            if changed:
                                actualizados += 1
                        else:
                            emp = ProductoEmpaque(
                                producto_id=prod.id,
                                referencia_item=referencia,
                                codigo_barras=codigo_barras,
                                unidad_medida=unidad,
                                factor_conversion=factor,
                                origen='SIESA_GS1',
                                activo=True,
                            )
                            db.session.add(emp)
                            empaques_existentes[clave] = emp
                            insertados += 1

                    except Exception as e:
                        logger.warning(f'[EMPAQUES SYNC] Fila q28 inválida: {e}')
                        errores += 1

                db.session.commit()
                logger.info(
                    f'[EMPAQUES SYNC] q28 pág {pag}: '
                    f'insertados={insertados} actualizados={actualizados} '
                    f'sin_factor={sin_factor}'
                )

            resultado = {
                'insertados': insertados,
                'actualizados': actualizados,
                'sin_producto_local': sin_producto,
                'sin_factor_q35': sin_factor,
                'errores': errores,
                'factores_q35_cargados': len(factores_q35),
                'timestamp': datetime.utcnow().isoformat(),
            }
            _sync_estado['ultimo_resultado'] = resultado
            _sync_estado['ultimo_error'] = None
            logger.info(f'[EMPAQUES SYNC] Completado: {resultado}')

        except Exception as e:
            db.session.rollback()
            _sync_estado['ultimo_error'] = str(e)
            logger.error(f'[EMPAQUES SYNC] Error fatal: {e}')
        finally:
            _sync_estado['en_curso'] = False
            if lock_adquirido:
                try:
                    db.session.execute(
                        _text('SELECT pg_advisory_unlock(:key)'), {'key': _ADVISORY_LOCK_EMPAQUES}
                    )
                    db.session.commit()
                except Exception:
                    pass


def ejecutar_sync(app):
    global _sync_estado
    with _sync_lock:
        if _sync_estado['en_curso']:
            logger.info('[EMPAQUES SYNC] Ya hay un sync en curso, se omite')
            return
        _sync_estado['en_curso'] = True
        _sync_estado['ultimo_inicio'] = datetime.utcnow().isoformat()
    t = threading.Thread(target=_run_sync, args=(app,), daemon=True)
    t.start()


def get_estado():
    return dict(_sync_estado)


def init_scheduler(app):
    """Cron diario a las 02:30 hora Bogotá — después del sync de barcodes (02:00)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[EMPAQUES SYNC] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=ejecutar_sync,
        trigger=CronTrigger(hour=2, minute=30, timezone='America/Bogota'),
        kwargs={'app': app},
        id='sync_empaques_siesa',
        name='Sync empaques/LPN Siesa → WMS (02:30 Bogotá)',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300
    )
    scheduler.start()
    logger.info('[EMPAQUES SYNC] Scheduler iniciado — sync diario 02:30 Bogotá')
    return scheduler
