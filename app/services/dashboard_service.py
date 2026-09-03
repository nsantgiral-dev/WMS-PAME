"""
Dashboard Service — KPIs operativos en tiempo real.
Consolida datos de todos los módulos para visibilidad gerencial.
"""
import logging
from datetime import datetime, timedelta
from sqlalchemy import func, case
from app.extensions import db
from app.models.picking import TareaPicking
from app.models.packing import TareaPacking
from app.models.recepcion import RecepcionMercancia
from app.models.conteo import SesionConteo
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.producto import Producto
from app.models.usuario import Usuario
from app.models.ubicacion import Ubicacion
from app.services.connekta_gateway import connekta
# La política única de «esto no es vendible». Acá había una tupla propia
# (`_ZONAS_NO_VENDIBLES = ('AVERIAS',)`) que contestaba la misma pregunta que el
# FEFO con otras palabras, y las dos ignoraban al escritor que no marca
# `tipo_zona`. Ver el bloque de cabecera de `picking_service.py`.
from app.services.picking_service import filtro_ubicacion_vendible

logger = logging.getLogger(__name__)

# Tope de filas que `alertas_stock` serializa en una respuesta. Medido: con el
# outerjoin (que ahora sí ve el agotado total), una carga masiva de mínimos
# lleva la respuesta de 1.000 a 28.000 filas y de 277 ms a 6,9 s — un endpoint
# de tablero que tarda siete segundos deja de mirarse.
#
# El tope NO es la medición: `total_alertas` sigue siendo el conteo real y sin
# tope, y la respuesta declara cuántas mostró y si truncó. Un número recortado
# sin decir sobre qué base se recortó es un conteo sin su base (CLAUDE.md), y
# ahí el jefe de almacén cree que hay 500 productos por reponer cuando hay
# 28.000. Las que se recortan son las MENOS urgentes: el orden lo pone la
# consulta, no el `LIMIT`.
LIMITE_ALERTAS_STOCK = 500


def consulta_productos_bajo_minimo(almacen_id: int):
    """
    Productos activos cuyo stock **vendible** en este almacén no alcanza su
    `stock_minimo`. Devuelve un Query de filas `(Producto, stock_vendible)`
    para que el llamador decida si lo cuenta o lo lista.

    Existe como función única porque la consulta estaba escrita dos veces —
    `alertas_stock()` y el bloque de alertas de `kpis_operativos()`— y las dos
    copias tenían los mismos dos defectos. Una política, una función (Regla 0
    de CLAUDE.md): el mismo patrón duplicado ya costó tres horas y 25× de
    sobrecompra en el fallback de días expuestos. Con dos copias, el próximo
    arreglo se aplica en una sola y el tablero y la lista dejan de coincidir
    sin que nadie sepa cuál de los dos números creer.

    Los dos defectos que corrige, y lo que costaban:

    · **`outerjoin`, no `join`.** El join interno exigía al menos una fila de
      `UbicacionProducto`, así que el producto **agotado total** — el que no
      tiene ninguna — jamás entraba, justo el caso que el docstring de
      `alertas_stock` promete («bajo mínimo *o sin stock*») y el más urgente de
      reponer. Sin fila, `stock_vendible` es NULL, y `NULL <= minimo` no es
      verdadero: por eso el `coalesce(..., 0)` es parte del arreglo, no
      cosmética.

    · **AVERIAS fuera de la suma.** Mercancía dañada sumaba como cobertura:
      2 unidades vendibles + 40 averiadas tapaban un mínimo de 10. La alerta de
      reposición se apagaba con inventario que no se puede vender.

    El filtro por almacén se conserva dentro de la subconsulta: la pregunta es
    «¿cuánto hay ACÁ?», y el stock de otra bodega no cubre este mínimo.

    Qué zona es vendible NO se decide acá: lo decide `filtro_ubicacion_vendible`
    (`picking_service`), la misma cláusula que usa el FEFO. Acá había una tupla
    propia con el literal `'AVERIAS'`, y una copia de la política es una
    política que va a divergir — de hecho ya divergía del escritor.

    Viene ORDENADA por urgencia (agotado primero, luego clasificación ABC).
    El orden es parte de la consulta y no del llamador porque `alertas_stock`
    tiene tope: ordenar después del `LIMIT` tiraría justo los CRITICO, que son
    los únicos que no se pueden perder.
    """
    stock_vendible = db.session.query(
        UbicacionProducto.producto_id,
        func.sum(UbicacionProducto.cantidad).label('stock_total')
    ).join(Ubicacion).filter(
        Ubicacion.almacen_id == almacen_id,
        filtro_ubicacion_vendible(),
    ).group_by(UbicacionProducto.producto_id).subquery()

    stock = func.coalesce(stock_vendible.c.stock_total, 0)

    return db.session.query(Producto, stock.label('stock_total')).outerjoin(
        stock_vendible,
        Producto.id == stock_vendible.c.producto_id
    ).filter(
        Producto.activo == True,
        Producto.stock_minimo > 0,
        stock <= Producto.stock_minimo
    ).order_by(
        # CRITICO (stock 0) antes que BAJO — mismo criterio que `urgencia`
        case((stock == 0, 0), else_=1).asc(),
        # 'Z' para el producto sin clasificar: va último, no primero
        func.coalesce(Producto.clasificacion_abc, 'Z').asc(),
        Producto.id.asc(),   # desempate estable — sin él el tope corta distinto en cada corrida
    )


