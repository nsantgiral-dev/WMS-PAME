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
FRECUENCIA_DIAS = {'A': 30, 'B': 90, 'C': 180}

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

        ventana = datetime.utcnow() - timedelta(days=WATCHDOG_VENTANA_DIAS)
        overrides = []

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
                    TareaPicking.estado == 'COMPLETADA',
                    TareaPicking.fecha_completado >= ventana
                )
                .group_by(TareaPicking.producto_id)
                .all()
            )
            picks_por_producto = {pid: cnt for pid, cnt in picks_rows}

            for producto in productos_clase:
                picks = picks_por_producto.get(producto.id, 0)

                if picks < umbral:
                    continue

                # Buscar ubicaciones del producto en este almacén
                registros = (
                    UbicacionProducto.query
                    .join(Ubicacion)
                    .filter(
                        UbicacionProducto.producto_id == producto.id,
                        UbicacionProducto.cantidad > 0,
                        Ubicacion.almacen_id == almacen_id
                    ).all()
                )

                for reg in registros:
                    # No duplicar si ya hay conteo activo — usa el set pre-cargado
                    if (producto.id, reg.ubicacion_id) in conteos_activos:
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
                        maneja_lote=bool(reg.lote if hasattr(reg, 'lote') else False),
                        estado='PENDIENTE'
                    )
                    db.session.add(sesion)
                    conteos_activos.add((producto.id, reg.ubicacion_id))  # evita duplicar en misma corrida
                    overrides.append({
                        'producto_id': producto.id,
                        'producto_codigo': producto.codigo_siesa or producto.codigo,
                        'clase_actual': clase,
                        'picks_7dias': picks,
                        'umbral': umbral,
                        'sesion_codigo': codigo,
                    })

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC WATCHDOG] Error guardando overrides: {e}')
            raise

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
            ubic_ids_all = [x[0] for x in pares_ubic_prod]
            sesiones_activas = SesionConteo.query.filter(
                SesionConteo.producto_id.in_(producto_ids),
                SesionConteo.ubicacion_id.in_(ubic_ids_all),
                SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
            ).all()
            activos_set = {(s.ubicacion_id, s.producto_id) for s in sesiones_activas}

            # Pre-cargar último conteo completado por (producto_id, ubicacion_id)
            sesiones_completadas = SesionConteo.query.filter(
                SesionConteo.producto_id.in_(producto_ids),
                SesionConteo.ubicacion_id.in_(ubic_ids_all),
                SesionConteo.estado.in_(['MATCH', 'AJUSTADO'])
            ).order_by(SesionConteo.fecha_cierre.desc()).all()
            ultimo_por_par = {}
            for s in sesiones_completadas:
                key = (s.producto_id, s.ubicacion_id)
                if key not in ultimo_por_par:
                    ultimo_por_par[key] = s.fecha_cierre
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

        # Crear tareas
        tareas_creadas = []
        for _, producto, reg in candidatos:
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
            tareas_creadas.append(sesion)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC] Error guardando tareas de conteo: {e}')
            raise

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
        for clase in ['A', 'B', 'C']:
            r = ABCService.generar_tareas_conteo_diario(almacen_id, clase,
                                                        forzar_todo=forzar_todo)
            resultados[clase] = r
            total += r['tareas_creadas']

        # Watchdog DESPUÉS de las tareas normales — detecta reclasificaciones urgentes
        try:
            overrides = ABCService.watchdog_anomalias(almacen_id)
            total += len(overrides)
            resultados['watchdog'] = {
                'overrides': len(overrides),
                'detalle': overrides
            }
        except Exception as e:
            logger.error(f'[ABC WATCHDOG] Error en almacén {almacen_id}: {e}')
            resultados['watchdog'] = {'error': str(e)}

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
                from app.models.almacen import Almacen
                almacenes = Almacen.query.filter_by(activo=True).all()
                for a in almacenes:
                    try:
                        ABCService.generar_todas_las_clases(a.id)
                    except Exception as ex:
                        logger.error(f'[ABC] Error almacén {a.id}: {ex}')

        scheduler = BackgroundScheduler(timezone='America/Bogota')
        scheduler.add_job(
            func=_job,
            trigger=CronTrigger(hour=6, minute=0, timezone='America/Bogota'),
            id='abc_conteo_diario',
            name='Generar tareas conteo cíclico ABC — 6am Bogotá',
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown(wait=False))
        logger.info('[ABC] Scheduler configurado — corre a las 6am hora Bogotá')
        return scheduler

    @staticmethod
    def resumen_abc(almacen_id: int):
        """
        Resumen de la distribución ABC del inventario.
        """
        from app.models.inventario import UbicacionProducto
        from app.extensions import db as database
        from sqlalchemy import func

        resumen = {}
        for clasificacion in ['A', 'B', 'C']:
            total = (
                database.session.query(func.count(ProductoClasificacionABC.id))
                .filter(
                    ProductoClasificacionABC.almacen_id == almacen_id,
                    ProductoClasificacionABC.clasificacion == clasificacion,
                )
                .scalar()
            )
            resumen[clasificacion] = {
                'total_productos': total,
                'descripcion': {
                    'A': 'Alta rotación — contar semanalmente',
                    'B': 'Rotación media — contar mensualmente',
                    'C': 'Baja rotación — contar trimestralmente'
                }.get(clasificacion)
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

            # Mapa referencia → Producto
            existentes = {
                p.codigo_siesa or p.codigo: p
                for p in Producto.query.filter(
                    db.or_(
                        Producto.codigo_siesa.in_(refs),
                        Producto.codigo.in_(refs)
                    ),
                    Producto.activo == True
                ).all()
            }

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

            db.session.commit()

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