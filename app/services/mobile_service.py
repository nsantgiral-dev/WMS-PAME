"""
Servicio Mobile — API optimizada para tablets y celulares de operarios.
Dispensador automático de tareas — el operario pide trabajo, el sistema asigna.
"""

import logging
import threading
from datetime import datetime, date, time as dtime
from app.extensions import db

# Debounce de scans: evita doble-conteo cuando la red cae después del commit
# y la PWA reintenta el mismo scan. Cache por (tarea_id, tipo, codigo) → (ts, resultado).
# Scope: por proceso/worker. No requiere migración. TTL 5s cubre el 95% de retries de red.
_SCAN_DEBOUNCE: dict = {}
_SCAN_DEBOUNCE_LOCK = threading.Lock()
_SCAN_DEBOUNCE_TTL = 5  # segundos

# Limitar threads de verificación de stock — evita colapsar el pool de conexiones DB
# bajo carga alta (10-30 operarios pickeando simultáneamente en apertura de turno).
_STOCK_VERIF_SEMAPHORE = threading.Semaphore(2)
from app.models.picking import TareaPicking, EstadoPicking
from app.models.packing import TareaPacking, ItemPacking
from app.models.conteo import SesionConteo, EstadoConteo
from app.models.producto import Producto
from app.services.picking_service import PickingService
from app.services.packing_service import PackingService
from app.services.conteo_service import ConteoService

logger = logging.getLogger(__name__)


