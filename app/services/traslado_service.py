"""
TrasladoService — Orquesta el flujo completo de traslados entre bodega principal
y puntos de venta. Máquina de estados + Siesa triggers.

Flujo normal (EN_TRANSITO):
  1. Tienda crea solicitud (BORRADOR) y envía (ENVIADA)
  2. Admin bodega aprueba → tareas picking (sin RIT aún)
  2b. Picker confirma → fire 174646 RIT con ubicaciones reales → tarea packing
  3. Empacador sella cajas → despacho → fire 174930 (STS desde RIT)
  4. Conductor carga, llega a tienda
  5. Admin tienda confirma recepción → fire 173079 (entrada tránsito)

Flujo contingencia (DIRECTA — sin bodega tránsito en Siesa):
  Pasos 1-2 igual. En paso 3 → fire 173066 (transferencia directa). No hay paso 5.
"""
import uuid
import logging
from datetime import datetime
from app.extensions import db
from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado, EstadoTraslado
from app.models.producto import Producto
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.almacen import Almacen
from app.services.connekta_gateway import connekta
from app.services.siesa_traslado_adapter import siesa_traslado

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
                        bodega_origen: str = None,
                        observaciones: str = None) -> SolicitudTraslado:
        """
        Tienda arma el carrito y crea la solicitud en BORRADOR.
        items: [{producto_id, cantidad_solicitada}]
        bodega_origen: bodega fuente del traslado (default NB1)
        """
        if not items:
            raise ValueError('La solicitud debe tener al menos un ítem')

        solicitud = SolicitudTraslado(
            codigo=TrasladoService._codigo_solicitud(),
            bodega_origen_siesa=bodega_origen or BODEGA_ORIGEN_DEFAULT,
            bodega_destino_siesa=bodega_destino,
            nombre_punto_venta=nombre_punto_venta,
            estado=EstadoTraslado.BORRADOR,
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
        if s.estado != EstadoTraslado.BORRADOR:
            raise ValueError(f'Solo se puede enviar una solicitud en BORRADOR (estado actual: {s.estado})')
        if not s.items:
            raise ValueError('No se puede enviar una solicitud sin ítems')

        s.estado = EstadoTraslado.ENVIADA
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
        if s.estado not in (EstadoTraslado.ENVIADA,):
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

        s.siesa_error = None
        s.estado = EstadoTraslado.EN_PICKING
        db.session.flush()  # necesario antes de crear tareas (s.id ya existe)

        # ── Reserva dura FEFO: crea TareaPicking prioridad 10 ──
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
    def confirmar_picking_traslado(solicitud_id: int, usuario_id: int,
                                   items_confirmados: list = None) -> SolicitudTraslado:
        """
        Operario terminó el picking. Actualiza cantidades reales, dispara RIT 174646
        con ubicaciones reales y transiciona EN_PICKING → EN_PACKING.
        174720 (Compromisos) se dispara en confirmar_packing.
        items_confirmados: [{'id': item_id, 'cantidad_confirmada': N}]
        """
        from app.models.picking import TareaPicking, EstadoPicking as _EP

        s = SolicitudTraslado.query.filter_by(id=solicitud_id).with_for_update().first()
        if not s:
            from flask import abort; abort(404)
        if s.estado != EstadoTraslado.EN_PICKING:
            raise ValueError(f'Solo se puede confirmar picking en EN_PICKING (estado: {s.estado})')

        # Actualizar cantidades reales por ítem
        if items_confirmados:
            mapa = {i['id']: i['cantidad_confirmada'] for i in items_confirmados}
            for item in s.items:
                if item.id in mapa:
                    item.cantidad_enviada = max(0, mapa[item.id])
        else:
            for item in s.items:
                if not item.cantidad_enviada:
                    item.cantidad_enviada = item.cantidad_aprobada or item.cantidad_solicitada

        # ── 174646 RIT: reserva SIESA con ubicaciones reales del picking ──
        if not s.siesa_requisicion_consec:
            _tareas_rit = TareaPicking.query.filter_by(
                referencia_documento=s.codigo,
                tipo_documento='TRASLADO',
                estado=_EP.COMPLETADO,
            ).all()
            _ubicaciones_rit = {
                t.producto_id: t.ubicacion.codigo for t in _tareas_rit if t.ubicacion
            }
            _rit_items = [
                {
                    'codigo_siesa':     item.producto_codigo_siesa,
                    'cantidad':         item.cantidad_enviada or 0,
                    'unidad_medida':    (item.producto.unidad_medida if item.producto else '') or '',
                    'ubicacion_codigo': _ubicaciones_rit.get(item.producto_id) or s.bodega_origen_siesa,
                }
                for item in s.items
                if (item.cantidad_enviada or 0) > 0 and item.producto_codigo_siesa
            ]
            if _rit_items:
                try:
                    res_rit = siesa_traslado.crear_rit(
                        bodega_origen=s.bodega_origen_siesa,
                        bodega_destino=s.bodega_destino_siesa,
                        items=_rit_items,
                        codigo=s.codigo,
                    )
                    if not res_rit.get('simulado') and not res_rit.get('modo_ensayo'):
                        consec = TrasladoService._extraer_consec(res_rit)
                        if consec:
                            s.siesa_requisicion_consec = consec
                            s.siesa_error = None
                            logger.info('[TRASLADO] %s RIT → consec %s', s.codigo, consec)
                        else:
                            logger.warning('[TRASLADO] %s RIT sin consecutivo', s.codigo)
                except Exception as e_rit:
                    s.siesa_error = f'RIT 174646: {e_rit}'
                    logger.error('[TRASLADO] %s Error RIT: %s', s.codigo, e_rit)

        # ── Crear TareaPacking unificada (ST) ────────────────────────────────
        # El empacador la verá en la misma cola que los PD, con etiqueta [TRASLADO].
        # Solo se crea si no existe ya una (idempotente en caso de reintento).
        from app.models.packing import TareaPacking, ItemPacking
        from app.models.picking import TareaPicking as _TP, EstadoPicking as _EP2
        import uuid as _uuid

        packing_existente = TareaPacking.query.filter_by(solicitud_id=s.id).first()
        if not packing_existente:
            almacen_obj = Almacen.query.filter_by(
                bodega_siesa_id=s.bodega_origen_siesa
            ).first()
            almacen_id_pk = almacen_obj.id if almacen_obj else None

            tareas_ok = _TP.query.filter_by(
                referencia_documento=s.codigo,
                tipo_documento='TRASLADO',
                estado=_EP2.COMPLETADO,
            ).all()
            ubicacion_por_prod = {
                t.producto_id: t.ubicacion.codigo for t in tareas_ok if t.ubicacion
            }

            codigo_pack = f'PACK-ST-{_uuid.uuid4().hex[:8].upper()}'
            tarea_pack = TareaPacking(
                codigo=codigo_pack,
                tipo_documento='TRASLADO',
                referencia_doc=s.codigo,
                solicitud_id=s.id,
                tienda_destino=s.nombre_punto_venta or s.bodega_destino_siesa,
                bodega_origen_siesa=s.bodega_origen_siesa,
                almacen_id=almacen_id_pk or 1,
                estado='PENDIENTE',
            )
            db.session.add(tarea_pack)
            db.session.flush()

            for item in s.items:
                cantidad = item.cantidad_enviada or 0
                if cantidad <= 0 or not item.producto_id:
                    continue
                db.session.add(ItemPacking(
                    tarea_id=tarea_pack.id,
                    producto_id=item.producto_id,
                    cantidad_esperada=int(cantidad),
                ))
            logger.info('[TRASLADO] %s TareaPacking %s creada', s.codigo, codigo_pack)

        s.estado = EstadoTraslado.EN_PACKING
        db.session.commit()
        logger.info('[TRASLADO] %s → EN_PACKING (picking confirmado por %s)', s.codigo, usuario_id)
        return s

    @staticmethod
    def confirmar_packing_traslado(solicitud_id: int, usuario_id: int) -> SolicitudTraslado:
        """
        Operario verificó empaque (segundo conteo). Dispara 174720 (Compromisos desde RIT)
        y transiciona EN_PACKING → PREPARADO.
        """
        from app.models.picking import TareaPicking, EstadoPicking as _EP

        s = SolicitudTraslado.query.filter_by(id=solicitud_id).with_for_update().first()
        if not s:
            from flask import abort; abort(404)
        if s.estado != EstadoTraslado.EN_PACKING:
            raise ValueError(f'Solo se puede confirmar packing en EN_PACKING (estado: {s.estado})')

        # Ítems con cantidades confirmadas + ubicaciones del picking
        tareas = TareaPicking.query.filter_by(
            referencia_documento=s.codigo,
            tipo_documento='TRASLADO',
            estado=_EP.COMPLETADO,
        ).all()
        ubicacion_por_producto = {
            t.producto_id: t.ubicacion.codigo
            for t in tareas if t.ubicacion
        }

        _comp_items = []
        for item in s.items:
            cantidad = item.cantidad_enviada or 0
            if cantidad <= 0 or not item.producto_codigo_siesa:
                continue
            _comp_items.append({
                'codigo_siesa':    item.producto_codigo_siesa,
                'cantidad':        cantidad,
                'cantidad_packing': cantidad,
                'unidad_medida':   (item.producto.unidad_medida if item.producto else '') or '',
                'ubicacion_codigo': ubicacion_por_producto.get(item.producto_id),
            })

        if s.siesa_requisicion_consec and _comp_items:
            try:
                siesa_traslado.registrar_compromisos(
                    consec_rit=s.siesa_requisicion_consec,
                    bodega_origen=s.bodega_origen_siesa,
                    bodega_destino=s.bodega_destino_siesa,
                    items=_comp_items,
                    codigo=s.codigo,
                )
                s.siesa_error = None
                logger.info('[TRASLADO] %s compromisos 174720 registrados', s.codigo)
            except Exception as e_comp:
                s.siesa_error = f'174720: {e_comp}'
                logger.error('[TRASLADO] %s Error compromisos: %s', s.codigo, e_comp)
        else:
            logger.warning('[TRASLADO] %s sin RIT consec — 174720 omitido', s.codigo)

        s.estado = EstadoTraslado.PREPARADO
        db.session.commit()
        logger.info('[TRASLADO] %s → PREPARADO (packing confirmado por %s)', s.codigo, usuario_id)
        return s

    @staticmethod
    def rechazar_solicitud(solicitud_id: int, aprobador_id: int,
                           motivo: str) -> SolicitudTraslado:
        """Admin rechaza la solicitud."""
        s = SolicitudTraslado.query.get_or_404(solicitud_id)
        if s.estado not in (EstadoTraslado.ENVIADA,):
            raise ValueError(f'Solo se puede rechazar una solicitud ENVIADA (estado: {s.estado})')

        s.estado = EstadoTraslado.RECHAZADA
        s.aprobador_id = aprobador_id
        s.motivo_rechazo = motivo
        db.session.commit()
        logger.info(f'[TRASLADO] {s.codigo} → RECHAZADA por {aprobador_id}: {motivo}')
        return s

    @staticmethod
    def despachar(solicitud_id: int) -> SolicitudTraslado:
        """
        Admin despacha. Si la solicitud pasó por el flujo completo (tiene RIT),
        usa 174930 (Transfer desde Requisición). Fallback a 173076/173066 para
        traslados manuales sin RIT.
        """
        s = SolicitudTraslado.query.filter_by(id=solicitud_id).with_for_update().first()
        if not s:
            from flask import abort
            abort(404)
        if s.estado not in (EstadoTraslado.PREPARADO, EstadoTraslado.EN_PICKING,
                             EstadoTraslado.EN_PACKING):
            raise ValueError(f'No se puede despachar en estado {s.estado}')
        # Guard idempotencia: si Siesa ya procesó este despacho (siesa_salida_consec asignado)
        # y el estado ya avanzó, evitar llamar Siesa de nuevo (duplicación de documento)
        if s.siesa_salida_consec and s.estado in (EstadoTraslado.EN_TRANSITO, EstadoTraslado.ENTREGADA):
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
            if s.siesa_requisicion_consec:
                # Flujo completo: 174646 + 174720 ya disparados — usar 174930
                res = siesa_traslado.despachar_desde_rit(
                    consec_rit=s.siesa_requisicion_consec,
                    codigo=s.codigo,
                )
            elif s.modo_transferencia == 'EN_TRANSITO':
                # Fallback manual: sin RIT, usar 173076 directamente
                bodega_transito = s.bodega_transito_siesa or siesa_traslado.bodega_transito
                if not bodega_transito:
                    raise ValueError('SIESA_BODEGA_TRANSITO no configurada — usar modo DIRECTA')
                res = siesa_traslado.registrar_salida_transito(
                    bodega_origen=s.bodega_origen_siesa,
                    bodega_transito=bodega_transito,
                    items=items_payload,
                    codigo=s.codigo,
                    consec_requisicion=None,
                )
            else:
                res = siesa_traslado.registrar_salida_directa(
                    bodega_origen=s.bodega_origen_siesa,
                    bodega_destino=s.bodega_destino_siesa,
                    items=items_payload,
                    codigo=s.codigo,
                )

            if not res.get('simulado') and not res.get('modo_ensayo'):
                consec = TrasladoService._extraer_consec(res)
                if consec:
                    s.siesa_salida_consec = consec
                else:
                    # 173076 aceptado pero consecutivo no vino en la respuesta.
                    # Auto-recovery: consulta API v2 (176) por f450_docto_alterno.
                    logger.warning(
                        '[TRASLADO] %s: consecutivo null en respuesta 173076 — '
                        'intentando recovery via API_v2_Inventarios_Transferencia_Salida_Transito',
                        s.codigo
                    )
                    consec_recuperado = siesa_traslado.recuperar_consec_salida(s.codigo)
                    if consec_recuperado:
                        s.siesa_salida_consec = consec_recuperado
                        s.siesa_error = None
                        logger.info(
                            '[TRASLADO] %s: consecutivo recuperado via API v2 (176): %s',
                            s.codigo, consec_recuperado
                        )
                    else:
                        s.siesa_error = (
                            'AVISO: 173076 enviado correctamente a Siesa pero el WMS no pudo '
                            'leer el consecutivo. Usa WMS Admin → Traslados → Reintentar despacho '
                            'para forzar el recovery, o cárgalo manualmente.'
                        )
                        logger.error(
                            '[TRASLADO] %s: 173076 OK pero consecutivo null y recovery fallido — '
                            '173079 bloqueado hasta resolución manual. Response: %s',
                            s.codigo, str(res)[:500]
                        )
            else:
                s.siesa_error = None
        except Exception as e:
            s.siesa_error = f'Despacho Siesa: {str(e)}'
            logger.error(f'[TRASLADO] Error despacho Siesa {s.codigo}: {e}', exc_info=True)
            # Alerta inmediata — el traslado físico ya salió pero Siesa no tiene documento.
            # Ops debe reintentar vía WMS admin → Traslados → Reintentar despacho.
            try:
                from app.services.alertas_service import _enviar_email_con_dlq
                _txt = (
                    f'Traslado {s.codigo} despachado físicamente pero Siesa rechazó el documento.\n\n'
                    f'Error: {str(e)[:400]}\n\n'
                    f'ACCIÓN REQUERIDA:\n'
                    f'WMS Admin → Traslados → {s.codigo} → Reintentar despacho\n'
                )
                _html = (
                    f'<p><b>Traslado {s.codigo}</b> despachado físicamente pero Siesa rechazó el documento.</p>'
                    f'<p style="color:#ef4444;"><b>Error:</b> {str(e)[:400]}</p>'
                    f'<p><b>ACCIÓN:</b> WMS Admin → Traslados → {s.codigo} → Reintentar despacho</p>'
                )
                _enviar_email_con_dlq(
                    f'🚨 WMS — Traslado {s.codigo} sin documento Siesa',
                    _html, _txt, f'traslado_despacho_fail_{s.id}',
                )
            except Exception as _ae:
                logger.warning(f'[TRASLADO] No se pudo enviar alerta email para {s.codigo}: {_ae}')

        nuevo_estado = EstadoTraslado.EN_TRANSITO if s.modo_transferencia == 'EN_TRANSITO' else EstadoTraslado.ENTREGADA
        s.estado = nuevo_estado
        s.fecha_despacho = datetime.utcnow()
        if nuevo_estado == EstadoTraslado.ENTREGADA:
            s.fecha_entrega = datetime.utcnow()

        # Invalidar cache stock: la mercancía ya salió de la bodega origen
        TrasladoService.invalidar_cache_stock(s.bodega_origen_siesa)

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
                {'estado': EstadoTraslado.EN_TRANSITO}, synchronize_session=False
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
        if s.estado not in (EstadoTraslado.EN_TRANSITO, 'DESPACHADA'):
            raise ValueError(f'No se puede confirmar recepción en estado {s.estado}')
        # Guard idempotencia: 173079 ya se envió exitosamente — no duplicar
        if s.siesa_entrada_consec and s.estado == EstadoTraslado.ENTREGADA:
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
            # CO de la sede destino — necesario para f350_id_co / f470_id_co_movto en 173079
            from app.models.usuario import Usuario
            _solicitante = Usuario.query.get(s.solicitante_id) if s.solicitante_id else None
            _co_destino = _solicitante.siesa_co_id if _solicitante else None

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
                bodega_transito = s.bodega_transito_siesa or siesa_traslado.bodega_transito
                res = siesa_traslado.registrar_entrada(
                    bodega_transito=bodega_transito,
                    bodega_destino=s.bodega_destino_siesa,
                    items=items_payload,
                    codigo=s.codigo,
                    consec_salida=s.siesa_salida_consec,
                    co_destino=_co_destino,
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

        s.estado = EstadoTraslado.ENTREGADA
        s.fecha_entrega = datetime.utcnow()
        db.session.commit()
        # Invalidar cache stock de la bodega destino: ya recibió mercancía
        TrasladoService.invalidar_cache_stock(s.bodega_destino_siesa)
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
            # Tienda sin gestión WMS — crear Almacen + Ubicacion virtuales
            return TrasladoService._crear_picking_tienda(solicitud)

        # Verificar si el almacen tiene inventario WMS gestionado (UbicacionProducto).
        # Un almacen virtual de tienda (creado automáticamente para compatibilidad de FK)
        # no tiene UbicacionProducto → tratar igual que si no existiera almacen.
        from app.models.inventario import UbicacionProducto as _UP
        from app.models.ubicacion import Ubicacion as _Ub
        tiene_inventario_wms = (
            _UP.query.join(_Ub, _UP.ubicacion_id == _Ub.id)
            .filter(_Ub.almacen_id == almacen.id)
            .limit(1).count() > 0
        )
        if not tiene_inventario_wms:
            return TrasladoService._crear_picking_tienda(solicitud)

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
                    bodega_origen_siesa=solicitud.bodega_origen_siesa,
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
    def _crear_picking_tienda(solicitud: SolicitudTraslado) -> list:
        """
        Crea TareasPicking para traslados cuyo origen es una tienda sin bins WMS.
        No usa FEFO — el picker confirma manualmente las cantidades físicas.
        Crea Almacen + Ubicacion virtuales si no existen (idempotente).
        """
        from app.models.ubicacion import Ubicacion
        from app.models.picking import TareaPicking

        bodega = solicitud.bodega_origen_siesa

        # Get-or-create Almacen virtual para la tienda
        almacen = Almacen.query.filter_by(bodega_siesa_id=bodega).first()
        if not almacen:
            almacen = Almacen(
                codigo=bodega,
                nombre=f'Tienda {bodega}',
                bodega_siesa_id=bodega,
                activo=True,
            )
            db.session.add(almacen)
            db.session.flush()
            logger.info('[TRASLADO] Almacen virtual creado para tienda %s', bodega)

        # Get-or-create Ubicacion virtual "TIENDA"
        codigo_ub = f'{bodega}-TIENDA'
        ubicacion = Ubicacion.query.filter_by(codigo=codigo_ub).first()
        if not ubicacion:
            ubicacion = Ubicacion(
                codigo=codigo_ub,
                almacen_id=almacen.id,
                tipo_zona='GENERAL',
                activo=True,
            )
            db.session.add(ubicacion)
            db.session.flush()
            logger.info('[TRASLADO] Ubicacion virtual creada: %s', codigo_ub)

        # Crear TareasPicking directas (sin FEFO — tienda no tiene bins WMS)
        for item in solicitud.items:
            cantidad = item.cantidad_aprobada or item.cantidad_solicitada
            if not cantidad or cantidad <= 0 or not item.producto_id:
                continue
            codigo = f'PICK-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:6].upper()}'
            tarea = TareaPicking(
                codigo=codigo,
                producto_id=item.producto_id,
                cantidad_solicitada=cantidad,
                ubicacion_id=ubicacion.id,
                almacen_id=almacen.id,
                estado='PENDIENTE',
                prioridad=10,
                referencia_documento=solicitud.codigo,
                tipo_documento='TRASLADO',
                bodega_origen_siesa=bodega,
                operario_id=solicitud.operario_id,
            )
            db.session.add(tarea)
            logger.info('[TRASLADO] %s TareaPicking tienda creada: %s × %s uds',
                        solicitud.codigo, item.producto_id, cantidad)

        db.session.commit()
        return []  # Sin "sin_stock" — la tienda confirma lo que tiene físicamente

    @staticmethod
    def _liberar_reservas_traslado(solicitud: SolicitudTraslado):
        """
        Cancela las TareaPicking pendientes de un traslado y libera el campo
        reservado en UbicacionProducto. Se llama al cancelar/rechazar en EN_PICKING.
        """
        from app.models.picking import TareaPicking, EstadoPicking as _EP

        tareas = TareaPicking.query.filter_by(
            referencia_documento=solicitud.codigo,
            tipo_documento='TRASLADO',
        ).filter(TareaPicking.estado.in_([_EP.PENDIENTE, _EP.EN_PROCESO])).all()

        for t in tareas:
            reg = UbicacionProducto.query.filter_by(
                ubicacion_id=t.ubicacion_id,
                producto_id=t.producto_id,
            ).with_for_update().first()
            if reg:
                reg.reservado = max(0, reg.reservado - t.cantidad_solicitada)
            t.estado = _EP.CANCELADO
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
        from app.models.picking import TareaPicking, EstadoPicking as _EP2

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
                if t.estado in (_EP2.PENDIENTE, _EP2.EN_PROCESO):
                    reg = UbicacionProducto.query.filter_by(
                        ubicacion_id=t.ubicacion_id,
                        producto_id=t.producto_id,
                    ).with_for_update().first()
                    if reg:
                        reg.reservado = max(0, reg.reservado - t.cantidad_solicitada)
                    t.estado = _EP2.CANCELADO
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
        """
        Extrae el consecutivo del documento creado en Siesa.

        Connekta puede devolver el consecutivo en estructuras distintas según
        la versión del conector. Orden de búsqueda:
          1. detalle.Table[0] — campos canónicos (f350_consec_docto, consec_docto, consecutivo)
          2. detalle.Table[0] — cualquier clave que contenga 'consec' con valor entero
          3. Nivel raíz del response — mismos campos
          4. detalle como lista — primer elemento con campo canónico

        Cuando falla, loguea la estructura completa para diagnóstico en Railway.
        """
        try:
            # 1. Ruta canónica: detalle.Table[0]
            detalle = respuesta_siesa.get('detalle', {})
            if isinstance(detalle, dict):
                tabla = detalle.get('Table', [])
                if tabla and isinstance(tabla, list) and len(tabla) > 0:
                    row = tabla[0]
                    for key in ('f350_consec_docto', 'consec_docto', 'consecutivo',
                                'f350_consec', 'consec', 'NumeroDocumento'):
                        if key in row:
                            try:
                                return int(row[key])
                            except (ValueError, TypeError):
                                pass
                    # Búsqueda amplia: cualquier clave con 'consec' y valor entero
                    for key, val in row.items():
                        if 'consec' in key.lower():
                            try:
                                v = int(val)
                                if v > 0:
                                    logger.info(f'[TRASLADO] _extraer_consec: encontrado via clave amplia "{key}"={v}')
                                    return v
                            except (ValueError, TypeError):
                                pass

            # 2. Nivel raíz del response
            for key in ('f350_consec_docto', 'consec_docto', 'consecutivo', 'consec'):
                if key in respuesta_siesa:
                    try:
                        return int(respuesta_siesa[key])
                    except (ValueError, TypeError):
                        pass

            # 3. detalle como lista
            if isinstance(detalle, list) and detalle:
                row = detalle[0]
                if isinstance(row, dict):
                    for key in ('f350_consec_docto', 'consec_docto', 'consecutivo'):
                        if key in row:
                            try:
                                return int(row[key])
                            except (ValueError, TypeError):
                                pass

            # No se encontró — logear estructura para diagnóstico
            logger.warning(
                '[TRASLADO] _extraer_consec: consecutivo no encontrado. '
                'Estructura response: %s',
                str(respuesta_siesa)[:600]
            )
        except Exception as e:
            logger.error(f'[TRASLADO] _extraer_consec excepción: {e} — response: {str(respuesta_siesa)[:400]}')
        return None

    # ── Cache stock Siesa — proceso-nivel, TTL 5 min por bodega ─────────────────
    _stock_siesa_cache: dict = {}   # {bodega_id: {'data': dict, 'ts': float}}
    _STOCK_SIESA_TTL = 300.0        # 5 minutos

    @staticmethod
    def invalidar_cache_stock(bodega_id: str = None):
        """Invalida el cache de stock Siesa. Llamar al despachar o recibir un traslado."""
        if bodega_id:
            TrasladoService._stock_siesa_cache.pop(bodega_id, None)
        else:
            TrasladoService._stock_siesa_cache.clear()

    @staticmethod
    def get_stock_disponible(bodega_id: str = None):
        """
        Consulta stock disponible directamente en Siesa (API_v2_Inventarios_InvFecha).
        Fuente de verdad: f400_cant_existencia_1 de la bodega origen en tiempo real.
        Cache TTL 5 min para no saturar Connekta en cada apertura del tab Pedir.
        Fallback automático a WMS local si Siesa no responde.
        """
        import time
        from app.models.producto import Producto

        bod = bodega_id or BODEGA_ORIGEN_DEFAULT

        # Servir desde cache si está vigente
        entrada = TrasladoService._stock_siesa_cache.get(bod)
        if entrada and (time.time() - entrada['ts']) < TrasladoService._STOCK_SIESA_TTL:
            return entrada['data']

        # Consulta real a Siesa
        try:
            res_siesa = connekta.get_stock_bodega(bod)

            if connekta.modo_simulacion:
                # En modo simulación no hay datos reales — usar WMS local
                return TrasladoService._get_stock_wms(bod)

            rows = res_siesa.get('detalle', {}).get('Table', []) or []
            if not rows:
                logger.warning('[TRASLADO] stock Siesa vacío para bodega %s — usando WMS', bod)
                return TrasladoService._get_stock_wms(bod)

            # Disponible = existencia_1 - comprometida_1 (pedidos activos) - salida_sin_conf_1 (tránsito)
            # Mismo cálculo que Siesa muestra en pantalla de pedidos como "Disponible en UND".
            siesa_stock = {
                str(r.get('f120_referencia', '')).strip(): max(0,
                    int(r.get('f400_cant_existencia_1') or 0)
                    - int(r.get('f400_cant_comprometida_1') or 0)
                    - int(r.get('f400_cant_salida_sin_conf_1') or 0)
                )
                for r in rows
                if r.get('f120_referencia') and int(r.get('f400_cant_existencia_1') or 0) > 0
            }
            # Excluir productos con disponible = 0 (todo el stock ya comprometido)
            siesa_stock = {k: v for k, v in siesa_stock.items() if v > 0}

            if not siesa_stock:
                return {'items': [], 'bodega': bod, 'total': 0, 'fuente': 'siesa'}

            # Cross-reference con catálogo local para nombre, producto_id y unidad
            productos = {
                p.codigo_siesa: p
                for p in Producto.query.filter(
                    Producto.codigo_siesa.in_(list(siesa_stock.keys())),
                    Producto.activo == True,
                ).all()
                if p.codigo_siesa
            }

            items = []
            for codigo_siesa, cantidad in siesa_stock.items():
                prod = productos.get(codigo_siesa)
                if not prod:
                    # Producto en Siesa sin espejo en WMS — omitir (no tiene producto_id)
                    continue
                items.append({
                    'codigo_siesa': codigo_siesa,
                    'nombre': prod.nombre,
                    'producto_id': prod.id,
                    'disponible': cantidad,
                    'existencia': cantidad,
                    'unidad_medida': prod.unidad_medida,
                })

            items.sort(key=lambda x: x['nombre'])
            resultado = {'items': items, 'bodega': bod, 'total': len(items), 'fuente': 'siesa'}

            TrasladoService._stock_siesa_cache[bod] = {'data': resultado, 'ts': time.time()}
            logger.info('[TRASLADO] stock Siesa cargado: %d ítems en %s', len(items), bod)
            return resultado

        except Exception as exc:
            logger.error('[TRASLADO] Siesa stock falló (%s) — fallback a WMS local', exc)
            return TrasladoService._get_stock_wms(bod)

    @staticmethod
    def _get_stock_wms(bodega_id: str):
        """Fallback: stock desde WMS local (PostgreSQL) cuando Siesa no responde."""
        from app.models.producto import Producto
        from app.models.inventario import UbicacionProducto
        from app.models.almacen import Almacen
        from app.models.ubicacion import Ubicacion

        bod = bodega_id or BODEGA_ORIGEN_DEFAULT
        almacen = Almacen.query.filter_by(bodega_siesa_id=bod).first()
        if not almacen:
            return {'items': [], 'bodega': bod, 'total': 0, 'fuente': 'wms_fallback'}

        registros = (
            db.session.query(
                UbicacionProducto.producto_id,
                db.func.sum(UbicacionProducto.cantidad).label('existencia'),
                db.func.sum(UbicacionProducto.reservado).label('reservado'),
            )
            .join(Ubicacion, UbicacionProducto.ubicacion_id == Ubicacion.id)
            .filter(Ubicacion.almacen_id == almacen.id, UbicacionProducto.cantidad > 0)
            .group_by(UbicacionProducto.producto_id)
            .all()
        )
        if not registros:
            return {'items': [], 'bodega': bod, 'total': 0, 'fuente': 'wms_fallback'}

        productos = {
            p.id: p
            for p in Producto.query.filter(
                Producto.id.in_([r.producto_id for r in registros]),
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
        return {'items': items, 'bodega': bod, 'total': len(items), 'fuente': 'wms_fallback'}

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


# ── Stock pre-warmer — carga cache de todas las bodegas al arrancar ───────────

# Todas las bodegas configuradas en el selector "Pedir desde" del frontend.
_BODEGAS_PREWARM = ['NB1', 'NS1', 'NS2', 'NC1', 'FC1', 'PC1', 'PT1', 'FF1', 'FN1', 'FP1']


def init_scheduler(app):
    """Pre-calienta el cache de stock Siesa para todas las bodegas.
    Corre al arrancar (next_run_time=ahora) y cada 4 min en background.
    TTL del cache = 5 min → el scheduler siempre llega antes de que expire."""
    from apscheduler.schedulers.background import BackgroundScheduler

    def _warm_all():
        with app.app_context():
            for bod in _BODEGAS_PREWARM:
                try:
                    TrasladoService.invalidar_cache_stock(bod)
                    TrasladoService.get_stock_disponible(bod)
                except Exception as exc:
                    logger.warning('[STOCK_PREWARM] %s: %s', bod, exc)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _warm_all, 'interval', minutes=4,
        id='stock_prewarm',
        next_run_time=datetime.utcnow(),
    )
    scheduler.start()
    logger.info(
        '[STOCK_PREWARM] Scheduler iniciado — %d bodegas cada 4 min',
        len(_BODEGAS_PREWARM),
    )
