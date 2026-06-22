"""TrasladoPackingCloser — cierre de caja para Requisiciones/Traslados (ST).

Al cerrar caja:
  1. Crea bultos físicos
  2. Dispara 174720 (Compromisos con cantidades reales + ubicaciones)
  3. Encola SiesaJob DESPACHO_TRASLADO → 174930 (STS) vía DLQ con retry
  4. Transiciona SolicitudTraslado a EN_TRANSITO cuando DLQ confirma

El empacador hace exactamente lo mismo que en un PD.
El sistema decide qué llama a Siesa al final.
"""
import logging

from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.models.siesa_job import SiesaJob, EstadoSiesaJob
from .base import IPackingCloser, CierreResult

logger = logging.getLogger(__name__)


class TrasladoPackingCloser(IPackingCloser):

    def ejecutar_cierre(self, tarea_id: int, bultos_data: list,
                        usuario_id: int) -> CierreResult:
        from sqlalchemy.orm import selectinload
        from app.models.bulto import Bulto
        from app.models.traslado import SolicitudTraslado

        tarea = (TareaPacking.query
                 .options(selectinload(TareaPacking.items).selectinload(ItemPacking.producto))
                 .filter_by(id=tarea_id)
                 .with_for_update()
                 .first())
        if not tarea:
            return CierreResult(exitoso=False, error='Tarea no encontrada',
                                mensaje='Tarea no encontrada')

        if tarea.siesa_triggered:
            return CierreResult(exitoso=False,
                                error='Siesa ya procesó este despacho',
                                mensaje='Siesa ya procesó este despacho')

        siesa_pendiente = tarea.estado == 'DESPACHADO' and not tarea.siesa_triggered
        if tarea.estado not in ['VERIFICADO'] and not siesa_pendiente:
            return CierreResult(exitoso=False,
                                error='El packing debe estar VERIFICADO antes de cerrar',
                                mensaje='El packing debe estar VERIFICADO antes de cerrar')

        if not tarea.solicitud_id:
            return CierreResult(exitoso=False,
                                error='Tarea de traslado sin solicitud_id — datos incompletos',
                                mensaje='Tarea de traslado sin solicitud_id')

        solicitud = SolicitudTraslado.query.get(tarea.solicitud_id)
        if not solicitud:
            return CierreResult(exitoso=False,
                                error=f'SolicitudTraslado {tarea.solicitud_id} no encontrada',
                                mensaje='Solicitud de traslado no encontrada')

        # Total bultos
        total = self._calcular_total(tarea_id, bultos_data)
        if isinstance(total, str):
            return CierreResult(exitoso=False, error=total, mensaje=total)

        # Crear bultos
        self._crear_bultos(tarea, tarea_id, bultos_data, total)

        # Construir ítems con cantidades reales y ubicaciones del picking
        items_comp = self._construir_items_packing(tarea, solicitud)

        # ── 174720 Compromisos (sync — sin DLQ) ──────────────────────────────
        # Registra cantidades reales + ubicaciones físicas sobre la RIT ya creada.
        # Es idempotente: si falla, se puede reintentar sin duplicar el documento.
        if solicitud.siesa_requisicion_consec and items_comp:
            try:
                from app.services.siesa_traslado_adapter import siesa_traslado
                siesa_traslado.registrar_compromisos(
                    consec_rit=solicitud.siesa_requisicion_consec,
                    bodega_origen=solicitud.bodega_origen_siesa,
                    bodega_destino=solicitud.bodega_destino_siesa,
                    items=items_comp,
                    codigo=solicitud.codigo,
                )
                logger.info('[TRASLADO_CLOSER] %s compromisos 174720 OK', solicitud.codigo)
            except Exception as e:
                logger.error('[TRASLADO_CLOSER] %s 174720 falló: %s', solicitud.codigo, e)
                solicitud.siesa_error = f'174720: {e}'

        # ── Descontar inventario WMS ─────────────────────────────────────────
        # Los bienes salen físicamente al cerrar caja; se descuenta ahora
        # independientemente del resultado del job Siesa (el camión ya salió).
        from app.services.traslado_service import TrasladoService
        TrasladoService._descontar_inventario_wms(solicitud)

        # ── Encolar DESPACHO_TRASLADO → 174930 (DLQ con retry) ───────────────
        self._encolar_job_traslado(tarea_id, solicitud, items_comp)

        tarea.estado = 'DESPACHADO'
        db.session.commit()

        from app.services.siesa_job_service import disparar_dlq_inmediato
        disparar_dlq_inmediato()

        logger.info('[TRASLADO_CLOSER] tarea=%s solicitud=%s job encolado',
                    tarea_id, solicitud.codigo)
        return CierreResult(exitoso=True,
                            mensaje=f'Traslado {solicitud.codigo} encolado para despacho')

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

    def _crear_bultos(self, tarea: TareaPacking, tarea_id: int,
                      bultos_data: list, total: int):
        from app.models.bulto import Bulto
        if Bulto.query.filter_by(tarea_id=tarea_id).count():
            return
        numero = 1
        ref = tarea.referencia_doc or str(tarea_id)
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

    def _construir_items_packing(self, tarea: TareaPacking, solicitud) -> list:
        from app.models.picking import TareaPicking
        from app.services.traslado_service import _resolver_empaque
        tareas_picking = TareaPicking.query.filter_by(
            referencia_documento=solicitud.codigo,
            tipo_documento='TRASLADO',
            estado='COMPLETADO',
        ).all()
        ubicacion_por_producto = {
            t.producto_id: (t.ubicacion.codigo if t.ubicacion else None)
            for t in tareas_picking
        }
        items = []
        for i in tarea.items:
            if not i.producto or not i.producto.codigo_siesa:
                continue
            cantidad = i.cantidad_real if i.cantidad_real is not None else i.cantidad_esperada
            if not cantidad or cantidad <= 0:
                continue
            uom_emp, factor_emp = _resolver_empaque(i.producto)
            items.append({
                'codigo_siesa':     i.producto.codigo_siesa,
                'cantidad':         cantidad,
                'cantidad_packing': cantidad,
                'unidad_medida':    i.producto.unidad_medida or '',
                'unidad_empaque':   uom_emp,
                'factor_empaque':   factor_emp,
                'ubicacion_codigo': ubicacion_por_producto.get(i.producto_id),
            })
        return items

    def _encolar_job_traslado(self, tarea_id: int, solicitud, items_comp: list):
        job = SiesaJob.query.filter(
            SiesaJob.tipo == 'DESPACHO_TRASLADO',
            SiesaJob.referencia_tipo == 'TareaPacking',
            SiesaJob.referencia_id == tarea_id,
            SiesaJob.estado.in_(list(EstadoSiesaJob.ACTIVOS) + [EstadoSiesaJob.FALLIDO]),
        ).first()
        payload_dict = {
            'tarea_id':     tarea_id,
            'solicitud_id': solicitud.id,
            'consec_rit':   solicitud.siesa_requisicion_consec,
            'bodega_origen': solicitud.bodega_origen_siesa,
            'bodega_destino': solicitud.bodega_destino_siesa,
            'codigo':       solicitud.codigo,
            'items':        items_comp,
        }
        if job and job.estado == EstadoSiesaJob.FALLIDO:
            import json
            job.estado = EstadoSiesaJob.PENDIENTE
            job.intentos = 0
            job.proximo_intento = None
            job.error_ultimo = None
            job.payload = json.dumps(payload_dict, ensure_ascii=False)
        elif not job:
            SiesaJob.encolar(
                tipo='DESPACHO_TRASLADO',
                payload=payload_dict,
                referencia_tipo='TareaPacking',
                referencia_id=tarea_id,
            )
