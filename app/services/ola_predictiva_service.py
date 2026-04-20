"""
Reposición Predictiva — Pre-verificación de Ola de Picking.

Problema que resuelve:
  Sin esto: el picker llega a PIK-01 y encuentra 0 unidades porque cayó un pedido
  masivo que vacía la zona PICKING antes de que el Abastecedor pueda reaccionar.

  Con esto: ANTES de liberar las tareas de picking al operario, el sistema
  cruza la demanda total de la ola contra el stock actual de PICKING.
  Si la demanda supera el stock disponible, dispara TareaReposicion al Abastecedor
  AHORA — el Abastecedor ya está en camino mientras el picker empieza.

Cuándo llamar:
  - En PickingService.crear_tareas() — al crear tareas desde un pedido
  - En el endpoint de liberación de pedidos (picking masivo)

Retorna:
  {
    'items_ok': [...],           # tienen stock suficiente en PICKING
    'items_con_alerta': [...],   # demanda > stock PICKING — se pre-disparó reposición
    'reposiciones_generadas': N,
  }
"""

import logging
from app.extensions import db
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto
from app.models.lpn import LPN
from app.models.tarea_reposicion import TareaReposicion

logger = logging.getLogger(__name__)


def pre_verificar_ola(items: list, almacen_id: int) -> dict:
    """
    Cruza la demanda de una ola contra el stock de zonas PICKING.

    items: [{'producto_id': int, 'cantidad': int}, ...]
    almacen_id: int

    Pre-dispara TareaReposicion para ítems con déficit antes de que el picker salga.
    """
    items_ok = []
    items_con_alerta = []
    reposiciones_generadas = 0

    for item in items:
        producto_id = item['producto_id']
        cantidad_demandada = item['cantidad']

        # Stock disponible SOLO en zonas PICKING
        stock_picking = _stock_disponible_picking(producto_id, almacen_id)

        if stock_picking >= cantidad_demandada:
            items_ok.append({
                'producto_id': producto_id,
                'cantidad_demandada': cantidad_demandada,
                'stock_picking': stock_picking,
            })
            continue

        deficit = cantidad_demandada - stock_picking
        items_con_alerta.append({
            'producto_id': producto_id,
            'cantidad_demandada': cantidad_demandada,
            'stock_picking': stock_picking,
            'deficit': deficit,
        })

        # Pre-disparar reposición si hay LPN disponible en RESERVA
        generadas = _pre_reponer(producto_id, almacen_id, deficit)
        reposiciones_generadas += generadas

        if generadas:
            logger.info(
                f'[OLA PREDICTIVA] Producto {producto_id} — déficit {deficit} UNDs '
                f'→ {generadas} TareaReposicion pre-disparadas'
            )
        else:
            logger.warning(
                f'[OLA PREDICTIVA] Producto {producto_id} — déficit {deficit} UNDs '
                f'pero sin LPN disponible en RESERVA'
            )

    if reposiciones_generadas:
        db.session.commit()

    return {
        'items_ok': items_ok,
        'items_con_alerta': items_con_alerta,
        'reposiciones_generadas': reposiciones_generadas,
        'hay_deficit': len(items_con_alerta) > 0,
    }


def _stock_disponible_picking(producto_id: int, almacen_id: int) -> int:
    """Suma el stock disponible (cantidad - reservado) en todas las ubicaciones PICKING."""
    registros = (
        UbicacionProducto.query
        .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
        .filter(
            UbicacionProducto.producto_id == producto_id,
            Ubicacion.almacen_id == almacen_id,
            Ubicacion.tipo_zona == 'PICKING',
            Ubicacion.activo == True,
        )
        .all()
    )
    return sum(max(0, r.cantidad - (r.reservado or 0)) for r in registros)


def _pre_reponer(producto_id: int, almacen_id: int, deficit: int) -> int:
    """
    Genera TareaReposicion preventivas para cubrir el déficit.
    Puede generar múltiples tareas si se necesitan varios LPNs.
    Retorna el número de tareas generadas.
    """
    # LPNs disponibles en RESERVA, orden FIFO
    lpns = (
        LPN.query
        .join(Ubicacion, Ubicacion.id == LPN.ubicacion_id)
        .filter(
            LPN.producto_id == producto_id,
            LPN.almacen_id == almacen_id,
            LPN.estado == 'ACTIVO',
            Ubicacion.tipo_zona == 'RESERVA',
        )
        .order_by(LPN.fecha_creacion.asc())
        .all()
    )

    if not lpns:
        return 0

    # Zona PICKING destino — la que tiene menor secuencia (más cerca de la salida)
    ub_picking = (
        UbicacionProducto.query
        .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
        .filter(
            UbicacionProducto.producto_id == producto_id,
            Ubicacion.almacen_id == almacen_id,
            Ubicacion.tipo_zona == 'PICKING',
            Ubicacion.activo == True,
        )
        .order_by(Ubicacion.secuencia_ruteo.asc().nullslast())
        .first()
    )

    if not ub_picking:
        return 0

    pendiente = deficit
    generadas = 0

    for lpn in lpns:
        if pendiente <= 0:
            break

        # ¿Ya existe tarea activa para este LPN?
        ya_existe = TareaReposicion.query.filter(
            TareaReposicion.lpn_id == lpn.id,
            TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO']),
        ).first()
        if ya_existe:
            pendiente -= lpn.cantidad_actual
            continue

        # ¿Ya existe tarea para esta (ubicacion_picking, producto)?
        tarea_dup = TareaReposicion.query.filter(
            TareaReposicion.ubicacion_picking_id == ub_picking.ubicacion_id,
            TareaReposicion.producto_id == producto_id,
            TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO']),
        ).first()
        if tarea_dup:
            pendiente -= lpn.cantidad_actual
            continue

        tarea = TareaReposicion(
            codigo=TareaReposicion.generar_codigo(),
            producto_id=producto_id,
            almacen_id=almacen_id,
            cantidad_unidades=min(lpn.cantidad_actual, pendiente),
            ubicacion_reserva_id=lpn.ubicacion_id,
            ubicacion_picking_id=ub_picking.ubicacion_id,
            lpn_id=lpn.id,
            estado='PENDIENTE',
            notas='Pre-disparada por ola predictiva',
        )
        db.session.add(tarea)
        pendiente -= lpn.cantidad_actual
        generadas += 1

    return generadas
