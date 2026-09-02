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
        try:
            items_comp = self._construir_items_packing(tarea, solicitud)
        except ValueError as e:
            return CierreResult(exitoso=False, error=str(e), mensaje=str(e))

        # ── 174720 Compromisos (sync — con abort on failure) ────────────────
        # Registra cantidades reales + ubicaciones físicas sobre la RIT ya creada.
        # Es idempotente: Siesa actualiza compromisos existentes, no duplica.
        # Si falla, NO podemos continuar con DESPACHO_TRASLADO (174930) porque
        # el STS heredaría cantidades planificadas del RIT en vez de las reales.
        _compromisos_ok = False
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
                _compromisos_ok = True
            except Exception as e:
                logger.error('[TRASLADO_CLOSER] %s 174720 falló: %s — '
                             'abortando cierre para evitar STS con cantidades incorrectas',
                             solicitud.codigo, e)
                solicitud.siesa_error = f'174720: {e}'
                db.session.commit()
                return CierreResult(
                    exitoso=False,
                    error=f'Error registrando compromisos en Siesa (174720): {e}. '
                          f'Reintente el cierre cuando Siesa esté disponible.',
                    mensaje='Error de comunicación con Siesa — reintente en unos minutos')
        else:
            # Sin RIT o sin items → no requiere compromisos
            _compromisos_ok = True

        # ── Descontar inventario WMS ─────────────────────────────────────────
        # Los bienes salen físicamente al cerrar caja; se descuenta ahora
        # independientemente del resultado del job Siesa (el camión ya salió).
        # Guard de idempotencia: en retry tras Gunicorn timeout, el inventario
        # ya fue descontado en el primer intento exitoso (commit pasó, respuesta no llegó).
        from app.services.traslado_service import TrasladoService
        if solicitud.inventario_descontado:
            logger.info('[TRASLADO_CLOSER] %s inventario ya descontado — saltando',
                        solicitud.codigo)
        else:
            TrasladoService._descontar_inventario_wms(solicitud)
            solicitud.inventario_descontado = True

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
        from sqlalchemy.orm import selectinload
        tareas_picking = (TareaPicking.query
            .options(selectinload(TareaPicking.ubicacion))
            .filter_by(
                referencia_documento=solicitud.codigo,
                tipo_documento='TRASLADO',
                estado='COMPLETADO',
            ).all())
        # setdefault: si un producto se pickó de varias ubicaciones, conservar
        # la primera (la más común). Dict comprehension sobreescribía con la última.
        ubicacion_por_producto = {}
        for t in tareas_picking:
            ubicacion_por_producto.setdefault(
                t.producto_id,
                t.ubicacion.codigo if t.ubicacion else None
            )
        items = []
        for i in tarea.items:
            if not i.producto or not i.producto.codigo_siesa:
                raise ValueError(
                    f'Item {i.id} (producto_id={i.producto_id}) sin codigo_siesa — '
                    f'no se puede cerrar traslado. Corregir producto en maestros antes de reintentar.'
                )
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
        # Sin RIT no se bloquea: el ejecutor de DESPACHO_TRASLADO
        # (siesa_job_service.py) ya sabe despachar sin ella —
        # `consec_rit=None` cae a 173076 directo (registrar_salida_transito),
        # el mismo fallback que TrasladoService.despachar() usaba antes de
        # unificar el cierre de PD y ST en este closer. El payload de abajo
        # ya trae 'items'/'bodega_origen'/'bodega_destino', que es todo lo
        # que ese fallback necesita — bloquear acá exigía más de lo que el
        # job de verdad requiere.
        #
        # Si el RIT 174646 sí fue aceptado por Siesa pero el WMS no pudo leer
        # su consecutivo (huérfana — ver CLAUDE.md "Las 28 requisiciones
        # huérfanas"), esa RIT queda suelta en Siesa para cerrar a mano; no
        # bloquea el movimiento real, que sale por el STS directo.
        if not solicitud.siesa_requisicion_consec:
            logger.warning(
                '[TRASLADO_CLOSER] %s sin siesa_requisicion_consec — '
                'DESPACHO_TRASLADO usará 173076 directo (sin RIT)',
                solicitud.codigo,
            )
        # Incluir COMPLETADO en el filtro: si el DLQ ya procesó el job pero
        # siesa_triggered no se persistió (crash entre commit y post_completado),
        # no crear un job duplicado que generaría un STS duplicado en Siesa.
        job = SiesaJob.query.filter(
            SiesaJob.tipo == 'DESPACHO_TRASLADO',
            SiesaJob.referencia_tipo == 'TareaPacking',
            SiesaJob.referencia_id == tarea_id,
            SiesaJob.estado.in_(
                list(EstadoSiesaJob.ACTIVOS) + [EstadoSiesaJob.FALLIDO, EstadoSiesaJob.COMPLETADO]
            ),
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
