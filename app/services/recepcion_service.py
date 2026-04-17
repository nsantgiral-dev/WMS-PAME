"""
Servicio de Recepción de Mercancía.
Lógica: Recepción ciega → Validación excesos → Cross-dock vs Put-away → Trigger Siesa.
"""
import uuid
import json
import logging
from datetime import datetime
from app.extensions import db
from app.models.recepcion import RecepcionMercancia, ItemRecepcion
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

        recepcion.estado = 'EN_PROCESO'
        recepcion.recepcionista_id = recepcionista_id
        recepcion.fecha_inicio = datetime.utcnow()
        db.session.commit()
        return recepcion

    @staticmethod
    def escanear_producto(recepcion_id: int, producto_id: int,
                          cantidad: int, lote: str = None,
                          fecha_vencimiento=None):
        """
        Recepción ciega — el operario escanea sin ver cantidades esperadas.
        El sistema valida excesos en tiempo real y bloquea si supera tolerancia.
        """
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
            raise ValueError(
                f'Producto no pertenece a esta OC. '
                f'Verificar con el proveedor — posible error de despacho.'
            )

        # Calcular nueva cantidad
        nueva_cantidad = item.cantidad_recibida + cantidad
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
        zona_preferida = {
            'A': 'A',
            'B': 'B',
            'C': 'C'
        }.get(clasificacion_abc, 'B')

        ubicacion = Ubicacion.query.filter_by(
            almacen_id=almacen_id,
            zona=zona_preferida,
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
        recepcion = RecepcionMercancia.query.get(recepcion_id)
        if not recepcion:
            raise ValueError('Recepción no encontrada')
        # Idempotente: si ya fue confirmada devolver los datos sin error
        if recepcion.estado == 'CONFIRMADA':
            return recepcion
        if recepcion.estado != 'EN_PROCESO':
            raise ValueError(f'No se puede confirmar en estado {recepcion.estado}')

        # Ingresar al inventario
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
        recepcion.estado = 'CONFIRMADA'
        recepcion.fecha_confirmacion = datetime.utcnow()

        # Trigger a Siesa — genera entrada contable automáticamente

        # Remisión del proveedor: capturada al iniciar recepción o pasada al confirmar (fallback rec. #8)
        remision = _limpiar_remision(num_remision_prov or recepcion.num_remision_prov or '')
        if remision and not recepcion.num_remision_prov:
            recepcion.num_remision_prov = remision

        # Lookup en vivo a Siesa: proveedor, sucursal y moneda (no persisten en BD aún)
        proveedor_id = recepcion.proveedor_codigo or ''
        sucursal_prov = recepcion.sucursal_prov_siesa or ''
        moneda_docto = None
        moneda_conv = None
        moneda_local = None
        tasa_conv = 0.0
        tasa_local = 0.0
        tercero_comprador = None
        sucursal_comprador = None
        try:
            consec = recepcion.consec_docto_oc_siesa or recepcion.numero_oc_siesa
            resultado_oc = connekta.get_ordenes_compra_aprobadas(sin_filtros=True)
            rows_oc = resultado_oc.get('detalle', {}).get('Table', [])
            for row in rows_oc:
                if str(row.get('f420_consec_docto', '')).strip() == str(consec).strip():
                    proveedor_id = proveedor_id or (row.get('f200_nit_prov', '') or row.get('f200_id_prov', '')).strip()
                    sucursal_prov = sucursal_prov or row.get('f202_id_sucursal_prov', '').strip()
                    moneda_docto = row.get('f420_id_moneda_docto') or None
                    moneda_conv = row.get('f420_id_moneda_conv') or None
                    moneda_local = row.get('f420_id_moneda_local') or None
                    tasa_conv = float(row.get('f420_tasa_conv') or 0.0)
                    tasa_local = float(row.get('f420_tasa_local') or 0.0)
                    tercero_comprador = (row.get('f200_nit_comprador') or row.get('f200_id_comprador') or '').strip() or None
                    sucursal_comprador = row.get('f202_id_sucursal_comprador', '').strip() or None
                    if proveedor_id:
                        recepcion.proveedor_codigo = proveedor_id
                    if sucursal_prov:
                        recepcion.sucursal_prov_siesa = sucursal_prov
                    break
            logger.warning(
                f'[RECEPCION] Lookup OC: proveedor={proveedor_id!r} sucursal={sucursal_prov!r} '
                f'moneda={moneda_docto!r} comprador={tercero_comprador!r} remision={remision!r}'
            )
        except Exception as lookup_err:
            logger.warning(f'[RECEPCION] Lookup OC falló: {lookup_err}')

        if not remision:
            raise ValueError('Número de remisión del proveedor requerido para confirmar la recepción.')

        items_payload = [{
            'producto_codigo': i.producto.codigo,
            'cantidad_recibida': i.cantidad_recibida,
            'cantidad_ordenada': i.cantidad_ordenada,
            'lote': i.lote,
            'es_parcial': i.es_faltante(),
            'destino': i.destino
        } for i in recepcion.items]

        try:
            respuesta_siesa = connekta.confirmar_entrada_compras(
                id_co_oc=recepcion.co_oc_siesa or connekta.centro_op,
                tipo_docto_oc=recepcion.tipo_docto_oc_siesa or '',
                consec_docto_oc=recepcion.consec_docto_oc_siesa or recepcion.numero_oc_siesa,
                items=items_payload,
                es_parcial=tiene_faltantes,
                proveedor_id=proveedor_id,
                sucursal_prov=sucursal_prov,
                tercero_comprador=tercero_comprador,
                sucursal_comprador=sucursal_comprador,
                moneda_docto=moneda_docto,
                moneda_conv=moneda_conv,
                moneda_local=moneda_local,
                tasa_conv=tasa_conv,
                tasa_local=tasa_local,
                num_docto_referencia=remision or None,
                cond_pago=recepcion.cond_pago_siesa or ''
            )
            recepcion.siesa_triggered = True
            recepcion.siesa_response = json.dumps(respuesta_siesa)
            recepcion.siesa_triggered_at = datetime.utcnow()
            logger.info(f'[RECEPCION] Siesa triggered para OC {recepcion.numero_oc_siesa}')

        except Exception as e:
            logger.error(f'[RECEPCION] Error triggering Siesa: {str(e)}')
            recepcion.siesa_triggered = False
            recepcion.siesa_response = str(e)
            # NO hacemos commit aquí — el estado permanece EN_PROCESO para que el
            # operario pueda reintentar. El inventario se confirma solo cuando Siesa
            # responde correctamente (o en modo simulación).
            db.session.rollback()
            raise Exception(f'Error al comunicar con Siesa: {str(e)}. La recepción NO fue confirmada — reintenta.')

        db.session.commit()
        return recepcion