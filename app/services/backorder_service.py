"""
Qué líneas de un pedido comprometió Siesa de verdad — antes de que el WMS
mande al operario a pickear algo que Siesa ya decidió cancelar (pedido con
`f430_ind_backorder` = "despachar disponible, cancelar el resto").

Verificado en vivo contra Siesa QA (2026-08-24): 3 de 4 pedidos con la misma
referencia perdieron esa línea en la factura (`cantidad_remisionada=0`)
mientras el WMS local creía tener stock suficiente para los 4 — el
inventario local (`UbicacionProducto`) y el real de Siesa pueden divergir
sin que nada lo detecte antes de comprometer.

No persiste nada propio: la línea excluida se registra reutilizando el
ciclo BLOQUEADO → auditar_tarea() que ya existe para "el operario no lo
encontró" — ver `PickingService.bloquear_por_backorder_siesa` y el resultado
de auditoría `DISCREPANCIA_SIESA`, que ya estaba pensado para exactamente
este caso.
"""
import logging

logger = logging.getLogger(__name__)


def referencias_comprometidas_por_siesa(tipo_docto: str, consec_docto: str) -> set | None:
    """SKUs que Siesa confirma comprometidos (listos para remisionar) para
    este pedido, o `None` si no se pudo consultar (fallo de red).

    `None` es una señal explícita de "no sé" — el caller NO debe tratarlo
    como "ninguno está comprometido" (eso bloquearía picking legítimo por
    un timeout de Siesa, el mismo error que ya costó una vez con
    `CompromisosNoDisponibles`). Regla 0: fallo de red no es evidencia de
    falta de stock.
    """
    from app.services.despacho_parcial_service import DespachoParialService
    from app.services.connekta_gateway import CompromisosNoDisponibles

    try:
        filas = DespachoParialService.obtener_compromisos(tipo_docto, consec_docto)
    except CompromisosNoDisponibles as e:
        logger.warning(
            '[BACKORDER] No se pudo consultar compromisos de %s-%s — '
            'no se filtra ninguna línea este ciclo: %s',
            tipo_docto, consec_docto, e,
        )
        return None
    except Exception as e:
        logger.warning(
            '[BACKORDER] Consulta de compromisos falló (%s-%s) — '
            'no se filtra ninguna línea este ciclo: %s',
            tipo_docto, consec_docto, e,
        )
        return None

    return {
        str(f.get('f120_referencia', '')).strip()
        for f in filas
        if str(f.get('f120_referencia', '')).strip()
    }
