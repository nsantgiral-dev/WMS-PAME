"""
TrasladoService — Orquesta el flujo completo de traslados entre bodega principal
y puntos de venta. Máquina de estados + Siesa triggers.

Flujo normal (EN_TRANSITO):
  1. Tienda crea solicitud (BORRADOR) y envía (ENVIADA)
  2. Admin bodega aprueba → fire 174646 (requisición Siesa) → tareas picking
  3. Empacador sella cajas → despacho → fire 173076 (salida tránsito)
  4. Conductor carga, llega a tienda
  5. Admin tienda confirma recepción → fire 173079 (entrada tránsito)

Flujo contingencia (DIRECTA — sin bodega tránsito en Siesa):
  Pasos 1-2 igual. En paso 3 → fire 173066 (transferencia directa). No hay paso 5.
"""
import uuid
import logging
from datetime import datetime
from app.extensions import db
from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
from app.models.producto import Producto
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.almacen import Almacen
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

# Bodega origen por defecto (bodega principal WMS)
BODEGA_ORIGEN_DEFAULT = connekta.bodega  # NB1


class TrasladoService:

    @staticmethod
    def _codigo_solicitud():
        hoy = datetime.utcnow().strftime('%Y%m%d')
        uid = str(uuid.uuid4())[:4].upper()
        return f'ST-{hoy}-{uid}'

    @staticmethod
    def crear_solicitud(solicitante_id: int, bodega_destino: str,
                        nombre_punto_venta: str, items: list,
                        observaciones: str = None) -> SolicitudTraslado:
        """
        Tienda arma el carrito y crea la solicitud en BORRADOR.
        items: [{producto_id, cantidad_solicitada}]
        """
        if not items:
            raise ValueError('La solicitud debe tener al menos un ítem')

        solicitud = SolicitudTraslado(
            codigo=TrasladoService._codigo_solicitud(),
            bodega_origen_siesa=BODEGA_ORIGEN_DEFAULT,
            bodega_destino_siesa=bodega_destino,
            nombre_punto_venta=nombre_punto_venta,
            estado='BORRADOR',
            modo_transferencia='EN_TRANSITO' if connekta.bodega_transito else 'DIRECTA',
            bodega_transito_siesa=connekta.bodega_transito or None,
            solicitante_id=solicitante_id,
            observaciones=observaciones
        )
        db.session.add(solicitud)
        db.session.flush()

        for item_data in items:
            producto = Producto.query.get(item_data['producto_id'])
            if not producto:
                raise ValueError(f"Producto {item_data['producto_id']} no encontrado")
            item = ItemSolicitudTraslado(
                solicitud_id=solicitud.id,
                producto_id=producto.id,
                producto_codigo_siesa=producto.codigo_siesa or producto.codigo,
                cantidad_solicitada=item_data['cantidad_solicitada'],
                disponible_siesa=item_data.get('disponible_siesa'),
            )
            db.session.add(item)

        db.session.commit()
        logger.info(f'[TRASLADO] Solicitud {solicitud.codigo} creada por usuario {solicitante_id}')
        return solicitud

    @staticmethod
    def enviar_solicitud(solicitud_id: int) -> SolicitudTraslado:
        """Tienda confirma y envía al admin bodega."""
        s = SolicitudTraslado.query.get_or_404(solicitud_id)
        if s.estado != 'BORRADOR':
            raise ValueError(f'Solo se puede enviar una solicitud en BORRADOR (estado actual: {s.estado})')
        if not s.items:
            raise ValueError('No se puede enviar una solicitud sin ítems')

        s.estado = 'ENVIADA'
        s.fecha_envio = datetime.utcnow()
        db.session.commit()
        logger.info(f'[TRASLADO] {s.codigo} → ENVIADA')
        return s

    @staticmethod
    def aprobar_solicitud(solicitud_id: int, aprobador_id: int,
                          items_aprobados: list = None,
                          operario_id: int = None) -> SolicitudTraslado:
        """
        Admin bodega aprueba (puede ajustar cantidades) y asigna operario.
        items_aprobados: [{id, cantidad_aprobada}] — si None, aprueba cantidades solicitadas.
        operario_id: usuario con rol operario que irá a recoger los ítems.
        """
        s = SolicitudTraslado.query.filter_by(id=solicitud_id).with_for_update().first()
        if not s:
            from flask import abort
            abort(404)
        if s.estado not in ('ENVIADA',):
            raise ValueError(f'Solo se puede aprobar una solicitud ENVIADA (estado: {s.estado})')

        # Actualizar cantidades aprobadas
        if items_aprobados:
            aprobados_map = {i['id']: i['cantidad_aprobada'] for i in items_aprobados}
            for item in s.items:
                item.cantidad_aprobada = aprobados_map.get(item.id, item.cantidad_solicitada)
        else:
            for item in s.items:
                item.cantidad_aprobada = item.cantidad_solicitada

        s.aprobador_id = aprobador_id
        s.operario_id = operario_id
        s.fecha_aprobacion = datetime.utcnow()
        db.session.flush()

        # ── Validación: todos los ítems deben tener unidad_negocio_id ──
        sin_unidad = [
            item.producto_codigo_siesa
            for item in s.items
            if item.cantidad_aprobada and item.cantidad_aprobada > 0
            and item.producto and not (item.producto.unidad_negocio_id or '').strip()
        ]
        if sin_unidad:
            raise ValueError(
                f'Productos sin Unidad de Negocio configurada: {", ".join(sin_unidad)}. '
                f'Configura el mapeo en /api/config/mapeo-unidades y vuelve a aprobar.'
            )

        # 174646 (Requisición de traslado) eliminado del flujo.
        # El WMS es el único libro de estado (EN_PICKING → PREPARADO → EN_TRANSITO).
        # Siesa solo recibe los movimientos reales: 173076 al despachar y 173079 al recibir.
        s.siesa_error = None
        s.estado = 'EN_PICKING'
        db.session.flush()  # necesario antes de crear tareas (s.id ya existe)

        # ── Reserva dura FEFO: crea TareaPicking prioridad 10 ──
        # Bloquea el stock en PostgreSQL. Pedidos de venta no pueden "robar" estas unidades.
        # Si no hay stock en WMS (arranque sin inventario mapeado), degradar a picking manual.
        tareas_sin_stock = TrasladoService._crear_picking_tasks(s)
        if tareas_sin_stock:
            logger.warning(
                f'[TRASLADO] {s.codigo}: {len(tareas_sin_stock)} ítems sin stock WMS '
                f'({tareas_sin_stock}) — picking manual requerido para esos ítems'
            )

        db.session.commit()
        logger.info(f'[TRASLADO] {s.codigo} → EN_PICKING (aprobado por {aprobador_id}, operario {operario_id})')
        return s

    @staticmethod
    def rechazar_solicitud(solicitud_id: int, aprobador_id: int,
                           motivo: str) -> SolicitudTraslado:
        """Admin rechaza la solicitud."""
        s = SolicitudTraslado.query.get_or_404(solicitud_id)
        if s.estado not in ('ENVIADA',):
            raise ValueError(f'Solo se puede rechazar una solicitud ENVIADA (estado: {s.estado})')

        s.estado = 'RECHAZADA'
        s.aprobador_id = aprobador_id
        s.motivo_rechazo = motivo
        db.session.commit()
        logger.info(f'[TRASLADO] {s.codigo} → RECHAZADA por {aprobador_id}: {motivo}')
        return s

    @staticmethod
    def despachar(solicitud_id: int) -> SolicitudTraslado:
        """
        Empacador selló las cajas. Dispara 173076 (tránsito) o 173066 (directa).
        La solicitud puede estar EN_PICKING o APROBADA (si picking manual).
        """
        s = SolicitudTraslado.query.filter_by(id=solicitud_id).with_for_update().first()
        if not s:
            from flask import abort
            abort(404)
        if s.estado not in ('PREPARADO', 'EN_PICKING'):
            raise ValueError(f'No se puede despachar en estado {s.estado}')
        # Guard idempotencia: si Siesa ya procesó este despacho (siesa_salida_consec asignado)
        # y el estado ya avanzó, evitar llamar Siesa de nuevo (duplicación de documento)
        if s.siesa_salida_consec and s.estado in ('EN_TRANSITO', 'ENTREGADA'):
            raise ValueError(
                f'Despacho ya registrado en Siesa (consec={s.siesa_salida_consec}). '
                f'Si necesitas reintentar, usa el endpoint de reintento.'
            )

        # Usar cantidad_enviada si fue confirmada por picking; si no, caer a aprobada.
        # Esto permite que el picking parcial (menos ítems de los aprobados) sea correcto.

        # Pre-cargar productos para evitar N+1
        prod_ids = [item.producto_id for item in s.items if item.producto_id]
        prods_map = {p.id: p for p in Producto.query.filter(Producto.id.in_(prod_ids)).all()} if prod_ids else {}

        items_payload = []
        for item in s.items:
            # cantidad_enviada=None → picking no corrió, usar fallback.
            # cantidad_enviada=0   → operario confirmó 0 unidades, saltar el ítem.
            if item.cantidad_enviada is not None:
                cantidad = item.cantidad_enviada
            else:
                cantidad = item.cantidad_aprobada or item.cantidad_solicitada
                item.cantidad_enviada = cantidad  # fijar para que recepción tenga referencia exacta
            if not cantidad or cantidad <= 0:
                continue
            prod = prods_map.get(item.producto_id)
            items_payload.append({
                'codigo_siesa': item.producto_codigo_siesa,
                'codigo': prod.codigo if prod else '',
                'cantidad': cantidad,
                'unidad_medida': prod.unidad_medida if prod else '',
                'unidad_negocio_id': prod.unidad_negocio_id if prod else '',
            })

        if not items_payload:
            raise ValueError('No hay ítems con cantidad para despachar')

        try:
            if s.modo_transferencia == 'EN_TRANSITO':
                bodega_transito = s.bodega_transito_siesa or connekta.bodega_transito
                if not bodega_transito:
                    raise ValueError('SIESA_BODEGA_TRANSITO no configurada — usar modo DIRECTA')
                res = connekta.transferencia_transito_salida(
                    bodega_origen=s.bodega_origen_siesa,
                    bodega_transito=bodega_transito,
                    items=items_payload,
                    codigo_solicitud=s.codigo,
                    consec_requisicion=s.siesa_requisicion_consec
                )
            else:
                res = connekta.transferencia_directa(
                    bodega_origen=s.bodega_origen_siesa,
                    bodega_destino=s.bodega_destino_siesa,
                    items=items_payload,
                    codigo_solicitud=s.codigo
                )

            if not res.get('simulado') and not res.get('modo_ensayo'):
                consec = TrasladoService._extraer_consec(res)
                if consec:
                    s.siesa_salida_consec = consec
            s.siesa_error = None
        except Exception as e:
            s.siesa_error = f'Despacho Siesa: {str(e)}'
            logger.error(f'[TRASLADO] Error despacho Siesa {s.codigo}: {e}')

        nuevo_estado = 'EN_TRANSITO' if s.modo_transferencia == 'EN_TRANSITO' else 'ENTREGADA'
        s.estado = nuevo_estado
        s.fecha_despacho = datetime.utcnow()
        if nuevo_estado == 'ENTREGADA':
            s.fecha_entrega = datetime.utcnow()

        # ── Descontar inventario WMS ──
        # Se descuenta al despachar (los bienes salen físicamente de la bodega).
        # Se hace independientemente del resultado de Siesa — el camión ya salió.
        TrasladoService._descontar_inventario_wms(s)

        # ── Confirmar LPNs → EN_TRANSITO ──────────────────────────────────────
        # Los LPNs que el operario vinculó durante el picking ya deberían estar
        # EN_TRANSITO; esto cubre el caso de LPNs que quedaron en ACTIVO por
        # algún fallo intermedio.
        try:
            from app.models.lpn import LPN
            LPN.query.filter_by(traslado_id=s.id, estado='ACTIVO').update(
                {'estado': 'EN_TRANSITO'}, synchronize_session=False
            )
        except Exception as e_lpn:
            logger.warning(f'[TRASLADO] Error confirmando LPNs EN_TRANSITO para {s.codigo}: {e_lpn}')

        db.session.commit()
        logger.info(f'[TRASLADO] {s.codigo} → {nuevo_estado}')
        return s

    @staticmethod
    def confirmar_recepcion(solicitud_id: int, usuario_id: int,
                             items_recibidos: list = None) -> SolicitudTraslado:
        """
        Admin tienda confirma recepción física. Dispara 173079 (entrada tránsito).
        items_recibidos: [{id, cantidad_recibida}] — si None, confirma cantidades despachadas.
        """
        s = SolicitudTraslado.query.filter_by(id=solicitud_id).with_for_update().first()
        if not s:
            from flask import abort
            abort(404)
        if s.estado not in ('EN_TRANSITO', 'DESPACHADA'):
            raise ValueError(f'No se puede confirmar recepción en estado {s.estado}')
        # Guard idempotencia: 173079 ya se envió exitosamente — no duplicar
        if s.siesa_entrada_consec and s.estado == 'ENTREGADA':
            raise ValueError(
                f'Recepción ya registrada en Siesa (consec={s.siesa_entrada_consec}). '
                f'El traslado ya fue confirmado.'
            )

        # Actualizar cantidades recibidas.
        # Fallback: cantidad_enviada (lo que salió) > cantidad_aprobada > solicitada.
        if items_recibidos:
            recibidos_map = {i['id']: i['cantidad_recibida'] for i in items_recibidos}
            for item in s.items:
                fallback = item.cantidad_enviada or item.cantidad_aprobada or item.cantidad_solicitada
                item.cantidad_recibida = recibidos_map.get(item.id, fallback)
        else:
            for item in s.items:
                item.cantidad_recibida = item.cantidad_enviada or item.cantidad_aprobada or item.cantidad_solicitada
        db.session.flush()

        # ── Trigger Siesa: Entrada tránsito ──
        if s.modo_transferencia == 'EN_TRANSITO':
            # Pre-cargar productos para evitar N+1
            _prod_ids = [i.producto_id for i in s.items if i.producto_id]
            _prods = {p.id: p for p in Producto.query.filter(Producto.id.in_(_prod_ids)).all()} if _prod_ids else {}
            items_payload = [
                {
                    'codigo_siesa': item.producto_codigo_siesa,
                    'codigo': _prods[item.producto_id].codigo if item.producto_id in _prods else '',
                    'cantidad': item.cantidad_recibida,
                    'unidad_medida': _prods[item.producto_id].unidad_medida if item.producto_id in _prods else '',
                    'unidad_negocio_id': _prods[item.producto_id].unidad_negocio_id if item.producto_id in _prods else '',
                }
                for item in s.items
            ]
            try:
                bodega_transito = s.bodega_transito_siesa or connekta.bodega_transito
                res = connekta.transferencia_transito_entrada(
                    bodega_transito=bodega_transito,
                    bodega_destino=s.bodega_destino_siesa,
                    items=items_payload,
                    codigo_solicitud=s.codigo,
                    consec_salida=s.siesa_salida_consec
                )
                if not res.get('simulado') and not res.get('modo_ensayo'):
                    consec = TrasladoService._extraer_consec(res)
                    if consec:
                        s.siesa_entrada_consec = consec
                s.siesa_error = None
            except Exception as e:
                s.siesa_error = f'173079: {str(e)}'
                logger.error(f'[TRASLADO] Error entrada Siesa {s.codigo}: {e}')

        # ── Consumir LPNs que llegaron ─────────────────────────────────────────
        # Las pacas físicas viajaron y se recibieron en el punto de venta.
        # Se marcan CONSUMIDO porque al ingresar al PV se abren (no tienen LPN propio allá).
        try:
            from app.models.lpn import LPN
            now_utc = datetime.utcnow()
            for lpn in LPN.query.filter_by(traslado_id=s.id, estado='EN_TRANSITO').all():
                lpn.estado = 'CONSUMIDO'
                lpn.fecha_consumo = now_utc
        except Exception as e_lpn:
            logger.warning(f'[TRASLADO] Error consumiendo LPNs para {s.codigo}: {e_lpn}')

        s.estado = 'ENTREGADA'
        s.fecha_entrega = datetime.utcnow()
        db.session.commit()
        logger.info(f'[TRASLADO] {s.codigo} → ENTREGADA (confirmado por usuario {usuario_id})')
        return s

    @staticmethod
    def _crear_picking_tasks(solicitud: SolicitudTraslado) -> list:
        """
        Crea TareaPicking (prioridad=10) con FEFO para cada ítem aprobado.
        Bloquea el stock en UbicacionProducto.reservado para que pedidos de venta
        no puedan tomar esas unidades mientras el traslado está en picking.

        Retorna lista de códigos_siesa que NO tuvieron stock suficiente en WMS
        (degradación graceful — esos ítems requieren picking manual).
        """
        from app.services.picking_service import PickingService

        almacen = Almacen.query.filter_by(
            bodega_siesa_id=solicitud.bodega_origen_siesa
        ).first()
        if not almacen:
            logger.warning(
                f'[TRASLADO] No hay almacen WMS para bodega {solicitud.bodega_origen_siesa} '
                f'— picking manual para toda la solicitud {solicitud.codigo}'
            )
            return [item.producto_codigo_siesa for item in solicitud.items]

        sin_stock = []
        for item in solicitud.items:
            cantidad = item.cantidad_aprobada or item.cantidad_solicitada
            if not cantidad or cantidad <= 0 or not item.producto_id:
                continue
            try:
                PickingService.crear_tareas(
                    producto_id=item.producto_id,
                    cantidad=cantidad,
                    almacen_id=almacen.id,
                    referencia_documento=solicitud.codigo,
                    tipo_documento='TRASLADO',
                    operario_id=solicitud.operario_id,
                    prioridad=10,
                )
            except ValueError as e:
                # Stock insuficiente en WMS para este ítem — picking manual
                sin_stock.append(item.producto_codigo_siesa)
                logger.warning(
                    f'[TRASLADO] Sin stock WMS para {item.producto_codigo_siesa} '
                    f'en {solicitud.codigo}: {e}'
                )
        return sin_stock

    @staticmethod
    def _liberar_reservas_traslado(solicitud: SolicitudTraslado):
        """
        Cancela las TareaPicking pendientes de un traslado y libera el campo
        reservado en UbicacionProducto. Se llama al cancelar/rechazar en EN_PICKING.
        """
        from app.models.picking import TareaPicking

        tareas = TareaPicking.query.filter_by(
            referencia_documento=solicitud.codigo,
            tipo_documento='TRASLADO',
        ).filter(TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO'])).all()

        for t in tareas:
            reg = UbicacionProducto.query.filter_by(
                ubicacion_id=t.ubicacion_id,
                producto_id=t.producto_id,
            ).with_for_update().first()
            if reg:
                reg.reservado = max(0, reg.reservado - t.cantidad_solicitada)
            t.estado = 'CANCELADO'
            logger.info(f'[TRASLADO] Tarea {t.codigo} cancelada por cancelación de {solicitud.codigo}')

    @staticmethod
    def _descontar_inventario_wms(solicitud: SolicitudTraslado):
        """
        Descuenta las cantidades despachadas de ubicaciones_productos y registra
        un MovimientoInventario de tipo SALIDA_TRASLADO.
        Solo opera sobre la bodega origen (NB1 en WMS); la bodega destino es un
        punto de venta externo no gestionado por este WMS.

        Si existe TareaPicking para este traslado, PickingService.confirmar_picking
        ya decrementó 'cantidad' al confirmar cada ítem — solo se cancelan las
        tareas pendientes (liberando reservas) y se retorna para evitar doble descuento.
        """
        from app.models.picking import TareaPicking

        # ── Anti-double-decrement: detectar si picking ya corrió ──
        tareas_traslado = TareaPicking.query.filter_by(
            referencia_documento=solicitud.codigo,
            tipo_documento='TRASLADO',
        ).all()

        if tareas_traslado:
            # Picking formal creó tareas. PickingService.confirmar_picking ya
            # decrementó 'cantidad'. Solo cancelar las que quedaron pendientes
            # (items no recogidos — el operario no llegó, o picking parcial).
            for t in tareas_traslado:
                if t.estado in ('PENDIENTE', 'EN_PROCESO'):
                    reg = UbicacionProducto.query.filter_by(
                        ubicacion_id=t.ubicacion_id,
                        producto_id=t.producto_id,
                    ).with_for_update().first()
                    if reg:
                        reg.reservado = max(0, reg.reservado - t.cantidad_solicitada)
                    t.estado = 'CANCELADO'
            logger.info(
                f'[TRASLADO] {solicitud.codigo}: stock descontado vía picking formal '
                f'({len(tareas_traslado)} tareas). _descontar_inventario_wms omitido.'
            )
            return

        # ── Fallback: no hubo picking formal → descontar manualmente ──
        almacen = Almacen.query.filter_by(
            bodega_siesa_id=solicitud.bodega_origen_siesa
        ).first()
        if not almacen:
            logger.warning(
                f'[TRASLADO] No se encontró almacen WMS para bodega {solicitud.bodega_origen_siesa} '
                f'— inventario WMS no descontado para {solicitud.codigo}'
            )
            return

        for item in solicitud.items:
            # Usar la cantidad que el operario confirma haber enviado.
            # Si picking no corrió (despacho directo), caer a cantidad_aprobada → solicitada.
            cantidad = item.cantidad_enviada or item.cantidad_aprobada or item.cantidad_solicitada
            if not cantidad or cantidad <= 0 or not item.producto_id:
                continue

            restante = cantidad
            ubicaciones = (
                UbicacionProducto.query
                .filter_by(producto_id=item.producto_id)
                .filter(UbicacionProducto.cantidad > 0)
                .order_by(UbicacionProducto.cantidad.asc())  # FIFO: vaciar las más pequeñas primero
                .with_for_update()
                .all()
            )
            saldo_antes = sum(u.cantidad for u in ubicaciones)

            for ub in ubicaciones:
                if restante <= 0:
                    break
                descuento = min(ub.cantidad, restante)
                ub.cantidad -= descuento
                restante -= descuento

            saldo_despues = saldo_antes - (cantidad - restante)
            mov = MovimientoInventario(
                producto_id=item.producto_id,
                almacen_id=almacen.id,
                tipo='SALIDA_TRASLADO',
                cantidad=-(cantidad - restante),
                saldo_antes=saldo_antes,
                saldo_despues=saldo_despues,
                motivo=f'Traslado {solicitud.codigo} → {solicitud.nombre_punto_venta}',
                numero_documento=solicitud.codigo,
                siesa_sync='OMITIDO',  # Siesa lo maneja por su cuenta con 173066/173076
            )
            db.session.add(mov)

            if restante > 0:
                logger.warning(
                    f'[TRASLADO] Stock WMS insuficiente para {item.producto_codigo_siesa}: '
                    f'pedido {cantidad}, disponible {cantidad - restante}'
                )

    @staticmethod
    def _extraer_consec(respuesta_siesa: dict) -> int | None:
        """Intenta extraer el consecutivo del documento creado en Siesa."""
        try:
            detalle = respuesta_siesa.get('detalle', {})
            if isinstance(detalle, dict):
                tabla = detalle.get('Table', [])
                if tabla and isinstance(tabla, list) and len(tabla) > 0:
                    row = tabla[0]
                    for key in ['f350_consec_docto', 'consec_docto', 'consecutivo']:
                        if key in row:
                            return int(row[key])
        except Exception:
            pass
        return None

    @staticmethod
    def get_stock_disponible(bodega_id: str = None):
        """
        Consulta stock disponible en el WMS local (PostgreSQL) para que la tienda
        sepa qué puede pedir. No llama a Siesa — usa el inventario del WMS que se
        sincroniza periódicamente. Respuesta inmediata sin dependencia de red.
        """
        from app.models.producto import Producto
        from app.models.inventario import UbicacionProducto
        from app.models.almacen import Almacen
        from app.models.ubicacion import Ubicacion

        bod = bodega_id or BODEGA_ORIGEN_DEFAULT
        almacen = Almacen.query.filter_by(bodega_siesa_id=bod).first()
        if not almacen:
            logger.warning(f'[TRASLADO] stock_disponible: no hay almacén WMS para bodega {bod}')
            return {'items': [], 'bodega': bod, 'total': 0, 'fuente': 'wms'}

        # 2 queries planas — sin N+1
        # Query 1: stock agrupado por producto en este almacén
        # UbicacionProducto no tiene almacen_id directo — se llega vía JOIN con Ubicacion
        registros = (
            db.session.query(
                UbicacionProducto.producto_id,
                db.func.sum(UbicacionProducto.cantidad).label('existencia'),
                db.func.sum(UbicacionProducto.reservado).label('reservado'),
            )
            .join(Ubicacion, UbicacionProducto.ubicacion_id == Ubicacion.id)
            .filter(
                Ubicacion.almacen_id == almacen.id,
                UbicacionProducto.cantidad > 0,
            )
            .group_by(UbicacionProducto.producto_id)
            .all()
        )

        if not registros:
            return {'items': [], 'bodega': bod, 'total': 0, 'fuente': 'wms'}

        # Query 2: todos los productos en un solo IN (no 1 query por producto)
        producto_ids = [r.producto_id for r in registros]
        productos = {
            p.id: p
            for p in Producto.query.filter(
                Producto.id.in_(producto_ids),
                Producto.activo == True,
            ).all()
        }

        items = []
        for reg in registros:
            prod = productos.get(reg.producto_id)
            if not prod:
                continue
            disponible = int((reg.existencia or 0) - (reg.reservado or 0))
            if disponible <= 0:
                continue
            items.append({
                'codigo_siesa': prod.codigo_siesa or prod.codigo,
                'nombre': prod.nombre,
                'producto_id': prod.id,
                'disponible': disponible,
                'existencia': int(reg.existencia or 0),
                'unidad_medida': prod.unidad_medida,
            })

        items.sort(key=lambda x: x['nombre'])
        return {'items': items, 'bodega': bod, 'total': len(items), 'fuente': 'wms'}

    # Cache de bodegas — proceso-nivel, TTL 1 hora. Evita llamar a Siesa en cada request.
    _bodegas_cache: dict = {'data': None, 'ts': 0.0}
    _BODEGAS_TTL = 3600.0

    @staticmethod
    def get_bodegas_disponibles(forzar_refresh: bool = False):
        """
        Lista las bodegas de Siesa para seleccionar punto de venta destino.
        Cachea el resultado 1 hora en memoria para no saturar el Gateway de Connekta.
        """
        import time
        cache = TrasladoService._bodegas_cache
        ahora = time.time()

        if (not forzar_refresh
                and cache['data'] is not None
                and (ahora - cache['ts']) < TrasladoService._BODEGAS_TTL):
            return cache['data']

        try:
            res = connekta.get_bodegas_siesa()
            if res.get('simulado'):
                return {'simulado': True, 'bodegas': []}
            rows = res.get('detalle', {}).get('Table', [])
            bodegas = [
                {
                    'id': row.get('f150_id', '').strip(),
                    'nombre': row.get('f150_nombre', '').strip(),
                    'activo': row.get('f150_ind_estado', 1) == 1,
                }
                for row in rows if row.get('f150_id')
            ]
            resultado = {'bodegas': bodegas}
            cache['data'] = resultado
            cache['ts'] = ahora
            return resultado
        except Exception as e:
            logger.error(f'[TRASLADO] Error bodegas Siesa: {e}')
            # Si hay cache viejo, devolver en lugar de fallar
            if cache['data']:
                logger.warning('[TRASLADO] Usando cache de bodegas expirado como fallback')
                return cache['data']
            raise
