"""
DevolucionClienteService — devolución física de cliente atada al pedido/factura.

Reemplaza el flujo reactivo de TareaDevolucion (devolucion_service.py, DEPRECATED).
El recepcionista busca el pedido ya despachado, cuenta físicamente cuánto se
devuelve por línea (total o parcial, con o sin avería) y confirma en un solo
paso: entra al inventario del WMS y dispara automáticamente la Nota Crédito
(142946) hacia Siesa vía SiesaJob(tipo='NOTA_CREDITO_DEVOLUCION_CLIENTE').

IMPORTANTE — F430_ID_TIPO_DOCTO/F430_CONSEC_DOCTO de 142946 deben ser el
tipo/consecutivo de la FACTURA ELECTRÓNICA real (spec DOCX), no del pedido.
Por eso este servicio resuelve tipo_docto_fe/consec_fe vía
connekta.get_detalle_factura() antes de tocar get_rowids_factura/
trigger_nota_factura — nunca reutiliza TareaPacking.tipo_docto_pedido_siesa/
consec_docto_pedido_siesa directamente para esos dos campos.
"""
import logging
from datetime import datetime
from app.extensions import db
from app.models.devolucion_cliente import DevolucionCliente, LineaDevolucionCliente, EstadoDevolucionCliente
from app.models.packing import TareaPacking
from app.models.producto import Producto
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.ubicacion import Ubicacion
from app.models.siesa_job import SiesaJob, EstadoSiesaJob
from app.services.connekta_gateway import connekta
from app.services.recepcion_service import RecepcionService

logger = logging.getLogger(__name__)

_UBICACION_AVERIADOS = 'AVERIADOS'


