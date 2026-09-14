"""
Métrica 1 — Pedidos despachados desde una bodega.

Casos borde pedidos explícitamente por el usuario en Fase 5: día sin
operación (ceros, no excepción) y agregación correcta de líneas/unidades/$.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.services.metricas.pedidos_despachados import (
    calcular_pedidos_despachados, listar_pedidos_despachados_detalle,
)


def _tarea_despachada(almacen, producto, cuando, codigo, valor_factura=1000, cliente='Cliente X'):
    tarea = TareaPacking(
        codigo=codigo, estado='DESPACHADO', almacen_id=almacen.id,
        numero_pedido_siesa=codigo, tipo_documento='PEDIDO',
        cliente=cliente, valor_factura=valor_factura, fecha_despachado=cuando,
    )
    db.session.add(tarea)
    db.session.flush()
    db.session.add(ItemPacking(tarea_id=tarea.id, producto_id=producto.id,
                                cantidad_esperada=10, cantidad_real=9, verificado=True))
    db.session.commit()
    return tarea


class TestCalcularPedidosDespachados:

    def test_dia_sin_operacion_retorna_ceros(self, app, db, almacen):
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        resultado = calcular_pedidos_despachados(almacen.id, hoy, hoy)
        assert resultado == {
            'pedidos': 0, 'lineas': 0, 'unidades': 0, 'valor_total': 0.0, 'por_dia': {},
            'fecha_desde': hoy.isoformat(), 'fecha_hasta': hoy.isoformat(),
        }

    def test_agrega_pedidos_lineas_unidades_valor(self, app, db, almacen, producto):
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando = inicio_del_dia_utc(hoy) + timedelta(hours=10)
        _tarea_despachada(almacen, producto, cuando, 'PD-BI-001', valor_factura=1500)
        _tarea_despachada(almacen, producto, cuando, 'PD-BI-002', valor_factura=2500)

        resultado = calcular_pedidos_despachados(almacen.id, hoy, hoy)
        assert resultado['pedidos'] == 2
        assert resultado['lineas'] == 2
        assert resultado['unidades'] == 18  # 9 + 9
        assert resultado['valor_total'] == 4000.0
        assert resultado['por_dia'] == {hoy.isoformat(): 4000.0}

    def test_excluye_fuera_de_rango_y_otro_almacen(self, app, db, almacen, producto):
        from app.models.almacen import Almacen
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando_hoy = inicio_del_dia_utc(hoy) + timedelta(hours=5)
        cuando_ayer = inicio_del_dia_utc(hoy) - timedelta(hours=1)
        otro_almacen = Almacen(codigo='ALM-OTRO', nombre='Otro', bodega_siesa_id='NS1', activo=True)
        db.session.add(otro_almacen)
        db.session.commit()

        _tarea_despachada(almacen, producto, cuando_hoy, 'PD-BI-DENTRO')
        _tarea_despachada(almacen, producto, cuando_ayer, 'PD-BI-AYER')
        _tarea_despachada(otro_almacen, producto, cuando_hoy, 'PD-BI-OTRO')

        resultado = calcular_pedidos_despachados(almacen.id, hoy, hoy)
        assert resultado['pedidos'] == 1

    def test_no_cuenta_pendiente_ni_bloqueado(self, app, db, almacen, producto):
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando = inicio_del_dia_utc(hoy) + timedelta(hours=5)
        tarea = TareaPacking(codigo='PD-BI-PEND', estado='PENDIENTE', almacen_id=almacen.id,
                              numero_pedido_siesa='PD-BI-PEND', fecha_despachado=cuando)
        db.session.add(tarea)
        db.session.commit()
        resultado = calcular_pedidos_despachados(almacen.id, hoy, hoy)
        assert resultado['pedidos'] == 0


class TestListarPedidosDespachadosDetalle:

    def test_pagina_resultados(self, app, db, almacen, producto):
        from app.utils.fecha import inicio_del_dia_utc
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        cuando = inicio_del_dia_utc(hoy) + timedelta(hours=5)
        for i in range(3):
            _tarea_despachada(almacen, producto, cuando, f'PD-BI-DET-{i}')

        pagina = listar_pedidos_despachados_detalle(almacen.id, hoy, hoy, page=1, per_page=2)
        assert pagina.total == 3
        assert len(pagina.items) == 2
