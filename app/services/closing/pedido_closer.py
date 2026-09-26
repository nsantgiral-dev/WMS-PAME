"""PedidoPackingCloser — cierre de caja para Pedidos (PD).

Crea los bultos y encola el DESPACHO_F470 (244328 → 142945 → 142943, en el
DLQ). **No marca DESPACHADO**: eso lo hace la emisión cuando la remisión y la
factura existen. Sin Siesa disponible el cierre se niega sin tocar nada.
"""
import json
import logging

from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.models.siesa_job import SiesaJob, EstadoSiesaJob
from app.services.connekta_gateway import connekta
from .base import (IPackingCloser, CierreResult, MotivoSiesaNoDisponible,
                   RETENIDO_CARTERA, SIESA_NO_DISPONIBLE)

logger = logging.getLogger(__name__)


class PedidoPackingCloser(IPackingCloser):

    def ejecutar_cierre(self, tarea_id: int, bultos_data: list,
                        usuario_id: int) -> CierreResult:
        from sqlalchemy.orm import selectinload
        from app.models.bulto import Bulto

        tarea_pre = TareaPacking.query.filter_by(id=tarea_id).first()
        if not tarea_pre:
            return CierreResult(exitoso=False, error='Tarea no encontrada',
                                mensaje='Tarea no encontrada')

        total = self._calcular_total(tarea_id, bultos_data)
        if isinstance(total, str):  # es un mensaje de error
            return CierreResult(exitoso=False, error=total, mensaje=total)

        # Decisión del dueño (2026-09-25): sin Siesa no se factura, y sin
        # factura la caja no se cierra. Se niega ANTES de tocar nada —ni
        # bultos, ni job, ni estado—: la caja queda VERIFICADA, esperando.
        from app.services import documento_fiscal as _doc
        _disponible, _motivo = _doc.siesa_disponible_para_facturar()
        if not _disponible:
            return CierreResult(exitoso=False, error=_motivo, mensaje=_motivo,
                                estado=SIESA_NO_DISPONIBLE)

        # El 142945 salió en un intento anterior y la remisión no se
        # identificó: re-cerrar re-encolaría el job, y el job no puede saber
        # si la RM existe. La salida es facturar-rm-manual.
        if _doc.rm_resultado_desconocido(tarea_pre):
            msg = (f'La remisión del pedido {tarea_pre.numero_pedido_siesa} se envió a '
                   f'Siesa y no se pudo confirmar. No se vuelve a enviar: identifíquela '
                   f'en Siesa y regístrela con «Facturar RM manual».')
            return CierreResult(exitoso=False, error=msg, mensaje=msg)

        # Pre-check Siesa antes de adquirir lock
        if tarea_pre.tipo_docto_pedido_siesa and tarea_pre.consec_docto_pedido_siesa:
            error = self._precheck_siesa(tarea_pre)
            if error:
                return CierreResult(
                    exitoso=False, error=error, mensaje=error,
                    estado=(SIESA_NO_DISPONIBLE
                            if isinstance(error, MotivoSiesaNoDisponible) else None))

        # Compuerta de cartera (G2): el último punto reversible antes del
        # 244328, con el valor EMPACADO. Se evalúa antes del lock pesimista
        # (puede ir a Siesa por GET) y se aplica después de crear los bultos:
        # si retiene, la caja queda VERIFICADA con sus piezas declaradas y se
        # cierra de nuevo cuando cartera la libere. Ver
        # `cartera_service.compuerta_cierre`.
        puerta_cartera = None
        if tarea_pre.estado == 'VERIFICADO' and not tarea_pre.siesa_triggered:
            from app.services import cartera_service as _cartera
            puerta_cartera = _cartera.compuerta_cierre(tarea_pre, usuario_id)

        # Adquirir lock pesimista
        tarea = (TareaPacking.query
                 .options(selectinload(TareaPacking.items).selectinload(ItemPacking.producto))
                 .filter_by(id=tarea_id)
                 .with_for_update()
                 .first())

        if tarea.siesa_triggered:
            return CierreResult(exitoso=False,
                                error='Siesa ya procesó este despacho',
                                mensaje='Siesa ya procesó este despacho')

        siesa_pendiente = tarea.estado == 'DESPACHADO' and not tarea.siesa_triggered
        if tarea.estado not in ['VERIFICADO'] and not siesa_pendiente:
            return CierreResult(exitoso=False,
                                error='El packing debe estar VERIFICADO antes de cerrar',
                                mensaje='El packing debe estar VERIFICADO antes de cerrar')

        # Crear bultos si no existen
        self._crear_bultos(tarea, tarea_id, bultos_data, total)

        if puerta_cartera is not None and not puerta_cartera.pasa:
            if not tarea.cerrado_por_id:
                tarea.cerrado_por_id = usuario_id or None
            db.session.commit()
            logger.info('[PEDIDO_CLOSER] tarea=%s retenida por cartera (retención %s)',
                        tarea_id, getattr(puerta_cartera.retencion, 'id', None))
            return CierreResult(exitoso=False, error=puerta_cartera.mensaje,
                                mensaje=puerta_cartera.mensaje,
                                estado=RETENIDO_CARTERA,
                                retencion_id=getattr(puerta_cartera.retencion, 'id', None))

        # Construir payload para 238925
        items_payload, error = self._construir_items_payload(tarea)
        if error:
            return CierreResult(exitoso=False, error=error, mensaje=error)

        if not tarea.tipo_docto_pedido_siesa or not tarea.consec_docto_pedido_siesa:
            msg = f'Tarea {tarea_id} sin datos Siesa válidos (tipo_docto / consec)'
            return CierreResult(exitoso=False, error=msg, mensaje=msg)

        # Quién cerró: puede no ser el empacador (supervisión cierra ajenas).
        # `usuario_id` llega en 0 cuando la vía no lo conoce — 0 no es nadie.
        # Un reintento del cierre no pisa al primero que cerró.
        if not tarea.cerrado_por_id:
            tarea.cerrado_por_id = usuario_id or None

        # Encolar SiesaJob DESPACHO_F470. La tarea NO pasa a DESPACHADO acá:
        # lo hace `DespachoParialService._persistir_resultado` cuando la
        # remisión y la factura existen (decisión del dueño, 2026-09-25).
        # Hasta entonces la caja queda VERIFICADA con sus bultos, y el muelle
        # no la ve (`documento_fiscal.despachable`).
        self._encolar_job(tarea, tarea_id, items_payload)
        db.session.commit()

        from app.services.siesa_job_service import disparar_dlq_inmediato
        disparar_dlq_inmediato()

        logger.info('[PEDIDO_CLOSER] tarea=%s job encolado, DLQ disparado', tarea_id)
        return CierreResult(exitoso=True,
                            mensaje=f'Pedido {tarea.numero_pedido_siesa} encolado para facturación')

    # ── helpers privados ──────────────────────────────────────────────────────

    def _calcular_total(self, tarea_id: int, bultos_data: list):
        from app.models.bulto import Bulto
        if not bultos_data:
            previos = Bulto.query.filter_by(tarea_id=tarea_id).all()
            if not previos:
                return 'Debe declarar al menos una pieza'
            return len(previos)
        total = sum(int(b.get('cantidad', 1)) for b in bultos_data)
        return total if total >= 1 else 'Total de piezas debe ser al menos 1'

    def _precheck_siesa(self, tarea_pre: TareaPacking):
        # Cada llamada HTTP a Siesa puede tardar hasta 30s (timeout de Connekta).
        # Ejecutar ambas en paralelo con timeout total de 8s para no bloquear el
        # request thread. Si Siesa no responde, el precheck se omite y el DLQ
        # handler maneja el rechazo (reconciliación automática ya lo cubre).
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
        import time as _time

        _PRECHECK_TIMEOUT = 8  # segundos — presupuesto TOTAL para ambos futures

        tipo = tarea_pre.tipo_docto_pedido_siesa
        consec = tarea_pre.consec_docto_pedido_siesa

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            _t0 = _time.monotonic()
            fut_estado = pool.submit(connekta.get_estado_pedido, tipo, consec)
            fut_factura = pool.submit(connekta.get_factura_desde_pedido, tipo, consec)

            estado = fut_estado.result(timeout=_PRECHECK_TIMEOUT)
            from app.services import estado_pedido_siesa as _eps
            if _eps.impide_facturar(estado):
                fut_factura.cancel()
                return (f'Pedido {tarea_pre.numero_pedido_siesa} anulado en Siesa '
                        f'(estado {estado}) — no se puede facturar')

            # Tiempo restante del presupuesto — evita que el segundo future
            # duplique el timeout (8+8=16s bloqueando el worker).
            _remaining = max(0.5, _PRECHECK_TIMEOUT - (_time.monotonic() - _t0))
            facturas = fut_factura.result(timeout=_remaining)
            if facturas:
                f0 = facturas[0]
                return (f'Pedido {tarea_pre.numero_pedido_siesa} ya tiene factura '
                        f'{f0.get("f350_id_tipo_docto","?")}{f0.get("f350_consec_docto","?")} en Siesa')
        except (FutTimeout, Exception) as e:
            # **No se continúa.** Este `except` decía «continuando (DLQ
            # reconciliará si hay conflicto)» y se tragaba el fail-fast
            # deliberado de `get_factura_desde_pedido`, cuyo propio comentario
            # dice que no devuelve `[]` ante error de red **precisamente**
            # para que nadie asuma que no hay FE y dispare una duplicada.
            #
            # El DLQ no reconcilia esto: aguas abajo se encola DESPACHO_F470,
            # que ejecuta 244328 → 142945 (remisión, descarga inventario) →
            # 142943 (FE). Cuando el DLQ mira, los documentos ya existen.
            #
            # Y el presupuesto son 8 s contra los 30 del GET: el camino de
            # fallo se dispara cada vez que Siesa va lento — que es justo
            # cuando el intento anterior quedó a medias.
            #
            # La versión que sí propagaba, `_cerrar_packing_pedido_legacy`,
            # no tiene un solo caller. Una política, dos implementaciones, y
            # la viva era la degradada.
            logger.error(
                '[PEDIDO_CLOSER] precheck Siesa falló para %s%s: %s — se '
                'ABORTA el cierre. No se puede saber si el pedido ya tiene '
                'factura, y seguir sería arriesgar una FE duplicada.',
                tipo, consec, e
            )
            # Un texto para «Siesa no está disponible», el del frente fiscal
            # (`MENSAJE_SIESA_NO_DISPONIBLE`); el tipo le dice a la ruta que es
            # un 503 y a la cola offline que se reintenta.
            from app.services.documento_fiscal import MENSAJE_SIESA_NO_DISPONIBLE
            return MotivoSiesaNoDisponible(
                f'{MENSAJE_SIESA_NO_DISPONIBLE} No se pudo verificar si el pedido '
                f'{tarea_pre.numero_pedido_siesa} ya tiene factura ({e}); cerrar '
                f'ahora podría emitir una factura duplicada.')
        finally:
            # shutdown(wait=False) evita bloquear hasta 30s si un future aún
            # espera respuesta HTTP de Connekta después de nuestro timeout de 8s.
            # cancel_futures=True (Python 3.9+) cancela futures pendientes.
            pool.shutdown(wait=False, cancel_futures=True)
        return None

    def _crear_bultos(self, tarea: TareaPacking, tarea_id: int,
                      bultos_data: list, total: int):
        from app.models.bulto import Bulto
        if Bulto.query.filter_by(tarea_id=tarea_id).count():
            return
        numero = 1
        ref = tarea.referencia_doc or tarea.numero_pedido_siesa or str(tarea_id)
        for b in bultos_data:
            for _ in range(int(b.get('cantidad', 1))):
                db.session.add(Bulto(
                    tarea_id=tarea_id,
                    codigo_barras=f'{ref}-{numero:02d}',
                    tipo=b.get('tipo', 'Caja'),
                    numero=numero,
                    total=total,
                ))
                numero += 1
        db.session.flush()

    def _construir_items_payload(self, tarea: TareaPacking):
        from app.models.pedido_siesa import PedidoSiesa
        regs = {r.item_codigo: r for r in
                PedidoSiesa.query.filter_by(numero_pedido=tarea.numero_pedido_siesa).all()}
        items = []
        for i in tarea.items:
            if not i.producto:
                return None, f'ItemPacking {i.id} sin producto en BD'
            if not i.producto.codigo_siesa:
                return None, f'Producto {i.producto.codigo} sin codigo_siesa'
            codigo = i.producto.codigo_siesa
            fc = i.producto.factor_conversion or 1
            emp = i.producto.unidad_empaque or ''
            cant_raw = i.cantidad_real if i.cantidad_real is not None else i.cantidad_esperada
            if fc > 1 and emp and cant_raw >= fc:
                cant_siesa, uom = round(cant_raw / fc, 4), emp
            else:
                cant_siesa, uom = cant_raw, i.producto.unidad_medida or ''
            reg = regs.get(codigo)
            items.append({
                'producto_codigo': codigo,
                'cantidad_empacada': cant_siesa,
                'cantidad_pedida': i.cantidad_esperada,
                'lote': i.lote or '',
                'item_id_siesa': reg.item_id_siesa if reg else '',
                'unidad_medida': uom,
            })
        return items, None

    def _encolar_job(self, tarea: TareaPacking, tarea_id: int, items_payload: list):
        # Incluir COMPLETADO: si el DLQ completó el job pero siesa_triggered no se
        # persistió (crash window), no crear duplicado que generaría doble factura.
        job = SiesaJob.query.filter(
            SiesaJob.tipo == 'DESPACHO_F470',
            SiesaJob.referencia_tipo == 'TareaPacking',
            SiesaJob.referencia_id == tarea_id,
            SiesaJob.estado.in_(
                list(EstadoSiesaJob.ACTIVOS) + [EstadoSiesaJob.FALLIDO, EstadoSiesaJob.COMPLETADO]
            ),
        ).first()
        payload_dict = {
            'tarea_id': tarea_id,
            'tipo_docto_pedido': tarea.tipo_docto_pedido_siesa or '',
            'consec_docto_pedido': tarea.consec_docto_pedido_siesa,
            'items': items_payload,
            'numero_pedido_siesa': tarea.numero_pedido_siesa,
        }
        if job and job.estado == EstadoSiesaJob.FALLIDO:
            from app.services.siesa_job_service import reencolar_job_fallido
            reencolar_job_fallido(
                job, usuario_id=(getattr(tarea, 'cerrado_por_id', None) or None),
                motivo='Reintento del cierre de packing',
                payload=json.dumps(payload_dict, ensure_ascii=False))
        elif not job:
            SiesaJob.encolar(
                tipo='DESPACHO_F470',
                payload=payload_dict,
                referencia_tipo='TareaPacking',
                referencia_id=tarea_id,
            )
