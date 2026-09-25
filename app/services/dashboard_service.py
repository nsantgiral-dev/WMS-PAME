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

    # Día OPERATIVO (Colombia), no día UTC. Las columnas guardan UTC naive, así
    # que el corte se expresa en ese mismo marco: `inicio_del_dia_utc`.
    from app.utils.fecha import dia_operativo, dia_operativo_de, inicio_del_dia_utc
    hoy = dia_operativo()
    inicio_ventana = inicio_del_dia_utc(hoy - timedelta(days=6))

    # Agrupado por día OPERATIVO en Python. Antes era `GROUP BY cast(col, Date)`,
    # que es el día UTC: lo hecho entre las 7 p. m. y la medianoche se contaba
    # en la barra de mañana (y en la última barra, en ninguna). La ventana ya
    # viene acotada a 7 días, así que traer los timestamps cuesta poco.
    def _por_dia(columna, *filtros):
        cuenta = {}
        for (momento,) in db.session.query(columna).filter(
                columna >= inicio_ventana, *filtros):
            clave = str(dia_operativo_de(momento))
            cuenta[clave] = cuenta.get(clave, 0) + 1
        return cuenta

    picking_map = _por_dia(TareaPicking.fecha_completado,
                           TareaPicking.estado == 'COMPLETADO')
    conteo_map = _por_dia(SesionConteo.fecha_cierre,
                          SesionConteo.estado.in_(['MATCH', 'AJUSTADO']))
    traslado_map = _por_dia(SolicitudTraslado.fecha_entrega,
                            SolicitudTraslado.estado == 'ENTREGADA')
    ruta_map = _por_dia(RutaDespacho.fecha_entregada,
                        RutaDespacho.estado == 'ENTREGADA')

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


def _cola_vacia():
    return {'n': 0, 'antes': 0, 'mas_viejo': None}


def _cola(col_fecha, *filtros):
    """Una cola del tablero: `{n, antes, mas_viejo}`. `n` cuenta desde el corte
    (`FECHA_INICIO_AUDITORIA`, sobre `col_fecha`); lo anterior va en `antes`.
    Una fila **sin fecha** cuenta como vigente (Regla 0: no saber cuándo no la
    vuelve vieja)."""
    from sqlalchemy import or_
    from app.services import corte
    c = corte.inicio_auditoria()
    q = db.session.query(func.count(), func.min(col_fecha)).filter(*filtros)
    antes = 0
    if c is not None:
        antes = (db.session.query(func.count()).filter(*filtros)
                 .filter(col_fecha < c).scalar() or 0)
        q = q.filter(or_(col_fecha >= c, col_fecha.is_(None)))
    n, mas_viejo = q.one()
    return {'n': int(n or 0), 'antes': int(antes), 'mas_viejo': mas_viejo}


def _edad_de(*colas):
    """`{mas_viejo_utc, edad_horas}` de lo más viejo entre varias colas."""
    fechas = [c['mas_viejo'] for c in colas if c['mas_viejo'] is not None]
    if not fechas:
        return {'mas_viejo_utc': None, 'edad_horas': None}
    viejo = min(fechas)
    horas = (datetime.utcnow() - viejo).total_seconds() / 3600
    return {'mas_viejo_utc': viejo.isoformat(), 'edad_horas': round(horas, 1)}


def _ira_del_dia(dia, almacen_id) -> dict:
    """IRA exacta de las cadenas cerradas `dia`: `metricas.conteo.exactitud_total`,
    la misma que usan el KPI diario y las estadísticas. Con menos de
    `MIN_N_EXACTITUD` no hay tasa (`muestra_chica`), pero sí la cuenta."""
    from app.services.metricas.conteo import (MIN_N_EXACTITUD, cadenas_del_dia,
                                              exactitud_total)
    ex = exactitud_total([c for c in cadenas_del_dia(dia, almacen_id) if c.cerrada])
    num, den = ex['numerador'], ex['denominador']
    return {'numerador': num, 'denominador': den,
            'tasa': (num / den) if den >= MIN_N_EXACTITUD else None,
            'muestra_chica': 0 < den < MIN_N_EXACTITUD,
            'min_n': MIN_N_EXACTITUD, 'definicion': ex['definicion'],
            'excluidos': ex['excluidos']}


def _tope_de_conteo(almacen_id) -> dict:
    try:
        from app.services.conteo_politica import tope_de_generacion
        return tope_de_generacion(almacen_id)
    except Exception:                                     # noqa: BLE001
        logger.warning('[DASHBOARD] tope de conteo no disponible', exc_info=True)
        return {}


