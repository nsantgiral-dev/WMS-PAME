"""PedidoPackingCloser — cierre de caja para Pedidos (PD).

Extracción 1:1 de la lógica existente en PackingService.cerrar_packing().
Genera Factura (FE) + Remisión (RM) en Siesa vía 238925 (DLQ).
"""
import json
import logging

from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.models.siesa_job import SiesaJob, EstadoSiesaJob
from app.services.connekta_gateway import connekta
from .base import IPackingCloser, CierreResult

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

        # Pre-check Siesa antes de adquirir lock
        if tarea_pre.tipo_docto_pedido_siesa and tarea_pre.consec_docto_pedido_siesa:
            error = self._precheck_siesa(tarea_pre)
            if error:
                return CierreResult(exitoso=False, error=error, mensaje=error)

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

        # Construir payload para 238925
        items_payload, error = self._construir_items_payload(tarea)
        if error:
            return CierreResult(exitoso=False, error=error, mensaje=error)

        if not tarea.tipo_docto_pedido_siesa or not tarea.consec_docto_pedido_siesa:
            msg = f'Tarea {tarea_id} sin datos Siesa válidos (tipo_docto / consec)'
            return CierreResult(exitoso=False, error=msg, mensaje=msg)

        # Encolar SiesaJob DESPACHO_F470
        self._encolar_job(tarea, tarea_id, items_payload)
        tarea.estado = 'DESPACHADO'
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
                return 'Debes declarar al menos una pieza'
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
            if estado is not None and str(estado) in ('-1', '5', '9'):
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
            logger.warning(
                '[PEDIDO_CLOSER] precheck Siesa timeout/error para %s%s: %s — '
                'continuando (DLQ reconciliará si hay conflicto)',
                tipo, consec, e
            )
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
            job.estado = EstadoSiesaJob.PENDIENTE
            job.intentos = 0
            job.proximo_intento = None
            job.error_ultimo = None
            job.payload = json.dumps(payload_dict, ensure_ascii=False)
        elif not job:
            SiesaJob.encolar(
                tipo='DESPACHO_F470',
                payload=payload_dict,
                referencia_tipo='TareaPacking',
                referencia_id=tarea_id,
            )