class DevolucionClienteService:

    @staticmethod
    def buscar_pedido(numero_pedido_siesa: str) -> dict:
        """
        Localiza el pedido ya despachado y trae, desde Siesa, el detalle real
        de la factura electrónica generada para ese pedido. No persiste nada.
        """
        tarea = (TareaPacking.query
                 .filter_by(numero_pedido_siesa=numero_pedido_siesa, tipo_documento='PEDIDO')
                 .order_by(TareaPacking.id.desc())
                 .first())
        if not tarea:
            raise ValueError(f'No se encontró el pedido {numero_pedido_siesa}')
        if not tarea.siesa_triggered:
            raise ValueError(
                f'El pedido {numero_pedido_siesa} aún no ha sido facturado en Siesa '
                '(despacho no confirmado) — no se puede procesar una devolución todavía'
            )

        # Resolver tipo/consec REALES de la FE — nunca los _pedido_siesa
        filas_fe = connekta.get_detalle_factura(
            tipo_docto_rm=tarea.rm_tipo or '',
            consec_rm=tarea.rm_consec or 0,
            consec_pedido=tarea.consec_docto_pedido_siesa,
        )
        if not filas_fe:
            raise ValueError(
                f'No se pudo localizar la factura electrónica del pedido {numero_pedido_siesa} '
                'en Siesa — verifica que la FE ya se haya generado'
            )
        cab = filas_fe[0]
        tipo_docto_fe = str(cab.get('f350_id_tipo_docto') or '').strip()
        consec_fe = str(cab.get('f350_consec_docto') or '').strip()
        if not tipo_docto_fe or not consec_fe:
            raise ValueError(
                f'La factura electrónica del pedido {numero_pedido_siesa} no trae '
                'tipo/consecutivo de documento — no se puede continuar'
            )

        # Líneas reales de la FE (cantidad facturada, bodega, uom, rowid)
        rowids_data = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
        if not rowids_data:
            raise ValueError(
                f'La factura {tipo_docto_fe}-{consec_fe} no tiene líneas en Siesa'
            )

        lineas = []
        for row in rowids_data:
            ref = (row.get('f120_referencia') or '').strip()
            if not ref:
                continue
            producto = Producto.query.filter_by(codigo_siesa=ref).first()
            if not producto:
                logger.warning(
                    '[DEV_CLIENTE] buscar_pedido: referencia Siesa %r sin producto WMS — omitida', ref
                )
                continue
            lineas.append({
                'producto_id': producto.id,
                'producto_codigo': producto.codigo,
                'producto_nombre': producto.nombre,
                'codigo_siesa': ref,
                'cantidad_facturada': float(row.get('f470_cant_base') or 0),
                'f470_id_unidad_medida': (row.get('f470_id_unidad_medida') or '').strip(),
                'f150_id_bodega': (row.get('f150_id') or '').strip(),
                'f470_rowid': str(row.get('f470_rowid') or ''),
            })

        return {
            'tarea_packing_id': tarea.id,
            'numero_pedido_siesa': tarea.numero_pedido_siesa,
            'cliente': tarea.cliente,
            'almacen_id': tarea.almacen_id,
            'tipo_docto_fe': tipo_docto_fe,
            'consec_fe': consec_fe,
            'lineas': lineas,
        }

    @staticmethod
    def crear_devolucion(tarea_packing_id: int, tipo_docto_fe: str, consec_fe: str,
                         almacen_id: int, recepcionista_id: int, lineas: list,
                         es_total: bool = False, observaciones: str = None) -> DevolucionCliente:
        """
        Crea la cabecera ABIERTA + líneas con cantidad_devuelta > 0.
        lineas: [{producto_id, codigo_siesa, cantidad_facturada, cantidad_devuelta,
                  es_averiado, f470_id_unidad_medida, f150_id_bodega, f470_rowid}]
        """
        tarea = db.session.get(TareaPacking, tarea_packing_id)
        if not tarea:
            raise ValueError(f'TareaPacking {tarea_packing_id} no existe')

        lineas_validas = [l for l in lineas if float(l.get('cantidad_devuelta') or 0) > 0]
        if not lineas_validas:
            raise ValueError('Debes indicar al menos una línea con cantidad devuelta > 0')

        for l in lineas_validas:
            cant_dev = float(l['cantidad_devuelta'])
            cant_fact = float(l.get('cantidad_facturada') or 0)
            if cant_dev > cant_fact:
                raise ValueError(
                    f'Producto {l.get("codigo_siesa")}: cantidad devuelta ({cant_dev}) '
                    f'no puede superar la cantidad facturada ({cant_fact})'
                )

        codigo = f'DEVC-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{tarea_packing_id}'
        devolucion = DevolucionCliente(
            codigo=codigo,
            tarea_packing_id=tarea_packing_id,
            numero_pedido_siesa=tarea.numero_pedido_siesa,
            tipo_docto_fe=tipo_docto_fe,
            consec_fe=str(consec_fe),
            cliente=tarea.cliente,
            almacen_id=almacen_id,
            recepcionista_id=recepcionista_id,
            estado=EstadoDevolucionCliente.ABIERTA,
            es_total=bool(es_total),
            observaciones=observaciones,
        )
        db.session.add(devolucion)
        db.session.flush()

        for l in lineas_validas:
            linea = LineaDevolucionCliente(
                devolucion_id=devolucion.id,
                producto_id=l['producto_id'],
                codigo_siesa=l['codigo_siesa'],
                cantidad_facturada=l.get('cantidad_facturada') or 0,
                cantidad_devuelta=l['cantidad_devuelta'],
                es_averiado=bool(l.get('es_averiado')),
                f470_id_unidad_medida=l.get('f470_id_unidad_medida'),
                f150_id_bodega=l.get('f150_id_bodega'),
                f470_rowid=l.get('f470_rowid'),
            )
            db.session.add(linea)

        db.session.commit()
        logger.info('[DEV_CLIENTE] Devolución creada: %s · pedido %s · %d línea(s)',
                    codigo, tarea.numero_pedido_siesa, len(lineas_validas))
        return devolucion

    @staticmethod
    def confirmar_entrada_fisica(devolucion_id: int, recepcionista_id: int) -> DevolucionCliente:
        """
        Confirma la devolución: ingresa el stock físico y encola la NC (142946).
        Calca el patrón de recepcion_service.confirmar_recepcion / devolucion_service.confirmar_ubicacion:
        GET a Siesa antes de los row-locks de inventario, with_for_update para
        serializar confirmaciones concurrentes, SiesaJob creado antes del commit final.
        """
        devolucion = (DevolucionCliente.query
                      .filter_by(id=devolucion_id)
                      .with_for_update()
                      .first())
        if not devolucion:
            raise ValueError(f'Devolución {devolucion_id} no existe')

        if devolucion.estado == EstadoDevolucionCliente.CONFIRMADA:
            job_existente = SiesaJob.query.filter_by(
                referencia_tipo='DevolucionCliente', referencia_id=devolucion.id,
            ).filter(SiesaJob.estado.in_([
                EstadoSiesaJob.PENDIENTE, EstadoSiesaJob.PROCESANDO,
                EstadoSiesaJob.REINTENTANDO, EstadoSiesaJob.COMPLETADO,
            ])).first()
            if job_existente:
                return devolucion
            logger.warning(
                '[DEV_CLIENTE] Devolución %s CONFIRMADA sin SiesaJob activo — no se re-encola '
                'automáticamente, requiere revisión manual', devolucion.codigo
            )
            return devolucion

        if devolucion.estado != EstadoDevolucionCliente.ABIERTA:
            raise ValueError(f'No se puede confirmar en estado {devolucion.estado}')

        # [C6] Re-GET a Siesa ANTES de tomar row-locks de inventario — revalida
        # que lo contado no exceda lo facturado ahora mismo (el dato pudo cambiar
        # entre la búsqueda y la confirmación).
        rowids_data = connekta.get_rowids_factura(devolucion.tipo_docto_fe, devolucion.consec_fe)
        if not rowids_data:
            raise ValueError(
                f'No se pudo revalidar la factura {devolucion.tipo_docto_fe}-{devolucion.consec_fe} '
                'en Siesa — no se confirma la devolución'
            )
        facturado_actual = {}
        for row in rowids_data:
            ref = (row.get('f120_referencia') or '').strip()
            if ref:
                facturado_actual[ref] = float(row.get('f470_cant_base') or 0)

        for linea in devolucion.lineas:
            cant_fact_actual = facturado_actual.get(linea.codigo_siesa)
            if cant_fact_actual is None:
                raise ValueError(
                    f'Producto {linea.codigo_siesa} ya no aparece en la factura '
                    f'{devolucion.tipo_docto_fe}-{devolucion.consec_fe} en Siesa'
                )
            if float(linea.cantidad_devuelta) > cant_fact_actual:
                raise ValueError(
                    f'Producto {linea.codigo_siesa}: cantidad devuelta ({linea.cantidad_devuelta}) '
                    f'supera la cantidad facturada actual en Siesa ({cant_fact_actual})'
                )

        # Ingresar al inventario — locks adquiridos DESPUÉS del GET a Siesa
        for linea in devolucion.lineas:
            ubicacion = DevolucionClienteService._resolver_ubicacion(
                producto_id=linea.producto_id,
                almacen_id=devolucion.almacen_id,
                es_averiado=linea.es_averiado,
            )
            linea.ubicacion_id = ubicacion.id

            reg = UbicacionProducto.query.filter_by(
                ubicacion_id=ubicacion.id, producto_id=linea.producto_id,
            ).with_for_update().first()

            saldo_antes = reg.cantidad if reg else 0
            if reg:
                reg.cantidad += float(linea.cantidad_devuelta)
                reg.row_version += 1
            else:
                reg = UbicacionProducto(
                    ubicacion_id=ubicacion.id,
                    producto_id=linea.producto_id,
                    cantidad=float(linea.cantidad_devuelta),
                    fecha_ingreso=datetime.utcnow(),
                )
                db.session.add(reg)
                db.session.flush()

            movimiento = MovimientoInventario(
                producto_id=linea.producto_id,
                ubicacion_id=ubicacion.id,
                almacen_id=devolucion.almacen_id,
                tipo='DEVOLUCION_CLIENTE_AVERIADO' if linea.es_averiado else 'DEVOLUCION_CLIENTE',
                cantidad=float(linea.cantidad_devuelta),
                saldo_antes=saldo_antes,
                saldo_despues=reg.cantidad,
                motivo=f'Devolución cliente {devolucion.codigo} · pedido {devolucion.numero_pedido_siesa}',
                numero_documento=devolucion.numero_pedido_siesa,
                usuario_id=recepcionista_id,
                idempotency_key=f'DEVC-{devolucion.id}-{linea.id}',
            )
            db.session.add(movimiento)

        devolucion.estado = EstadoDevolucionCliente.CONFIRMADA
        devolucion.recepcionista_id = recepcionista_id
        devolucion.fecha_confirmacion = datetime.utcnow()

        # [P8] SiesaJob creado ANTES del commit final — atómico con el inventario.
        items_devueltos = [
            {
                'codigo': l.codigo_siesa,
                'cantidad_devuelta': float(l.cantidad_devuelta),
                'es_averiado': l.es_averiado,
            }
            for l in devolucion.lineas
        ]
        SiesaJob.encolar(
            tipo='NOTA_CREDITO_DEVOLUCION_CLIENTE',
            payload={
                'devolucion_id': devolucion.id,
                'tipo_docto_fe': devolucion.tipo_docto_fe,
                'consec_fe': devolucion.consec_fe,
                'es_total': devolucion.es_total,
                'items_devueltos': items_devueltos,
                'notas': devolucion.observaciones or '',
            },
            referencia_tipo='DevolucionCliente',
            referencia_id=devolucion.id,
        )

        db.session.commit()

        try:
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
        except Exception as e_dlq:
            logger.warning('[DEV_CLIENTE] disparar_dlq_inmediato falló (DLQ scheduler lo recogerá): %s', e_dlq)

        logger.info('[DEV_CLIENTE] Devolución %s confirmada — NC encolada', devolucion.codigo)
        return devolucion

    @staticmethod
    def cancelar(devolucion_id: int, recepcionista_id: int, motivo: str = None) -> DevolucionCliente:
        """Solo válido si estado == ABIERTA — nada tocó stock ni Siesa todavía."""
        devolucion = db.session.get(DevolucionCliente, devolucion_id)
        if not devolucion:
            raise ValueError(f'Devolución {devolucion_id} no existe')
        if devolucion.estado != EstadoDevolucionCliente.ABIERTA:
            raise ValueError(
                f'Solo se puede cancelar una devolución ABIERTA (estado actual: {devolucion.estado})'
            )
        devolucion.estado = EstadoDevolucionCliente.CANCELADA
        devolucion.recepcionista_id = recepcionista_id
        devolucion.observaciones = motivo or devolucion.observaciones
        db.session.commit()
        return devolucion

    @staticmethod
    def _resolver_ubicacion(producto_id: int, almacen_id: int, es_averiado: bool) -> Ubicacion:
        """
        Preferencia de put-away: si es_averiado, bucket AVERIADOS (find-or-create,
        igual que devolucion_service.py). Si no, primero el slot fijo Layout↔Picking
        (Ubicacion.producto_asignado_id), luego fallback a _buscar_ubicacion_optima.
        """
        if es_averiado:
            ub = Ubicacion.query.filter_by(codigo=_UBICACION_AVERIADOS, almacen_id=almacen_id).first()
            if not ub:
                ub = Ubicacion(
                    codigo=_UBICACION_AVERIADOS, almacen_id=almacen_id,
                    zona='CUARENTENA', tipo='cuarentena', activo=True,
                )
                db.session.add(ub)
                try:
                    db.session.flush()
                except Exception:
                    db.session.rollback()
                    ub = Ubicacion.query.filter_by(codigo=_UBICACION_AVERIADOS, almacen_id=almacen_id).first()
                    if not ub:
                        raise
            return ub

        slot_fijo = Ubicacion.query.filter_by(
            producto_asignado_id=producto_id, almacen_id=almacen_id, activo=True,
        ).first()
        if slot_fijo:
            return slot_fijo

        producto = db.session.get(Producto, producto_id)
        clasificacion = producto.clasificacion_abc if producto else 'C'
        ubicacion = RecepcionService._buscar_ubicacion_optima(
            almacen_id=almacen_id, clasificacion_abc=clasificacion,
        )
        if not ubicacion:
            raise ValueError(
                f'No hay ninguna ubicación activa en el almacén {almacen_id} para recibir la devolución'
            )
        return ubicacion
