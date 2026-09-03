"""
El FEFO podía asignar mercancía averiada a un pedido de cliente.

`calcular_fefo` ordenaba las ubicaciones por (ubicación real antes que
SIESA-GENERAL) → vencimiento → fecha de ingreso, y **no excluía AVERIAS**.
Como la zona de averías se llena con lo que la auditoría de picking manda ahí
(`auditar_tarea`, resultado `AVERIA`) y ese stock se queda quieto, su
`fecha_ingreso` es casi siempre la más vieja del producto: en un empate FEFO
la ubicación de averías **gana**. El resultado es una `TareaPicking` contra
una ubicación de producto dañado, un operario recogiéndolo, y una caja
averiada saliendo hacia un cliente.

`AVERIAS` está definida en `app/models/ubicacion.py:39` como «productos
dañados/en revisión». Las demás zonas del layout (PICKING, RESERVA,
IMPORTADOS, GENERAL — ver `layout_service.ZONAS_VALIDAS`) sí son mercancía
despachable, y su orden de prioridad no se toca: este camino es el caliente
del picking y una prioridad movida de más rompe la operación completa.

Detector en las dos direcciones: averías nunca se asigna (ni siquiera cuando
es el único stock que hay), y el FEFO entre dos ubicaciones legítimas sigue
eligiendo exactamente lo mismo que antes.
"""
from datetime import datetime, timedelta, date

import pytest

from app.services.picking_service import PickingService


def _ubicacion(db, almacen, codigo, tipo_zona):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo=codigo, almacen_id=almacen.id,
                  tipo_zona=tipo_zona, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _stock(db, ubicacion, producto, cantidad, dias_atras=0, vence=None):
    from app.models.inventario import UbicacionProducto
    reg = UbicacionProducto(
        ubicacion_id=ubicacion.id, producto_id=producto.id,
        cantidad=cantidad, reservado=0, bloqueado=0,
        fecha_ingreso=datetime.utcnow() - timedelta(days=dias_atras),
        fecha_vencimiento=vence,
    )
    db.session.add(reg)
    db.session.commit()
    return reg


class TestFefoNoAsignaAverias:

    def test_averias_mas_antigua_no_le_gana_a_picking(
        self, app, db, almacen, producto, ub_picking
    ):
        """
        La ubicación de averías tiene la fecha de ingreso más vieja — hoy gana
        el FEFO. Debe perder siempre: no es stock, es un problema pendiente.
        """
        ub_averias = _ubicacion(db, almacen, 'AVE1', 'AVERIAS')
        _stock(db, ub_averias, producto, 100, dias_atras=200)   # la más vieja
        _stock(db, ub_picking, producto, 50, dias_atras=1)

        fefo = PickingService.calcular_fefo(producto.id, 10, almacen.id)

        ubicaciones = [a['ubicacion_id'] for a in fefo['asignaciones']]
        assert ub_averias.id not in ubicaciones, (
            'El FEFO asignó mercancía averiada a un pedido de cliente'
        )
        assert ubicaciones == [ub_picking.id]
        assert fefo['completo'] is True

    def test_averias_no_completa_lo_que_falta(
        self, app, db, almacen, producto, ub_picking
    ):
        """
        Excluir averías del primer lugar no basta: tampoco puede entrar como
        respaldo. Se piden 60 con 50 vendibles y 100 averiadas — el faltante se
        declara (Regla 0: ante dato ausente, fallar hacia el lado conservador y
        declararlo). `crear_tareas` levanta «stock insuficiente» y alguien
        decide; asignar la avería no da error y termina en el cliente.
        """
        ub_averias = _ubicacion(db, almacen, 'AVE1', 'AVERIAS')
        _stock(db, ub_averias, producto, 100, dias_atras=200)
        _stock(db, ub_picking, producto, 50, dias_atras=1)

        fefo = PickingService.calcular_fefo(producto.id, 60, almacen.id)

        ubicaciones = [a['ubicacion_id'] for a in fefo['asignaciones']]
        assert ub_averias.id not in ubicaciones
        assert fefo['completo'] is False
        assert fefo['cantidad_disponible'] == 50
        assert fefo['cantidad_faltante'] == 10

    def test_solo_averias_no_produce_ninguna_asignacion(
        self, app, db, almacen, producto
    ):
        """Sin stock vendible, el FEFO no encuentra nada — aunque la bodega de
        averías esté llena."""
        ub_averias = _ubicacion(db, almacen, 'AVE1', 'AVERIAS')
        _stock(db, ub_averias, producto, 500, dias_atras=200)

        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)

        assert fefo['asignaciones'] == []
        assert fefo['completo'] is False
        assert fefo['cantidad_faltante'] == 5


class TestFefoEntreUbicacionesLegitimasNoCambia:
    """Dirección contraria — el camino caliente del picking, intacto."""

    def test_entre_dos_zonas_vendibles_sigue_ganando_la_mas_antigua(
        self, app, db, almacen, producto, ub_picking, ub_reserva
    ):
        _stock(db, ub_picking, producto, 20, dias_atras=1)
        _stock(db, ub_reserva, producto, 20, dias_atras=90)   # la más antigua

        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)

        assert [a['ubicacion_id'] for a in fefo['asignaciones']] == [ub_reserva.id]

    def test_el_vencimiento_sigue_mandando_sobre_la_fecha_de_ingreso(
        self, app, db, almacen, producto, ub_picking, ub_reserva
    ):
        """FEFO es First *Expired* First Out: el lote que vence antes sale
        primero aunque haya entrado después."""
        _stock(db, ub_reserva, producto, 20, dias_atras=90,
               vence=date(2030, 12, 31))
        _stock(db, ub_picking, producto, 20, dias_atras=1,
               vence=date(2026, 9, 1))    # vence primero

        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)

        assert [a['ubicacion_id'] for a in fefo['asignaciones']] == [ub_picking.id]

    def test_general_sigue_de_ultimo_y_sigue_sirviendo_de_respaldo(
        self, app, db, almacen, producto, ub_picking, ub_general
    ):
        """El criterio de PD1343 (ubicación real antes que SIESA-GENERAL, sin
        importar la antigüedad) no se toca — y GENERAL sigue completando."""
        _stock(db, ub_general, producto, 4000, dias_atras=120)
        _stock(db, ub_picking, producto, 10, dias_atras=0)

        fefo = PickingService.calcular_fefo(producto.id, 15, almacen.id)

        assert fefo['completo'] is True
        assert [(a['ubicacion_id'], a['cantidad']) for a in fefo['asignaciones']] == [
            (ub_picking.id, 10), (ub_general.id, 5)
        ]

    def test_crear_tareas_no_genera_picking_contra_averias(
        self, app, db, almacen, producto, ub_picking
    ):
        """El defecto no termina en una lista: termina en una TareaPicking que
        un operario ejecuta. Se verifica sobre el camino completo."""
        from app.models.picking import TareaPicking

        ub_averias = _ubicacion(db, almacen, 'AVE1', 'AVERIAS')
        _stock(db, ub_averias, producto, 100, dias_atras=200)
        _stock(db, ub_picking, producto, 50, dias_atras=1)

        tareas = PickingService.crear_tareas(
            producto_id=producto.id, cantidad=10, almacen_id=almacen.id,
            referencia_documento='PED-AVE-001', tipo_documento='PEDIDO',
        )

        assert len(tareas) == 1
        assert tareas[0].ubicacion_id == ub_picking.id
        creadas = TareaPicking.query.filter_by(ubicacion_id=ub_averias.id).all()
        assert creadas == [], 'Se creó una tarea de picking contra la zona de averías'
