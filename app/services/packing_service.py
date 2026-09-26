"""
Servicio de Packing con verificación ítem por ítem y trigger automático a Siesa.
Flujo: Crear → Iniciar → Escanear ítems → Confirmar → Siesa factura solo.
"""
import uuid
import json
from datetime import datetime
from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking, EstadoPacking
from app.models.picking import TareaPicking
from app.services.connekta_gateway import connekta
from app.services.cadena_pedido import clave_de_documento
import logging
from app.utils.fecha import ahora_bogota as _ahora_bogota

logger = logging.getLogger(__name__)


class CierreNoEmitido(ValueError):
    """El cierre no emitió por algo que NO es un error del empaque: la caja está
    retenida por cartera, o Siesa no está disponible. Subclase de `ValueError`
    para que ningún llamador viejo cambie de rama; la ruta y la cola offline
    la leen para decirlo con sus palabras (`estado`)."""

    def __init__(self, mensaje, estado, retencion_id=None):
        super().__init__(mensaje)
        self.estado = estado
        self.retencion_id = retencion_id


class PackingService:

    @staticmethod
    def crear_desde_picking(tareas_picking_ids: list, numero_pedido_siesa: str, almacen_id: int,
                            tipo_docto_pedido_siesa: str = '', consec_docto_pedido_siesa: str = ''):
        """
        Crea una tarea de packing a partir de tareas de picking completadas.
        Agrupa todos los ítems del pedido en una sola tarea de packing.
        """
        from app.extensions import db as _db
        tareas_picking = TareaPicking.query.filter(
            TareaPicking.id.in_(tareas_picking_ids),
            _db.or_(
                _db.and_(
                    TareaPicking.estado == 'COMPLETADO',
                    TareaPicking.cantidad_recogida > 0
                ),
                _db.and_(
                    TareaPicking.estado == 'BLOQUEADO',
                    TareaPicking.cantidad_recogida > 0
                )
            )
        ).all()

        if not tareas_picking:
            raise ValueError('No hay tareas de picking con unidades recogidas para empacar')

        # Verificar que no exista ya un packing para este pedido
        existente = TareaPacking.query.filter_by(
            numero_pedido_siesa=numero_pedido_siesa
        ).filter(TareaPacking.estado.notin_(['CANCELADO'])).first()

        if existente:
            raise ValueError(f'Ya existe una tarea de packing para el pedido {numero_pedido_siesa}')
        # Una caja cancelada del mismo pedido con remisión o factura: esta
        # emitiría la segunda.
        from app.services.documento_fiscal import exigir_pedido_sin_documento
        exigir_pedido_sin_documento(numero_pedido_siesa, 'crear otra caja')

        codigo = f'PACK-{_ahora_bogota().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        tarea = TareaPacking(
            codigo=codigo,
            numero_pedido_siesa=numero_pedido_siesa,
            tipo_docto_pedido_siesa=tipo_docto_pedido_siesa,
            consec_docto_pedido_siesa=consec_docto_pedido_siesa,
            # La misma clave que sus tareas de picking (m036fotos).
            pedido_clave=clave_de_documento(
                numero_pedido=numero_pedido_siesa, tipo=tipo_docto_pedido_siesa,
                consec=consec_docto_pedido_siesa, almacen_id=almacen_id),
            almacen_id=almacen_id,
            estado='PENDIENTE'
        )
        db.session.add(tarea)
        db.session.flush()
        # Clasificación de cobro lo antes posible: la condición del pedido que
        # el sync ya guardó. Sin red y sin inventar: si no hay, queda NULL.
        from app.services import cond_pago as _cp_hist
        _cp_hist.anotar_desde_historia(tarea)

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
        from app.services.documento_fiscal import exigir_pedido_sin_documento
        exigir_pedido_sin_documento(numero_pedido_siesa, 'crear otra caja')

        codigo = f'PACK-{_ahora_bogota().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        tarea = TareaPacking(
            codigo=codigo,
            numero_pedido_siesa=numero_pedido_siesa,
            tipo_docto_pedido_siesa=tipo_docto_pedido_siesa,
            consec_docto_pedido_siesa=consec_docto_pedido_siesa,
            # La misma clave que sus tareas de picking (m036fotos).
            pedido_clave=clave_de_documento(
                numero_pedido=numero_pedido_siesa, tipo=tipo_docto_pedido_siesa,
                consec=consec_docto_pedido_siesa, almacen_id=almacen_id),
            almacen_id=almacen_id,
            cliente=cliente,
            municipio=municipio,
            estado='PENDIENTE'
        )
        db.session.add(tarea)
        db.session.flush()
        # La condición del pedido desde la historia del sync, como en
        # `crear_desde_picking`. Sin esto, todo packing de «Aprobar pedido»
        # llegaba al cierre con `cond_pago = NULL` y la compuerta de cartera
        # G2 lo daba por contado supuesto (NO_APLICA), también un C04.
        from app.services import cond_pago as _cp_hist
        _cp_hist.anotar_desde_historia(tarea)

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
        """
        Empacador toma la tarea.

        with_for_update(): sin este lock, dos empacadores haciendo clic en la
        misma card casi al tiempo pasaban los dos el check de abajo (ambos
        leían PENDIENTE antes de que cualquiera confirmara) y el commit que
        llegaba último pisaba en silencio el empacador_id del primero — los
        dos recibían 200 OK, uno de los dos quedaba "dueño" de una tarea que
        cree tener pero ya no es suya. TareaPicking ya resolvía esto mismo con
        with_for_update(skip_locked=True) en el dispensador; TareaPacking
        nunca lo había heredado.
        """
        tarea = TareaPacking.query.filter_by(id=tarea_id).with_for_update().first()
        if not tarea:
            raise ValueError('Tarea no encontrada')
        # [29] Verificar que el pedido no fue anulado en Siesa antes de iniciar
        if getattr(tarea, 'pedido_anulado_siesa', False):
            raise ValueError('Pedido anulado en Siesa — no se puede iniciar packing')
        if tarea.estado != 'PENDIENTE':
            raise ValueError(f'No se puede iniciar una tarea en estado {tarea.estado}')

        tarea.estado = EstadoPacking.EN_PROCESO
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
        # verificado = True solo al alcanzar/superar lo esperado — igual que
        # mobile_service.confirmar_tarea. Antes era `is not None`, que marcaba
        # el ítem como listo tras el primer escaneo aunque faltaran unidades.
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
    def confirmar_packing(tarea_id: int, observaciones: str = None, forzar: bool = False,
                          usuario_id: int = None):
        """
        Paso 1: Verifica que todos los ítems fueron escaneados y guarda estado VERIFICADO.
        NO dispara Siesa — eso ocurre en cerrar_packing() después de declarar los bultos.

        ## Por qué la propiedad se verifica ACÁ y no solo en la ruta

        `/api/packing/<id>/confirmar` comprobaba permiso de empaque **y**
        propiedad de la tarea. `/api/mobile/confirmar` con `tipo='PACKING'`
        llamaba a este servicio **sin pasar el usuario**: cualquier operario
        podía confirmar el packing de otro, y sin permiso de empaque.

        El guard estaba en la capa equivocada. Picking ya lo tenía bien
        —`confirmar_picking` verifica la propiedad dentro del servicio, así que
        toda vía lo hereda— y packing lo tenía solo en una de sus dos puertas.

        `usuario_id=None` se conserva para los llamadores internos (arneses,
        scripts) y **se declara en el log**: un bypass silencioso es lo que
        acaba de costar esto.
        """
        from sqlalchemy.orm import joinedload as _jl
        tarea = (TareaPacking.query
                 .options(_jl(TareaPacking.items).joinedload(ItemPacking.producto))
                 .filter_by(id=tarea_id).first())
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if usuario_id is None:
            logger.info(
                '[PACKING] confirmar_packing tarea=%s SIN usuario — no se '
                'verifica propiedad (llamador interno)', tarea_id)
        else:
            from app.models.usuario import Usuario as _U
            # `_puede_empacar` es la MISMA función que usa la ruta directa: el
            # permiso de empaque incluye el flag `puede_empacar`, no solo el
            # rol. Reimplementarlo acá sería la divergencia de siempre.
            from app.routes._auth_helpers import Roles as _R, _puede_empacar
            _u = _U.query.get(usuario_id)
            if not _u or not _u.activo:
                raise ValueError('Usuario no válido para confirmar packing')
            if not _puede_empacar(_u):
                raise ValueError(f'El rol "{_u.rol}" no puede confirmar packing')
            if _u.rol not in (_R.ADMIN, _R.SUPERVISOR, _R.JEFE_ALMACEN):
                if tarea.empacador_id and tarea.empacador_id != usuario_id:
                    raise ValueError('Esta tarea pertenece a otro empacador')

        # [29] El pedido puede anularse en Siesa DESPUÉS de que el empacador inició —
        # verificar aquí también evita que se registre un bulto para un pedido cancelado.
        if getattr(tarea, 'pedido_anulado_siesa', False):
            raise ValueError('Pedido anulado en Siesa — no se puede confirmar packing')

        if tarea.estado == 'DESPACHADO':
            raise ValueError('Este pedido ya fue despachado')
        # Desde m048fiscal la caja cerrada queda VERIFICADA hasta que Siesa
        # emite: re-confirmarla cambiaría cantidades de una caja que ya tiene
        # (o puede tener) remisión.
        from app.services.documento_fiscal import exigir_sin_documento
        exigir_sin_documento(tarea, 'volver a confirmar el empaque')
        if tarea.estado not in ['EN_PROCESO', 'PENDIENTE', 'VERIFICADO']:
            raise ValueError(f'No se puede confirmar en estado {tarea.estado}')

        items_sin_verificar = [i for i in tarea.items if not i.verificado]
        if items_sin_verificar:
            if not forzar:
                nombres = [
                    (i.producto.nombre if i.producto else f'ID {i.producto_id}')
                    for i in items_sin_verificar[:3]
                ]
                raise ValueError(f'Faltan por escanear: {", ".join(nombres)}')
            for item in items_sin_verificar:
                item.cantidad_real = 0
                item.verificado = True

        items_con_diferencia = [i for i in tarea.items if i.tiene_diferencia()]
        if items_con_diferencia and not forzar:
            diferencias = [{
                'producto': (i.producto.nombre if i.producto else f'Producto {i.producto_id}'),
                'esperado': i.cantidad_esperada,
                'real': i.cantidad_real,
                'diferencia': i.diferencia()
            } for i in items_con_diferencia]
            raise ValueError({
                'mensaje': 'Hay diferencias en el packing. Use forzar=true para confirmar de todas formas.',
                'diferencias': diferencias
            })

        tarea.verificacion_exitosa = not bool(items_con_diferencia)
        tarea.observaciones = observaciones
        if tarea.estado != EstadoPacking.VERIFICADO:
            tarea.estado = EstadoPacking.VERIFICADO
            tarea.fecha_verificado = datetime.utcnow()
        db.session.commit()

    @staticmethod
    def cerrar_packing(tarea_id: int, bultos_data: list, usuario_id: int = None):
        """
        Empacador declara bultos y cierra la caja.
        Delega al closer correspondiente según tipo_documento (PEDIDO | TRASLADO).
        bultos_data: [{'tipo': 'Caja', 'cantidad': 2}, {'tipo': 'Bolsa', 'cantidad': 1}]
        """
        from app.services.closing.factory import PackingCloserFactory
        tarea = TareaPacking.query.filter_by(id=tarea_id).first()
        if not tarea:
            raise ValueError('Tarea no encontrada')
        closer = PackingCloserFactory.get(tarea.tipo_documento or 'PEDIDO')
        resultado = closer.ejecutar_cierre(tarea_id, bultos_data, usuario_id or 0)
        if not resultado.exitoso:
            if resultado.estado:
                raise CierreNoEmitido(resultado.error or resultado.mensaje,
                                      resultado.estado, resultado.retencion_id)
            raise ValueError(resultado.error or resultado.mensaje)
        return resultado

    @staticmethod
    def cerrar_packing_resultado(tarea_id: int, bultos_data: list, usuario_id: int = None):
        """
        Cierra el packing y arma el dict de respuesta — compartido entre la ruta HTTP
        (`POST /api/packing/<id>/cerrar`) y la sincronización offline (`/api/mobile/sync`),
        para que ambas vías queden idénticas y no diverjan con el tiempo.
        """
        from app.models.bulto import Bulto
        from sqlalchemy.orm import selectinload
        PackingService.cerrar_packing(tarea_id=tarea_id, bultos_data=bultos_data, usuario_id=usuario_id)
        tarea = db.session.get(TareaPacking, tarea_id)
        # Re-query con eager load — expire_on_commit invalida los objetos que devolvió
        # cerrar_packing; b.to_dict() accede b.tarea (lazy) sin esto → N+1
        bultos_resp = (Bulto.query
                       .options(selectinload(Bulto.tarea))
                       .filter_by(tarea_id=tarea_id).all())
        return {
            'ok': True,
            'mensaje': (
                f'{len(bultos_resp)} pieza(s) registradas — Siesa confirmó la remisión'
                if tarea.siesa_triggered else
                # No promete lo que no pasó: la caja espera la remisión y la
                # factura (o a cartera) y solo entonces sale al muelle.
                f'{len(bultos_resp)} pieza(s) registradas — la remisión y la factura se '
                f'están emitiendo en Siesa; la caja sale al muelle cuando estén confirmadas'
            ),
            # Lo que la pantalla dice, sin adivinar: CONFIRMADO solo si Siesa ya
            # contestó; EN_COLA es «se encoló, todavía no se sabe». Antes el
            # empacador leía «Siesa procesó la factura» en los dos.
            'estado_siesa': 'CONFIRMADO' if tarea.siesa_triggered else 'EN_COLA',
            'siesa_triggered': tarea.siesa_triggered,
            'numero_pedido': tarea.numero_pedido_siesa,
            'cliente': tarea.cliente or '',
            'municipio': tarea.municipio or '',
            'bultos': [b.to_dict() for b in bultos_resp],
        }

    @staticmethod
    def _cerrar_packing_pedido_legacy(tarea_id: int, bultos_data: list):
        """Lógica original de cierre PD — mantenida para referencia interna."""
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
            # Retry path: la route ya verificó que existen bultos en DB; recalcular total.
            from app.models.bulto import Bulto as _BultoCheck
            bultos_previos = _BultoCheck.query.filter_by(tarea_id=tarea_id).all()
            if not bultos_previos:
                raise ValueError('Debe declarar al menos una pieza')
            total = len(bultos_previos)
        else:
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
            from app.services import estado_pedido_siesa as _eps
            if estado_siesa is not None and str(estado_siesa) not in ('3', '4'):
                nombre_estado = _eps.nombre(estado_siesa)
                anulado = _eps.impide_facturar(estado_siesa)
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

            # Guard anti-duplicado: verificar si ya existe factura activa en Siesa
            facturas_activas = connekta.get_factura_desde_pedido(
                tarea_pre.tipo_docto_pedido_siesa,
                tarea_pre.consec_docto_pedido_siesa
            )
            if facturas_activas:
                f0 = facturas_activas[0]
                tipo_f  = f0.get('f350_id_tipo_docto', '?')
                consec_f = f0.get('f350_consec_docto', '?')
                logger.error(
                    f'[PACKING] ⛔ Factura duplicada detectada para '
                    f'{tarea_pre.numero_pedido_siesa}: {tipo_f}{consec_f} '
                    f'ya existe en Siesa — trigger_factura bloqueado'
                )
                raise ValueError(
                    f'El pedido {tarea_pre.numero_pedido_siesa} ya tiene una factura activa '
                    f'en Siesa ({tipo_f}{consec_f}) — verificar en ERP antes de continuar.'
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
        from app.models.siesa_job import SiesaJob as _SiesaJob, EstadoSiesaJob as _ESJ
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
                # [M19] Raise instead of silently skipping — a partial factura in Siesa
                # would leave inventory and accounting inconsistent with the physical shipment.
                raise ValueError(
                    f'ItemPacking {i.id} referencia producto_id={i.producto_id} que no existe en BD. '
                    'Corregir el catálogo de productos antes de cerrar packing.'
                )
            if not i.producto.codigo_siesa:
                raise ValueError(
                    f'Producto {i.producto.codigo} (id={i.producto_id}) no tiene codigo_siesa. '
                    'Configure el campo en el catálogo de productos antes de cerrar packing.'
                )
            codigo = i.producto.codigo_siesa
            # Buscar el ID interno de Siesa para este producto en este pedido
            reg_siesa = regs_siesa_map.get(codigo)
            # ── Normalización UND→UOM_Siesa ───────────────────────────────────
            # Tras el fix de dual-unit (commit 6f20698), cantidad_real se almacena
            # en UND (p. ej. 20 UND = 2 PQ × factor 10). Siesa espera la cantidad
            # en la unidad de empaque del pedido (PQ). Convertir aquí para que
            # 244328 y 142945 reciban el valor correcto (2 PQ, no 20 PQ).
            # Regla: si factor > 1 y unidad_empaque existe y cantidad >= factor
            #        → la cantidad está en UND → dividir por factor → PQ.
            # Productos simples (factor=1): sin cambio.
            _fc_pay  = i.producto.factor_conversion or 1
            _emp_pay = i.producto.unidad_empaque or ''
            _cant_raw = (
                i.cantidad_real if i.cantidad_real is not None else i.cantidad_esperada
            )
            if _fc_pay > 1 and _emp_pay and _cant_raw >= _fc_pay:
                _cant_siesa = round(_cant_raw / _fc_pay, 4)   # UND → PQ
                _uom_siesa  = _emp_pay
            else:
                _cant_siesa = _cant_raw                        # ya en UOM correcta
                _uom_siesa  = i.producto.unidad_medida or ''
            # ─────────────────────────────────────────────────────────────────
            items_payload.append({
                'producto_codigo': codigo,
                'cantidad_empacada': _cant_siesa,
                'cantidad_pedida': i.cantidad_esperada,
                'lote': i.lote or '',
                'item_id_siesa': reg_siesa.item_id_siesa if reg_siesa else '',
                'unidad_medida': _uom_siesa
            })

        # Validar datos Siesa antes de crear job — un payload inválido causaría DLQ permanente
        if not tarea.tipo_docto_pedido_siesa:
            raise ValueError(
                f'Tarea {tarea_id} no tiene tipo_docto_pedido_siesa — '
                'el pedido no tiene datos Siesa válidos. Contacte al administrador.'
            )
        # [A14] Validar consec_docto_pedido_siesa — sin consecutivo, Siesa no puede localizar el pedido
        if not tarea.consec_docto_pedido_siesa:
            raise ValueError(
                f'Tarea {tarea_id} no tiene consec_docto_pedido_siesa — '
                'el pedido no tiene consecutivo Siesa válido. Contacte al administrador.'
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
            _SiesaJob.estado.in_(list(_ESJ.ACTIVOS) + [_ESJ.FALLIDO]),
        ).first()
        if job_dlq and job_dlq.estado == _ESJ.FALLIDO:
            # Reusar el job fallido: resetear a PENDIENTE con el payload actualizado.
            # Esto evita crear un job nuevo que duplicaría el envío a Siesa.
            from app.services.siesa_job_service import reencolar_job_fallido
            reencolar_job_fallido(
                job_dlq, origen='PackingService._cerrar_packing_pedido_legacy',
                motivo='Reintento del cierre de packing',
                payload=json.dumps({
                    'tarea_id': tarea_id,
                    'tipo_docto_pedido': tarea.tipo_docto_pedido_siesa or '',
                    'consec_docto_pedido': consec_para_siesa,
                    'items': items_payload,
                    'numero_pedido_siesa': tarea.numero_pedido_siesa,
                }, ensure_ascii=False))
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
    def cancelar(tarea_id: int, motivo: str = None, usuario_id: int = None):
        """Cancela una tarea de packing.

        El motivo es obligatorio. Antes pisaba `observaciones` y no dejaba
        quién: ahora `observaciones` conserva lo que tenía más el motivo, y la
        bitácora guarda quién, cuándo, el antes y cada bulto borrado.
        """
        from app.models.bulto import Bulto
        from app.models.siesa_job import SiesaJob as _SJ
        from app.services.bitacora import registrar_accion, motivo_obligatorio, foto
        from app.services.documento_fiscal import exigir_sin_documento
        motivo = motivo_obligatorio(motivo, 'cancelar un empaque')
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        # Con remisión, factura o un 142945 sin confirmar, cancelar deja el
        # documento en Siesa sin caja en el WMS — y la caja que se abra
        # después para el mismo pedido emite el segundo.
        exigir_sin_documento(tarea, 'cancelar el empaque')

        # [C2] Bloquear cancelación si hay un SiesaJob activo (PENDIENTE/PROCESANDO/REINTENTANDO).
        # Cancelar mientras Siesa está procesando podría dejar la remisión creada en Siesa
        # sin reflejo en el WMS — inconsistencia imposible de detectar automáticamente.
        #
        # Excepción: el job PENDIENTE que solo espera a cartera (la retención
        # frenó la emisión ANTES del 244328, sin documento). Esa espera no
        # tiene fin propio; cancelar la caja es su salida: el job queda
        # DESCARTADO y la retención CANCELADA, las dos con bitácora.
        job_activo = (_SJ.query.filter_by(referencia_tipo='TareaPacking', referencia_id=tarea_id)
                      .filter(_SJ.estado.in_(['PENDIENTE', 'PROCESANDO', 'REINTENTANDO']))
                      .with_for_update().first())
        retenido = None
        if job_activo is not None:
            retenido = PackingService._retencion_que_frena(tarea, job_activo)
        if job_activo is not None and retenido is None:
            raise ValueError(
                f'No se puede cancelar — hay un job Siesa {job_activo.estado} (id={job_activo.id}). '
                'Espere a que termine o falle definitivamente antes de cancelar.'
            )
        if retenido is not None:
            from app.services import cartera_service as _cartera
            from app.services.siesa_job_service import descartar_job_retenido
            descartar_job_retenido(job_activo, usuario_id=usuario_id,
                                   motivo=f'Caja cancelada: {motivo}')
            _cartera.cancelar(retenido, f'La caja {tarea.codigo} se canceló: {motivo}',
                              usuario_id=usuario_id, origen='WMS')

        # Si tiene bultos sin cargar, eliminarlos antes de cancelar — cada uno
        # deja su fila completa en la bitácora: es lo único que queda de él.
        antes = foto(tarea, ['estado', 'observaciones', 'empacador_id'])
        for b in Bulto.query.filter_by(tarea_id=tarea_id, estado='PENDIENTE').all():
            registrar_accion('ELIMINAR', b, usuario_id=usuario_id, motivo=motivo,
                             antes=foto(b), almacen_id=tarea.almacen_id)
        Bulto.query.filter_by(tarea_id=tarea_id, estado='PENDIENTE').delete()
        tarea.estado = EstadoPacking.CANCELADO
        tarea.observaciones = (f'{tarea.observaciones} | Cancelado: {motivo}'
                               if tarea.observaciones else motivo)
        registrar_accion('CANCELAR', tarea, usuario_id=usuario_id, motivo=motivo,
                         antes=antes, despues=foto(tarea, ['estado', 'observaciones']))
        db.session.commit()
        return tarea

    @staticmethod
    def _retencion_que_frena(tarea, job):
        """La retención de cartera viva que tiene al job esperando, o `None`.

        Solo cuenta si el job está PENDIENTE (no ejecutándose) y la caja no
        tiene documento en Siesa: la compuerta de emisión frena ANTES del
        244328."""
        from app.services.documento_fiscal import tiene_documento_en_siesa
        if job is None or job.tipo != 'DESPACHO_F470' or job.estado != 'PENDIENTE':
            return None
        if tiene_documento_en_siesa(tarea) or not tarea.pedido_clave:
            return None
        from app.services import cartera_service as _cartera
        return _cartera.retencion_viva(tarea.pedido_clave)

    @staticmethod
    def resetear_siesa(tarea_id: int, usuario_id: int = None, motivo: str = None):
        """
        Elimina los bultos pendientes y vuelve el estado a VERIFICADO
        para poder reintentar el cierre con Siesa.
        Solo aplica cuando Siesa falló (siesa_triggered=False).

        Limpieza técnica con razón propia (Siesa falló): el motivo es opcional
        porque la pantalla no lo pide, y el porqué real —`siesa_response`, que
        esta función borra— queda en el `antes` de la bitácora.
        """
        from app.models.bulto import Bulto
        from app.services.bitacora import registrar_accion, foto
        from app.services.documento_fiscal import exigir_sin_documento
        tarea = TareaPacking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        # Resetear borra los bultos y vuelve a VERIFICADO para re-cerrar: con
        # una remisión ya creada, el re-cierre sería la segunda.
        exigir_sin_documento(tarea, 'resetear el envío a Siesa')
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
                'Use el reintento de Siesa en su lugar.'
            )

        _motivo = motivo or 'Reset tras fallo de Siesa: redeclarar piezas'
        for b in Bulto.query.filter_by(tarea_id=tarea_id).all():
            registrar_accion('ELIMINAR', b, usuario_id=usuario_id, motivo=_motivo,
                             antes=foto(b), almacen_id=tarea.almacen_id)
        antes = foto(tarea, ['estado', 'siesa_response'])
        Bulto.query.filter_by(tarea_id=tarea_id).delete()
        tarea.estado = EstadoPacking.VERIFICADO
        tarea.siesa_response = None
        registrar_accion('REABRIR', tarea, usuario_id=usuario_id, motivo=_motivo,
                         antes=antes, despues={'estado': tarea.estado})
        db.session.commit()
        return tarea