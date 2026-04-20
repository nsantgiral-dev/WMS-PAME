"""
Servicio Mobile — API optimizada para tablets y celulares de operarios.
Dispensador automático de tareas — el operario pide trabajo, el sistema asigna.
"""

import logging
from datetime import datetime
from app.extensions import db
from app.models.picking import TareaPicking
from app.models.packing import TareaPacking, ItemPacking
from app.models.conteo import SesionConteo
from app.models.producto import Producto
from app.services.picking_service import PickingService
from app.services.packing_service import PackingService
from app.services.conteo_service import ConteoService

logger = logging.getLogger(__name__)


class MobileService:

    @staticmethod
    def get_tareas_operario(operario_id: int):
        """Todas las tareas activas del operario."""
        pickings = TareaPicking.query.filter(
            TareaPicking.operario_id == operario_id,
            TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        ).order_by(TareaPicking.prioridad.desc()).all()

        packings = TareaPacking.query.filter(
            TareaPacking.empacador_id == operario_id,
            TareaPacking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        ).all()

        conteos = SesionConteo.query.filter(
            SesionConteo.operario_id == operario_id,
            SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO'])
        ).all()

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
        Si no, toma la siguiente de la cola global.
        """
        # Verificar si ya tiene tarea activa
        tarea_activa = TareaPicking.query.filter_by(
            operario_id=operario_id,
            estado='EN_PROCESO'
        ).first()

        if tarea_activa:
            return {
                'id': tarea_activa.id,
                'tipo': 'PICKING',
                'prioridad': tarea_activa.prioridad,
                'ubicacion': tarea_activa.ubicacion.codigo if tarea_activa.ubicacion else '',
                'producto_id': tarea_activa.producto_id,
                'almacen_id': tarea_activa.almacen_id,
                'producto_codigo': tarea_activa.producto.codigo if tarea_activa.producto else '',
                'producto_nombre': tarea_activa.producto.nombre if tarea_activa.producto else '',
                'cantidad_requerida': tarea_activa.cantidad_solicitada,
                'cantidad_escaneada': tarea_activa.cantidad_recogida,
                'estado': tarea_activa.estado,
                'referencia': tarea_activa.referencia_documento,
                'lote': tarea_activa.lote
            }

        # Tomar siguiente tarea de la cola global — más prioritaria y más antigua.
        # REGLA ESTRICTA: el picker solo puede ir a ubicaciones tipo_zona = PICKING o GENERAL.
        # Las zonas RESERVA (pacas selladas en alto) son exclusivas del Abastecedor.
        from app.models.ubicacion import Ubicacion
        tarea = (
            TareaPicking.query
            .join(Ubicacion, Ubicacion.id == TareaPicking.ubicacion_id, isouter=True)
            .filter(
                TareaPicking.estado == 'PENDIENTE',
                TareaPicking.operario_id.is_(None),
                # Permitir PICKING, GENERAL, y tareas sin ubicación asignada todavía
                db.or_(
                    Ubicacion.id.is_(None),
                    Ubicacion.tipo_zona.in_(['PICKING', 'GENERAL']),
                ),
            )
            .order_by(TareaPicking.prioridad.desc(), TareaPicking.fecha_creacion.asc())
            .first()
        )

        if not tarea:
            # Sin picking pendiente — buscar conteo sin asignar
            conteo = SesionConteo.query.filter_by(
                estado='PENDIENTE',
                operario_id=None
            ).order_by(SesionConteo.fecha_creacion.asc()).first()

            if conteo:
                conteo.operario_id = operario_id
                conteo.estado = 'EN_PROCESO'
                conteo.fecha_inicio = datetime.utcnow()
                db.session.commit()
                logger.info(f'[MOBILE] Conteo {conteo.codigo} asignado a operario {operario_id}')
                return MobileService._conteo_a_dict(conteo)

            # Sin picking ni conteo — verificar packing
            resultado = MobileService.get_tareas_operario(operario_id)
            if resultado['tareas']:
                return resultado['tareas'][0]
            return None

        # ── Task Interleaving ────────────────────────────────────────────────────
        # El picker va a la ubicación X. Si hay un conteo pendiente ahí, lo
        # asignamos ahora para que lo haga al terminar el picking — sin viaje extra.
        # Respetar capacidad_diaria_conteo del operario (0 = sin límite).
        conteo_intercalado = None
        if tarea.ubicacion_id:
            from app.models.usuario import Usuario
            op_usuario = Usuario.query.get(operario_id)
            capacidad = (op_usuario.capacidad_diaria_conteo
                         if op_usuario and op_usuario.capacidad_diaria_conteo is not None
                         else 15)

            bajo_tope = True
            if capacidad > 0:
                hoy = datetime.utcnow().date()
                conteos_hoy = SesionConteo.query.filter(
                    SesionConteo.operario_id == operario_id,
                    db.func.date(SesionConteo.fecha_inicio) == hoy
                ).count()
                bajo_tope = conteos_hoy < capacidad
                if not bajo_tope:
                    logger.info(
                        f'[INTERLEAVING] Operario {operario_id} alcanzó tope diario '
                        f'({conteos_hoy}/{capacidad}) — no se inyecta conteo'
                    )

            if bajo_tope:
                conteo_mismo_lugar = SesionConteo.query.filter_by(
                    ubicacion_id=tarea.ubicacion_id,
                    estado='PENDIENTE',
                    operario_id=None
                ).first()
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

        # Asignar picking al operario
        tarea.operario_id = operario_id
        tarea.estado = 'EN_PROCESO'
        tarea.fecha_inicio = datetime.utcnow()
        db.session.commit()

        logger.info(f'[MOBILE] Picking {tarea.codigo} asignado a operario {operario_id}')

        resultado = {
            'id': tarea.id,
            'tipo': 'PICKING',
            'prioridad': tarea.prioridad,
            'ubicacion': tarea.ubicacion.codigo if tarea.ubicacion else '',
            'producto_id': tarea.producto_id,
            'almacen_id': tarea.almacen_id,
            'producto_codigo': tarea.producto.codigo if tarea.producto else '',
            'producto_nombre': tarea.producto.nombre if tarea.producto else '',
            'cantidad_requerida': tarea.cantidad_solicitada,
            'cantidad_escaneada': tarea.cantidad_recogida,
            'estado': tarea.estado,
            'referencia': tarea.referencia_documento,
            'lote': tarea.lote,
            'conteo_intercalado': conteo_intercalado,  # None si no hay conteo en este pasillo
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
            'cantidad_escaneada': 0,
            'estado': c.estado,
            'referencia': c.codigo,
            'maneja_lote': c.maneja_lote,
            'clasificacion_abc': c.clasificacion_abc,
            'conteo_intercalado': None,
        }

    @staticmethod
    def _normalizar(codigo: str) -> str:
        """Limpia ruido del lector (saltos de línea, espacios, mayúsculas)."""
        return str(codigo or '').strip().upper()

    @staticmethod
    def _codigos_validos(producto) -> set:
        """Conjunto de todos los códigos aceptables para un producto."""
        validos = {
            MobileService._normalizar(producto.codigo),
            MobileService._normalizar(producto.codigo_siesa or ''),
            MobileService._normalizar(producto.codigo_barras or ''),
        }
        validos.discard('')
        return validos

    @staticmethod
    def procesar_escaneo(operario_id: int, tarea_id: int,
                         tipo: str, codigo: str, cantidad: int = 1,
                         lpn_codigo: str = None):
        """
        Procesa un escaneo — funciona con cámara o láser Bluetooth.
        Valida que el código escaneado corresponde al producto de la tarea.
        Acepta: referencia interna, código Siesa, o código de barras EAN.

        lpn_codigo: si se provee y la tarea es de TRASLADO, vincula el LPN
                    automáticamente al traslado y lo marca EN_TRANSITO.
        """
        codigo_limpio = MobileService._normalizar(codigo)

        if tipo == 'PICKING':
            tarea = TareaPicking.query.get(tarea_id)
            if not tarea:
                raise ValueError('Tarea no encontrada')

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

            nueva_cantidad = tarea.cantidad_recogida + cantidad
            if nueva_cantidad > tarea.cantidad_solicitada:
                raise ValueError({
                    'tipo': 'EXCESO',
                    'mensaje': f'Ya tienes {tarea.cantidad_recogida} de {tarea.cantidad_solicitada}',
                    'cantidad_actual': tarea.cantidad_recogida,
                    'cantidad_maxima': tarea.cantidad_solicitada
                })

            tarea.cantidad_recogida = nueva_cantidad
            if tarea.estado == 'PENDIENTE':
                tarea.estado = 'EN_PROCESO'
                tarea.operario_id = operario_id
                tarea.fecha_inicio = datetime.utcnow()

            # ── Vincular LPN al traslado si aplica ───────────────────────────
            # Cuando el operario escanea un LPN durante el picking de un traslado,
            # lo marcamos EN_TRANSITO y lo asociamos a la solicitud. Esto permite
            # rastrear qué pacas físicas van en cada envío.
            if lpn_codigo and tarea.tipo_documento == 'TRASLADO' and tarea.referencia_documento:
                try:
                    from app.models.lpn import LPN
                    from app.models.traslado import SolicitudTraslado
                    lpn = LPN.query.filter_by(codigo=lpn_codigo, estado='ACTIVO').first()
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

            db.session.commit()
            completado = nueva_cantidad >= tarea.cantidad_solicitada

            return {
                'exito': True,
                'tipo': 'PICKING',
                'codigo_escaneado': codigo,
                'cantidad_actual': nueva_cantidad,
                'cantidad_requerida': tarea.cantidad_solicitada,
                'completado': completado,
                'puede_confirmar': completado,
                'mensaje': '¡Completo! Presiona confirmar' if completado else f'{nueva_cantidad} de {tarea.cantidad_solicitada}'
            }

        elif tipo == 'PACKING':
            tarea = TareaPacking.query.get(tarea_id)
            if not tarea:
                raise ValueError('Tarea no encontrada')

            producto = (
                Producto.query.filter(db.func.upper(Producto.codigo) == codigo_limpio).first() or
                Producto.query.filter(db.func.upper(Producto.codigo_siesa) == codigo_limpio).first() or
                Producto.query.filter(db.func.upper(Producto.codigo_barras) == codigo_limpio).first()
            )
            if not producto:
                raise ValueError(f'Producto {codigo_limpio} no encontrado')

            item = ItemPacking.query.filter_by(
                tarea_id=tarea_id,
                producto_id=producto.id
            ).first()

            if not item:
                raise ValueError(f'Producto {codigo} no pertenece a este pedido')

            item.cantidad_real += cantidad
            item.verificado = item.cantidad_real >= item.cantidad_esperada
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
            sesion = SesionConteo.query.get(tarea_id)
            if not sesion:
                raise ValueError('Sesión de conteo no encontrada')

            producto = sesion.producto
            if codigo_limpio not in MobileService._codigos_validos(producto):
                raise ValueError(f'Producto incorrecto — escanea {producto.codigo}')

            if not sesion.cantidad_fisica:
                sesion.cantidad_fisica = 0
            sesion.cantidad_fisica += cantidad
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
            resultado = PickingService.confirmar_picking(
                tarea_id=tarea_id,
                cantidad_recogida=cantidad,
                usuario_id=operario_id
            ).to_dict()
            # Disparar verificación de stock en background — puede generar TareaReposicion
            try:
                import threading as _t
                from app.services.reposicion_service import verificar_stock_picking as _vsp
                _t.Thread(target=_vsp, args=(almacen_id,), daemon=True).start()
            except Exception as _e:
                logger.warning(f'[MOBILE] verificar_stock_picking falló silenciosamente: {_e}')
            return resultado

        elif tipo == 'PACKING':
            resultado = PackingService.confirmar_packing(tarea_id=tarea_id)
            return resultado.to_dict()

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