def _tendencia_7d():
    """Actividad diaria de los últimos 7 días para gráfica de tendencias.
    4 queries GROUP BY en lugar de 28 queries individuales.
    """
    from app.models.traslado import SolicitudTraslado
    from app.models.ruta_despacho import RutaDespacho
    from app.extensions import db
    from sqlalchemy import func, cast, Date

    # Día OPERATIVO (Colombia), no día UTC. Las columnas guardan UTC naive, así
    # que el corte se expresa en ese mismo marco: `inicio_del_dia_utc`.
    from app.utils.fecha import dia_operativo, inicio_del_dia_utc
    hoy = dia_operativo()
    inicio_ventana = inicio_del_dia_utc(hoy - timedelta(days=6))

    # Una query por modelo, agrupada por día
    picking_rows = (
        db.session.query(cast(TareaPicking.fecha_completado, Date), func.count(TareaPicking.id))
        .filter(TareaPicking.estado == 'COMPLETADO', TareaPicking.fecha_completado >= inicio_ventana)
        .group_by(cast(TareaPicking.fecha_completado, Date)).all()
    )
    conteo_rows = (
        db.session.query(cast(SesionConteo.fecha_cierre, Date), func.count(SesionConteo.id))
        .filter(SesionConteo.estado.in_(['MATCH', 'AJUSTADO']), SesionConteo.fecha_cierre >= inicio_ventana)
        .group_by(cast(SesionConteo.fecha_cierre, Date)).all()
    )
    traslado_rows = (
        db.session.query(cast(SolicitudTraslado.fecha_entrega, Date), func.count(SolicitudTraslado.id))
        .filter(SolicitudTraslado.estado == 'ENTREGADA', SolicitudTraslado.fecha_entrega >= inicio_ventana)
        .group_by(cast(SolicitudTraslado.fecha_entrega, Date)).all()
    )
    ruta_rows = (
        db.session.query(cast(RutaDespacho.fecha_entregada, Date), func.count(RutaDespacho.id))
        .filter(RutaDespacho.estado == 'ENTREGADA', RutaDespacho.fecha_entregada >= inicio_ventana)
        .group_by(cast(RutaDespacho.fecha_entregada, Date)).all()
    )

    picking_map   = {str(d): c for d, c in picking_rows}
    conteo_map    = {str(d): c for d, c in conteo_rows}
    traslado_map  = {str(d): c for d, c in traslado_rows}
    ruta_map      = {str(d): c for d, c in ruta_rows}

    dias = []
    for i in range(6, -1, -1):
        dia = hoy - timedelta(days=i)
        key = str(dia)
        dias.append({
            'fecha':     dia.strftime('%d/%m'),
            'picking':   picking_map.get(key, 0),
            'conteos':   conteo_map.get(key, 0),
            'traslados': traslado_map.get(key, 0),
            'rutas':     ruta_map.get(key, 0),
        })
    return dias


