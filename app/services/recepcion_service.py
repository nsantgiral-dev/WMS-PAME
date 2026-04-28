"""
Servicio de Recepción de Mercancía.
Lógica: Recepción ciega → Validación excesos → Cross-dock vs Put-away → Trigger Siesa.
"""
import uuid
import json
import logging
from datetime import datetime
from app.extensions import db
from app.models.recepcion import RecepcionMercancia, ItemRecepcion, EstadoRecepcion
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.ubicacion import Ubicacion
from app.models.picking import TareaPicking
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


def _limpiar_remision(valor: str) -> str:
    """Elimina caracteres especiales y trunca a 12 chars según spec conector 142948."""
    import re
    limpio = re.sub(r'[^A-Za-z0-9]', '', (valor or ''))
    return limpio[:12]


class RecepcionService:

    @staticmethod
    def crear_recepcion(numero_oc_siesa: str, almacen_id: int,
                        proveedor_codigo: str, proveedor_nombre: str,
                        items: list, co_oc_siesa: str = '',
                        tipo_docto_oc_siesa: str = '', consec_docto_oc_siesa: str = '',
                        cond_pago_siesa: str = '', sucursal_prov_siesa: str = '',
                        num_remision_prov: str = ''):
        """
        Crea una recepción a partir de una OC de Siesa.
        items: [{'producto_id', 'cantidad_ordenada', 'tolerancia_exceso_pct'}]
        Solo acepta OCs en estado Aprobado — validar antes de llamar este método.
        """
        existente = RecepcionMercancia.query.filter_by(
            numero_oc_siesa=numero_oc_siesa
        ).filter(RecepcionMercancia.estado.notin_(['CANCELADA'])).first()

        if existente:
            raise ValueError(f'Ya existe una recepción para la OC {numero_oc_siesa}')

        codigo = f'REC-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        recepcion = RecepcionMercancia(
            codigo=codigo,
            numero_oc_siesa=numero_oc_siesa,
            co_oc_siesa=co_oc_siesa,
            tipo_docto_oc_siesa=tipo_docto_oc_siesa,
            consec_docto_oc_siesa=consec_docto_oc_siesa,
            proveedor_codigo=proveedor_codigo,
            proveedor_nombre=proveedor_nombre,
            cond_pago_siesa=cond_pago_siesa or '',
            sucursal_prov_siesa=sucursal_prov_siesa or '',
            num_remision_prov=_limpiar_remision(num_remision_prov),
            almacen_id=almacen_id,
            estado='ABIERTA'
        )
        db.session.add(recepcion)
        db.session.flush()

        for item_data in items:
            item = ItemRecepcion(
                recepcion_id=recepcion.id,
                producto_id=item_data['producto_id'],
                cantidad_ordenada=item_data['cantidad_ordenada'],
                tolerancia_exceso_pct=item_data.get('tolerancia_exceso_pct', 0.0)
            )
            db.session.add(item)

        db.session.commit()
        return recepcion

    @staticmethod
    def iniciar(recepcion_id: int, recepcionista_id: int):
        """Recepcionista toma la tarea — pantalla en blanco esperando escaneos."""
        recepcion = RecepcionMercancia.query.get(recepcion_id)
        if not recepcion:
            raise ValueError('Recepción no encontrada')
        if recepcion.estado != 'ABIERTA':
            raise ValueError(f'No se puede iniciar en estado {recepcion.estado}')

        recepcion.estado = EstadoRecepcion.EN_PROCESO
        recepcion.recepcionista_id = recepcionista_id
        recepcion.fecha_inicio = datetime.utcnow()
        db.session.commit()
        return recepcion

    @staticmethod
    def escanear_producto(recepcion_id: int, producto_id: int,
                          cantidad: int, lote: str = None,
                          fecha_vencimiento=None, es_empaque: bool = False,
                          es_bonificacion: bool = False):
        """
        Recepción ciega — el operario escanea sin ver cantidades esperadas.
        El sistema valida excesos en tiempo real y bloquea si supera tolerancia.
        Si es_bonificacion=True y el producto no está en la OC, se registra como
        ítem adicional con motivo 04 (Obsequio/Bonificación, $0, no afecta costo promedio).
        """
        import os
        recepcion = RecepcionMercancia.query.get(recepcion_id)
        if not recepcion:
            raise ValueError('Recepción no encontrada')
        if recepcion.estado != 'EN_PROCESO':
            raise ValueError('La recepción no está en proceso')

        item = ItemRecepcion.query.filter_by(
            recepcion_id=recepcion_id,
            producto_id=producto_id
        ).first()

        if not item:
            if not es_bonificacion:
                raise ValueError({
                    'tipo': 'PRODUCTO_NO_EN_OC',
                    'mensaje': 'Producto no está en esta OC. '
                               'Si es bonificación u obsequio del proveedor, confirma para registrarlo.',
                    'producto_id': producto_id,
                    'requiere_confirmacion': True
                })
            motivo_codigo = os.environ.get('SIESA_MOTIVO_OBSEQUIO', '04')
            item = ItemRecepcion(
                recepcion_id=recepcion_id,
                producto_id=producto_id,
                cantidad_ordenada=0,
                tolerancia_exceso_pct=0.0,
                tipo='BONIFICACION',
                motivo_siesa=motivo_codigo
            )
            db.session.add(item)
            db.session.flush()

        # Conversión de empaque: si escanearon la caja/paca, multiplicar por factor
        if es_empaque and item.producto and (item.producto.factor_conversion or 1) > 1:
            factor = item.producto.factor_conversion
            unidades_este_scan = cantidad * factor
            nuevos_empaques = (item.empaques_escaneados or 0) + cantidad
        else:
            unidades_este_scan = cantidad
            nuevos_empaques = item.empaques_escaneados or 0

        # Calcular nueva cantidad
        nueva_cantidad = item.cantidad_recibida + unidades_este_scan
        maxima_permitida = item.cantidad_maxima_permitida()

        # BLOQUEO DE EXCESO
        if nueva_cantidad > maxima_permitida:
            raise ValueError({
                'tipo': 'EXCESO_BLOQUEADO',
                'mensaje': f'EXCESO BLOQUEADO: OC pide {item.cantidad_ordenada} '
                           f'unidades de {item.producto.nombre}. '
                           f'Máximo permitido: {maxima_permitida}. '
                           f'Ya escaneadas: {item.cantidad_recibida}. '
                           f'Intentando agregar: {cantidad}.',
                'cantidad_ordenada': item.cantidad_ordenada,
                'maxima_permitida': maxima_permitida,
                'ya_escaneadas': item.cantidad_recibida,
                'intentando_agregar': cantidad
            })

        item.cantidad_recibida = nueva_cantidad
        item.empaques_escaneados = nuevos_empaques
        if lote:
            item.lote = lote
        if fecha_vencimiento:
            item.fecha_vencimiento = fecha_vencimiento

        # Verificar cross-dock
        destino_info = RecepcionService._decidir_destino(
            producto_id=producto_id,
            almacen_id=recepcion.almacen_id,
            cantidad=nueva_cantidad
        )
        item.destino = destino_info['destino']
        item.ubicacion_id = destino_info.get('ubicacion_id')
        item.ubicacion_cross_dock_id = destino_info.get('ubicacion_cross_dock_id')

        alerta = None
        if item.es_exceso():
            alerta = f'EXCESO: {item.diferencia()} unidades de más'
        elif destino_info['destino'] == 'CROSS_DOCK':
            alerta = f'CROSS-DOCK: Este producto tiene pedidos pendientes — llevar directo a zona de despacho'

        db.session.commit()

        return {
            'item': item.to_dict(),
            'destino': destino_info,
            'alerta': alerta,
            'items_pendientes': sum(
                1 for i in recepcion.items if i.cantidad_recibida == 0
            )
        }

    @staticmethod
    def _decidir_destino(producto_id: int, almacen_id: int, cantidad: int):
        """
        Decide si el producto va a Cross-Dock o a inventario.
        Cross-Dock: hay pedidos de venta pendientes para este producto.
        Put-Away: va a ubicación según clasificación ABC.
        """
        # Verificar si hay backorders (picking pendiente para este producto)
        backorders = TareaPicking.query.filter_by(
            producto_id=producto_id,
            almacen_id=almacen_id,
            estado='PENDIENTE'
        ).count()

        if backorders > 0:
            # Buscar zona cross-dock del almacén
            ubicacion_cd = Ubicacion.query.filter_by(
                almacen_id=almacen_id,
                tipo='cross_dock',
                activo=True
            ).first()

            return {
                'destino': 'CROSS_DOCK',
                'backorders': backorders,
                'ubicacion_cross_dock_id': ubicacion_cd.id if ubicacion_cd else None,
                'mensaje': f'Hay {backorders} pedido(s) pendiente(s) — llevar a Cross-Dock'
            }

        # Sin backorders — put-away según ABC
        from app.models.producto import Producto
        producto = Producto.query.get(producto_id)
        clasificacion = producto.clasificacion_abc if producto else 'C'

        # Buscar ubicación óptima según ABC
        ubicacion = RecepcionService._buscar_ubicacion_optima(
            almacen_id=almacen_id,
            clasificacion_abc=clasificacion
        )

        return {
            'destino': 'INVENTARIO',
            'backorders': 0,
            'ubicacion_id': ubicacion.id if ubicacion else None,
            'ubicacion_codigo': ubicacion.codigo if ubicacion else None,
            'clasificacion_abc': clasificacion,
            'mensaje': f'Producto clase {clasificacion} — almacenar en ubicación sugerida'
        }

    @staticmethod
    def _buscar_ubicacion_optima(almacen_id: int, clasificacion_abc: str):
        """
        Busca la ubicación con menos ocupación según clasificación ABC.
        Clase A: zona de alta rotación (cerca de despacho)
        Clase B/C: zonas normales
        """
        tipo_zona_preferida = 'PICKING' if clasificacion_abc == 'A' else 'RESERVA'

        ubicacion = Ubicacion.query.filter_by(
            almacen_id=almacen_id,
            tipo_zona=tipo_zona_preferida,
            activo=True
        ).first()

        if not ubicacion:
            ubicacion = Ubicacion.query.filter_by(
                almacen_id=almacen_id,
                activo=True
            ).first()

        return ubicacion

    @staticmethod
    def confirmar_recepcion(recepcion_id: int, observaciones: str = None,
                            num_remision_prov: str = None):
        """
        Confirma la recepción e ingresa todo al inventario.
        Dispara Siesa automáticamente para generar entrada contable.
        """
        # [C1] with_for_update() serializa dos workers que lleguen simultáneamente —
        # el segundo ve estado='CONFIRMADA' y sale limpio, sin doble ENTRADA_OC en Siesa.
        recepcion = RecepcionMercancia.query.filter_by(id=recepcion_id).with_for_update().first()
        if not recepcion:
            raise ValueError('Recepción no encontrada')
        # Idempotente: si ya fue confirmada, verificar que Siesa también fue disparado
        if recepcion.estado == 'CONFIRMADA':
            if recepcion.siesa_triggered:
                return recepcion
            # CONFIRMADA pero Siesa nunca se disparó — verificar job en DLQ
            from app.models.siesa_job import SiesaJob, EstadoSiesaJob
            # [C7] Incluir COMPLETADO: si el job terminó exitosamente pero siesa_triggered=False
            # (bug en el handler), re-encolar generaría doble ENTRADA_OC en Siesa.
            job_existente = SiesaJob.query.filter_by(
                referencia_tipo='RecepcionMercancia',
                referencia_id=recepcion.id,
            ).filter(SiesaJob.estado.in_([
                EstadoSiesaJob.PENDIENTE, EstadoSiesaJob.PROCESANDO, EstadoSiesaJob.REINTENTANDO,
                EstadoSiesaJob.FALLIDO, EstadoSiesaJob.COMPLETADO,
            ])).first()
            if job_existente:
                if job_existente.estado == EstadoSiesaJob.FALLIDO:
                    logger.error(
                        f'[RECEPCION] Job FALLIDO {job_existente.id} para recepcion={recepcion.id} '
                        f'(CONFIRMADA sin siesa_triggered) — NO rescatado automáticamente. '
                        f'Verificar en Siesa si la entrada OC ya fue registrada antes de rescatar '
                        f'desde el panel de administración.'
                    )
                elif job_existente.estado == EstadoSiesaJob.COMPLETADO:
                    # [C7] Job completado pero siesa_triggered no se actualizó (bug en handler).
                    # Siesa ya procesó la entrada — NO re-encolar bajo ninguna circunstancia.
                    logger.error(
                        f'[RECEPCION] Job COMPLETADO {job_existente.id} para recepcion={recepcion.id} '
                        f'pero siesa_triggered=False — posible bug en DLQ handler. '
                        f'Corrigiendo siesa_triggered=True para evitar re-encolar.'
                    )
                    recepcion.siesa_triggered = True
                    db.session.commit()
                return recepcion
            # Sin job alguno — re-encolar con los datos disponibles en el registro.
            # El inventario ya fue ingresado; solo falta crear la entrada contable en Siesa.
            logger.warning(
                f'[RECEPCION] recepcion={recepcion.id} CONFIRMADA sin SiesaJob — re-encolando ENTRADA_OC'
            )

            # Re-fetch OC data from Siesa to get bodega/uom/fecha_entrega per item.
            # Siesa validates these fields against the original OC — defaults would be rejected.
            _oc_items: dict = {}
            _oc_fallback: dict = {}
            _re_proveedor = recepcion.proveedor_codigo or ''
            _re_sucursal = recepcion.sucursal_prov_siesa or ''
            _re_tercero_comprador = None
            _re_moneda_docto = None
            _re_moneda_conv = None
            _re_moneda_local = None
            _re_tasa_conv = 0.0
            _re_tasa_local = 0.0
            try:
                _consec = recepcion.consec_docto_oc_siesa or recepcion.numero_oc_siesa
                _oc_result = connekta.get_ordenes_compra_aprobadas(consec=_consec)
                _rows_oc = _oc_result.get('detalle', {}).get('Table', [])
                _header_leido = False
                for _row in _rows_oc:
                    if not _header_leido:
                        _re_proveedor = _re_proveedor or (_row.get('f200_nit_prov', '') or _row.get('f200_id_prov', '')).strip()
                        _re_sucursal = _re_sucursal or _row.get('f202_id_sucursal_prov', '').strip()
                        _re_moneda_docto = _row.get('f420_id_moneda_docto') or None
                        _re_moneda_conv = _row.get('f420_id_moneda_conv') or None
                        _re_moneda_local = _row.get('f420_id_moneda_local') or None
                        _re_tasa_conv = float(_row.get('f420_tasa_conv') or 0.0)
                        _re_tasa_local = float(_row.get('f420_tasa_local') or 0.0)
                        _re_tercero_comprador = (_row.get('f200_nit_comprador') or _row.get('f200_id_comprador') or '').strip() or None
                        _header_leido = True
                    _ref = (_row.get('f120_referencia', '') or '').strip()
                    _idata = {
                        'bodega': (_row.get('f150_id', '') or '').strip() or None,
                        'uom': (_row.get('f421_id_unidad_medida', '') or '').strip() or None,
                        'fecha_entrega': (_row.get('f421_fecha_entrega', '') or '').strip() or None,
                        'motivo': (_row.get('f421_id_motivo', '') or '').strip() or None,
                    }
                    if not _oc_fallback and _idata['bodega']:
                        _oc_fallback = _idata
                    if _ref:
                        _oc_items[_ref.upper()] = _idata
                    _id_siesa = str(_row.get('f120_id', '') or '').strip()
                    if _id_siesa:
                        _oc_items[_id_siesa] = _idata
            except Exception as _lookup_err:
                logger.error(
                    f'[RECEPCION] Re-enqueue: lookup OC falló para {recepcion.numero_oc_siesa}: {_lookup_err} '
                    f'— re-enqueueing sin bodega/uom/fecha_entrega (Siesa usará defaults)',
                    exc_info=True
                )

            _items_rec = []
            for _ir in recepcion.items:
                if (_ir.cantidad_recibida or 0) <= 0:
                    continue
                _cod = (_ir.producto.codigo_siesa or _ir.producto.codigo) if _ir.producto else None
                if not _cod:
                    continue
                _oc_data = _oc_items.get((_cod or '').upper()) or _oc_fallback
                _items_rec.append({
                    'producto_codigo': _cod,
                    'cantidad_recibida': _ir.cantidad_recibida,
                    'cantidad_ordenada': _ir.cantidad_ordenada,
                    'lote': _ir.lote,
                    'es_parcial': _ir.es_faltante(),
                    'tipo': _ir.tipo or 'NORMAL',
                    'motivo_siesa': _ir.motivo_siesa or _oc_data.get('motivo'),
                    'bodega': _oc_data.get('bodega'),
                    'uom': _oc_data.get('uom'),
                    'fecha_entrega': _oc_data.get('fecha_entrega'),
                })
            _job_rec = SiesaJob.encolar(
                tipo='ENTRADA_OC',
                payload={
                    'recepcion_id': recepcion.id,
                    'numero_oc_siesa': recepcion.numero_oc_siesa,
                    'id_co_oc': recepcion.co_oc_siesa or connekta.centro_op,
                    'tipo_docto_oc': recepcion.tipo_docto_oc_siesa or '',
                    'consec_docto_oc': recepcion.consec_docto_oc_siesa or recepcion.numero_oc_siesa,
                    'items': _items_rec,
                    'es_parcial': recepcion.es_parcial,
                    'proveedor_id': _re_proveedor,
                    'sucursal_prov': _re_sucursal,
                    'tercero_comprador': _re_tercero_comprador,
                    'moneda_docto': _re_moneda_docto,
                    'moneda_conv': _re_moneda_conv,
                    'moneda_local': _re_moneda_local,
                    'tasa_conv': _re_tasa_conv,
                    'tasa_local': _re_tasa_local,
                    'cond_pago': recepcion.cond_pago_siesa or '',
                    'num_docto_referencia': recepcion.num_remision_prov or None,
                },
                referencia_tipo='RecepcionMercancia',
                referencia_id=recepcion.id,
            )
            db.session.commit()
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
            logger.info(f'[RECEPCION] Job {_job_rec.id} encolado para recepcion={recepcion.id}')
            return recepcion
        if recepcion.estado != 'EN_PROCESO':
            raise ValueError(f'No se puede confirmar en estado {recepcion.estado}')

        # [C6] Lookup a Siesa ANTES de adquirir los row-locks de UbicacionProducto.
        # Si este bloque estuviera después del for-loop, los locks de inventario quedarían
        # retenidos durante ~30s de HTTP call, bloqueando workers concurrentes y
        # agotando el connection pool bajo carga paralela.
        remision = _limpiar_remision(num_remision_prov or recepcion.num_remision_prov or '')
        if remision and not recepcion.num_remision_prov:
            recepcion.num_remision_prov = remision

        proveedor_id = recepcion.proveedor_codigo or ''
        sucursal_prov = recepcion.sucursal_prov_siesa or ''
        moneda_docto = None
        moneda_conv = None
        moneda_local = None
        tasa_conv = 0.0
        tasa_local = 0.0
        tercero_comprador = None
        items_oc = {}
        oc_fallback = {}
        try:
            consec = recepcion.consec_docto_oc_siesa or recepcion.numero_oc_siesa
            resultado_oc = connekta.get_ordenes_compra_aprobadas(consec=consec)
            rows_oc = resultado_oc.get('detalle', {}).get('Table', [])
            if not rows_oc:
                logger.warning(f'[RECEPCION] OC {consec} devolvió Table vacía — sin datos de bodega/UOM')
            header_leido = False
            for row in rows_oc:
                if not header_leido:
                    proveedor_id = proveedor_id or (row.get('f200_nit_prov', '') or row.get('f200_id_prov', '')).strip()
                    sucursal_prov = sucursal_prov or row.get('f202_id_sucursal_prov', '').strip()
                    moneda_docto = row.get('f420_id_moneda_docto') or None
                    moneda_conv = row.get('f420_id_moneda_conv') or None
                    moneda_local = row.get('f420_id_moneda_local') or None
                    tasa_conv = float(row.get('f420_tasa_conv') or 0.0)
                    tasa_local = float(row.get('f420_tasa_local') or 0.0)
                    tercero_comprador = (row.get('f200_nit_comprador') or row.get('f200_id_comprador') or '').strip() or None
                    if proveedor_id:
                        recepcion.proveedor_codigo = proveedor_id
                    if sucursal_prov:
                        recepcion.sucursal_prov_siesa = sucursal_prov
                    header_leido = True

                ref_siesa = (row.get('f120_referencia', '') or '').strip()
                item_data = {
                    'ref_siesa': ref_siesa,
                    'bodega': (row.get('f150_id', '') or '').strip() or None,
                    'uom': (row.get('f421_id_unidad_medida', '') or '').strip() or None,
                    'fecha_entrega': (row.get('f421_fecha_entrega', '') or '').strip() or None,
                    'motivo': (row.get('f421_id_motivo', '') or '').strip() or None,
                }
                if not oc_fallback and item_data['bodega']:
                    oc_fallback = item_data
                if ref_siesa:
                    items_oc[ref_siesa.upper()] = item_data
                id_siesa = str(row.get('f120_id', '') or '').strip()
                if id_siesa:
                    items_oc[id_siesa] = item_data

            logger.warning(
                f'[RECEPCION] Lookup OC: proveedor={proveedor_id!r} '
                f'items_oc keys={list(items_oc.keys())} fallback={oc_fallback!r}'
            )
        except Exception as lookup_err:
            logger.error(
                f'[RECEPCION] Lookup OC falló para {recepcion.numero_oc_siesa}: {lookup_err}',
                exc_info=True
            )

        # Siesa requiere proveedor_id — fallar rápido antes de crear SiesaJob
        if not proveedor_id:
            raise ValueError(
                f'No se pudo obtener el proveedor de la OC {recepcion.numero_oc_siesa} en Siesa. '
                'Verifica que la OC exista en SIESA o asigna el código de proveedor manualmente.'
            )

        # Ingresar al inventario — row-locks adquiridos DESPUÉS del HTTP call
        tiene_excesos = False
        tiene_faltantes = False
        tiene_cross_dock = False

        for item in recepcion.items:
            if item.es_exceso():
                tiene_excesos = True
            if item.es_faltante():
                tiene_faltantes = True

            destino_ubicacion_id = (
                item.ubicacion_cross_dock_id
                if item.destino == 'CROSS_DOCK'
                else item.ubicacion_id
            )

            if item.destino == 'CROSS_DOCK':
                tiene_cross_dock = True

            if destino_ubicacion_id and item.cantidad_recibida > 0:
                reg = UbicacionProducto.query.filter_by(
                    ubicacion_id=destino_ubicacion_id,
                    producto_id=item.producto_id
                ).with_for_update().first()

                if not reg:
                    reg = UbicacionProducto(
                        ubicacion_id=destino_ubicacion_id,
                        producto_id=item.producto_id,
                        cantidad=0,
                        lote=item.lote,
                        fecha_vencimiento=item.fecha_vencimiento,
                        fecha_ingreso=datetime.utcnow()
                    )
                    db.session.add(reg)
                    db.session.flush()

                saldo_antes = reg.cantidad
                reg.cantidad += item.cantidad_recibida
                reg.row_version += 1

                movimiento = MovimientoInventario(
                    producto_id=item.producto_id,
                    ubicacion_id=destino_ubicacion_id,
                    almacen_id=recepcion.almacen_id,
                    tipo='ENTRADA',
                    cantidad=item.cantidad_recibida,
                    saldo_antes=saldo_antes,
                    saldo_despues=reg.cantidad,
                    motivo=f'Recepción {recepcion.codigo} - OC {recepcion.numero_oc_siesa}',
                    numero_documento=recepcion.numero_oc_siesa,
                    usuario_id=recepcion.recepcionista_id,
                    idempotency_key=f'REC-{recepcion.id}-{item.producto_id}'
                )
                db.session.add(movimiento)
                item.ingresado_inventario = True

        recepcion.es_parcial = tiene_faltantes
        recepcion.tiene_excesos = tiene_excesos
        recepcion.tiene_cross_dock = tiene_cross_dock
        recepcion.observaciones = observaciones
        recepcion.estado = EstadoRecepcion.CONFIRMADA
        recepcion.fecha_confirmacion = datetime.utcnow()

        items_payload = []
        for i in recepcion.items:
            if i.cantidad_recibida <= 0:
                continue

            if i.tipo == 'BONIFICACION':
                # Obsequio/bonificación: usa su propio código, bodega y uom del fallback de la OC.
                # Precio $0, motivo 04 — nunca debe heredar la referencia de otro ítem.
                items_payload.append({
                    'producto_codigo': i.producto.codigo_siesa or i.producto.codigo,
                    'cantidad_recibida': i.cantidad_recibida,
                    'cantidad_ordenada': 0,
                    'lote': i.lote,
                    'es_parcial': False,
                    'destino': i.destino,
                    'tipo': i.tipo,
                    'motivo_siesa': i.motivo_siesa,
                    'bodega': oc_fallback.get('bodega'),
                    'uom': oc_fallback.get('uom'),
                    'fecha_entrega': oc_fallback.get('fecha_entrega'),
                })
                continue

            # Ítems de OC: lookup normalizado por código WMS, código Siesa e ID numérico
            oc_data = (
                items_oc.get((i.producto.codigo or '').strip().upper()) or
                items_oc.get((i.producto.codigo_siesa or '').strip().upper())
            )
            _usó_fallback = oc_data is None
            if _usó_fallback:
                # [OC-FALLBACK] Ítem no encontrado en líneas de la OC.
                # Riesgo: OC con bodegas mixtas → bodega del primer ítem ≠ bodega correcta.
                # Se usa oc_fallback solo si no hay otra opción; el WARNING es visible en logs.
                oc_data = oc_fallback
                logger.warning(
                    f'[RECEPCION] OC-FALLBACK: item {i.producto.codigo!r} '
                    f'(siesa={i.producto.codigo_siesa!r}) no encontrado en OC '
                    f'{recepcion.numero_oc_siesa!r} — usando bodega/uom del primer ítem '
                    f'bodega={oc_data.get("bodega")!r} uom={oc_data.get("uom")!r}. '
                    f'Verificar que código Siesa del producto coincida con OC.'
                )
            else:
                logger.debug(
                    f'[RECEPCION] item {i.producto.codigo!r} → ref_siesa={oc_data.get("ref_siesa")!r} '
                    f'bodega={oc_data.get("bodega")!r} uom={oc_data.get("uom")!r}'
                )
            # Cuando hay fallback, oc_data.get('ref_siesa') es el código del PRIMER ítem de la OC
            # (no del producto actual) → se registraría la cantidad bajo código incorrecto en Siesa.
            # Usar siempre el codigo_siesa del producto cuando está disponible.
            _producto_codigo_siesa = (
                (i.producto.codigo_siesa or '').strip() or
                oc_data.get('ref_siesa') or
                i.producto.codigo
            ) if not _usó_fallback else (
                (i.producto.codigo_siesa or '').strip() or i.producto.codigo
            )
            items_payload.append({
                'producto_codigo': _producto_codigo_siesa,
                'cantidad_recibida': i.cantidad_recibida,
                'cantidad_ordenada': i.cantidad_ordenada,
                'lote': i.lote,
                'es_parcial': i.es_faltante(),
                'destino': i.destino,
                'tipo': i.tipo,
                'motivo_siesa': i.motivo_siesa or oc_data.get('motivo'),
                'bodega': oc_data.get('bodega'),
                'uom': oc_data.get('uom'),
                'fecha_entrega': oc_data.get('fecha_entrega'),
            })

        # Validar que tenemos datos de bodega/uom para al menos los items de OC (no BONIFICACION).
        # Si el lookup OC falló Y recepcion no tenía bodega en BD, el payload sería inválido
        # y el job quedaría en DLQ permanente sin posibilidad de reintento exitoso.
        items_sin_bodega = [
            item['producto_codigo'] for item in items_payload
            if item.get('tipo') != 'BONIFICACION' and not item.get('bodega')
        ]
        if items_sin_bodega:
            raise ValueError(
                f'No se pudo obtener bodega/UOM para {len(items_sin_bodega)} ítem(s) de la OC '
                f'{recepcion.numero_oc_siesa} en Siesa: {items_sin_bodega[:3]}. '
                'Verifica que la OC exista en Siesa y tenga líneas activas.'
            )

        # [P8] Crear SiesaJob ANTES de llamar a Siesa — atómico con el inventario.
        # Si el commit falla, ni el inventario ni el job persisten (consistente).
        # Si Siesa falla después, el job ya está PENDIENTE para DLQ automático.
        from app.models.siesa_job import SiesaJob
        job_dlq = SiesaJob.encolar(
            tipo='ENTRADA_OC',
            payload={
                'recepcion_id': recepcion.id,
                'numero_oc_siesa': recepcion.numero_oc_siesa,
                'id_co_oc': recepcion.co_oc_siesa or connekta.centro_op,
                'tipo_docto_oc': recepcion.tipo_docto_oc_siesa or '',
                'consec_docto_oc': recepcion.consec_docto_oc_siesa or recepcion.numero_oc_siesa,
                'items': items_payload,
                'es_parcial': tiene_faltantes,
                'proveedor_id': proveedor_id,
                'sucursal_prov': sucursal_prov,
                'tercero_comprador': tercero_comprador,
                'moneda_docto': moneda_docto,
                'moneda_conv': moneda_conv,
                'moneda_local': moneda_local,
                'tasa_conv': tasa_conv,
                'tasa_local': tasa_local,
                'num_docto_referencia': remision or None,
                'cond_pago': recepcion.cond_pago_siesa or '',
            },
            referencia_tipo='RecepcionMercancia',
            referencia_id=recepcion.id,
        )
        # Commit inventario + SiesaJob en una sola transacción
        db.session.commit()

        # Disparar DLQ en hilo daemon — procesa el job recién encolado sin bloquear el worker.
        # Si Siesa falla, el job queda PENDIENTE para reintento automático cada 5 min.
        from app.services.siesa_job_service import disparar_dlq_inmediato
        disparar_dlq_inmediato()

        logger.info(
            f'[RECEPCION] recepcion={recepcion.id} OC={recepcion.numero_oc_siesa} — '
            f'job_dlq={job_dlq.id} encolado, DLQ disparado async'
        )
        return recepcion