class MobileService:

    @staticmethod
    def get_tareas_operario(operario_id: int):
        """Todas las tareas activas del operario."""
        from sqlalchemy.orm import joinedload as _jl
        from app.models.producto import Producto as _Prod
        from app.models.ubicacion import Ubicacion as _Ub

        pickings = (TareaPicking.query
                    .options(_jl(TareaPicking.producto), _jl(TareaPicking.ubicacion))
                    .filter(
                        TareaPicking.operario_id == operario_id,
                        TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
                    ).order_by(TareaPicking.prioridad.desc()).all())

        packings = TareaPacking.query.options(
            _jl(TareaPacking.items).joinedload(ItemPacking.producto)
        ).filter(
            TareaPacking.empacador_id == operario_id,
            TareaPacking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        ).all()

        conteos = (SesionConteo.query
                   .options(_jl(SesionConteo.producto), _jl(SesionConteo.ubicacion))
                   .filter(
                       SesionConteo.operario_id == operario_id,
                       SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO'])
                   ).all())

        tareas = []

        # Lookup cliente names for all pedidos referenced in pickings
        from app.models.pedido_siesa import PedidoSiesa
        referencias = {p.referencia_documento for p in pickings if p.referencia_documento}
        pedidos_map = {}
        if referencias:
            pedidos = PedidoSiesa.query.filter(PedidoSiesa.numero_pedido.in_(referencias)).all()
            pedidos_map = {ped.numero_pedido: ped.cliente for ped in pedidos}

        for p in pickings:
            tareas.append({
                'id': p.id,
                'tipo': 'PICKING',
                'prioridad': p.prioridad,
                'ubicacion': p.ubicacion.codigo if p.ubicacion else '',
                'producto_codigo': p.producto.codigo if p.producto else '',
                'producto_nombre': p.producto.nombre if p.producto else '',
                'cantidad_requerida': p.cantidad_solicitada,
                'cantidad_escaneada': p.cantidad_recogida,
                'estado': p.estado,
                'referencia': p.referencia_documento,
                'cliente': pedidos_map.get(p.referencia_documento),
                'lote': p.lote
            })

        for pk in packings:
            for item in pk.items:
                if not item.verificado:
                    tareas.append({
                        'id': pk.id,
                        'tipo': 'PACKING',
                        'prioridad': 2,
                        'ubicacion': 'ZONA PACKING',
                        'producto_codigo': item.producto.codigo if item.producto else '',
                        'producto_nombre': item.producto.nombre if item.producto else '',
                        'cantidad_requerida': item.cantidad_esperada,
                        'cantidad_escaneada': item.cantidad_real,
                        'estado': pk.estado,
                        'referencia': pk.numero_pedido_siesa,
                        'item_packing_id': item.id
                    })

        for c in conteos:
            tareas.append({
                'id': c.id,
                'tipo': 'CONTEO',
                'prioridad': 1,
                'ubicacion': c.ubicacion.codigo if c.ubicacion else '',
                'producto_codigo': c.producto.codigo if c.producto else '',
                'producto_nombre': c.producto.nombre if c.producto else '',
                'cantidad_requerida': None,
                'cantidad_escaneada': 0,
                'estado': c.estado,
                'referencia': c.codigo,
                'maneja_lote': c.maneja_lote
            })

        tareas.sort(key=lambda x: x['prioridad'], reverse=True)

        return {
            'operario_id': operario_id,
            'total_tareas': len(tareas),
            'tareas': tareas
        }

    @staticmethod
    def get_tarea_actual(operario_id: int):
        """
        Dispensador automático — el operario pide trabajo y el sistema lo asigna.
        Si ya tiene una tarea en proceso la devuelve.
        Si no: primero intenta seguir en el mismo pedido/traslado que ya tenía
        en curso (evita saltar entre documentos a medio terminar), y si no hay
        más pendientes ahí, toma la siguiente de la cola global en orden
        físico de recorrido (pasillo->fila->cuerpo->nivel->hueco — ver
        PickingService.orden_ruta_fisica()), no por fecha de creación.
        """
        # Verificar si ya tiene tarea activa — eager-load relaciones para evitar lazy queries
        from sqlalchemy.orm import joinedload as _jl
        tarea_activa = (TareaPicking.query
                        .options(_jl(TareaPicking.producto), _jl(TareaPicking.ubicacion))
                        .filter_by(operario_id=operario_id, estado='EN_PROCESO')
                        .first())

        if tarea_activa:
            return {
                'id': tarea_activa.id,
                'tipo': 'PICKING',
                'tipo_documento': tarea_activa.tipo_documento or 'PEDIDO',
                'prioridad': tarea_activa.prioridad,
                'ubicacion': tarea_activa.ubicacion.codigo if tarea_activa.ubicacion else '',
                'producto_id': tarea_activa.producto_id,
                'almacen_id': tarea_activa.almacen_id,
                'producto_codigo': tarea_activa.producto.codigo if tarea_activa.producto else '',
                'producto_nombre': tarea_activa.producto.nombre if tarea_activa.producto else '',
                'cantidad_requerida': tarea_activa.cantidad_solicitada,
                'cantidad_escaneada': tarea_activa.cantidad_recogida,
                'empaques_escaneados': tarea_activa.empaques_escaneados or 0,
                'factor_conversion': (tarea_activa.producto.factor_conversion or 1) if tarea_activa.producto else 1,
                'unidad_empaque': (tarea_activa.producto.unidad_empaque or '').upper() if tarea_activa.producto else '',
                'estado': tarea_activa.estado,
                'referencia': tarea_activa.referencia_documento,
                'lote': tarea_activa.lote,
            }

        # Tomar siguiente tarea de la cola global — más prioritaria y más antigua.
        # REGLA ESTRICTA: el picker solo puede ir a ubicaciones tipo_zona = PICKING o GENERAL.
        # Las zonas RESERVA (pacas selladas en alto) son exclusivas del Abastecedor.
        from app.models.ubicacion import Ubicacion
        from app.models.usuario import Usuario as _U
        from app.models.almacen import Almacen as _Alm
        _u_pick = _U.query.get(operario_id)
        # picker_traslado y roles de tienda: solo ven tareas TRASLADO de su bodega
        _solo_traslado = _u_pick and _u_pick.rol in ('picker_traslado', 'packer_traslado')
        # picker_traslado (no packer) recibe conteo cíclico cuando no hay traslados
        _picker_tienda = _u_pick and _u_pick.rol == 'picker_traslado'

        # Conteo activo — el picker ya tiene un conteo en curso (p.ej. recargó la PWA).
        # Solo se devuelve de inmediato para roles de tienda/traslado (comportamiento
        # sin cambios). Para el resto (ej. 'operario' de NB1) se pospone: un picking o
        # traslado pendiente tiene prioridad y debe atenderse ya — el conteo se queda
        # intacto en EN_PROCESO (con su cantidad_fisica acumulada) y se retoma más abajo,
        # exactamente donde quedó, en cuanto no haya más trabajo pendiente.
        if _solo_traslado:
            conteo_activo = (SesionConteo.query
                             .filter_by(operario_id=operario_id, estado=EstadoConteo.EN_PROCESO)
                             .first())
            if conteo_activo:
                return MobileService._conteo_a_dict(conteo_activo)

        # Bodega propia del operario — por bodega_siesa_id directo (roles de tienda)
        # o resuelta desde su almacen_id (roles NB1 como 'operario'). Se usa para que
        # un operario de NB1 nunca reciba un TRASLADO cuyo origen es otra bodega
        # (ej. NC1 → NB1): NB1 solo debe pickear traslados donde NB1 es el origen.
        _bodega_propia = _u_pick.bodega_siesa_id if _u_pick else None
        if not _bodega_propia and _u_pick and _u_pick.almacen_id:
            _almacen_propio = _Alm.query.get(_u_pick.almacen_id)
            _bodega_propia = _almacen_propio.bodega_siesa_id if _almacen_propio else None

        # Subquery: IDs de ubicaciones permitidas para pickers (no RESERVA)
        _ids_validos = db.session.query(Ubicacion.id).filter(
            Ubicacion.tipo_zona.in_(['PICKING', 'GENERAL'])
        ).scalar_subquery()
        _filtros_base = [
            TareaPicking.estado == 'PENDIENTE',
            TareaPicking.operario_id.is_(None),
            db.or_(
                TareaPicking.ubicacion_id.is_(None),
                TareaPicking.ubicacion_id.in_(_ids_validos),
            ),
        ]
        if _solo_traslado:
            _filtros_base.append(TareaPicking.tipo_documento == 'TRASLADO')
        if _bodega_propia:
            _filtros_base.append(
                db.or_(
                    db.func.coalesce(TareaPicking.tipo_documento, '') != 'TRASLADO',
                    TareaPicking.bodega_origen_siesa == _bodega_propia,
                )
            )

        # Orden real de despacho — reutiliza PickingService.orden_ruta_fisica()
        # en vez de duplicarlo: una sola fuente de verdad de qué es "cerca".
        # PD (PEDIDO) antes que ST (TRASLADO), luego prioridad, luego el
        # recorrido físico pasillo->fila->cuerpo->nivel->hueco, y fecha_creacion
        # como último desempate.
        _orden_dispatch = (
            db.case({'PEDIDO': 0, 'TRASLADO': 1},
                    value=TareaPicking.tipo_documento, else_=0).asc(),
            TareaPicking.prioridad.desc(),
            *PickingService.orden_ruta_fisica(),
            TareaPicking.fecha_creacion.asc(),
        )
        # JOIN necesario para ordenar por ubicacion — with_for_update(of=TareaPicking)
        # lockea solo tareas_picking, evitando el error de Postgres "FOR UPDATE cannot
        # be applied to the nullable side of an outer join" (el join es INNER: ubicacion_id
        # es NOT NULL en TareaPicking, así que nunca se pierde una fila por el join).
        _base_tarea_query = (
            TareaPicking.query
            .join(Ubicacion, TareaPicking.ubicacion_id == Ubicacion.id)
            .filter(*_filtros_base)
        )

        # Terminar lo que ya empezó antes de saltar a otro pedido/traslado — mismo
        # criterio que PickingService.siguiente_tarea_para(), reutilizado aquí en
        # vez de reimplementado, para no volver a divergir del original.
        tarea = None
        _documento_actual = PickingService._documento_en_curso(operario_id)
        if _documento_actual:
            tarea = (
                _base_tarea_query
                .filter(TareaPicking.referencia_documento == _documento_actual)
                .order_by(*_orden_dispatch)
                .with_for_update(of=TareaPicking, skip_locked=True)
                .first()
            )

        if not tarea:
            tarea = (
                _base_tarea_query
                .order_by(*_orden_dispatch)
                .with_for_update(of=TareaPicking, skip_locked=True)
                .first()
            )

        if not tarea:
            # Reposición RESERVA→PICKING — nivel 2 de la cola unificada, entre
            # Pedido/Traslado (arriba) y Conteo cíclico (abajo): un hueco
            # PICKING vacío bloquea el próximo pedido/traslado que se pueda
            # pickear de ahí, así que pesa más que el conteo (higiene de
            # inventario, puede esperar). Gateado por puede_abastecer — RESERVA
            # es zona exclusiva de Abastecedor (ver REGLA ESTRICTA arriba);
            # roles de tienda/traslado nunca la reciben, igual que Conteo.
            # Reutiliza get_tarea_abastecedor() — misma función que ya usa la
            # pantalla dedicada del abastecedor puro, Regla 0.
            if not _solo_traslado and _u_pick and _u_pick.puede_abastecer:
                from app.services.reposicion_service import get_tarea_abastecedor as _get_rep
                rep = _get_rep(operario_id)
                if rep:
                    return rep

            # Roles de tienda/traslado nunca reciben conteos cíclicos — solo NB1
            if not _solo_traslado:
                # Retomar el conteo propio en curso (pospuesto arriba porque un
                # picking/traslado nuevo tiene prioridad). Ya no hay nada pendiente,
                # así que se le devuelve tal cual quedó, con lo ya contado intacto.
                conteo_activo = (SesionConteo.query
                                 .filter_by(operario_id=operario_id, estado=EstadoConteo.EN_PROCESO)
                                 .first())
                if conteo_activo:
                    return MobileService._conteo_a_dict(conteo_activo)

                # CC2 pre-asignado (doble ciego): tiene operario_id pero sigue en PENDIENTE
                _cc2 = MobileService._get_conteo_preassignado(operario_id)
                if _cc2:
                    return _cc2

                # Sin picking pendiente — buscar conteo sin asignar
                # with_for_update(skip_locked=True) evita que dos workers tomen el mismo conteo
                # selectinload: pre-cargar producto/ubicacion antes del commit — después del
                # commit expire_on_commit invalida todo y _conteo_a_dict necesita ambas relaciones.
                from sqlalchemy.orm import selectinload as _sl_conteo_disp
                conteo = (SesionConteo.query
                          .options(_sl_conteo_disp(SesionConteo.producto), _sl_conteo_disp(SesionConteo.ubicacion))
                          .filter_by(estado='PENDIENTE', operario_id=None)
                          .order_by(SesionConteo.fecha_creacion.asc())
                          .with_for_update(skip_locked=True)
                          .first())

                if conteo:
                    conteo.operario_id = operario_id
                    conteo.estado = EstadoConteo.EN_PROCESO
                    conteo.fecha_inicio = datetime.utcnow()
                    db.session.commit()
                    logger.info(f'[MOBILE] Conteo {conteo.codigo} asignado a operario {operario_id}')
                    return MobileService._conteo_a_dict(conteo)

                # Sin picking ni conteo — verificar packing
                resultado = MobileService.get_tareas_operario(operario_id)
                if resultado['tareas']:
                    return resultado['tareas'][0]
                return None

            # Liberar conteos de OTRA bodega asignados erróneamente.
            # Los de la propia bodega son válidos — los gestiona _next_conteo_tienda.
            _almacen_propio = _u_pick.almacen_id if _u_pick else None
            # Fallback: picker_traslado puede no tener almacen_id (FK) asignado,
            # pero sí bodega_siesa_id ('NC1'). Resolver por lookup en almacenes.
            if not _almacen_propio and _u_pick and _u_pick.bodega_siesa_id:
                from app.models.almacen import Almacen as _Alm
                _alm_ref = _Alm.query.filter_by(
                    bodega_siesa_id=_u_pick.bodega_siesa_id
                ).first()
                _almacen_propio = _alm_ref.id if _alm_ref else None
            _q_err = SesionConteo.query.filter(
                SesionConteo.operario_id == operario_id,
                SesionConteo.estado.in_([EstadoConteo.EN_PROCESO, EstadoConteo.PENDIENTE]),
            )
            if _almacen_propio:
                _q_err = _q_err.filter(
                    db.or_(
                        SesionConteo.almacen_id.is_(None),
                        SesionConteo.almacen_id != _almacen_propio,
                    )
                )
            _erroneos = _q_err.all()
            if _erroneos:
                for c in _erroneos:
                    c.operario_id = None
                    c.estado = EstadoConteo.PENDIENTE
                    c.fecha_inicio = None
                db.session.commit()
                logger.info('[MOBILE] %d conteo(s) de otra bodega liberados de picker_traslado %s',
                            len(_erroneos), operario_id)

            if _picker_tienda and _almacen_propio:
                # CC2 pre-asignado tiene prioridad sobre el dispatcher cíclico
                _cc2 = MobileService._get_conteo_preassignado(operario_id)
                if _cc2:
                    return _cc2
                conteo = MobileService._next_conteo_tienda(operario_id, _almacen_propio)
                if conteo:
                    return conteo

            return None

        # ── Task Interleaving ────────────────────────────────────────────────────
        # El picker va a la ubicación X. Si hay un conteo pendiente ahí, lo
        # asignamos ahora para que lo haga al terminar el picking — sin viaje extra.
        # Respetar capacidad_diaria_conteo del operario (0 = sin límite).
        conteo_intercalado = None
        if tarea.ubicacion_id:
            # Una sola query: capacidad del operario + conteos de hoy juntos
            from sqlalchemy import text as _text
            # `capacidad_diaria_conteo` es un CUPO DIARIO. Con corte UTC se
            # reiniciaba a las 7 p.m. Colombia y el operario podía hacer el
            # doble en el turno de la tarde sin que nada lo notara.
            from datetime import timedelta as _td

            from app.utils.fecha import dia_operativo, inicio_del_dia_utc
            hoy = dia_operativo()
            inicio_hoy = inicio_del_dia_utc(hoy)
            fin_hoy = inicio_del_dia_utc(hoy + _td(days=1))
            _row = db.session.execute(
                _text("""
                    SELECT u.capacidad_diaria_conteo,
                           COUNT(sc.id) AS conteos_hoy
                    FROM usuarios u
                    LEFT JOIN sesiones_conteo sc
                        ON sc.operario_id = u.id
                        AND sc.fecha_inicio >= :inicio
                        AND sc.fecha_inicio < :fin
                    WHERE u.id = :uid
                    GROUP BY u.capacidad_diaria_conteo
                """),
                {'uid': operario_id, 'inicio': inicio_hoy, 'fin': fin_hoy}
            ).first()
            capacidad = (_row.capacidad_diaria_conteo if _row and _row.capacidad_diaria_conteo is not None else 15)
            conteos_hoy = int(_row.conteos_hoy) if _row else 0

            bajo_tope = True
            if capacidad > 0:
                bajo_tope = conteos_hoy < capacidad
                if not bajo_tope:
                    logger.info(
                        f'[INTERLEAVING] Operario {operario_id} alcanzó tope diario '
                        f'({conteos_hoy}/{capacidad}) — no se inyecta conteo'
                    )

            if bajo_tope and not _solo_traslado:
                from sqlalchemy.orm import joinedload as _jl_cm
                conteo_mismo_lugar = (SesionConteo.query
                    .options(_jl_cm(SesionConteo.producto))
                    .filter_by(
                        ubicacion_id=tarea.ubicacion_id,
                        estado='PENDIENTE',
                        operario_id=None
                    ).first())
                if conteo_mismo_lugar:
                    conteo_mismo_lugar.operario_id = operario_id
                    # No cambia a EN_PROCESO aún — el operario primero hace el picking
                    db.session.flush()
                    conteo_intercalado = {
                        'id':              conteo_mismo_lugar.id,
                        'codigo':          conteo_mismo_lugar.codigo,
                        'producto_codigo': conteo_mismo_lugar.producto.codigo if conteo_mismo_lugar.producto else '',
                        'producto_nombre': conteo_mismo_lugar.producto.nombre if conteo_mismo_lugar.producto else '',
                        'clasificacion':   conteo_mismo_lugar.clasificacion_abc or '?',
                    }
                    logger.info(
                        f'[INTERLEAVING] Conteo {conteo_mismo_lugar.codigo} '
                        f'inyectado junto al picking {tarea.codigo} — ubicación {tarea.ubicacion_id} '
                        f'({conteos_hoy + 1 if capacidad > 0 else "∞"}/{capacidad or "∞"})'
                    )

        # Capturar atributos antes del commit — expire_on_commit los invalida después
        _tarea_id = tarea.id
        _tarea_codigo = tarea.codigo
        _tarea_prioridad = tarea.prioridad
        _tarea_ubicacion = tarea.ubicacion.codigo if tarea.ubicacion else ''
        _tarea_producto_id = tarea.producto_id
        _tarea_almacen_id = tarea.almacen_id
        _tarea_producto_codigo = tarea.producto.codigo if tarea.producto else ''
        _tarea_producto_nombre = tarea.producto.nombre if tarea.producto else ''
        _tarea_cantidad_requerida = tarea.cantidad_solicitada
        _tarea_cantidad_escaneada = tarea.cantidad_recogida
        _tarea_empaques_escaneados = tarea.empaques_escaneados or 0
        _tarea_factor = tarea.producto.factor_conversion or 1 if tarea.producto else 1
        _tarea_unidad_empaque = (tarea.producto.unidad_empaque or '').upper() if tarea.producto else ''
        _tarea_referencia = tarea.referencia_documento
        _tarea_lote = tarea.lote
        _tarea_tipo_documento = tarea.tipo_documento or 'PEDIDO'

        # Asignar picking al operario
        tarea.operario_id = operario_id
        tarea.estado = EstadoPicking.EN_PROCESO
        tarea.fecha_inicio = datetime.utcnow()
        db.session.commit()

        logger.info(f'[MOBILE] Picking {_tarea_codigo} asignado a operario {operario_id}')

        resultado = {
            'id': _tarea_id,
            'tipo': 'PICKING',
            'tipo_documento': _tarea_tipo_documento,
            'prioridad': _tarea_prioridad,
            'ubicacion': _tarea_ubicacion,
            'producto_id': _tarea_producto_id,
            'almacen_id': _tarea_almacen_id,
            'producto_codigo': _tarea_producto_codigo,
            'producto_nombre': _tarea_producto_nombre,
            'cantidad_requerida': _tarea_cantidad_requerida,
            'cantidad_escaneada': _tarea_cantidad_escaneada,
            'empaques_escaneados': _tarea_empaques_escaneados,
            'factor_conversion': _tarea_factor,
            'unidad_empaque': _tarea_unidad_empaque,
            'estado': 'EN_PROCESO',
            'referencia': _tarea_referencia,
            'lote': _tarea_lote,
            'conteo_intercalado': conteo_intercalado,
        }
        return resultado

    @staticmethod
    def _conteo_a_dict(c: SesionConteo) -> dict:
        return {
            'id': c.id,
            'tipo': 'CONTEO',
            'prioridad': 1,
            'ubicacion': c.ubicacion.codigo if c.ubicacion else '',
            'producto_codigo': c.producto.codigo if c.producto else '',
            'producto_nombre': c.producto.nombre if c.producto else '',
            'cantidad_requerida': None,
            'cantidad_escaneada': c.cantidad_fisica or 0,
            'estado': c.estado,
            'referencia': c.codigo,
            'maneja_lote': c.maneja_lote,
            'clasificacion_abc': c.clasificacion_abc,
            'conteo_intercalado': None,
        }

    @staticmethod
    def _next_conteo_tienda(operario_id: int, almacen_id: int):
        """
        SRP: única responsabilidad — asignar el próximo conteo cíclico de la bodega
        a un picker_traslado cuando no hay traslados pendientes.
        Prioridad A → B → C, más antigua primero dentro de cada clase.
        Usa skip_locked para evitar colisiones entre workers concurrentes.
        """
        from sqlalchemy import case as _sa_case
        from sqlalchemy.orm import selectinload as _sl_tienda
        _prioridad_abc = _sa_case(
            {'A': 1, 'B': 2, 'C': 3},
            value=SesionConteo.clasificacion_abc,
            else_=4,
        )
        # selectinload: pre-cargar producto/ubicacion antes del commit — expire_on_commit
        # los invalida y _conteo_a_dict necesita ambas relaciones.
        sesion = (
            SesionConteo.query
            .options(_sl_tienda(SesionConteo.producto), _sl_tienda(SesionConteo.ubicacion))
            .filter(
                SesionConteo.almacen_id == almacen_id,
                SesionConteo.estado == EstadoConteo.PENDIENTE,
                SesionConteo.operario_id.is_(None),
                SesionConteo.es_segundo_conteo.is_(False),  # CC2 va al admin, no al dispatcher cíclico
            )
            .order_by(_prioridad_abc, SesionConteo.fecha_creacion.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not sesion:
            return None
        sesion.operario_id = operario_id
        sesion.estado = EstadoConteo.EN_PROCESO
        sesion.fecha_inicio = datetime.utcnow()
        db.session.commit()
        logger.info('[MOBILE] Conteo %s asignado a picker_traslado %s (bodega almacen_id=%s)',
                    sesion.codigo, operario_id, almacen_id)
        return MobileService._conteo_a_dict(sesion)

    @staticmethod
    def _get_conteo_preassignado(operario_id: int):
        """
        SRP: activa y retorna un conteo pre-asignado PENDIENTE (ej: CC2 de doble ciego).
        A diferencia de _next_conteo_tienda, no busca operario_id=None — busca tareas
        que ya tienen este picker asignado pero aún no han sido iniciadas.
        """
        from app.models.conteo import EstadoConteo as _EC
        from sqlalchemy.orm import selectinload as _sl_pre
        # selectinload: pre-cargar producto/ubicacion antes del commit — expire_on_commit
        # los invalida y _conteo_a_dict necesita ambas relaciones.
        sesion = (
            SesionConteo.query
            .options(_sl_pre(SesionConteo.producto), _sl_pre(SesionConteo.ubicacion))
            .filter(
                SesionConteo.operario_id == operario_id,
                SesionConteo.estado == _EC.PENDIENTE,
            )
            .order_by(SesionConteo.fecha_creacion.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if not sesion:
            return None
        sesion.estado = _EC.EN_PROCESO
        sesion.fecha_inicio = datetime.utcnow()
        db.session.commit()
        logger.info('[MOBILE] Conteo pre-asignado %s activado para picker %s',
                    sesion.codigo, operario_id)
        return MobileService._conteo_a_dict(sesion)

    @staticmethod
    def _normalizar(codigo: str) -> str:
        """Limpia ruido del lector (saltos de línea, espacios, mayúsculas)."""
        return str(codigo or '').strip().upper()

    @staticmethod
    def _codigos_validos(producto) -> set:
        """Conjunto de todos los códigos aceptables (unidad suelta y empaque)."""
        validos = {
            MobileService._normalizar(producto.codigo),
            MobileService._normalizar(producto.codigo_siesa or ''),
            MobileService._normalizar(producto.codigo_barras or ''),
            MobileService._normalizar(producto.codigo_barras_empaque or ''),
        }
        validos.discard('')
        return validos

    @staticmethod
    def _es_escaneo_empaque(producto, codigo_limpio: str) -> bool:
        """True si el código escaneado es el EAN del empaque y el factor > 1."""
        if not producto.codigo_barras_empaque or (producto.factor_conversion or 1) <= 1:
            return False
        return codigo_limpio == MobileService._normalizar(producto.codigo_barras_empaque)

    @staticmethod
    def procesar_escaneo(operario_id: int, tarea_id: int,
                         tipo: str, codigo: str, cantidad: int = 1,
                         lpn_codigo: str = None,
                         total_acumulado: int = None):
        """
        Procesa un escaneo — funciona con cámara o láser Bluetooth.
        Valida que el código escaneado corresponde al producto de la tarea.
        Acepta: referencia interna, código Siesa, código de barras EAN (unidad o empaque).

        Si el código coincide con codigo_barras_empaque y factor_conversion > 1,
        cada scan suma factor_conversion unidades y acumula empaques_escaneados.

        lpn_codigo: si se provee y la tarea es de TRASLADO, vincula el LPN
                    automáticamente al traslado y lo marca EN_TRANSITO.
        """
        codigo_limpio = MobileService._normalizar(codigo)

        # Debounce: si el mismo scan llega dos veces en < 5s (retry por red),
        # devolver el resultado cacheado sin incrementar la cantidad.
        # total_acumulado forma parte de la key — scans distintos tienen totales distintos
        # y nunca colisionan entre sí, incluso en el mismo worker dentro del TTL.
        _db_key = (tarea_id, tipo, codigo_limpio, total_acumulado)
        _ahora = datetime.utcnow().timestamp()
        with _SCAN_DEBOUNCE_LOCK:
            _cached = _SCAN_DEBOUNCE.get(_db_key)
            if _cached:
                _ts_cache, _res_cache = _cached
                if _ahora - _ts_cache < _SCAN_DEBOUNCE_TTL:
                    logger.info(
                        f'[SCAN DEBOUNCE] Scan duplicado ignorado: tarea={tarea_id} '
                        f'tipo={tipo} codigo={codigo_limpio} (delta={_ahora - _ts_cache:.1f}s)'
                    )
                    return _res_cache

        if tipo == 'PICKING':
            # [C5] SELECT FOR UPDATE — serializa confirmaciones concurrentes.
            # selectinload en vez de joinedload: PostgreSQL no permite FOR UPDATE
            # con LEFT OUTER JOIN (psycopg2.errors.FeatureNotSupported).
            from sqlalchemy.orm import selectinload as _sl_scan
            tarea = (TareaPicking.query
                     .options(_sl_scan(TareaPicking.producto))
                     .filter_by(id=tarea_id)
                     .with_for_update()
                     .first())
            if not tarea:
                raise ValueError('Tarea no encontrada')

            # [P5] Bloquear picking si el pedido fue anulado en Siesa.
            # Solo aplica a tareas PD — los traslados nunca tienen pedido_anulado_siesa.
            if tarea.referencia_documento and tarea.tipo_documento != 'TRASLADO':
                from app.models.packing import TareaPacking as _TP
                _pk_anulado = _TP.query.filter_by(
                    numero_pedido_siesa=tarea.referencia_documento,
                    pedido_anulado_siesa=True,
                ).first()
                if _pk_anulado:
                    raise ValueError(
                        f'Pedido {tarea.referencia_documento} fue anulado en Siesa '
                        f'(estado={_pk_anulado.pedido_estado_siesa_detectado or "?"}). '
                        f'Detén el picking y contacta a tu supervisor.'
                    )

            producto = tarea.producto
            if not producto:
                raise ValueError('Producto no encontrado en la tarea')

            if codigo_limpio not in MobileService._codigos_validos(producto):
                raise ValueError({
                    'tipo': 'PRODUCTO_INCORRECTO',
                    'mensaje': f'Escaneaste {codigo_limpio} pero la tarea pide {producto.codigo}',
                    'esperado': producto.codigo,
                    'escaneado': codigo_limpio
                })

            # Detectar si escanearon el empaque (caja/paca) o la unidad suelta
            es_empaque = MobileService._es_escaneo_empaque(producto, codigo_limpio)
            factor = producto.factor_conversion or 1
            unidades_este_scan = cantidad * factor if es_empaque else cantidad

            if total_acumulado is not None:
                # Idempotente: el cliente lleva el contador. MAX evita que un paquete
                # retrasado (out-of-order entre workers) regrese el total en DB.
                nueva_cantidad = max(total_acumulado, tarea.cantidad_recogida)
            else:
                nueva_cantidad = tarea.cantidad_recogida + unidades_este_scan
            if nueva_cantidad > tarea.cantidad_solicitada:
                raise ValueError({
                    'tipo': 'EXCESO',
                    'mensaje': (
                        f'Ya tienes {tarea.cantidad_recogida} de {tarea.cantidad_solicitada}. '
                        f'{"Este empaque tiene " + str(factor) + " UND — " if es_empaque else ""}'
                        f'no caben {unidades_este_scan} más.'
                    ),
                    'cantidad_actual': tarea.cantidad_recogida,
                    'cantidad_maxima': tarea.cantidad_solicitada,
                    'unidades_este_scan': unidades_este_scan,
                })

            tarea.cantidad_recogida = nueva_cantidad
            if es_empaque:
                tarea.empaques_escaneados = (tarea.empaques_escaneados or 0) + cantidad

            if tarea.estado == EstadoPicking.PENDIENTE:
                tarea.estado = EstadoPicking.EN_PROCESO
                tarea.operario_id = operario_id
                tarea.fecha_inicio = datetime.utcnow()

            # ── Vincular LPN al traslado si aplica ───────────────────────────
            # Cuando el operario escanea un LPN durante el picking de un traslado,
            # lo marcamos EN_TRANSITO y lo asociamos a la solicitud. Esto permite
            # rastrear qué pacas físicas van en cada envío.
            if lpn_codigo and tarea.tipo_documento == 'TRASLADO' and tarea.referencia_documento:
                try:
                    from app.models.lpn import LPN, EstadoLPN
                    from app.models.traslado import SolicitudTraslado
                    lpn = LPN.query.filter_by(codigo=lpn_codigo, estado=EstadoLPN.ACTIVO).first()
                    if lpn and lpn.producto_id == producto.id:
                        traslado = SolicitudTraslado.query.filter_by(
                            codigo=tarea.referencia_documento
                        ).first()
                        if traslado:
                            lpn.traslado_id = traslado.id
                            lpn.estado = 'EN_TRANSITO'
                            logger.info(
                                f'[TRASLADO] LPN {lpn_codigo} vinculado a {traslado.codigo} → EN_TRANSITO'
                            )
                except Exception as e_lpn:
                    logger.warning(f'[TRASLADO] No se pudo vincular LPN {lpn_codigo}: {e_lpn}')

            # Capturar antes del commit — expire_on_commit invalida los atributos post-commit
            _cantidad_solicitada = tarea.cantidad_solicitada
            _unidad_empaque = producto.unidad_empaque
            db.session.commit()
            completado = nueva_cantidad >= _cantidad_solicitada
            unidad_label = (_unidad_empaque or 'PKG').upper()

            if completado:
                mensaje = '¡Completo! Presiona confirmar'
            elif es_empaque:
                mensaje = f'{cantidad} {unidad_label} = {unidades_este_scan} und  →  {nueva_cantidad}/{_cantidad_solicitada}'
            else:
                mensaje = f'{nueva_cantidad} de {_cantidad_solicitada}'

            _resultado_picking = {
                'exito': True,
                'tipo': 'PICKING',
                'codigo_escaneado': codigo,
                'es_empaque': es_empaque,
                'unidades_este_scan': unidades_este_scan,
                'empaques_escaneados': tarea.empaques_escaneados or 0,
                'factor_conversion': factor,
                'unidad_empaque': unidad_label,
                'cantidad_actual': nueva_cantidad,
                'cantidad_requerida': tarea.cantidad_solicitada,
                'completado': completado,
                'puede_confirmar': completado,
                'mensaje': mensaje,
            }
            with _SCAN_DEBOUNCE_LOCK:
                _SCAN_DEBOUNCE[_db_key] = (datetime.utcnow().timestamp(), _resultado_picking)
            return _resultado_picking

        elif tipo == 'PACKING':
            from sqlalchemy.orm import selectinload as _sl
            # with_for_update() previene lost-update cuando dos workers del mismo operario
            # (retry por conexión móvil inestable) leen item.cantidad_real simultáneamente.
            tarea = (TareaPacking.query
                     .options(_sl(TareaPacking.items).selectinload(ItemPacking.producto))
                     .filter_by(id=tarea_id)
                     .with_for_update()
                     .first())
            if not tarea:
                raise ValueError('Tarea no encontrada')

            # Buscar dentro de los ítems de la tarea — acepta codigo, codigo_siesa,
            # codigo_barras Y codigo_barras_empaque sin necesidad de query global.
            producto = None
            item = None
            for it in tarea.items:
                p = it.producto
                if p and codigo_limpio in MobileService._codigos_validos(p):
                    producto = p
                    item = it
                    break

            if not producto or not item:
                raise ValueError(f'Producto {codigo_limpio} no pertenece a este pedido')

            # Calcular cuántas unidades suma este scan.
            # El frontend resuelve el DUN-14 de empaque vía /api/empaques/scan y envía:
            #   { codigo: producto.codigo, cantidad: factor }  ← ya en UND
            # Si el código NO pasó por ese resolver (fallo de red, escaneo directo del EAN empaque),
            # es_empaque = True y aplicamos la conversión manualmente.
            es_empaque = MobileService._es_escaneo_empaque(producto, codigo_limpio)
            factor = producto.factor_conversion or 1

            # Detectar si cantidad_esperada está en unidades de empaque (PQ) o en UND.
            # Heurístico: si cant_esp > 0 y cant_esp < factor → está en empaque (PQ).
            # Esto cubre packings creados antes de la normalización a UND.
            _cant_en_pq = (factor > 1 and 0 < item.cantidad_esperada < factor)

            if _cant_en_pq:
                # cantidad_esperada está en PQ → contar en PQ también.
                # Independientemente de lo que envíe el frontend, sumamos 1 PQ por scan.
                unidades = 1
            elif es_empaque:
                # cantidad_esperada está en UND y el scan fue un empaque directo → convertir
                unidades = cantidad * factor
            else:
                # cantidad_esperada en UND, scan de unidad suelta o ya convertido por el frontend
                unidades = cantidad

            # Releer cantidad_real desde el objeto bloqueado (valor fresco post-lock)
            actual = item.cantidad_real or 0
            if total_acumulado is not None:
                # Modo idempotente: el cliente lleva el contador — reintento seguro
                nueva = total_acumulado
            else:
                # Modo legacy: += (protegido por debounce TTL 5s)
                nueva = actual + unidades
            if nueva > item.cantidad_esperada:
                _unid_label = f'{factor} und' if (not _cant_en_pq and factor > 1) else '1 PQ'
                raise ValueError(
                    f'Exceso: ya tienes {actual} de {item.cantidad_esperada} '
                    f'{"PQ" if _cant_en_pq else "und"} — '
                    f'no caben {unidades} más'
                )

            item.cantidad_real = nueva
            # verificado = True solo cuando se alcanza o supera la cantidad esperada.
            # Antes era `is not None`, que marcaba el ítem como done tras el PRIMER escaneo
            # aunque faltaran unidades → el HUD saltaba al siguiente producto prematuramente.
            item.verificado = nueva >= item.cantidad_esperada
            db.session.commit()

            todos_verificados = all(i.verificado for i in tarea.items)

            return {
                'exito': True,
                'tipo': 'PACKING',
                'codigo_escaneado': codigo,
                'producto_id': producto.id,
                'producto_codigo': producto.codigo,
                'producto_nombre': producto.nombre,
                'cantidad_actual': item.cantidad_real,
                'cantidad_requerida': item.cantidad_esperada,
                'item_completado': item.verificado,
                'todos_completados': todos_verificados,
                'puede_confirmar': todos_verificados,
                'tiene_diferencia': item.tiene_diferencia(),
                'mensaje': '¡Pedido listo!' if todos_verificados else f'{item.cantidad_real} de {item.cantidad_esperada}'
            }

        elif tipo == 'CONTEO':
            # WITH FOR UPDATE previene que dos workers procesen el mismo escaneo simultáneamente.
            # Idempotencia: el cliente envía total_acumulado → servidor usa SET en vez de +=.
            # Sin total_acumulado (legacy), el debounce de 5s cubre retries rápidos de red.
            from sqlalchemy.orm import selectinload as _sl_conteo
            sesion = (SesionConteo.query
                      .options(_sl_conteo(SesionConteo.producto))
                      .filter_by(id=tarea_id)
                      .with_for_update()
                      .first())
            if not sesion:
                raise ValueError('Sesión de conteo no encontrada')

            producto = sesion.producto
            if codigo_limpio not in MobileService._codigos_validos(producto):
                raise ValueError(f'Producto incorrecto — escanea {producto.codigo}')

            if total_acumulado is not None:
                # Modo idempotente: el cliente lleva el contador — reintento seguro
                sesion.cantidad_fisica = total_acumulado
            else:
                # Modo legacy: += (protegido por debounce TTL 5s)
                sesion.cantidad_fisica = (sesion.cantidad_fisica or 0) + cantidad
            db.session.commit()

            return {
                'exito': True,
                'tipo': 'CONTEO',
                'codigo_escaneado': codigo,
                'cantidad_contada': sesion.cantidad_fisica,
                'puede_confirmar': True,
                'mensaje': f'Contando: {sesion.cantidad_fisica} unidades'
            }

        raise ValueError(f'Tipo de tarea desconocido: {tipo}')

    @staticmethod
    def confirmar_tarea(operario_id: int, tarea_id: int,
                        tipo: str, items_escaneados: list = None,
                        cantidad_manual: int = None):
        """Confirma la tarea completa."""
        if tipo == 'PICKING':
            tarea = TareaPicking.query.get(tarea_id)
            if not tarea:
                raise ValueError('Tarea no encontrada')
            almacen_id = tarea.almacen_id
            # cantidad_manual: confirmación sin escáner (operario contó físicamente)
            cantidad = cantidad_manual if cantidad_manual is not None else tarea.cantidad_recogida
            # Capturar antes del commit — expire_on_commit invalida los atributos
            _ref_doc = tarea.referencia_documento
            _tipo_doc = tarea.tipo_documento
            resultado = PickingService.confirmar_picking(
                tarea_id=tarea_id,
                cantidad_recogida=cantidad,
                usuario_id=operario_id
            ).to_dict()
            try:
                from app.models.pedido_siesa import PedidoSiesa as _PS
                _operario_nombre = tarea.operario.nombre if tarea.operario else '—'
                _cliente = ''
                if _ref_doc:
                    ped = _PS.query.filter_by(numero_pedido=_ref_doc).first()
                    _cliente = ped.cliente if ped else ''
                resultado['canasto_data'] = {
                    'pedido':   _ref_doc or '—',
                    'cliente':  _cliente,
                    'operario': _operario_nombre,
                    'ref':      resultado.get('referencia') or '',
                    'cantidad': cantidad,
                }
            except Exception as _e:
                logger.warning('[MOBILE] canasto_data falló: %s', _e)
            # Disparar verificación de stock en background — throttled a 2 threads concurrentes
            # para no colapsar el connection pool DB durante apertura de turno (N operarios simultáneos)
            try:
                from app.services.reposicion_service import verificar_stock_picking as _vsp
                from flask import current_app as _curr_app
                _app = _curr_app._get_current_object()

                def _run_vsp(aid, app):
                    if _STOCK_VERIF_SEMAPHORE.acquire(blocking=False):
                        try:
                            with app.app_context():
                                _vsp(aid)
                        finally:
                            _STOCK_VERIF_SEMAPHORE.release()

                import threading as _t
                _t.Thread(target=_run_vsp, args=(almacen_id, _app), daemon=True).start()
            except Exception as _e:
                logger.warning(f'[MOBILE] verificar_stock_picking falló silenciosamente: {_e}')

            # Auto-trigger: si todos los picks de un TRASLADO están completos,
            # avanzar la solicitud → EN_PACKING y crear TareaPacking automáticamente.
            # Nunca afecta el flujo PD (tipo_documento != 'TRASLADO').
            if _tipo_doc == 'TRASLADO' and _ref_doc:
                try:
                    from app.models.traslado import SolicitudTraslado as _ST, EstadoTraslado as _ET
                    _sol = _ST.query.filter_by(codigo=_ref_doc).first()
                    if _sol and _sol.estado == _ET.EN_PICKING:
                        _pendientes = TareaPicking.query.filter(
                            TareaPicking.referencia_documento == _ref_doc,
                            TareaPicking.tipo_documento == 'TRASLADO',
                            TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO']),
                        ).count()
                        if _pendientes == 0:
                            _picks_ok = TareaPicking.query.filter_by(
                                referencia_documento=_ref_doc,
                                tipo_documento='TRASLADO',
                                estado='COMPLETADO',
                            ).all()
                            _recogido = {}
                            for _pk in _picks_ok:
                                _recogido[_pk.producto_id] = (
                                    _recogido.get(_pk.producto_id, 0)
                                    + (_pk.cantidad_recogida or 0)
                                )
                            _items_conf = [
                                {'id': _it.id, 'cantidad_confirmada': _recogido.get(_it.producto_id, 0)}
                                for _it in _sol.items
                            ]
                            from app.services.traslado_service import TrasladoService as _TS
                            _TS.confirmar_picking_traslado(
                                solicitud_id=_sol.id,
                                usuario_id=operario_id,
                                items_confirmados=_items_conf,
                            )
                            logger.info('[MOBILE] %s → EN_PACKING auto-trigger (todos los picks completos)', _ref_doc)
                except Exception as _e_at:
                    logger.error('[MOBILE] auto-trigger confirmar_picking_traslado falló: %s', _e_at, exc_info=True)

            return resultado

        elif tipo == 'PACKING':
            # `usuario_id` — sin esto, esta vía confirmaba el packing de
            # cualquier otro empacador y sin permiso de empaque. La ruta
            # directa sí lo verificaba; ésta no.
            PackingService.confirmar_packing(tarea_id=tarea_id,
                                             usuario_id=operario_id)
            from app.models.packing import TareaPacking
            tarea = TareaPacking.query.get(tarea_id)
            return tarea.to_dict()

        elif tipo == 'CONTEO':
            sesion = SesionConteo.query.get(tarea_id)
            if not sesion:
                raise ValueError('Sesión de conteo no encontrada')
            # cantidad_fisica None significa que no escaneó nada → conteo = 0 (ubicación vacía)
            cantidad = sesion.cantidad_fisica if sesion.cantidad_fisica is not None else 0
            return ConteoService.registrar_conteo(
                sesion_id=tarea_id,
                operario_id=operario_id,
                cantidad_fisica=cantidad
            )

        raise ValueError(f'Tipo desconocido: {tipo}')