class DashboardService:

    @staticmethod
    def kpis_operativos(almacen_id: int):
        """KPIs principales del almacén en tiempo real."""
        # "Completado hoy" con corte UTC se REINICIA A LAS 7 P.M. Colombia, en
        # mitad del turno de la tarde, y arrastra el trabajo de la noche
        # anterior. El día operativo empieza a medianoche de acá.
        from app.utils.fecha import dia_operativo, inicio_del_dia_utc
        hoy = dia_operativo()
        inicio_hoy = inicio_del_dia_utc(hoy)

        # --- PICKING — 3 counts en 1 query con aggregación condicional ---
        p_row = db.session.query(
            func.count(TareaPicking.id).filter(TareaPicking.estado == 'PENDIENTE').label('pendiente'),
            func.count(TareaPicking.id).filter(TareaPicking.estado == 'EN_PROCESO').label('en_proceso'),
            func.count(TareaPicking.id).filter(
                TareaPicking.estado == 'COMPLETADO',
                TareaPicking.fecha_completado >= inicio_hoy
            ).label('completado_hoy'),
        ).filter(TareaPicking.almacen_id == almacen_id).one()
        picking_pendiente      = p_row.pendiente
        picking_en_proceso     = p_row.en_proceso
        picking_completado_hoy = p_row.completado_hoy

        # --- PACKING — 3 counts en 1 query ---
        pk_row = db.session.query(
            func.count(TareaPacking.id).filter(TareaPacking.estado == 'PENDIENTE').label('pendiente'),
            func.count(TareaPacking.id).filter(TareaPacking.estado == 'EN_PROCESO').label('en_proceso'),
            func.count(TareaPacking.id).filter(
                TareaPacking.estado == 'VERIFICADO',
                TareaPacking.fecha_verificado >= inicio_hoy
            ).label('completado_hoy'),
            func.count(TareaPacking.id).filter(
                TareaPacking.siesa_triggered == True,
                TareaPacking.siesa_triggered_at >= inicio_hoy
            ).label('siesa_hoy'),
        ).filter(TareaPacking.almacen_id == almacen_id).one()
        packing_pendiente      = pk_row.pendiente
        packing_en_proceso     = pk_row.en_proceso
        packing_completado_hoy = pk_row.completado_hoy
        siesa_triggers_hoy     = pk_row.siesa_hoy

        # --- RECEPCIÓN ---
        recepciones_hoy = RecepcionMercancia.query.filter(
            RecepcionMercancia.almacen_id == almacen_id,
            RecepcionMercancia.fecha_confirmacion >= inicio_hoy
        ).count()

        # --- CONTEO CÍCLICO — 3 counts en 1 query ---
        c_row = db.session.query(
            func.count(SesionConteo.id).filter(SesionConteo.estado == 'PENDIENTE').label('pendientes'),
            func.count(SesionConteo.id).filter(SesionConteo.estado == 'SEGUNDO_CONTEO').label('descuadre'),
            func.count(SesionConteo.id).filter(
                SesionConteo.estado == 'MATCH',
                SesionConteo.fecha_cierre >= inicio_hoy
            ).label('match_hoy'),
        ).filter(SesionConteo.almacen_id == almacen_id).one()
        conteos_pendientes = c_row.pendientes
        conteos_descuadre  = c_row.descuadre
        conteos_match_hoy  = c_row.match_hoy

        # --- ALERTAS DE STOCK ---
        # LA MISMA consulta que alertas_stock(), no «la misma lógica»: acá
        # había una copia, y las copias divergen. El KPI del tablero y la lista
        # de alertas tienen que dar siempre el mismo número.
        productos_bajo_minimo = consulta_productos_bajo_minimo(almacen_id).count()

        return {
            'fecha': hoy.isoformat(),
            'almacen_id': almacen_id,
            'picking': {
                'pendiente': picking_pendiente,
                'en_proceso': picking_en_proceso,
                'completado_hoy': picking_completado_hoy,
                'total_activo': picking_pendiente + picking_en_proceso
            },
            'packing': {
                'pendiente': packing_pendiente,
                'en_proceso': packing_en_proceso,
                'completado_hoy': packing_completado_hoy,
                'facturas_generadas_hoy': siesa_triggers_hoy
            },
            'recepcion': {
                'confirmadas_hoy': recepciones_hoy
            },
            'conteo': {
                'pendientes': conteos_pendientes,
                'en_descuadre': conteos_descuadre,
                'match_hoy': conteos_match_hoy
            },
            'alertas': {
                'productos_bajo_minimo': productos_bajo_minimo,
                'conteos_descuadre': conteos_descuadre
            },
            'connekta': connekta.estado()
        }

    @staticmethod
    def productividad_operarios(almacen_id: int, dias: int = 7):
        """Productividad por operario en los últimos N días."""
        fecha_inicio = datetime.utcnow() - timedelta(days=dias)

        operarios = Usuario.query.filter(
            Usuario.almacen_id == almacen_id,
            Usuario.activo == True
        ).all()

        operario_ids = [o.id for o in operarios]

        # [28] Consolidar los 4 COUNT queries por operario en queries batch con GROUP BY
        #
        # Acá decía que `hoy = utcnow().date()` era una variable muerta y se
        # quitó. NO estaba muerta: `conteos_hoy_por_op` la usa más abajo. Desde
        # entonces esta función levantaba `NameError` **siempre** —
        # `/api/dashboard/productividad` devolvía 500, y en `resumen_completo`
        # el `_safe()` se lo tragaba y el panel de productividad salía vacío sin
        # ningún error visible. Un guard que no da error deja pasar.
        #
        # Se restituye con el día OPERATIVO, no el UTC: el cupo diario de conteo
        # del operario es una de las cuatro fechas que CLAUDE.md (Regla 5) lista
        # como reiniciadas a las 7 p.m. Colombia, en mitad del turno. Las
        # columnas guardan UTC naive, así que el corte se expresa en ese marco.
        from app.utils.fecha import dia_operativo, inicio_del_dia_utc
        inicio_hoy = inicio_del_dia_utc(dia_operativo())

        pickings_por_op = {
            row.operario_id: row.cnt
            for row in db.session.query(
                TareaPicking.operario_id,
                func.count(TareaPicking.id).label('cnt')
            ).filter(
                TareaPicking.operario_id.in_(operario_ids),
                TareaPicking.estado == 'COMPLETADO',
                TareaPicking.fecha_completado >= fecha_inicio
            ).group_by(TareaPicking.operario_id).all()
        }

        packings_por_op = {
            row.empacador_id: row.cnt
            for row in db.session.query(
                TareaPacking.empacador_id,
                func.count(TareaPacking.id).label('cnt')
            ).filter(
                TareaPacking.empacador_id.in_(operario_ids),
                TareaPacking.estado == 'VERIFICADO',
                TareaPacking.fecha_verificado >= fecha_inicio
            ).group_by(TareaPacking.empacador_id).all()
        }

        conteos_por_op = {
            row.operario_id: row.cnt
            for row in db.session.query(
                SesionConteo.operario_id,
                func.count(SesionConteo.id).label('cnt')
            ).filter(
                SesionConteo.operario_id.in_(operario_ids),
                SesionConteo.estado.in_(['MATCH', 'AJUSTADO']),
                SesionConteo.fecha_cierre >= fecha_inicio
            ).group_by(SesionConteo.operario_id).all()
        }

        conteos_hoy_por_op = {
            row.operario_id: row.cnt
            for row in db.session.query(
                SesionConteo.operario_id,
                func.count(SesionConteo.id).label('cnt')
            ).filter(
                SesionConteo.operario_id.in_(operario_ids),
                SesionConteo.fecha_inicio >= inicio_hoy
            ).group_by(SesionConteo.operario_id).all()
        }

        resultado = []
        for operario in operarios:
            pickings = pickings_por_op.get(operario.id, 0)
            packings = packings_por_op.get(operario.id, 0)
            conteos = conteos_por_op.get(operario.id, 0)
            conteos_hoy = conteos_hoy_por_op.get(operario.id, 0)
            resultado.append({
                'operario_id': operario.id,
                'nombre': operario.nombre,
                'rol': operario.rol,
                'pickings_completados': pickings,
                'packings_completados': packings,
                'conteos_completados': conteos,
                'conteos_hoy': conteos_hoy,
                'capacidad_diaria_conteo': operario.capacidad_diaria_conteo if operario.capacidad_diaria_conteo is not None else 15,
                'total_tareas': pickings + packings + conteos
            })

        resultado.sort(key=lambda x: x['total_tareas'], reverse=True)

        return {
            'periodo_dias': dias,
            'almacen_id': almacen_id,
            'operarios': resultado
        }

    @staticmethod
    def movimientos_recientes(almacen_id: int, limite: int = 20):
        """Últimos movimientos de inventario del almacén."""
        movimientos = MovimientoInventario.query.filter_by(
            almacen_id=almacen_id
        ).order_by(
            MovimientoInventario.fecha.desc()
        ).limit(limite).all()

        return {
            'movimientos': [m.to_dict() for m in movimientos],
            'total': len(movimientos)
        }

    @staticmethod
    def alertas_stock(almacen_id: int):
        """
        Productos bajo mínimo o sin stock — el disparador de reposición/compra
        que mira el jefe de almacén.

        «Sin stock» incluye el agotado total (sin ninguna fila de inventario) y
        el stock reportado es el **vendible**: lo que está en la zona de averías
        no cubre el mínimo. Ambas reglas viven en
        `consulta_productos_bajo_minimo`, la misma que alimenta el KPI
        `productos_bajo_minimo` del tablero.

        ── El tope, y por qué el total viaja igual ──────────────────────────
        Esta lista se serializaba entera. Medido tras abrir el join hacia
        afuera: una carga masiva de mínimos la lleva de 1.000 a 28.000 filas y
        de 277 ms a 6,9 s. Ahora se cortan las `LIMITE_ALERTAS_STOCK` más
        urgentes — y se declara la base:

          `total_alertas`     conteo REAL, sin tope (contrato que ya existía;
                              es el número que tiene que coincidir con el KPI
                              `productos_bajo_minimo` del tablero)
          `alertas_mostradas` cuántas trae esta respuesta
          `limite` / `truncado`  el tope aplicado y si mordió

        Un tope sin su base convierte «28.000 productos por reponer» en «500» y
        nadie se entera. `routes/dashboard.py` serializa este dict tal cual, así
        que la declaración viaja acá adentro y no en un header ni en un
        parámetro nuevo.
        """
        consulta = consulta_productos_bajo_minimo(almacen_id)

        # Dos consultas a propósito: el conteo tiene que ser el de TODAS las
        # filas, no el de las que sobrevivieron al tope.
        total = consulta.count()

        # [07] stock_total viene en el SELECT principal — evita N+1 por producto
        # La consulta ya viene ordenada por urgencia: el LIMIT recorta por la
        # cola (lo menos urgente), nunca por el medio.
        productos_alerta = consulta.limit(LIMITE_ALERTAS_STOCK).all()

        alertas = []
        for p, stock_actual in productos_alerta:
            stock_actual = stock_actual or 0
            alertas.append({
                'producto_id': p.id,
                'codigo': p.codigo,
                'nombre': p.nombre,
                'clasificacion_abc': p.clasificacion_abc,
                'stock_actual': stock_actual,
                'stock_minimo': p.stock_minimo,
                'punto_pedido': p.punto_pedido,
                'urgencia': 'CRITICO' if stock_actual == 0 else 'BAJO'
            })

        # El orden ya lo puso la consulta (tiene que ser así: ordenar en Python
        # después del LIMIT ordenaría solo el pedazo que sobrevivió).

        return {
            'almacen_id': almacen_id,
            'total_alertas': total,
            'alertas_mostradas': len(alertas),
            'limite': LIMITE_ALERTAS_STOCK,
            'truncado': total > len(alertas),
            'alertas': alertas
        }

    @staticmethod
    def kpis_traslados_rutas():
        """KPIs de traslados y rutas — 4 counts en 2 queries con agregación condicional."""
        from app.models.traslado import SolicitudTraslado
        from app.models.ruta_despacho import RutaDespacho

        # "Completado hoy" con corte UTC se REINICIA A LAS 7 P.M. Colombia, en
        # mitad del turno de la tarde, y arrastra el trabajo de la noche
        # anterior. El día operativo empieza a medianoche de acá.
        from app.utils.fecha import dia_operativo, inicio_del_dia_utc
        hoy = dia_operativo()
        inicio_hoy = inicio_del_dia_utc(hoy)

        t_row = db.session.query(
            func.count(SolicitudTraslado.id).filter(SolicitudTraslado.estado == 'EN_PICKING').label('en_picking'),
            func.count(SolicitudTraslado.id).filter(SolicitudTraslado.estado == 'PREPARADO').label('preparado'),
            func.count(SolicitudTraslado.id).filter(SolicitudTraslado.estado == 'EN_TRANSITO').label('en_transito'),
            func.count(SolicitudTraslado.id).filter(
                SolicitudTraslado.estado == 'ENTREGADA',
                SolicitudTraslado.fecha_entrega >= inicio_hoy
            ).label('entregadas_hoy'),
        ).one()

        r_row = db.session.query(
            func.count(RutaDespacho.id).filter(RutaDespacho.estado == 'EN_CARGUE').label('en_cargue'),
            func.count(RutaDespacho.id).filter(RutaDespacho.estado == 'EN_TRANSITO').label('en_transito'),
            func.count(RutaDespacho.id).filter(
                RutaDespacho.estado == 'ENTREGADA',
                RutaDespacho.fecha_entregada >= inicio_hoy
            ).label('entregadas_hoy'),
        ).one()

        traslados = {
            'en_picking':     t_row.en_picking,
            'preparado':      t_row.preparado,
            'en_transito':    t_row.en_transito,
            'entregadas_hoy': t_row.entregadas_hoy,
        }
        traslados['total_activos'] = (
            traslados['en_picking'] + traslados['preparado'] + traslados['en_transito']
        )

        rutas = {
            'en_cargue':      r_row.en_cargue,
            'en_transito':    r_row.en_transito,
            'entregadas_hoy': r_row.entregadas_hoy,
        }

        return {'traslados': traslados, 'rutas': rutas}
