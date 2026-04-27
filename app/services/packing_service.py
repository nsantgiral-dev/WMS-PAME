"""
Servicio de Packing con verificación ítem por ítem y trigger automático a Siesa.
Flujo: Crear → Iniciar → Escanear ítems → Confirmar → Siesa factura solo.
"""
import uuid
import json
from datetime import datetime
from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.models.picking import TareaPicking
from app.services.connekta_gateway import connekta
import logging

logger = logging.getLogger(__name__)


class PackingService:

    @staticmethod
    def crear_desde_picking(tareas_picking_ids: list, numero_pedido_siesa: str, almacen_id: int,
                            tipo_docto_pedido_siesa: str = '', consec_docto_pedido_siesa: str = ''):
        """
        Crea una tarea de packing a partir de tareas de picking completadas.
        Agrupa todos los ítems del pedido en una sola tarea de packing.
        """
        tareas_picking = TareaPicking.query.filter(
            TareaPicking.id.in_(tareas_picking_ids),
            TareaPicking.estado == 'COMPLETADO'
        ).all()

        if not tareas_picking:
            raise ValueError('No hay tareas de picking completadas para empacar')

        # Verificar que no exista ya un packing para este pedido
        existente = TareaPacking.query.filter_by(
            numero_pedido_siesa=numero_pedido_siesa
        ).filter(TareaPacking.estado.notin_(['CANCELADO'])).first()

        if existente:
            raise ValueError(f'Ya existe una tarea de packing para el pedido {numero_pedido_siesa}')

        codigo = f'PACK-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        tarea = TareaPacking(
            codigo=codigo,
            numero_pedido_siesa=numero_pedido_siesa,
            tipo_docto_pedido_siesa=tipo_docto_pedido_siesa,
            consec_docto_pedido_siesa=consec_docto_pedido_siesa,
            almacen_id=almacen_id,
            estado='PENDIENTE'
        )
        db.session.add(tarea)
        db.session.flush()

        # Agrupar ítems por producto
        items_por_producto = {}
        for tp in tareas_picking:
            pid = tp.producto_id
            if pid not in items_por_producto:
                items_por_producto[pid] = {
                    'cantidad': 0,
                    'lote': tp.lote
                }
            items_por_producto[pid]['cantidad'] += tp.cantidad_recogida

        for producto_id, datos in items_por_producto.items():
            item = ItemPacking(
                tarea_id=tarea.id,
                producto_id=producto_id,
                cantidad_esperada=datos['cantidad'],
                lote=datos['lote']
            )
            db.session.add(item)

        db.session.commit()
        return tarea

    @staticmethod
    def crear_manual(numero_pedido_siesa: str, almacen_id: int, items: list,
                     tipo_docto_pedido_siesa: str = '', consec_docto_pedido_siesa: str = '',
                     cliente: str = '', municipio: str = ''):
        """
        Crea una tarea de packing manualmente con lista de ítems.
        Útil cuando el pedido viene directo de Siesa sin picking previo.
        items: [{'producto_id': int, 'cantidad': int, 'lote': str}]
        """
        existente = TareaPacking.query.filter_by(
            numero_pedido_siesa=numero_pedido_siesa
        ).filter(TareaPacking.estado.notin_(['CANCELADO'])).first()

        if existente:
            raise ValueError(f'Ya existe una tarea de packing para el pedido {numero_pedido_siesa}')

        codigo = f'PACK-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        tarea = TareaPacking(
            codigo=codigo,
            numero_pedido_siesa=numero_pedido_siesa,
            tipo_docto_pedido_siesa=tipo_docto_pedido_siesa,
            consec_docto_pedido_siesa=consec_docto_pedido_siesa,
            almacen_id=almacen_id,
            cliente=cliente,
            municipio=municipio,
            estado='PENDIENTE'
        )
        db.session.add(tarea)
        db.session.flush()

        for item_data in items:
            item = ItemPacking(
                tarea_id=tarea.id,
                producto_id=item_data['producto_id'],
                cantidad_esperada=item_data['cantidad'],
                lote=item_data.get('lote')
            )
            db.session.add(item)

        db.session.commit()
        return tarea

    @staticmethod
    def iniciar(tarea_id: int, empacador_id: int):
        """Empacador toma la tarea."""
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        # [29] Verificar que el pedido no fue anulado en Siesa antes de iniciar
        if getattr(tarea, 'pedido_anulado_siesa', False):
            raise ValueError('Pedido anulado en Siesa — no se puede iniciar packing')
        if tarea.estado != 'PENDIENTE':
            raise ValueError(f'No se puede iniciar una tarea en estado {tarea.estado}')

        tarea.estado = 'EN_PROCESO'
        tarea.empacador_id = empacador_id
        tarea.fecha_inicio = datetime.utcnow()
        db.session.commit()
        return tarea

    @staticmethod
    def escanear_item(tarea_id: int, producto_id: int, cantidad_real: int, lote: str = None):
        """
        El empacador escanea un ítem y registra la cantidad real.
        Este es el corazón del proceso de verificación.
        """
        from sqlalchemy.orm import joinedload as _jl
        from app.models.producto import Producto as _Prod
        tarea = (TareaPacking.query
                 .options(_jl(TareaPacking.items).joinedload(ItemPacking.producto))
                 .filter_by(id=tarea_id).first())
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.estado not in ['EN_PROCESO', 'PENDIENTE']:
            raise ValueError(f'No se puede escanear en estado {tarea.estado}')

        item = ItemPacking.query.filter_by(
            tarea_id=tarea_id,
            producto_id=producto_id
        ).first()

        if not item:
            raise ValueError(f'Producto {producto_id} no pertenece a esta tarea de packing')

        if cantidad_real > item.cantidad_esperada:
            raise ValueError(
                f'Exceso: la cantidad ({cantidad_real}) supera lo recogido en picking ({item.cantidad_esperada})'
            )

        item.cantidad_real = cantidad_real
        item.verificado = item.cantidad_real >= item.cantidad_esperada
        if lote:
            item.lote = lote

        # Alerta inmediata si hay diferencia
        alerta = None
        if item.tiene_diferencia():
            diferencia = item.diferencia()
            _nombre = item.producto.nombre if item.producto else f'Producto {item.producto_id}'
            if diferencia > 0:
                alerta = f'SOBRANTE: hay {diferencia} unidad(es) de más de {_nombre}'
            else:
                alerta = f'FALTANTE: faltan {abs(diferencia)} unidad(es) de {_nombre}'
            logger.warning(f'[PACKING] {alerta} en tarea {tarea.codigo}')

        db.session.commit()

        return {
            'item': item.to_dict(),
            'alerta': alerta,
            'items_pendientes': sum(1 for i in tarea.items if not i.verificado)
        }

    @staticmethod
    def confirmar_packing(tarea_id: int, observaciones: str = None, forzar: bool = False):
        """
        Paso 1: Verifica que todos los ítems fueron escaneados y guarda estado VERIFICADO.
        NO dispara Siesa — eso ocurre en cerrar_packing() después de declarar los bultos.
        """
        from sqlalchemy.orm import joinedload as _jl
        tarea = (TareaPacking.query
                 .options(_jl(TareaPacking.items).joinedload(ItemPacking.producto))
                 .filter_by(id=tarea_id).first())
        if not tarea:
            raise ValueError('Tarea no encontrada')

        # [29] El pedido puede anularse en Siesa DESPUÉS de que el empacador inició —
        # verificar aquí también evita que se registre un bulto para un pedido cancelado.
        if getattr(tarea, 'pedido_anulado_siesa', False):
            raise ValueError('Pedido anulado en Siesa — no se puede confirmar packing')

        if tarea.estado == 'DESPACHADO':
            raise ValueError('Este pedido ya fue despachado')
        if tarea.estado not in ['EN_PROCESO', 'PENDIENTE', 'VERIFICADO']:
            raise ValueError(f'No se puede confirmar en estado {tarea.estado}')

        items_sin_verificar = [i for i in tarea.items if not i.verificado]
        if items_sin_verificar:
            nombres = [
                (i.producto.nombre if i.producto else f'ID {i.producto_id}')
                for i in items_sin_verificar[:3]
            ]
            raise ValueError(f'Faltan por escanear: {", ".join(nombres)}')

        items_con_diferencia = [i for i in tarea.items if i.tiene_diferencia()]
        if items_con_diferencia and not forzar:
            diferencias = [{
                'producto': (i.producto.nombre if i.producto else f'Producto {i.producto_id}'),
                'esperado': i.cantidad_esperada,
                'real': i.cantidad_real,
                'diferencia': i.diferencia()
            } for i in items_con_diferencia]
            raise ValueError({
                'mensaje': 'Hay diferencias en el packing. Usa forzar=true para confirmar de todas formas.',
                'diferencias': diferencias
            })

        tarea.verificacion_exitosa = not bool(items_con_diferencia)
        tarea.observaciones = observaciones
        if tarea.estado != 'VERIFICADO':
            tarea.estado = 'VERIFICADO'
            tarea.fecha_verificado = datetime.utcnow()
        db.session.commit()

    @staticmethod
    def cerrar_packing(tarea_id: int, bultos_data: list):
        """
        Paso 2: El empacador declara las piezas físicas (bultos).
        Crea los Bultos, dispara Siesa y marca la tarea como DESPACHADO.
        bultos_data: [{'tipo': 'Caja', 'cantidad': 2}, {'tipo': 'Bolsa', 'cantidad': 1}]
        """
        from app.models.bulto import Bulto
        from sqlalchemy.orm import selectinload

        # Lectura previa SIN lock — solo para obtener datos del pre-check Siesa.
        # El HTTP a Siesa puede tardar hasta 30s; retener el lock ese tiempo bloquearía
        # cualquier otro worker que intente cerrar un packing simultáneamente.
        tarea_pre = TareaPacking.query.filter_by(id=tarea_id).first()
        if not tarea_pre:
            raise ValueError('Tarea no encontrada')

        # Validaciones rápidas antes del HTTP (evita llamadas inútiles a Siesa)
        if not bultos_data:
            raise ValueError('Debes declarar al menos una pieza')
        total = sum(int(b.get('cantidad', 1)) for b in bultos_data)
        if total < 1:
            raise ValueError('Total de piezas debe ser al menos 1')

        # Pre-verificar estado en Siesa ANTES de adquirir el lock de fila.
        # Solo se verifica si hay tipo_docto y consec válidos (pedido real de Siesa).
        if tarea_pre.tipo_docto_pedido_siesa and tarea_pre.consec_docto_pedido_siesa:
            logger.info(
                f'[PACKING] Pre-check Siesa para {tarea_pre.numero_pedido_siesa} '
                f'(tipo={tarea_pre.tipo_docto_pedido_siesa} consec={tarea_pre.consec_docto_pedido_siesa})'
            )
            estado_siesa = connekta.get_estado_pedido(
                tarea_pre.tipo_docto_pedido_siesa,
                tarea_pre.consec_docto_pedido_siesa
            )
            logger.info(
                f'[PACKING] Pre-check resultado: {tarea_pre.numero_pedido_siesa} → estado_siesa={estado_siesa}'
            )
            if estado_siesa is not None and str(estado_siesa) not in ('3', '4'):
                ESTADOS = {
                    '-1': 'No encontrado en Siesa (eliminado)',
                    '0': 'Ingresado (sin aprobar)',
                    '1': 'Aprobado',
                    '2': 'Aprobado',
                    '5': 'Anulado',
                    '9': 'Anulado / ya procesado en Siesa',
                }
                nombre_estado = ESTADOS.get(str(estado_siesa), f'desconocido (código {estado_siesa})')
                anulado = str(estado_siesa) in ('-1', '5', '9')
                if anulado:
                    logger.error(
                        f'[PACKING] ⛔ PRE-CHECK BLOQUEÓ cierre de {tarea_pre.numero_pedido_siesa}: '
                        f'estado_siesa={estado_siesa} ({nombre_estado}) — '
                        f'trigger_factura NO fue enviado a Siesa'
                    )
                    raise ValueError(
                        f'El pedido {tarea_pre.numero_pedido_siesa} está Anulado en Siesa '
                        f'(estado {estado_siesa}) — no se puede facturar. '
                        f'Cancelar este packing y esperar el pedido clonado del área comercial.'
                    )
                logger.warning(
                    f'[PACKING] ⚠ Pedido {tarea_pre.numero_pedido_siesa} en estado '
                    f'"{nombre_estado}" ({estado_siesa}) — se intenta trigger_factura de todas formas'
                )
            elif estado_siesa is None:
                logger.warning(
                    f'[PACKING] No se pudo verificar estado de {tarea_pre.numero_pedido_siesa} '
                    f'en Siesa — continuando de todas formas'
                )

        # Ahora sí — adquirir lock pesimista para el resto de la transacción.
        # El pre-check ya terminó; el lock solo cubre el tiempo de escritura en DB (<1s).
        tarea = (TareaPacking.query
                 .options(selectinload(TareaPacking.items).selectinload(ItemPacking.producto))
                 .filter_by(id=tarea_id)
                 .with_for_update()
                 .first())
        if not tarea:
            raise ValueError('Tarea no encontrada')
        # Guard de idempotencia: si Siesa ya confirmó, bloquear sin importar el estado
        if tarea.siesa_triggered:
            raise ValueError('Siesa ya procesó este despacho — verificar en ERP antes de reintentar')
        # Permitir retry si Siesa falló (VERIFICADO o DESPACHADO sin siesa_triggered)
        siesa_pendiente = tarea.estado == 'DESPACHADO' and not tarea.siesa_triggered
        if tarea.estado not in ['VERIFICADO'] and not siesa_pendiente:
            raise ValueError('El packing debe estar VERIFICADO antes de cerrar')

        # Crear bultos solo si no existen aún (idempotente en caso de reintento)
        bultos_existentes = Bulto.query.filter_by(tarea_id=tarea_id).all()
        if not bultos_existentes:
            numero = 1
            for b in bultos_data:
                tipo = b.get('tipo', 'Caja')
                cantidad = int(b.get('cantidad', 1))
                for _ in range(cantidad):
                    bulto = Bulto(
                        tarea_id=tarea_id,
                        codigo_barras=f'{tarea.numero_pedido_siesa}-{numero:02d}',
                        tipo=tipo,
                        numero=numero,
                        total=total
                    )
                    db.session.add(bulto)
                    numero += 1

        # [11] Commit bultos + SiesaJob de respaldo en la MISMA transacción antes de llamar a Siesa.
        # Así, si Siesa falla, el SiesaJob queda PENDIENTE para reintento automático (DLQ).
        from app.models.siesa_job import SiesaJob as _SiesaJob
        db.session.flush()  # obtener IDs de bultos antes del commit
        bultos_existentes = Bulto.query.filter_by(tarea_id=tarea_id).all()

        # Construir payload para Siesa — incluir item_id_siesa y unidad_medida
        from app.models.pedido_siesa import PedidoSiesa
        # [25] Pre-cargar todos los registros PedidoSiesa del pedido en un dict (evita N+1 en loop)
        regs_siesa_qs = PedidoSiesa.query.filter_by(
            numero_pedido=tarea.numero_pedido_siesa
        ).all()
        regs_siesa_map = {r.item_codigo: r for r in regs_siesa_qs}

        items_payload = []
        for i in tarea.items:
            if not i.producto:
                logger.error(f'[PACKING] ItemPacking {i.id} sin producto (producto_id={i.producto_id}) — saltando del payload Siesa')
                continue
            if not i.producto.codigo_siesa:
                raise ValueError(
                    f'Producto {i.producto.codigo} (id={i.producto_id}) no tiene codigo_siesa. '
                    'Configura el campo en el catálogo de productos antes de cerrar packing.'
                )
            codigo = i.producto.codigo_siesa
            # Buscar el ID interno de Siesa para este producto en este pedido
            reg_siesa = regs_siesa_map.get(codigo)
            items_payload.append({
                'producto_codigo': codigo,
                'cantidad_empacada': i.cantidad_real if i.cantidad_real is not None else i.cantidad_esperada,
                'cantidad_pedida': i.cantidad_esperada,
                'lote': i.lote or '',
                'item_id_siesa': reg_siesa.item_id_siesa if reg_siesa else '',
                'unidad_medida': i.producto.unidad_medida or ''
            })

        # Validar datos Siesa antes de crear job — un payload inválido causaría DLQ permanente
        if not tarea.tipo_docto_pedido_siesa:
            raise ValueError(
                f'Tarea {tarea_id} no tiene tipo_docto_pedido_siesa — '
                'el pedido no tiene datos Siesa válidos. Contacta al administrador.'
            )
        # [A14] Validar consec_docto_pedido_siesa — sin consecutivo, Siesa no puede localizar el pedido
        if not tarea.consec_docto_pedido_siesa:
            raise ValueError(
                f'Tarea {tarea_id} no tiene consec_docto_pedido_siesa — '
                'el pedido no tiene consecutivo Siesa válido. Contacta al administrador.'
            )

        # TRIGGER A SIESA — 238925 FacturaPedido → factura FE + remisión automática
        consec_para_siesa = tarea.consec_docto_pedido_siesa
        logger.info(
            f'[PACKING] ▶ Enviando trigger_factura a Siesa: '
            f'pedido={tarea.numero_pedido_siesa} '
            f'tipo_docto={tarea.tipo_docto_pedido_siesa!r} '
            f'consec={consec_para_siesa!r} '
            f'items={len(items_payload)} '
            f'bultos={total}'
        )

        # [P8] Crear SiesaJob de respaldo ANTES del commit — atómico con los bultos.
        # Si el commit falla, ni los bultos ni el job persisten (sin pérdida silenciosa).
        # Si Siesa falla después, el job ya está en DB como PENDIENTE para DLQ.
        # En reintento (siesa_pendiente=True) puede existir ya un job activo — reutilizarlo.
        # [C3] Incluir PROCESANDO en la deduplicación — si el worker DLQ ya tomó el job
        # y está ejecutándolo, crear uno nuevo causaría doble envío a Siesa.
        # [CRÍTICO] Incluir FALLIDO — sin esto, tras resetear_siesa() el job FALLIDO queda
        # huérfano y se crea un job nuevo: Siesa factura dos veces el mismo pedido.
        # Si hay un job FALLIDO, reutilizarlo (resetear a PENDIENTE) en vez de crear uno nuevo.
        job_dlq = _SiesaJob.query.filter(
            _SiesaJob.tipo == 'DESPACHO_F470',
            _SiesaJob.referencia_tipo == 'TareaPacking',
            _SiesaJob.referencia_id == tarea_id,
            _SiesaJob.estado.in_(['PENDIENTE', 'PROCESANDO', 'REINTENTANDO', 'FALLIDO']),
        ).first()
        if job_dlq and job_dlq.estado == 'FALLIDO':
            # Reusar el job fallido: resetear a PENDIENTE con el payload actualizado.
            # Esto evita crear un job nuevo que duplicaría el envío a Siesa.
            job_dlq.estado = 'PENDIENTE'
            job_dlq.intentos = 0
            job_dlq.proximo_intento = None
            job_dlq.error_ultimo = None
            job_dlq.payload = json.dumps({
                'tarea_id': tarea_id,
                'tipo_docto_pedido': tarea.tipo_docto_pedido_siesa or '',
                'consec_docto_pedido': consec_para_siesa,
                'items': items_payload,
                'numero_pedido_siesa': tarea.numero_pedido_siesa,
            }, ensure_ascii=False)
            logger.warning(
                f'[PACKING] Job FALLIDO {job_dlq.id} reutilizado (reset a PENDIENTE) '
                f'para tarea {tarea_id} — evita doble factura a Siesa'
            )
        elif not job_dlq:
            job_dlq = _SiesaJob.encolar(
                tipo='DESPACHO_F470',
                payload={
                    'tarea_id': tarea_id,
                    'tipo_docto_pedido': tarea.tipo_docto_pedido_siesa or '',
                    'consec_docto_pedido': consec_para_siesa,
                    'items': items_payload,
                    'numero_pedido_siesa': tarea.numero_pedido_siesa,
                },
                referencia_tipo='TareaPacking',
                referencia_id=tarea_id,
            )
        # Commit bultos + SiesaJob en una sola transacción
        db.session.commit()

        # Disparar DLQ en hilo daemon — procesa el job recién encolado sin bloquear el worker.
        # Si Siesa falla, el job queda PENDIENTE para reintento automático cada 5 min.
        # El advisory lock en procesar_jobs_pendientes evita ejecuciones concurrentes.
        from app.services.siesa_job_service import disparar_dlq_inmediato
        disparar_dlq_inmediato()

        logger.info(
            f'[PACKING] bultos={total} pedido={tarea.numero_pedido_siesa} — '
            f'job_dlq={job_dlq.id} encolado, DLQ disparado async'
        )
        return bultos_existentes

    @staticmethod
    def cancelar(tarea_id: int, motivo: str = None):
        """Cancela una tarea de packing."""
        from app.models.bulto import Bulto
        from app.models.siesa_job import SiesaJob as _SJ
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.estado == 'DESPACHADO' and tarea.siesa_triggered:
            raise ValueError('No se puede cancelar — Siesa ya generó la remisión')

        # [C2] Bloquear cancelación si hay un SiesaJob activo (PENDIENTE/PROCESANDO/REINTENTANDO).
        # Cancelar mientras Siesa está procesando podría dejar la remisión creada en Siesa
        # sin reflejo en el WMS — inconsistencia imposible de detectar automáticamente.
        job_activo = _SJ.query.filter_by(
            referencia_tipo='TareaPacking',
            referencia_id=tarea_id,
        ).filter(_SJ.estado.in_(['PENDIENTE', 'PROCESANDO', 'REINTENTANDO'])).first()
        if job_activo:
            raise ValueError(
                f'No se puede cancelar — hay un job Siesa {job_activo.estado} (id={job_activo.id}). '
                'Espera a que termine o falle definitivamente antes de cancelar.'
            )

        # Si tiene bultos sin cargar, eliminarlos antes de cancelar
        Bulto.query.filter_by(tarea_id=tarea_id, estado='PENDIENTE').delete()
        tarea.estado = 'CANCELADO'
        tarea.observaciones = motivo
        db.session.commit()
        return tarea

    @staticmethod
    def resetear_siesa(tarea_id: int):
        """
        Elimina los bultos pendientes y vuelve el estado a VERIFICADO
        para poder reintentar el cierre con Siesa.
        Solo aplica cuando Siesa falló (siesa_triggered=False).
        """
        from app.models.bulto import Bulto
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.siesa_triggered:
            raise ValueError('Siesa ya procesó esta tarea')
        if tarea.estado not in ['VERIFICADO', 'DESPACHADO']:
            raise ValueError('Solo se puede resetear una tarea VERIFICADA o con error Siesa')

        # Bloquear reset si hay bultos ya entregados al cliente —
        # borrarlos eliminaría el registro de la entrega.
        bultos_entregados = Bulto.query.filter_by(
            tarea_id=tarea_id, estado='ENTREGADO'
        ).count()
        if bultos_entregados:
            raise ValueError(
                f'No se puede resetear: {bultos_entregados} bulto(s) ya entregados al cliente. '
                'Usa el retry de Siesa en su lugar.'
            )

        Bulto.query.filter_by(tarea_id=tarea_id).delete()
        tarea.estado = 'VERIFICADO'
        tarea.siesa_response = None
        db.session.commit()
        return tarea