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
        s = SolicitudTraslado.query.get_or_404(solicitud_id)
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
        s = SolicitudTraslado.query.get_or_404(solicitud_id)
        if s.estado not in ('PREPARADO', 'EN_PICKING'):
            raise ValueError(f'No se puede despachar en estado {s.estado}')

        # Usar cantidad_enviada si fue confirmada por picking; si no, caer a aprobada.
        # Esto permite que el picking parcial (menos ítems de los aprobados) sea correcto.
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
            items_payload.append({
                'codigo_siesa': item.producto_codigo_siesa,
                'codigo': item.producto.codigo if item.producto else '',
                'cantidad': cantidad,
                'unidad_medida': item.producto.unidad_medida if item.producto else '',
                'unidad_negocio_id': item.producto.unidad_negocio_id if item.producto else '',
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
        s = SolicitudTraslado.query.get_or_404(solicitud_id)
        if s.estado not in ('EN_TRANSITO', 'DESPACHADA'):
            raise ValueError(f'No se puede confirmar recepción en estado {s.estado}')

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
            items_payload = [
                {
                    'codigo_siesa': item.producto_codigo_siesa,
                    'codigo': item.producto.codigo if item.producto else '',
                    'cantidad': item.cantidad_recibida,
                    'unidad_medida': item.producto.unidad_medida if item.producto else '',
                    'unidad_negocio_id': item.producto.unidad_negocio_id if item.producto else '',
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

        s.estado = 'ENTREGADA'
        s.fecha_entrega = datetime.utcnow()
        db.session.commit()
        logger.info(f'[TRASLADO] {s.codigo} → ENTREGADA (confirmado por usuario {usuario_id})')
        return s

    @staticmethod
    def _crear_picking_tasks(solicitud: SolicitudTraslado):
        """
        Registra la solicitud como tarea de picking visible.
        TareaPicking requiere ubicacion_id (nullable=False) — por ahora el estado
        EN_PICKING de la solicitud es suficiente para que el jefe de almacén
        dirija al equipo. En una versión posterior se asignará la ubicación
        una vez el inventario WMS tenga las posiciones mapeadas.
        """
        logger.info(f'[TRASLADO] Solicitud {solicitud.codigo} en cola de picking — '
                    f'{len(solicitud.items)} ítems por recoger')

    @staticmethod
    def _descontar_inventario_wms(solicitud: SolicitudTraslado):
        """
        Descuenta las cantidades despachadas de ubicaciones_productos y registra
        un MovimientoInventario de tipo SALIDA_TRASLADO.
        Solo opera sobre la bodega origen (NB1 en WMS); la bodega destino es un
        punto de venta externo no gestionado por este WMS.
        """
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

    @staticmethod
    def get_bodegas_disponibles():
        """Lista las bodegas de Siesa para seleccionar punto de venta destino."""
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
            return {'bodegas': bodegas}
        except Exception as e:
            logger.error(f'[TRASLADO] Error bodegas Siesa: {e}')
            raise
