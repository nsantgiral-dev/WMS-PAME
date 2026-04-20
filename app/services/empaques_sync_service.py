"""
Sincronización de empaques Siesa → producto_empaques.

Consume dos APIs:
  - API_v2_ItemsBarras (query 28): códigos de barras por ítem
  - API_v2_ItemsUnidadesMedida (query 35): factores de conversión por ítem

Estrategia de upsert:
  - Si ya existe un empaque con mismo producto_id + codigo_barras → actualiza factor
  - Si no existe → inserta con origen='SIESA_GS1'
  - Registros con origen='WMS_LPN' nunca se tocan

Cron: diariamente a las 02:30 hora Bogotá (30 min después del sync de barcodes).
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

_sync_estado = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}


def _run_sync(app):
    global _sync_estado
    with app.app_context():
        insertados = 0
        actualizados = 0
        sin_producto = 0
        errores = 0

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
            # Clave: (producto_id, codigo_barras) → ProductoEmpaque
            empaques_existentes = {}
            for e in ProductoEmpaque.query.filter_by(activo=True).all():
                empaques_existentes[(e.producto_id, e.codigo_barras)] = e

            # ── Paso C: sync desde API_v2_ItemsBarras (query 28) ───────────────
            # Esta API devuelve los códigos de barras EAN de cada ítem.
            # Usamos esto para registrar el empaque UND base (factor=1) si no existe.
            campo_barras = None
            for pag in range(1, 2001):
                resp = connekta._get(connekta.api_barras, {
                    'paginacion': f'numPag={pag}|tamPag=100'
                })
                rows = resp.get('detalle', {}).get('Table', [])
                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break

                for row in rows:
                    try:
                        # Auto-detectar campo de barras en primera fila válida
                        if campo_barras is None:
                            for candidato in ('f131_id', 'f120_codigo_barras', 'f121_id', 'f131_barras'):
                                if row.get(candidato):
                                    campo_barras = candidato
                                    logger.info(f'[EMPAQUES SYNC] Campo barras detectado: {campo_barras}')
                                    break

                        if campo_barras is None:
                            continue

                        codigo_barras = (row.get(campo_barras) or '').strip()
                        referencia    = (row.get('f120_referencia') or '').strip()
                        unidad        = (row.get('f120_id_unidad_medida_inventario') or 'UND').strip()

                        if not codigo_barras or not referencia:
                            continue

                        # Solo EAN reales (dígitos, 8+ chars) — ignorar códigos internos
                        if not (codigo_barras.isdigit() and len(codigo_barras) >= 8):
                            continue

                        prod = prods_por_siesa.get(referencia) or prods_por_codigo.get(referencia)
                        if not prod:
                            sin_producto += 1
                            continue

                        clave = (prod.id, codigo_barras)
                        if clave in empaques_existentes:
                            # Ya existe — no pisar si es WMS_LPN
                            emp = empaques_existentes[clave]
                            if emp.origen == 'SIESA_GS1' and emp.unidad_medida != unidad:
                                emp.unidad_medida = unidad
                                actualizados += 1
                        else:
                            # Insertar nuevo empaque UND base (factor=1, se actualiza en Paso D)
                            emp = ProductoEmpaque(
                                producto_id=prod.id,
                                referencia_item=referencia,
                                codigo_barras=codigo_barras,
                                unidad_medida=unidad,
                                factor_conversion=1,
                                origen='SIESA_GS1',
                                activo=True,
                            )
                            db.session.add(emp)
                            empaques_existentes[clave] = emp
                            insertados += 1

                    except Exception as e:
                        logger.warning(f'[EMPAQUES SYNC] Fila barras inválida: {e}')
                        errores += 1

                db.session.commit()
                logger.info(f'[EMPAQUES SYNC] Barras pág {pag}: insertados={insertados}')

            # ── Paso D: sync desde API_v2_ItemsUnidadesMedida (query 35) ──────
            # Esta API devuelve los factores de conversión de empaques.
            # Cada fila: referencia + unidad + factor + codigo_barras_empaque
            # Con esto creamos/actualizamos los empaques de nivel superior (CAJ, PACA, etc.)
            campo_factor = None
            campo_barras_emp = None

            for pag in range(1, 2001):
                resp = connekta.get_items_unidades_medida(pag)
                rows = resp.get('detalle', {}).get('Table', [])
                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break

                for row in rows:
                    try:
                        # Auto-detectar campos en primera fila válida
                        if campo_factor is None:
                            for candidato in ('f122_factor', 'f120_factor', 'factor', 'f120_cant_paquete'):
                                if row.get(candidato) is not None:
                                    campo_factor = candidato
                                    break
                        if campo_barras_emp is None:
                            for candidato in ('f121_id', 'f131_id', 'f120_codigo_barras', 'codigo_barras_empaque'):
                                if row.get(candidato):
                                    campo_barras_emp = candidato
                                    break

                        if campo_factor is None:
                            # Sin factor no podemos hacer nada — log y break
                            logger.warning(f'[EMPAQUES SYNC] No se detectó campo factor. Claves: {list(row.keys())}')
                            break

                        referencia = (row.get('f120_referencia') or '').strip()
                        unidad     = (row.get('f122_id_unidad') or '').strip()
                        try:
                            factor = int(float(row.get(campo_factor) or 1))
                        except (ValueError, TypeError):
                            factor = 1

                        if factor <= 1 or not referencia or not unidad:
                            continue  # factor=1 ya lo maneja Paso C, o datos incompletos

                        prod = prods_por_siesa.get(referencia) or prods_por_codigo.get(referencia)
                        if not prod:
                            sin_producto += 1
                            continue

                        # Código de barras del empaque (DUN-14 u otro)
                        barcode_emp = ''
                        if campo_barras_emp:
                            barcode_emp = (row.get(campo_barras_emp) or '').strip()

                        # Si no hay barcode del empaque, usamos referencia+unidad como clave sintética
                        clave_barcode = barcode_emp if barcode_emp else f'{referencia}-{unidad}'
                        clave = (prod.id, clave_barcode)

                        if clave in empaques_existentes:
                            emp = empaques_existentes[clave]
                            if emp.origen == 'SIESA_GS1':
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
                                codigo_barras=clave_barcode,
                                unidad_medida=unidad,
                                factor_conversion=factor,
                                origen='SIESA_GS1',
                                activo=True,
                            )
                            db.session.add(emp)
                            empaques_existentes[clave] = emp
                            insertados += 1

                    except Exception as e:
                        logger.warning(f'[EMPAQUES SYNC] Fila unidades inválida: {e}')
                        errores += 1

                db.session.commit()
                logger.info(f'[EMPAQUES SYNC] Unidades pág {pag}: insertados={insertados} actualizados={actualizados}')

            resultado = {
                'insertados': insertados,
                'actualizados': actualizados,
                'sin_producto_local': sin_producto,
                'errores': errores,
                'campo_barras_detectado': campo_barras,
                'campo_factor_detectado': campo_factor,
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


def ejecutar_sync(app):
    global _sync_estado
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
