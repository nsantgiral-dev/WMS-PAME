"""
Servicio ABC — El WMS NO calcula clasificación ABC.
Siesa Enterprise tiene su propio motor estadístico.
Este servicio consume la clasificación de Siesa y genera tareas de conteo.
"""
import uuid
import logging
from datetime import datetime
from app.extensions import db
from app.models.conteo import SesionConteo
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


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
        Genera automáticamente tareas de conteo cíclico diario.
        Por defecto genera para productos clase A (alta rotación).
        Clase A: conteo semanal.
        Clase B: conteo mensual.
        Clase C: conteo trimestral.
        """
        # Productos de la clasificación solicitada
        productos = Producto.query.filter_by(
            clasificacion_abc=clasificacion,
            activo=True
        ).all()

        if not productos:
            return {
                'mensaje': f'No hay productos clase {clasificacion} para contar',
                'tareas_creadas': 0
            }

        tareas_creadas = []

        for producto in productos:
            # Buscar ubicaciones donde está el producto
            from app.models.inventario import UbicacionProducto
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
                # Verificar que no hay conteo pendiente para esta ubicación y producto
                existente = SesionConteo.query.filter_by(
                    producto_id=producto.id,
                    ubicacion_id=reg.ubicacion_id,
                    estado='PENDIENTE'
                ).first()

                if existente:
                    continue

                codigo = f'CC-{clasificacion}-{datetime.utcnow().strftime("%Y%m%d")}-{str(uuid.uuid4())[:6].upper()}'

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
            f'[ABC] {len(tareas_creadas)} tareas de conteo clase {clasificacion} generadas'
        )

        return {
            'tareas_creadas': len(tareas_creadas),
            'clasificacion': clasificacion,
            'almacen_id': almacen_id,
            'tareas': [t.to_dict_operario() for t in tareas_creadas]
        }

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