def semaforo_de_conteo(descuadres: int, definitivos: int, pendientes: int, tope: dict) -> dict:
    """El semáforo de conteo del tablero, **una función**:

    · rojo — hay decisiones esperando (descuadres por decidir o definitivos);
    · amarillo — las pendientes vivas superan el cupo diario (rezago: el
      generador ya no alcanza), o el cupo no se pudo leer;
    · verde — hay trabajo dentro del cupo;
    · gris — nada pendiente.
    """
    cupo, vivas = tope.get('cupo_diario'), tope.get('pendientes_vivas')
    if definitivos:
        return {'color': 'rojo', 'texto': f'{definitivos} definitivo(s) pendiente(s)'}
    if descuadres:
        return {'color': 'rojo', 'texto': f'{descuadres} con diferencia por decidir'}
    if cupo is None or vivas is None:
        return {'color': 'amarillo', 'texto': f'{pendientes} pendientes (cupo sin dato)'}
    if cupo and vivas > cupo:
        return {'color': 'amarillo',
                'texto': f'{vivas} pendientes para un cupo de {cupo}/día'}
    if pendientes:
        return {'color': 'verde', 'texto': f'{pendientes} pendientes (cupo {cupo}/día)'}
    return {'color': 'gris', 'texto': 'sin pendientes'}



def filtro_caja_cerrada_desde(inicio_utc) -> tuple:
    """«Caja cerrada» desde `inicio_utc`: **una definición** para el KPI de
    packing y la productividad del empacador (e2e 2026-09-25).

    Se filtraba `estado == 'VERIFICADO'`, un estado de paso: al cerrar la caja
    pasa a DESPACHADO (con `fecha_verificado` puesta) y dejaba de contar — 13
    cajas cerradas se leían «Packing completado hoy: 0». Cuenta la fecha en que
    se verificó, en cualquier estado posterior salvo CANCELADO."""
    return (TareaPacking.fecha_verificado.isnot(None),
            TareaPacking.fecha_verificado >= inicio_utc,
            TareaPacking.estado != 'CANCELADO')

