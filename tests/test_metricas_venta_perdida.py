"""
Métrica 5 — Venta perdida por agotados $.

Casos borde pedidos en Fase 5: día sin operación (ceros, no excepción),
agregación correcta por categoría/día, exclusión de traslados.
"""
from datetime import timedelta

from app.extensions import db
from app.models.evento_stock_agotado import EventoStockAgotado
from app.services.metricas.venta_perdida import calcular_venta_perdida, listar_venta_perdida_detalle


def _tarea_picking(almacen, producto, ub_picking, codigo):
    from app.models.picking import TareaPicking
    t = TareaPicking(codigo=codigo, producto_id=producto.id, cantidad_solicitada=5,
                      ubicacion_id=ub_picking.id, almacen_id=almacen.id, estado='BLOQUEADO',
                      motivo_bloqueo='FALTANTE', referencia_documento=codigo)
    db.session.add(t)
    db.session.commit()
    return t


def _evento(tarea, producto, almacen, cuando, cantidad_faltante=3, precio=100,
            categoria='PAPELERIA', tipo_documento='PEDIDO'):
    ev = EventoStockAgotado(
        tarea_picking_id=tarea.id, producto_id=producto.id, almacen_id=almacen.id,
        pedido_siesa_ref=tarea.referencia_documento, tipo_documento=tipo_documento,
        cantidad_faltante=cantidad_faltante, precio_venta_capturado=precio,
        categoria_producto=categoria, clasificacion_abc='A', creado_en=cuando,
    )
    db.session.add(ev)
    db.session.commit()
    return ev


class TestCalcularVentaPerdida:

    def test_dia_sin_operacion_retorna_ceros(self, app, db, almacen):
        from datetime import date
        resultado = calcular_venta_perdida(almacen.id, date(2026, 1, 1), date(2026, 1, 1))
        assert resultado['venta_perdida_total'] == 0.0
        assert resultado['por_categoria'] == {}
        assert resultado['por_dia'] == {}

    def test_agrega_total_y_por_categoria(self, app, db, almacen, producto, producto2, ub_picking):
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando = inicio_del_dia_utc(hoy) + timedelta(hours=8)

        t1 = _tarea_picking(almacen, producto, ub_picking, 'TP-VP-1')
        t2 = _tarea_picking(almacen, producto2, ub_picking, 'TP-VP-2')
        _evento(t1, producto, almacen, cuando, cantidad_faltante=3, precio=100, categoria='PAPELERIA')
        _evento(t2, producto2, almacen, cuando, cantidad_faltante=2, precio=50, categoria='UTILES')

        resultado = calcular_venta_perdida(almacen.id, hoy, hoy)
        assert resultado['venta_perdida_total'] == 300.0 + 100.0  # 3*100 + 2*50
        assert resultado['por_categoria'] == {'PAPELERIA': 300.0, 'UTILES': 100.0}
        assert sum(resultado['por_dia'].values()) == 400.0

    def test_excluye_traslados(self, app, db, almacen, producto, ub_picking):
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando = inicio_del_dia_utc(hoy) + timedelta(hours=8)
        t = _tarea_picking(almacen, producto, ub_picking, 'TP-VP-TRASLADO')
        _evento(t, producto, almacen, cuando, tipo_documento='TRASLADO')

        resultado = calcular_venta_perdida(almacen.id, hoy, hoy)
        assert resultado['venta_perdida_total'] == 0.0

    def test_excluye_otro_almacen(self, app, db, almacen, producto, ub_picking):
        from app.models.almacen import Almacen
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando = inicio_del_dia_utc(hoy) + timedelta(hours=8)
        otro = Almacen(codigo='ALM-OTRO2', nombre='Otro', bodega_siesa_id='NS1', activo=True)
        db.session.add(otro)
        db.session.commit()

        t = _tarea_picking(otro, producto, ub_picking, 'TP-VP-OTRO')
        _evento(t, producto, otro, cuando)

        resultado = calcular_venta_perdida(almacen.id, hoy, hoy)
        assert resultado['venta_perdida_total'] == 0.0


class TestListarVentaPerdidaDetalle:

    def test_pagina_resultados(self, app, db, almacen, producto, ub_picking):
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando = inicio_del_dia_utc(hoy) + timedelta(hours=8)
        for i in range(3):
            t = _tarea_picking(almacen, producto, ub_picking, f'TP-VP-DET-{i}')
            _evento(t, producto, almacen, cuando)

        pagina = listar_venta_perdida_detalle(almacen.id, hoy, hoy, page=1, per_page=2)
        assert pagina.total == 3
        assert len(pagina.items) == 2
