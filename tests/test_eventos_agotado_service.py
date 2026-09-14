"""
Hook de captura de agotados — PickingService.reportar_problema() debe dejar
un EventoStockAgotado solo cuando el bloqueo es un agotado físico real
(motivo='FALTANTE' con unidades sin cubrir), nunca para los otros motivos
de bloqueo ni cuando el operario terminó de encontrar todo lo solicitado.

Probado end-to-end desde el servicio real (no mockeado) — mismo criterio que
el resto de la suite de picking.
"""
import pytest

from app.extensions import db
from app.services.picking_service import PickingService
from app.services.eventos_agotado_service import registrar_evento_agotado
from app.models.evento_stock_agotado import EventoStockAgotado
from app.models.picking import TareaPicking


@pytest.fixture
def picking_setup(db, almacen, producto, ub_picking, inv_picking, usuario):
    return {
        'almacen': almacen,
        'producto': producto,
        'ubicacion': ub_picking,
        'inventario': inv_picking,
        'usuario': usuario,
    }


def _crear_tarea_en_proceso(s, cantidad=5, referencia_documento='PD1000', tipo_documento='PEDIDO'):
    tareas = PickingService.crear_tareas(
        producto_id=s['producto'].id,
        cantidad=cantidad,
        almacen_id=s['almacen'].id,
        referencia_documento=referencia_documento,
        tipo_documento=tipo_documento,
    )
    tarea = tareas[0]
    PickingService.iniciar_picking(tarea.id, s['usuario'].id)
    return tarea


class TestRegistraEventoSoloEnAgotadoReal:

    def test_faltante_con_unidades_sin_cubrir_crea_evento(self, app, db, picking_setup):
        s = picking_setup
        s['producto'].precio_venta = 100.50
        s['producto'].categoria = 'PAPELERIA'
        s['producto'].clasificacion_abc = 'A'
        db.session.commit()

        tarea = _crear_tarea_en_proceso(s, cantidad=5, referencia_documento='PD1000')
        PickingService.reportar_problema(
            tarea_id=tarea.id, operario_id=s['usuario'].id,
            motivo='FALTANTE', cantidad_encontrada=2,
        )

        eventos = EventoStockAgotado.query.all()
        assert len(eventos) == 1
        ev = eventos[0]
        assert ev.tarea_picking_id == tarea.id
        assert ev.producto_id == s['producto'].id
        assert ev.almacen_id == s['almacen'].id
        assert ev.pedido_siesa_ref == 'PD1000'
        assert ev.tipo_documento == 'PEDIDO'
        assert ev.cantidad_faltante == 3
        assert float(ev.precio_venta_capturado) == 100.50
        assert ev.categoria_producto == 'PAPELERIA'
        assert ev.clasificacion_abc == 'A'

    def test_faltante_sin_encontrar_nada_crea_evento_por_toda_la_cantidad(self, app, db, picking_setup):
        s = picking_setup
        tarea = _crear_tarea_en_proceso(s, cantidad=5)
        PickingService.reportar_problema(
            tarea_id=tarea.id, operario_id=s['usuario'].id,
            motivo='FALTANTE', cantidad_encontrada=0,
        )
        eventos = EventoStockAgotado.query.all()
        assert len(eventos) == 1
        assert eventos[0].cantidad_faltante == 5

    def test_faltante_pero_encontro_todo_no_crea_evento(self, app, db, picking_setup):
        s = picking_setup
        tarea = _crear_tarea_en_proceso(s, cantidad=5)
        PickingService.reportar_problema(
            tarea_id=tarea.id, operario_id=s['usuario'].id,
            motivo='FALTANTE', cantidad_encontrada=5,
        )
        assert EventoStockAgotado.query.count() == 0

    def test_otro_motivo_de_bloqueo_no_crea_evento(self, app, db, picking_setup):
        s = picking_setup
        tarea = _crear_tarea_en_proceso(s, cantidad=5)
        PickingService.reportar_problema(
            tarea_id=tarea.id, operario_id=s['usuario'].id,
            motivo='UBICACION_VACIA', cantidad_encontrada=0,
        )
        assert EventoStockAgotado.query.count() == 0

    def test_producto_sin_precio_venta_captura_cero_sin_fallar(self, app, db, picking_setup):
        s = picking_setup
        assert not s['producto'].precio_venta  # default del modelo (0)
        tarea = _crear_tarea_en_proceso(s, cantidad=5)
        PickingService.reportar_problema(
            tarea_id=tarea.id, operario_id=s['usuario'].id,
            motivo='FALTANTE', cantidad_encontrada=0,
        )
        ev = EventoStockAgotado.query.one()
        assert float(ev.precio_venta_capturado) == 0


class TestRegistrarEventoAgotadoAislado:
    """Unitario directo de `eventos_agotado_service.registrar_evento_agotado`,
    sin pasar por picking_service — la función es pura y se puede probar
    con una TareaPicking construida a mano."""

    def test_cantidad_faltante_cero_no_agrega_nada(self, app, db, picking_setup):
        s = picking_setup
        tarea = TareaPicking(
            codigo='TP-AISLADO-0', producto_id=s['producto'].id, cantidad_solicitada=5,
            ubicacion_id=s['ubicacion'].id, almacen_id=s['almacen'].id,
        )
        db.session.add(tarea)
        db.session.commit()

        resultado = registrar_evento_agotado(tarea, cantidad_faltante=0)
        assert resultado is None
        assert EventoStockAgotado.query.count() == 0

    def test_agrega_evento_con_los_campos_del_producto_y_la_tarea(self, app, db, picking_setup):
        s = picking_setup
        s['producto'].precio_venta = 250.0
        s['producto'].categoria = 'OFICINA'
        s['producto'].clasificacion_abc = 'B'
        db.session.commit()

        tarea = TareaPicking(
            codigo='TP-AISLADO-1', producto_id=s['producto'].id, cantidad_solicitada=8,
            ubicacion_id=s['ubicacion'].id, almacen_id=s['almacen'].id,
            referencia_documento='PD-AISLADO', tipo_documento='PEDIDO',
        )
        db.session.add(tarea)
        db.session.commit()

        evento = registrar_evento_agotado(tarea, cantidad_faltante=4)
        db.session.commit()

        assert evento is not None
        assert evento.tarea_picking_id == tarea.id
        assert evento.producto_id == s['producto'].id
        assert evento.almacen_id == s['almacen'].id
        assert evento.pedido_siesa_ref == 'PD-AISLADO'
        assert evento.tipo_documento == 'PEDIDO'
        assert evento.cantidad_faltante == 4
        assert float(evento.precio_venta_capturado) == 250.0
        assert evento.categoria_producto == 'OFICINA'
        assert evento.clasificacion_abc == 'B'
