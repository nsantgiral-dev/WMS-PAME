"""
Servicio ABC — El WMS NO calcula clasificación ABC.
Siesa Enterprise tiene su propio motor estadístico.
Este servicio consume la clasificación de Siesa y genera tareas de conteo.

Frecuencias de conteo cíclico (estrategia de costos bajos):
  A — Alta rotación  → contar cada 15 días
  B — Rotación media → contar cada 90 días
  C — Baja rotación  → contar cada 180 días

AI Watchdog — Anomaly Override:
  Si un producto clase B o C registra picks >= umbral en los últimos 7 días,
  el WMS ignora la regla de frecuencia y fuerza un conteo inmediato.
  Esto captura picos estacionales antes de que Siesa recalcule (mensual).
  Umbrales (picks en 7 días):
    C → >= 10 picks → override a A (conteo inmediato)
    B → >= 25 picks → override a A (conteo inmediato)
"""
import uuid
import logging
from datetime import datetime, timedelta
from sqlalchemy import func
from app.extensions import db
from app.models.conteo import SesionConteo
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.models.producto_clasificacion_abc import ProductoClasificacionABC
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

# Días mínimos entre conteos por clase
FRECUENCIA_DIAS = {'A': 15, 'B': 90, 'C': 180}

# Watchdog: picks en 7 días que disparan override a clase A
WATCHDOG_UMBRAL = {'B': 25, 'C': 10}
WATCHDOG_VENTANA_DIAS = 7