class DashboardService:

    @staticmethod
    def kpis_operativos(almacen_id: int):
        """KPIs principales del almacén en tiempo real.

        Cada cola cuenta **desde el corte** (`FECHA_INICIO_AUDITORIA`, por
        `fecha_creacion`): lo anterior —el ensayo— va en `antes_del_corte` de
        su bloque y no enciende el semáforo. Y cada cola dice cuánto lleva
        esperando lo más viejo (`mas_viejo_utc`, `edad_horas`): un número sin
        edad no distingue la cola de hoy de la de abril.
        """
        # "Completado hoy" con corte UTC se REINICIA A LAS 7 P.M. Colombia, en
        # mitad del turno de la tarde, y arrastra el trabajo de la noche
        # anterior. El día operativo empieza a medianoche de acá.
        from app.utils.fecha import dia_operativo, inicio_del_dia_utc
        from app.services.tablero_lider_conteo import filtro_descuadres_por_decidir
        hoy = dia_operativo()
        inicio_hoy = inicio_del_dia_utc(hoy)

        # --- PICKING ---
        p_pend = _cola(TareaPicking.fecha_creacion, TareaPicking.almacen_id == almacen_id,
                       TareaPicking.estado == 'PENDIENTE')
        p_proc = _cola(TareaPicking.fecha_creacion, TareaPicking.almacen_id == almacen_id,
                       TareaPicking.estado == 'EN_PROCESO')
        picking_completado_hoy = TareaPicking.query.filter(
            TareaPicking.almacen_id == almacen_id, TareaPicking.estado == 'COMPLETADO',
            TareaPicking.fecha_completado >= inicio_hoy).count()

        # --- PACKING ---
        k_pend = _cola(TareaPacking.fecha_creacion, TareaPacking.almacen_id == almacen_id,
                       TareaPacking.estado == 'PENDIENTE')
        k_proc = _cola(TareaPacking.fecha_creacion, TareaPacking.almacen_id == almacen_id,
                       TareaPacking.estado == 'EN_PROCESO')
        pk_row = db.session.query(
            func.count(TareaPacking.id).filter(
                *filtro_caja_cerrada_desde(inicio_hoy)
            ).label('completado_hoy'),
            func.count(TareaPacking.id).filter(
                TareaPacking.siesa_triggered == True,
                TareaPacking.siesa_triggered_at >= inicio_hoy
            ).label('siesa_hoy'),
        ).filter(TareaPacking.almacen_id == almacen_id).one()

        # --- RECEPCIÓN ---
        recepciones_hoy = RecepcionMercancia.query.filter(
            RecepcionMercancia.almacen_id == almacen_id,
            RecepcionMercancia.fecha_confirmacion >= inicio_hoy
        ).count()

        # --- CONTEO CÍCLICO ---
        # «Con diferencia» = raíces en DESCUADRE esperando decisión: la MISMA
        # definición que el tablero del líder. Antes contaba SEGUNDO_CONTEO —un
        # 1er conteo esperando el 2º—, que no es una diferencia confirmada.
        c_pend = _cola(SesionConteo.fecha_creacion, SesionConteo.almacen_id == almacen_id,
                       SesionConteo.estado == 'PENDIENTE')
        c_desc = _cola(SesionConteo.fecha_creacion, *filtro_descuadres_por_decidir(almacen_id))
        c_seg = _cola(SesionConteo.fecha_creacion, SesionConteo.almacen_id == almacen_id,
                      SesionConteo.es_segundo_conteo.is_(False),
                      SesionConteo.estado == 'SEGUNDO_CONTEO')

        # CC3 ("Conteo Definitivo") esperando que un supervisor lo tome —
        # mismo criterio que ConteoService.listar_definitivos(). Sin este
        # contador aparte, un CC3 podía quedar esperando indefinidamente sin
        # ninguna señal en el dashboard.
        from sqlalchemy.orm import aliased as _aliased_origen
        _origen_cc3 = _aliased_origen(SesionConteo)
        d_ids = [i for (i,) in db.session.query(SesionConteo.id).join(
            _origen_cc3, SesionConteo.sesion_origen_id == _origen_cc3.id
        ).filter(
            SesionConteo.almacen_id == almacen_id,
            SesionConteo.es_segundo_conteo.is_(True),
            _origen_cc3.es_segundo_conteo.is_(True),
            SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO']),
        ).all()]
        c_def = (_cola(SesionConteo.fecha_creacion, SesionConteo.id.in_(d_ids))
                 if d_ids else _cola_vacia())

        ira = _ira_del_dia(hoy, almacen_id)
        tope = _tope_de_conteo(almacen_id)
        semaforo = semaforo_de_conteo(c_desc['n'], c_def['n'], c_pend['n'], tope)

        # --- ALERTAS DE STOCK ---
        # LA MISMA consulta que alertas_stock(), no «la misma lógica».
        productos_bajo_minimo = consulta_productos_bajo_minimo(almacen_id).count()

        from app.services import corte as _corte
        return {
            'fecha': hoy.isoformat(),
            'almacen_id': almacen_id,
            'corte': _corte.estado(),
            'picking': {
                'pendiente': p_pend['n'],
                'en_proceso': p_proc['n'],
                'completado_hoy': picking_completado_hoy,
                'total_activo': p_pend['n'] + p_proc['n'],
                **_edad_de(p_pend, p_proc),
                'antes_del_corte': p_pend['antes'] + p_proc['antes'],
            },
            'packing': {
                'pendiente': k_pend['n'],
                'en_proceso': k_proc['n'],
                'completado_hoy': pk_row.completado_hoy,
                'facturas_generadas_hoy': pk_row.siesa_hoy,
                **_edad_de(k_pend, k_proc),
                'antes_del_corte': k_pend['antes'] + k_proc['antes'],
            },
            'recepcion': {
                'confirmadas_hoy': recepciones_hoy
            },
            'conteo': {
                'pendientes': c_pend['n'],
                'pendientes_edad': _edad_de(c_pend),
                'en_descuadre': c_desc['n'],
                'en_descuadre_edad': _edad_de(c_desc),
                'en_segundo_conteo': c_seg['n'],
                'definitivos_pendientes': c_def['n'],
                'definitivos_edad': _edad_de(c_def),
                'antes_del_corte': c_pend['antes'] + c_desc['antes'] + c_seg['antes'] + c_def['antes'],
                # IRA de HOY por cadenas: la misma definición que el KPI diario
                # y las estadísticas (`metricas.conteo.exactitud_total`).
                'ira_hoy': ira,
                # Antes: MATCH de sesiones cerradas hoy (un CC2 que confirma
                # contaba dos). Ahora: cadenas exactas de hoy.
                'match_hoy': ira['numerador'],
                'cupo_diario': tope.get('cupo_diario'),
                'pendientes_vivas': tope.get('pendientes_vivas'),
                'semaforo': semaforo,
            },
            'alertas': {
                'productos_bajo_minimo': productos_bajo_minimo,
                'conteos_descuadre': c_desc['n']
            },
            'connekta': connekta.estado()
        }

    @staticmethod
    def productividad_operarios(almacen_id: int, dias: int = 7):
        """Productividad por operario en los últimos N días, más qué está
        haciendo cada uno ahora mismo (tarea_actual — snapshot EN_PROCESO)."""
        from app.models.tarea_reposicion import TareaReposicion

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
                *filtro_caja_cerrada_desde(fecha_inicio)
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

        reposiciones_por_op = {
            row.abastecedor_id: row.cnt
            for row in db.session.query(
                TareaReposicion.abastecedor_id,
                func.count(TareaReposicion.id).label('cnt')
            ).filter(
                TareaReposicion.abastecedor_id.in_(operario_ids),
                TareaReposicion.estado == 'COMPLETADA',
                TareaReposicion.fecha_completada >= fecha_inicio
            ).group_by(TareaReposicion.abastecedor_id).all()
        }

        # --- Actividad en vivo — snapshot de la tarea EN_PROCESO de cada operario ---
        # Reutiliza el mismo poll de 30s que ya trae `cargarOperarios()` (TIMER_ADMIN
        # en app.js) — no es un timer nuevo, es un campo más en la misma respuesta.
        picking_activo = {
            row.operario_id: row
            for row in db.session.query(
                TareaPicking.operario_id,
                TareaPicking.tipo_documento,
                TareaPicking.referencia_documento,
                TareaPicking.fecha_inicio,
                Ubicacion.codigo.label('ubicacion_codigo'),
                Producto.nombre.label('producto_nombre'),
            ).outerjoin(Ubicacion, TareaPicking.ubicacion_id == Ubicacion.id)
             .outerjoin(Producto, TareaPicking.producto_id == Producto.id)
             .filter(
                TareaPicking.operario_id.in_(operario_ids),
                TareaPicking.estado == 'EN_PROCESO',
            ).all()
        }
        conteo_activo = {
            row.operario_id: row
            for row in db.session.query(
                SesionConteo.operario_id,
                SesionConteo.codigo,
                SesionConteo.fecha_inicio,
                Ubicacion.codigo.label('ubicacion_codigo'),
                Producto.nombre.label('producto_nombre'),
            ).outerjoin(Ubicacion, SesionConteo.ubicacion_id == Ubicacion.id)
             .outerjoin(Producto, SesionConteo.producto_id == Producto.id)
             .filter(
                SesionConteo.operario_id.in_(operario_ids),
                SesionConteo.estado == 'EN_PROCESO',
            ).all()
        }
        reposicion_activa = {
            row.abastecedor_id: row
            for row in db.session.query(
                TareaReposicion.abastecedor_id,
                TareaReposicion.codigo,
                TareaReposicion.fecha_inicio,
                Ubicacion.codigo.label('ubicacion_codigo'),
                Producto.nombre.label('producto_nombre'),
            ).outerjoin(Ubicacion, TareaReposicion.ubicacion_picking_id == Ubicacion.id)
             .outerjoin(Producto, TareaReposicion.producto_id == Producto.id)
             .filter(
                TareaReposicion.abastecedor_id.in_(operario_ids),
                TareaReposicion.estado == 'EN_PROCESO',
            ).all()
        }
        packing_activo = {
            row.empacador_id: row
            for row in db.session.query(
                TareaPacking.empacador_id,
                TareaPacking.numero_pedido_siesa,
                TareaPacking.fecha_inicio,
            ).filter(
                TareaPacking.empacador_id.in_(operario_ids),
                TareaPacking.estado == 'EN_PROCESO',
            ).all()
        }

        ahora = datetime.utcnow()

        def _minutos(fecha_inicio_tarea):
            if not fecha_inicio_tarea:
                return None
            return max(0, int((ahora - fecha_inicio_tarea).total_seconds() // 60))

        def _tarea_actual(operario_id):
            """Precedencia PICKING > REPOSICION > CONTEO > PACKING — un operario
            solo puede estar EN_PROCESO en un tipo a la vez salvo el caso borde
            de traer un conteo intercalado pausado (mobile_service.get_tarea_actual);
            ese caso ya se resuelve solo porque el conteo no vuelve a EN_PROCESO
            hasta que se retoma."""
            p = picking_activo.get(operario_id)
            if p:
                return {
                    'tipo': 'PICKING',
                    'tipo_documento': p.tipo_documento or 'PEDIDO',
                    'referencia': p.referencia_documento,
                    'ubicacion': p.ubicacion_codigo,
                    'producto': p.producto_nombre,
                    'minutos_en_tarea': _minutos(p.fecha_inicio),
                }
            r = reposicion_activa.get(operario_id)
            if r:
                return {
                    'tipo': 'REPOSICION',
                    'referencia': r.codigo,
                    'ubicacion': r.ubicacion_codigo,
                    'producto': r.producto_nombre,
                    'minutos_en_tarea': _minutos(r.fecha_inicio),
                }
            c = conteo_activo.get(operario_id)
            if c:
                return {
                    'tipo': 'CONTEO',
                    'referencia': c.codigo,
                    'ubicacion': c.ubicacion_codigo,
                    'producto': c.producto_nombre,
                    'minutos_en_tarea': _minutos(c.fecha_inicio),
                }
            pk = packing_activo.get(operario_id)
            if pk:
                return {
                    'tipo': 'PACKING',
                    'referencia': pk.numero_pedido_siesa,
                    'ubicacion': 'ZONA PACKING',
                    'producto': None,
                    'minutos_en_tarea': _minutos(pk.fecha_inicio),
                }
            return None

        resultado = []
        for operario in operarios:
            pickings = pickings_por_op.get(operario.id, 0)
            packings = packings_por_op.get(operario.id, 0)
            conteos = conteos_por_op.get(operario.id, 0)
            conteos_hoy = conteos_hoy_por_op.get(operario.id, 0)
            reposiciones = reposiciones_por_op.get(operario.id, 0)
            resultado.append({
                'operario_id': operario.id,
                'nombre': operario.nombre,
                'rol': operario.rol,
                'pickings_completados': pickings,
                'packings_completados': packings,
                'conteos_completados': conteos,
                'conteos_hoy': conteos_hoy,
                'reposiciones_completadas': reposiciones,
                'capacidad_diaria_conteo': operario.capacidad_diaria_conteo if operario.capacidad_diaria_conteo is not None else 15,
                'total_tareas': pickings + packings + conteos + reposiciones,
                'tarea_actual': _tarea_actual(operario.id),
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
        """KPIs de traslados y rutas en marcha, desde el corte
        (`FECHA_INICIO_AUDITORIA`) y con la edad de lo más viejo de cada cola."""
        from app.models.traslado import SolicitudTraslado
        from app.models.ruta_despacho import RutaDespacho

        # "Completado hoy" con corte UTC se REINICIA A LAS 7 P.M. Colombia, en
        # mitad del turno de la tarde, y arrastra el trabajo de la noche
        # anterior. El día operativo empieza a medianoche de acá.
        from app.utils.fecha import dia_operativo, inicio_del_dia_utc
        hoy = dia_operativo()
        inicio_hoy = inicio_del_dia_utc(hoy)

        t_pick = _cola(SolicitudTraslado.fecha_creacion, SolicitudTraslado.estado == 'EN_PICKING')
        t_prep = _cola(SolicitudTraslado.fecha_creacion, SolicitudTraslado.estado == 'PREPARADO')
        t_tran = _cola(SolicitudTraslado.fecha_creacion, SolicitudTraslado.estado == 'EN_TRANSITO')
        t_hoy = SolicitudTraslado.query.filter(
            SolicitudTraslado.estado == 'ENTREGADA',
            SolicitudTraslado.fecha_entrega >= inicio_hoy).count()

        r_carg = _cola(RutaDespacho.fecha_creacion, RutaDespacho.estado == 'EN_CARGUE')
        r_tran = _cola(RutaDespacho.fecha_creacion, RutaDespacho.estado == 'EN_TRANSITO')
        r_hoy = RutaDespacho.query.filter(
            RutaDespacho.estado == 'ENTREGADA',
            RutaDespacho.fecha_entregada >= inicio_hoy).count()

        traslados = {
            'en_picking':     t_pick['n'],
            'preparado':      t_prep['n'],
            'en_transito':    t_tran['n'],
            'entregadas_hoy': t_hoy,
            **_edad_de(t_pick, t_prep, t_tran),
            'antes_del_corte': t_pick['antes'] + t_prep['antes'] + t_tran['antes'],
        }
        traslados['total_activos'] = (
            traslados['en_picking'] + traslados['preparado'] + traslados['en_transito']
        )

        rutas = {
            'en_cargue':      r_carg['n'],
            'en_transito':    r_tran['n'],
            'entregadas_hoy': r_hoy,
            **_edad_de(r_carg, r_tran),
            'antes_del_corte': r_carg['antes'] + r_tran['antes'],
        }

        return {'traslados': traslados, 'rutas': rutas}
