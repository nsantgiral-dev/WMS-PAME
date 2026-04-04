"""
Servicio ABC — El WMS NO calcula clasificación ABC.
Siesa Enterprise tiene su propio motor estadístico.
Este servicio consume la clasificación de Siesa y genera tareas de conteo.

Frecuencias de conteo cíclico (estrategia de costos bajos):
  A — Alta rotación  → contar cada 15 días
  B — Rotación media → contar cada 90 días
  C — Baja rotación  → contar cada 180 días
"""
import uuid
import logging
from datetime import datetime, timedelta
from app.extensions import db
from app.models.conteo import SesionConteo
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

# Días mínimos entre conteos por clase
FRECUENCIA_DIAS = {'A': 15, 'B': 90, 'C': 180}


class ABCService:

    @staticmethod
    def sincronizar_clasificacion_desde_siesa():
        """
        Extrae la clasificación ABC de Siesa y actualiza los productos en el WMS.
        El WMS no calcula — solo sincroniza lo que Siesa ya calculó.
        """
        if connekta.modo_simulacion:
            logger.info('[ABC] Modo simulación — usando clasificación local')
            return {
                'simulado': True,
                'mensaje': 'En producción este método sincroniza ABC desde Siesa',
                'productos_actualizados': 0
            }

        try:
            # GET a Connekta — clasificación ABC de todos los ítems
            response = connekta._hacer_get('items/clasificacion-abc')
            items_siesa = response.get('items', [])

            actualizados = 0
            for item in items_siesa:
                producto = Producto.query.filter_by(
                    codigo_siesa=item.get('codigo')
                ).first()

                if producto:
                    producto.clasificacion_abc = item.get('clasificacion_abc')
                    producto.codigo_siesa = item.get('codigo')
                    actualizados += 1

            db.session.commit()
            logger.info(f'[ABC] {actualizados} productos sincronizados desde Siesa')

            return {
                'productos_actualizados': actualizados,
                'total_siesa': len(items_siesa)
            }

        except Exception as e:
            logger.error(f'[ABC] Error sincronizando desde Siesa: {str(e)}')
            raise

    @staticmethod
    def generar_tareas_conteo_diario(almacen_id: int, clasificacion: str = 'A'):
        """
        Genera tareas de conteo cíclico respetando la frecuencia por clase.
        Solo crea una tarea si el último conteo cerrado supera el umbral de días.
        Esto evita inflar la cola con productos recién contados.
        """
        from app.models.inventario import UbicacionProducto

        frecuencia = FRECUENCIA_DIAS.get(clasificacion, 15)
        umbral = datetime.utcnow() - timedelta(days=frecuencia)

        productos = Producto.query.filter_by(
            clasificacion_abc=clasificacion,
            activo=True
        ).all()

        if not productos:
            return {
                'mensaje': f'No hay productos clase {clasificacion} para contar',
                'tareas_creadas': 0,
                'clasificacion': clasificacion,
                'frecuencia_dias': frecuencia,
            }

        tareas_creadas = []
        omitidos_por_frecuencia = 0
        omitidos_por_pendiente = 0

        for producto in productos:
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
                # No crear si ya hay tarea activa para este producto+ubicación
                pendiente = SesionConteo.query.filter(
                    SesionConteo.producto_id == producto.id,
                    SesionConteo.ubicacion_id == reg.ubicacion_id,
                    SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
                ).first()
                if pendiente:
                    omitidos_por_pendiente += 1
                    continue

                # No crear si ya se contó dentro del período de frecuencia
                conteo_reciente = SesionConteo.query.filter(
                    SesionConteo.producto_id == producto.id,
                    SesionConteo.ubicacion_id == reg.ubicacion_id,
                    SesionConteo.estado.in_(['MATCH', 'AJUSTADO']),
                    SesionConteo.fecha_cierre >= umbral
                ).first()
                if conteo_reciente:
                    omitidos_por_frecuencia += 1
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
                    maneja_lote=bool(reg.lote),
                    estado='PENDIENTE'
                )
                db.session.add(sesion)
                tareas_creadas.append(sesion)

        db.session.commit()

        logger.info(
            f'[ABC] Clase {clasificacion} · {len(tareas_creadas)} tareas creadas · '
            f'{omitidos_por_pendiente} ya activas · {omitidos_por_frecuencia} dentro de frecuencia'
        )

        return {
            'tareas_creadas': len(tareas_creadas),
            'omitidos_por_pendiente': omitidos_por_pendiente,
            'omitidos_por_frecuencia': omitidos_por_frecuencia,
            'clasificacion': clasificacion,
            'frecuencia_dias': frecuencia,
            'almacen_id': almacen_id,
        }

    @staticmethod
    def generar_todas_las_clases(almacen_id: int):
        """
        Genera tareas para A, B y C en una sola llamada.
        Usado por el scheduler diario a las 6am.
        """
        resultados = {}
        total = 0
        for clase in ['A', 'B', 'C']:
            r = ABCService.generar_tareas_conteo_diario(almacen_id, clase)
            resultados[clase] = r
            total += r['tareas_creadas']

        logger.info(f'[ABC] Generación diaria completa · {total} tareas nuevas en almacén {almacen_id}')
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
            name='Generar tareas conteo cíclico ABC — 6am Bogotá'
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
                database.session.query(func.count(Producto.id))
                .filter_by(clasificacion_abc=clasificacion, activo=True)
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