class ABCService:

    @staticmethod
    def sincronizar_clasificacion_desde_siesa(api_abc: str = None):
        """
        Extrae la clasificación ABC de Siesa y actualiza los productos en el WMS.
        El WMS no calcula — solo sincroniza lo que Siesa ya calculó.

        API_v2_Items (ID 27) NO expone clasificación ABC — solo datos maestros.
        Se requiere un endpoint custom del Gestor de Consultas de Connekta V2.
        El parámetro api_abc acepta el nombre de esa consulta cuando el consultor
        la entregue (ej. 'API_custom_ABC_Rotacion').

        Mientras no exista ese endpoint, este método retorna pending=True y
        el scheduler diario lo omite sin error — el sistema sigue funcionando
        con la clasificación que Siesa estampe en el maestro de productos.
        """
        if connekta.modo_simulacion:
            logger.info('[ABC] Modo simulación — sincronización ABC omitida')
            return {'simulado': True, 'productos_actualizados': 0}

        if not api_abc:
            logger.info('[ABC] Endpoint ABC no configurado — pendiente Gestor de Consultas Connekta')
            return {
                'pending': True,
                'mensaje': (
                    'Requiere endpoint custom ABC del Gestor de Consultas de Connekta V2. '
                    'API_v2_Items (ID 27) no expone clasificación ABC. '
                    'Pedir al consultor: SELECT f120_referencia, clasificacion_abc FROM ... '
                    'y publicarla como API en el Gestor de Consultas.'
                ),
                'productos_actualizados': 0
            }

        try:
            # Paginar — puede haber miles de items
            actualizados = 0
            pag = 1
            while True:
                res = connekta._get(api_abc, {'paginacion': f'numPag={pag}|tamPag=200'})
                rows = res.get('detalle', {}).get('Table', [])
                if not rows:
                    break

                # Pre-cargar todos los productos de la página en un solo query
                codigos_pagina = list({
                    (r.get('f120_referencia') or '').strip()
                    for r in rows
                    if (r.get('f120_referencia') or '').strip()
                })
                productos_mapa = {
                    p.codigo_siesa: p
                    for p in Producto.query.filter(
                        Producto.codigo_siesa.in_(codigos_pagina)
                    ).all()
                }

                for row in rows:
                    codigo = (row.get('f120_referencia') or '').strip()
                    clasificacion = (row.get('clasificacion_abc') or '').strip().upper()
                    if not codigo or clasificacion not in ('A', 'B', 'C'):
                        continue
                    producto = productos_mapa.get(codigo)
                    if producto and producto.clasificacion_abc != clasificacion:
                        producto.clasificacion_abc = clasificacion
                        actualizados += 1

                if len(rows) < 200:
                    break
                pag += 1

            db.session.commit()
            logger.info(f'[ABC] {actualizados} productos reclasificados desde Siesa')
            return {'productos_actualizados': actualizados}

        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC] Error sincronizando desde Siesa: {e}')
            raise

    @staticmethod
    def watchdog_anomalias(almacen_id: int) -> list:
        """
        AI Watchdog — detecta productos cuya rotación real supera su clase ABC.

        Consulta cuántas veces fue picado cada producto en los últimos 7 días.
        Si un producto clase C supera 10 picks, o clase B supera 25 picks,
        fuerza una SesionConteo inmediata ignorando la regla de frecuencia.

        Retorna lista de overrides ejecutados: [{producto_id, clase_actual,
        picks_7dias, umbral, sesion_codigo}]
        """
        from app.models.picking import TareaPicking
        from app.models.inventario import UbicacionProducto

        # [A3] Advisory lock por almacén — evita ejecuciones concurrentes (scheduler + API manual).
        # Clave: 3000 + almacen_id (distinto de 2003 del scheduler general para no bloquear entre sí).
        _lock_key = 3000 + almacen_id
        _lock_acquired = db.session.execute(
            db.text(f'SELECT pg_try_advisory_lock({_lock_key})')
        ).scalar()
        if not _lock_acquired:
            logger.info(f'[ABC WATCHDOG] Almacén {almacen_id} — lock no disponible, omitiendo ejecución concurrente')
            return []

        ventana = datetime.utcnow() - timedelta(days=WATCHDOG_VENTANA_DIAS)
        overrides = []

        # [A1] Wrap everything in try/finally so advisory lock is always released,
        # even if an exception occurs during queries before the commit.
        try:
            # Pre-cargar todos los conteos activos del almacén en un set (producto_id, ubicacion_id)
            # para evitar N+1 en el check de duplicados dentro del loop
            conteos_activos = {
                (sc.producto_id, sc.ubicacion_id)
                for sc in SesionConteo.query.filter(
                    SesionConteo.almacen_id == almacen_id,
                    SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
                ).with_entities(SesionConteo.producto_id, SesionConteo.ubicacion_id).all()
            }

            # Clases susceptibles de override — consulta por almacén
            for clase, umbral in WATCHDOG_UMBRAL.items():
                productos_clase = (
                    Producto.query
                    .join(ProductoClasificacionABC,
                          ProductoClasificacionABC.producto_id == Producto.id)
                    .filter(
                        ProductoClasificacionABC.almacen_id == almacen_id,
                        ProductoClasificacionABC.clasificacion == clase,
                        Producto.activo == True
                    ).all()
                )

                if not productos_clase:
                    continue

                # Pre-cargar todos los counts de picks en un solo query GROUP BY
                producto_ids_clase = [p.id for p in productos_clase]
                picks_rows = (
                    db.session.query(TareaPicking.producto_id, func.count(TareaPicking.id))
                    .filter(
                        TareaPicking.producto_id.in_(producto_ids_clase),
                        TareaPicking.estado == 'COMPLETADO',  # [A] corrección typo: era 'COMPLETADA'
                        TareaPicking.fecha_completado >= ventana
                    )
                    .group_by(TareaPicking.producto_id)
                    .all()
                )
                picks_por_producto = {pid: cnt for pid, cnt in picks_rows}

                # Pre-cargar UbicacionProducto solo para productos que superan el umbral
                ids_sobre_umbral = [
                    p.id for p in productos_clase
                    if picks_por_producto.get(p.id, 0) >= umbral
                ]
                if not ids_sobre_umbral:
                    continue
                registros_por_prod: dict = {}
                for reg in (
                    UbicacionProducto.query
                    .join(Ubicacion)
                    .filter(
                        UbicacionProducto.producto_id.in_(ids_sobre_umbral),
                        UbicacionProducto.cantidad > 0,
                        Ubicacion.almacen_id == almacen_id
                    ).all()
                ):
                    registros_por_prod.setdefault(reg.producto_id, []).append(reg)

                for producto in productos_clase:
                    picks = picks_por_producto.get(producto.id, 0)

                    if picks < umbral:
                        continue

                    registros = registros_por_prod.get(producto.id, [])

                    for reg in registros:
                        # Verificación en memoria (evita duplicar dentro de la misma corrida)
                        if (producto.id, reg.ubicacion_id) in conteos_activos:
                            continue

                        # Verificación en DB justo antes del insert — reduce ventana de race condition
                        # entre dos workers que hayan pasado simultáneamente el check en memoria.
                        ya_existe = SesionConteo.query.filter(
                            SesionConteo.producto_id == producto.id,
                            SesionConteo.ubicacion_id == reg.ubicacion_id,
                            SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
                        ).first()
                        if ya_existe:
                            conteos_activos.add((producto.id, reg.ubicacion_id))
                            continue

                        codigo = (
                            f'CC-WATCHDOG-'
                            f'{datetime.utcnow().strftime("%Y%m%d")}-'
                            f'{str(uuid.uuid4())[:6].upper()}'
                        )
                        sesion = SesionConteo(
                            codigo=codigo,
                            tipo='WATCHDOG_ABC',
                            clasificacion_abc='A',   # override temporal
                            ubicacion_id=reg.ubicacion_id,
                            almacen_id=almacen_id,
                            producto_id=producto.id,
                            producto_codigo_siesa=producto.codigo_siesa,
                            maneja_lote=bool(getattr(reg, 'lote', None)),
                            estado='PENDIENTE'
                        )
                        from sqlalchemy.exc import IntegrityError as _IE_wd
                        _sp = db.session.begin_nested()
                        try:
                            db.session.add(sesion)
                            db.session.flush()
                            _sp.commit()
                            conteos_activos.add((producto.id, reg.ubicacion_id))  # evita duplicar en misma corrida
                            overrides.append({
                                'producto_id': producto.id,
                                'producto_codigo': producto.codigo_siesa or producto.codigo,
                                'clase_actual': clase,
                                'picks_7dias': picks,
                                'umbral': umbral,
                                'sesion_codigo': codigo,
                            })
                        except _IE_wd:
                            _sp.rollback()
                            logger.warning(f'[ABC WATCHDOG] Sesión duplicada ignorada — prod {producto.id} ubic {reg.ubicacion_id}')

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC WATCHDOG] Error en watchdog: {e}')
            raise
        finally:
            # [M2] Envolver en try propio: excepción aquí no debe reemplazar el resultado exitoso
            # del try ni del except principal (enmascararía overrides correctamente aplicados).
            try:
                db.session.execute(db.text(f'SELECT pg_advisory_unlock({_lock_key})'))
                db.session.commit()
            except Exception as _fe:
                logger.error(f'[ABC WATCHDOG] Error liberando advisory lock {_lock_key}: {_fe}')

        if overrides:
            logger.warning(
                f'[ABC WATCHDOG] {len(overrides)} overrides en almacén {almacen_id}: '
                + ', '.join(f"{o['producto_codigo']} ({o['clase_actual']}→A, {o['picks_7dias']} picks)" for o in overrides)
            )
        else:
            logger.info(f'[ABC WATCHDOG] Sin anomalías en almacén {almacen_id}')

        return overrides

    @staticmethod
    def generar_tareas_conteo_diario(almacen_id: int, clasificacion: str = 'A',
                                     forzar_todo: bool = False):
        """
        Genera tareas de conteo cíclico con lote diario proporcional.

        Modo normal (forzar_todo=False — scheduler 6am o botón "Lote de hoy"):
          Calcula cuántos productos hay en la clase y divide por la frecuencia
          para generar solo la "dosis diaria":
            A (15 días):  N_A ÷ 15   ≈  X productos/día
            B (90 días):  N_B ÷ 90   ≈  X productos/día
            C (180 días): N_C ÷ 180  ≈  X productos/día
          Los candidatos se ordenan por "última vez contado" (nunca contados primero),
          garantizando rotación equitativa del catálogo.

        Modo completo (forzar_todo=True — botón "Forzar todo"):
          Genera tarea para CADA producto elegible sin límite.
          Útil para arranque inicial o revisión de emergencia.
        """
        import math
        from app.models.inventario import UbicacionProducto

        frecuencia = FRECUENCIA_DIAS.get(clasificacion, 15)
        umbral = datetime.utcnow() - timedelta(days=frecuencia)

        # Solo productos con stock > 0 en este almacén — sin stock no hay nada que contar
        # Fetch solo columnas necesarias para reducir footprint de memoria
        todos_productos = (
            Producto.query
            .join(ProductoClasificacionABC,
                  ProductoClasificacionABC.producto_id == Producto.id)
            .join(UbicacionProducto,
                  UbicacionProducto.producto_id == Producto.id)
            .join(Ubicacion,
                  Ubicacion.id == UbicacionProducto.ubicacion_id)
            .filter(
                ProductoClasificacionABC.almacen_id == almacen_id,
                ProductoClasificacionABC.clasificacion == clasificacion,
                Producto.activo == True,
                Ubicacion.almacen_id == almacen_id,
                UbicacionProducto.cantidad > 0,
            )
            .with_entities(Producto.id, Producto.codigo_siesa)
            .distinct()
            .all()
        )

        if not todos_productos:
            return {
                'mensaje': f'No hay productos clase {clasificacion} para contar',
                'tareas_creadas': 0,
                'clasificacion': clasificacion,
                'frecuencia_dias': frecuencia,
                'batch_diario': 0,
                'total_clase': 0,
            }

        total_clase = len(todos_productos)
        # Lote diario: cubrir todo el catálogo en exactamente frecuencia_dias
        batch_diario = max(1, math.ceil(total_clase / frecuencia)) if not forzar_todo else None

        # Recopilar candidatos elegibles con su antigüedad de último conteo
        candidatos = []
        omitidos_por_pendiente = 0
        omitidos_por_frecuencia = 0

        # Pre-cargar todos los UbicacionProducto relevantes en un solo query
        producto_ids = [p.id for p in todos_productos]
        todos_registros = (
            UbicacionProducto.query
            .join(Ubicacion)
            .filter(
                UbicacionProducto.producto_id.in_(producto_ids),
                UbicacionProducto.cantidad > 0,
                Ubicacion.almacen_id == almacen_id
            ).all()
        )
        # Agrupar por producto_id
        from collections import defaultdict
        registros_por_producto = defaultdict(list)
        for r in todos_registros:
            registros_por_producto[r.producto_id].append(r)

        # Pre-cargar conteos activos (PENDIENTE/EN_PROCESO/SEGUNDO_CONTEO)
        pares_ubic_prod = [(r.ubicacion_id, r.producto_id) for r in todos_registros]
        if pares_ubic_prod:
            ubic_ids_all = list({x[0] for x in pares_ubic_prod})  # dedup: evita IN clause con N duplicados
            sesiones_activas = SesionConteo.query.filter(
                SesionConteo.producto_id.in_(producto_ids),
                SesionConteo.ubicacion_id.in_(ubic_ids_all),
                SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
            ).all()
            activos_set = {(s.ubicacion_id, s.producto_id) for s in sesiones_activas}

            # Pre-cargar fecha del último conteo completado por (producto_id, ubicacion_id)
            # GROUP BY en SQL evita cargar todas las filas históricas en memoria
            from sqlalchemy import func as _func
            ultimo_rows = (
                db.session.query(
                    SesionConteo.producto_id,
                    SesionConteo.ubicacion_id,
                    _func.max(SesionConteo.fecha_cierre).label('ultima')
                )
                .filter(
                    SesionConteo.producto_id.in_(producto_ids),
                    SesionConteo.ubicacion_id.in_(ubic_ids_all),
                    SesionConteo.estado.in_(['MATCH', 'AJUSTADO'])
                )
                .group_by(SesionConteo.producto_id, SesionConteo.ubicacion_id)
                .all()
            )
            ultimo_por_par = {(r.producto_id, r.ubicacion_id): r.ultima for r in ultimo_rows}
        else:
            activos_set = set()
            ultimo_por_par = {}

        for producto in todos_productos:
            registros = registros_por_producto.get(producto.id, [])

            for reg in registros:
                # Saltar si ya hay tarea activa
                if (reg.ubicacion_id, producto.id) in activos_set:
                    omitidos_por_pendiente += 1
                    continue

                # Obtener fecha del último conteo completado
                ultimo_fecha = ultimo_por_par.get((producto.id, reg.ubicacion_id))

                if ultimo_fecha and ultimo_fecha >= umbral:
                    omitidos_por_frecuencia += 1
                    continue

                # Sort key: nunca contado → datetime mínimo (va primero)
                sort_key = ultimo_fecha if ultimo_fecha else datetime(1970, 1, 1)
                candidatos.append((sort_key, producto, reg))

        # Ordenar: los que llevan más tiempo sin contar van primero
        candidatos.sort(key=lambda x: x[0])

        # Aplicar límite de lote diario
        if batch_diario is not None:
            candidatos = candidatos[:batch_diario]

        # Crear tareas — savepoint por ítem para tolerar race conditions entre
        # scheduler y API sin perder todos los inserts por un único conflicto.
        # La re-verificación dentro del savepoint cierra la ventana entre el
        # activos_set (leído antes) y el INSERT efectivo.
        from sqlalchemy.exc import IntegrityError as _IE
        tareas_creadas = []
        for _, producto, reg in candidatos:
            sp = db.session.begin_nested()
            try:
                # Re-verificar bajo savepoint: otro worker puede haber insertado
                # entre el SELECT de activos_set y este INSERT (race condition scheduler+API)
                ya_existe = SesionConteo.query.filter(
                    SesionConteo.ubicacion_id == reg.ubicacion_id,
                    SesionConteo.producto_id == producto.id,
                    SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
                ).first()
                if ya_existe:
                    sp.rollback()
                    continue

                codigo = (
                    f'CC-{clasificacion}-'
                    f'{datetime.utcnow().strftime("%Y%m%d")}-'
                    f'{str(uuid.uuid4())[:6].upper()}'
                )
                sesion = SesionConteo(
                    codigo=codigo,
                    tipo='DIARIO_ABC',
                    clasificacion_abc=clasificacion,
                    ubicacion_id=reg.ubicacion_id,
                    almacen_id=almacen_id,
                    producto_id=producto.id,
                    producto_codigo_siesa=producto.codigo_siesa,
                    maneja_lote=bool(getattr(reg, 'lote', None)),
                    estado='PENDIENTE'
                )
                db.session.add(sesion)
                db.session.flush()
                sp.commit()
                tareas_creadas.append(sesion)
            except _IE:
                sp.rollback()
                logger.warning(f'[ABC] Sesión duplicada ignorada — prod {producto.id} ubic {reg.ubicacion_id}')

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC] Error guardando tareas de conteo: {e}')
            raise

        # Prewarm consolidado: NO llamar aquí — se hace una única vez en generar_todas_las_clases()
        # para todas las clases A+B+C juntas. Si se llama aquí, el semáforo non-blocking descarta
        # los prewarms de B y C porque el de A todavía está en curso (cold cache garantizado).

        dias_para_ciclo = math.ceil(total_clase / (batch_diario or 1)) if batch_diario else 1

        logger.info(
            f'[ABC] Clase {clasificacion} · {len(tareas_creadas)} tareas creadas '
            f'(lote {batch_diario or "completo"} de {total_clase}) · '
            f'{omitidos_por_pendiente} ya activas · {omitidos_por_frecuencia} dentro de frecuencia'
        )

        return {
            'tareas_creadas': len(tareas_creadas),
            'batch_diario': batch_diario,
            'total_clase': total_clase,
            'dias_para_ciclo_completo': dias_para_ciclo,
            'omitidos_por_pendiente': omitidos_por_pendiente,
            'omitidos_por_frecuencia': omitidos_por_frecuencia,
            'clasificacion': clasificacion,
            'frecuencia_dias': frecuencia,
            'almacen_id': almacen_id,
            'modo': 'completo' if forzar_todo else 'lote_diario',
            '_sesiones_creadas': tareas_creadas,  # para prewarm consolidado en generar_todas_las_clases
        }

    @staticmethod
    def generar_todas_las_clases(almacen_id: int, forzar_todo: bool = False):
        """
        Genera tareas para A, B y C + ejecuta Watchdog de anomalías.
        Usado por el scheduler diario a las 6am (forzar_todo=False).
        El admin puede pasar forzar_todo=True desde la UI para generar todo de una vez.
        """
        resultados = {}
        total = 0
        todas_sesiones_nuevas = []
        for clase in ['A', 'B', 'C']:
            r = ABCService.generar_tareas_conteo_diario(almacen_id, clase,
                                                        forzar_todo=forzar_todo)
            resultados[clase] = r
            total += r['tareas_creadas']
            # Acumular sesiones recién creadas para prewarm consolidado al final
            if r.get('_sesiones_creadas'):
                todas_sesiones_nuevas.extend(r['_sesiones_creadas'])

        # Watchdog DESPUÉS de las tareas normales — detecta reclasificaciones urgentes
        try:
            overrides = ABCService.watchdog_anomalias(almacen_id)
            total += len(overrides)
            resultados['watchdog'] = {
                'overrides': len(overrides),
                'detalle': overrides
            }
        except Exception as e:
            # Productos que debían reclasificarse a A quedan en B/C → sin conteo urgente.
            logger.error(
                f'[ABC WATCHDOG] Error en almacén {almacen_id}: {e} '
                f'— overrides no aplicados; productos de alta rotación sin reclasificar',
                exc_info=True
            )
            resultados['watchdog'] = {'error': str(e)}
            # Intentar notificar por email (best-effort — no lanzar si falla)
            try:
                # [M26] Usar DLQ para que si Resend falla, el job quede visible en la queue
                # y se reintente — sin DLQ, la falla del watchdog queda completamente invisible.
                from app.services.alertas_service import _enviar_email_con_dlq
                _enviar_email_con_dlq(
                    asunto=f'[WMS ALERTA] ABC Watchdog falló — almacén {almacen_id}',
                    cuerpo_texto=(
                        f'El watchdog ABC del almacén {almacen_id} falló con error:\n{e}\n\n'
                        'Los productos de alta rotación no fueron reclasificados a clase A. '
                        'El conteo cíclico urgente no se generará automáticamente.'
                    ),
                    cuerpo_html=None,
                    tipo_alerta=f'abc_watchdog_fallo_{almacen_id}',
                )
            except Exception as _e_email_watchdog:
                logger.critical(
                    f'[ABC WATCHDOG] Email de alerta también falló — la falla del watchdog '
                    f'queda completamente invisible: {_e_email_watchdog}'
                )

        logger.info(f'[ABC] Generación completa · {total} tareas nuevas en almacén {almacen_id}')
        return {'total_tareas_creadas': total, 'por_clase': resultados}

    @staticmethod
    def init_scheduler(app):
        """Scheduler diario a las 6am para generar tareas de conteo cíclico ABC."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            import atexit
        except ImportError:
            logger.error('[ABC] APScheduler no instalado')
            return None

        def _job():
            with app.app_context():
                from app.extensions import db as _db
                # Advisory lock 2003 — garantiza que solo un worker Gunicorn ejecuta
                # este job a la vez. Si el lock no está disponible (otro worker ganó),
                # salir silenciosamente en vez de generar tareas duplicadas.
                lock_acquired = _db.session.execute(
                    _db.text('SELECT pg_try_advisory_lock(2003)')
                ).scalar()
                if not lock_acquired:
                    logger.info('[ABC] Job omitido — otro worker ya lo está ejecutando')
                    return
                try:
                    from app.models.almacen import Almacen
                    try:
                        almacenes = Almacen.query.filter_by(activo=True).all()
                    except Exception as _ex_setup:
                        logger.error('[ABC] Job falló consultando almacenes — sin tareas generadas', exc_info=True)
                        try:
                            from app.services.alertas_service import _enviar_email_con_dlq
                            _enviar_email_con_dlq(
                                asunto='[WMS ALERTA] ABC scheduler falló en setup (almacenes)',
                                cuerpo_texto=(
                                    f'El scheduler ABC no pudo obtener la lista de almacenes:\n{_ex_setup}\n\n'
                                    'No se generaron tareas de conteo cíclico hoy. '
                                    'Usar /api/abc/generar para re-disparar manualmente.'
                                ),
                                cuerpo_html=None,
                                tipo_alerta='abc_scheduler_setup_fallo',
                            )
                        except Exception as _e_em:
                            logger.critical('[ABC] Email de alerta de setup también falló: %s', _e_em)
                        return
                    logger.info(f'[ABC] Job iniciado — {len(almacenes)} almacén(es)')
                    completados = []
                    parciales = []   # [M28] Completados con watchdog fallido internamente
                    fallidos = []
                    for a in almacenes:
                        try:
                            res = ABCService.generar_todas_las_clases(a.id)
                            # [M28] generar_todas_las_clases no lanza excepción si el watchdog
                            # falla — lo registra en resultados['watchdog']['error'].
                            # Separar "completo" de "parcial" para que el log/email sea preciso.
                            watchdog_err = (res or {}).get('por_clase', {}).get('watchdog', {}).get('error')
                            if watchdog_err:
                                parciales.append(a.id)
                                logger.warning(
                                    f'[ABC] Almacén {a.id} parcial — watchdog falló: {watchdog_err}'
                                )
                            else:
                                completados.append(a.id)
                                logger.info(f'[ABC] Almacén {a.id} completado')
                        except Exception as ex:
                            fallidos.append(a.id)
                            logger.error(f'[ABC] Error almacén {a.id}: {ex}')
                    logger.info(
                        f'[ABC] Job finalizado — OK: {completados} | PARCIALES: {parciales} | FALLIDOS: {fallidos}'
                    )
                    if fallidos or parciales:
                        # SF_JOB_SILENCIOSO: almacenes fallidos quedan sin tareas de conteo hoy.
                        # Enviar alerta para que ops pueda disparar manualmente.
                        try:
                            from app.services.alertas_service import _enviar_email_con_dlq
                            _enviar_email_con_dlq(
                                asunto=f'[WMS ALERTA] ABC scheduler: {len(fallidos)} fallidos, {len(parciales)} parciales',
                                cuerpo_texto=(
                                    f'El scheduler ABC (2am Bogotá) tuvo problemas:\n'
                                    f'FALLIDOS (sin tareas generadas): {fallidos}\n'
                                    f'PARCIALES (watchdog sin reclasificar A): {parciales}\n'
                                    f'COMPLETADOS: {completados}\n\n'
                                    'Usa /api/abc/generar para re-disparar almacenes fallidos.'
                                ),
                                cuerpo_html=None,
                                tipo_alerta=f'abc_scheduler_fallo',
                            )
                        except Exception as _e_email_sched:
                            logger.critical(
                                f'[ABC] Email de alerta del scheduler también falló — '
                                f'falla de almacenes {fallidos} completamente invisible: {_e_email_sched}'
                            )
                finally:
                    try:
                        _db.session.rollback()
                        _db.session.execute(_db.text('SELECT pg_advisory_unlock(2003)'))
                        _db.session.commit()
                    except Exception as _fe:
                        logger.error(f'[ABC] Error liberando advisory lock 2003: {_fe}')

        def _prewarm_pre_turno(app):
            """
            Precalienta el caché de existencia Siesa 5 min antes del turno (5:55am).
            El prewarm de las 2am expira a las 2:05am — 4h antes de que los operarios
            lleguen. Este job garantiza caché caliente al inicio de turno, evitando
            SEGUNDO_CONTEOs falsos por cache miss en el primer conteo del día.
            """
            with app.app_context():
                try:
                    from app.models.conteo import SesionConteo, EstadoConteo
                    from app.services.conteo_service import ConteoService
                    from sqlalchemy.orm import selectinload
                    sesiones_pendientes = SesionConteo.query.options(
                        selectinload(SesionConteo.producto),
                        selectinload(SesionConteo.ubicacion),
                    ).filter(
                        SesionConteo.estado == EstadoConteo.PENDIENTE
                    ).all()
                    logger.info(
                        f'[ABC] Pre-turno check (5:55am): {len(sesiones_pendientes)} sesiones PENDIENTE'
                    )
                except Exception as e:
                    logger.error(f'[ABC] Pre-turno prewarm falló: {e}', exc_info=True)

        scheduler = BackgroundScheduler(timezone='America/Bogota')
        scheduler.add_job(
            func=_job,
            trigger=CronTrigger(hour=2, minute=0, timezone='America/Bogota'),
            id='abc_conteo_diario',
            name='Generar tareas conteo cíclico ABC — 2am Bogotá',
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            func=_prewarm_pre_turno,
            trigger=CronTrigger(hour=5, minute=55, timezone='America/Bogota'),
            kwargs={'app': app},
            id='abc_prewarm_pre_turno',
            name='Pre-calentar caché Siesa antes del turno — 5:55am Bogotá',
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown(wait=False))
        logger.info('[ABC] Scheduler configurado — ABC 2am + prewarm pre-turno 5:55am Bogotá')
        return scheduler

    @staticmethod
    def resumen_abc(almacen_id: int):
        """
        Resumen de la distribución ABC del inventario.
        """
        from app.models.inventario import UbicacionProducto
        from app.extensions import db as database
        from sqlalchemy import func

        # Una sola query GROUP BY en lugar de 3 COUNTs separados
        counts_rows = (
            database.session.query(
                ProductoClasificacionABC.clasificacion,
                func.count(ProductoClasificacionABC.id)
            )
            .filter(ProductoClasificacionABC.almacen_id == almacen_id)
            .group_by(ProductoClasificacionABC.clasificacion)
            .all()
        )
        counts_map = {clase: cnt for clase, cnt in counts_rows}
        descripciones = {
            'A': 'Alta rotación — contar semanalmente',
            'B': 'Rotación media — contar mensualmente',
            'C': 'Baja rotación — contar trimestralmente',
        }
        resumen = {
            cls: {'total_productos': counts_map.get(cls, 0), 'descripcion': descripciones[cls]}
            for cls in ['A', 'B', 'C']
        }

        return {
            'almacen_id': almacen_id,
            'distribucion_abc': resumen,
            'fuente': 'Siesa Enterprise' if not connekta.modo_simulacion else 'WMS local (simulación)'
        }

    @staticmethod
    def procesar_csv_abc(file_obj, ext: str, almacen_id: int = None) -> dict:
        """
        Parsea el archivo CSV/Excel del reporte "Recalculo de rotación ABC" de Siesa
        y actualiza clasificacion_abc en la tabla productos.

        Detecta automáticamente:
        - Las filas basura del encabezado (empresa, NIT, fechas, filtros)
        - Las columnas Referencia y Clasificación
        - El separador del CSV (coma, punto y coma, tab)

        Retorna resumen con actualizados, no_encontrados, distribucion.
        """
        import csv
        import io
        import re
        from collections import Counter

        def _norm(s):
            s = str(s).lower().strip()
            for a, b in [('á','a'),('é','e'),('í','i'),('ó','o'),('ú','u'),('ñ','n')]:
                s = s.replace(a, b)
            return re.sub(r'[^\w\s]', '', s).strip()

        ALIASES_REF = ['referencia', 'ref', 'codigo', 'codigo siesa', 'cod item',
                       'f120 referencia', 'item ref']
        ALIASES_ABC = ['clasificacion', 'clasificacion abc', 'abc', 'clase',
                       'clasif', 'rotacion abc']

        def _col(headers_norm, aliases):
            for alias in aliases:
                an = _norm(alias)
                for i, h in enumerate(headers_norm):
                    if an == h or an in h or h in an:
                        return i
            return None

        # ── Leer contenido ─────────────────────────────────────────────────
        contenido = file_obj.read()

        if ext in ('xlsx', 'xls'):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
                ws = wb.active
                filas = [[str(c) if c is not None else '' for c in row]
                         for row in ws.iter_rows(values_only=True)]
            except ImportError:
                raise RuntimeError('openpyxl no instalado en el servidor')
        else:
            # Detectar encoding — Siesa exporta en latin-1/cp1252, no en UTF-8
            texto = None
            for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
                try:
                    texto = contenido.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if texto is None:
                texto = contenido.decode('latin-1', errors='replace')

            # Auto-detectar separador
            muestra = texto[:4096]
            sep = ';' if muestra.count(';') >= muestra.count(',') else ','
            if muestra.count('\t') > muestra.count(sep):
                sep = '\t'
            filas = list(csv.reader(io.StringIO(texto), delimiter=sep))

        if not filas:
            raise ValueError('El archivo está vacío')

        # ── Encontrar fila de headers (saltar basura Siesa) ─────────────
        # La primera fila que contenga "referencia" o "clasificaci" es el header
        palabras_clave = {'referencia', 'clasificaci', 'abc', 'clasif', 'item'}
        idx_h = None
        for i, fila in enumerate(filas):
            norm_celdas = [_norm(c) for c in fila]
            hits = sum(1 for c in norm_celdas if any(p in c for p in palabras_clave) and len(c) > 2)
            if hits >= 2:
                idx_h = i
                break

        if idx_h is None:
            # Fallback: primera fila no vacía
            for i, fila in enumerate(filas):
                if any(c.strip() for c in fila):
                    idx_h = i
                    break

        if idx_h is None:
            raise ValueError('No se encontró la fila de encabezados. '
                             'Verifica que el archivo sea el reporte ABC de Siesa.')

        headers = filas[idx_h]
        hn = [_norm(h) for h in headers]

        i_ref = _col(hn, ALIASES_REF)
        i_abc = _col(hn, ALIASES_ABC)

        if i_ref is None:
            raise ValueError(f'No encontré columna "Referencia". Columnas detectadas: {headers}')
        if i_abc is None:
            raise ValueError(f'No encontré columna "Clasificación". Columnas detectadas: {headers}')

        # ── Parsear datos ───────────────────────────────────────────────
        datos = []
        omitidos = 0
        for fila in filas[idx_h + 1:]:
            if not fila or not any(c.strip() for c in fila):
                continue
            try:
                # strip() quita espacios; rstrip('-') quita guión de extensión de Siesa
                referencia = fila[i_ref].strip().rstrip('-').strip()
                clasificacion = fila[i_abc].strip().upper()
            except IndexError:
                omitidos += 1
                continue
            if not referencia or referencia in ('-', '') or clasificacion not in ('A', 'B', 'C'):
                omitidos += 1
                continue
            datos.append((referencia, clasificacion))

        if not datos:
            raise ValueError(f'No hay filas válidas en el archivo. Omitidos: {omitidos}')

        dist = Counter(c for _, c in datos)

        # ── Upsert en producto_clasificacion_abc ───────────────────────
        actualizados = 0
        no_encontrados = []
        LOTE = 500

        for i in range(0, len(datos), LOTE):
            lote = datos[i:i + LOTE]
            refs = [r for r, _ in lote]

            # Mapa referencia → Producto (prioridad: codigo_siesa sobre codigo para evitar colisiones)
            _prods_lote = Producto.query.filter(
                db.or_(
                    Producto.codigo_siesa.in_(refs),
                    Producto.codigo.in_(refs)
                ),
                Producto.activo == True
            ).all()
            existentes: dict = {}
            for _p in _prods_lote:
                # Indexar por codigo_siesa primero; fallback a codigo
                if _p.codigo_siesa and _p.codigo_siesa not in existentes:
                    existentes[_p.codigo_siesa] = _p
                if _p.codigo and _p.codigo not in existentes:
                    existentes[_p.codigo] = _p

            # Pre-cargar registros ABC existentes para el lote completo
            ids_lote = [existentes[r].id for r, _ in lote if existentes.get(r)]
            if almacen_id and ids_lote:
                abc_existentes = {
                    reg.producto_id: reg
                    for reg in ProductoClasificacionABC.query.filter(
                        ProductoClasificacionABC.producto_id.in_(ids_lote),
                        ProductoClasificacionABC.almacen_id == almacen_id
                    ).all()
                }
            else:
                abc_existentes = {}

            for referencia, clasificacion in lote:
                producto = existentes.get(referencia)
                if not producto:
                    no_encontrados.append(referencia)
                    continue

                if almacen_id:
                    # Upsert por (producto_id, almacen_id) usando el mapa pre-cargado
                    registro = abc_existentes.get(producto.id)
                    if registro:
                        registro.clasificacion = clasificacion
                        registro.updated_at = datetime.utcnow()
                    else:
                        nuevo = ProductoClasificacionABC(
                            producto_id=producto.id,
                            almacen_id=almacen_id,
                            clasificacion=clasificacion
                        )
                        db.session.add(nuevo)
                        abc_existentes[producto.id] = nuevo  # evita duplicados dentro del lote
                else:
                    # Fallback legacy: actualizar campo global
                    producto.clasificacion_abc = clasificacion

                actualizados += 1

            # flush sin commit — la transacción completa se confirma al final
            # para que un Railway restart no deje clasificaciones parcialmente actualizadas
            try:
                db.session.flush()
            except Exception as _flush_err:
                db.session.rollback()
                logger.error(
                    f'[ABC CSV] flush() falló en lote {i}–{i + LOTE}: {_flush_err}',
                    exc_info=True
                )
                raise

        try:
            db.session.commit()
        except Exception as _commit_err:
            db.session.rollback()
            logger.error(f'[ABC CSV] Error en commit final: {_commit_err}')
            raise

        logger.info(
            f'[ABC CSV] almacen={almacen_id} · {actualizados} upserted · '
            f'{len(no_encontrados)} no encontrados · '
            f'A={dist["A"]} B={dist["B"]} C={dist["C"]}'
        )

        return {
            'actualizados': actualizados,
            'no_encontrados': len(no_encontrados),
            'no_encontrados_muestra': no_encontrados[:20],
            'omitidos_formato': omitidos,
            'distribucion': {'A': dist['A'], 'B': dist['B'], 'C': dist['C']},
            'total_procesados': len(datos),
            'filas_encabezado_saltadas': idx_h,
            'almacen_id': almacen_id,
        }