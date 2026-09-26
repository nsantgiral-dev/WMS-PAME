"""
Cartera: qué consume cupo, y quién puede decidir (P1-1, P1-12, 2026-09-25).

P1-1. `consumo_wms` sumaba como consumo toda tarea no cancelada cuya FE no
estuviera en la cartera ABIERTA. Una FE **pagada** tampoco está ahí: consumía
cupo para siempre, y el cliente que pagaba quedaba retenido por un cupo que ya
había liberado. Y un pedido sin valor calculable sumaba 0.

Ahora consume: sin FE, o con FE de menos de 48 h todavía no indexada. Un
valor desconocido va aparte y la política retiene con `VALOR_DESCONOCIDO`.

P1-12. Con la compuerta en RETIENE (el defecto), sin nadie con el permiso de
autorizar y sin `CARTERA_GESTOR_TOKEN`, nadie decide: los pedidos quedan
retenidos sin salida. `salud()` cuenta los autorizadores y lo avisa.
"""
from datetime import datetime, timedelta

from app.services import cartera_service as cs
from tests.test_cartera_retencion import (NIT, _codigos, _historia, _inicio, _tarea,  # noqa: F401
                                          _usuario, fake)


def _con_fe(db, almacen, clave, *, hace_h, consec='900', valor=400_000):
    t = _tarea(db, almacen, clave, estado='DESPACHADO', fe=('FE', consec), valor=valor, cond='C04')
    t.fecha_despachado = datetime.utcnow() - timedelta(hours=hace_h)
    db.session.commit()
    return t


class TestQueConsumeCupo:

    def test_una_fe_pagada_no_consume(self, db, fake, almacen):
        """La FE salió hace 5 días y ya no está en la cartera abierta: se pagó."""
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-401', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-402', lineas=(('SKU1', 10, 900_000),))
        _con_fe(db, almacen, '003-PD-401', hace_h=24 * 5, valor=900_000)
        paso = _inicio('PD402', 'PD', '402', co='003', almacen_id=almacen.id)
        assert paso.pasa, paso.evaluacion.get('motivos')
        assert paso.evaluacion['consumo_wms'] == 0

    def test_una_fe_reciente_sin_indexar_si_consume(self, db, fake, almacen):
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-411', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-412', lineas=(('SKU1', 10, 900_000),))
        _con_fe(db, almacen, '003-PD-411', hace_h=2, valor=900_000)
        paso = _inicio('PD412', 'PD', '412', co='003', almacen_id=almacen.id)
        assert not paso.pasa
        assert _codigos(paso.evaluacion) == {cs.CUPO_EXCEDIDO}
        assert paso.evaluacion['consumo_wms'] == 900_000

    def test_la_fe_que_ya_esta_en_la_cartera_no_se_cuenta_dos_veces(self, db, fake, almacen):
        fake.cliente(cupo=2_000_000)
        fake.factura(saldo=900_000, vence_en=10, consec='921')
        _historia(db, '003-PD-421', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-422', lineas=(('SKU1', 10, 900_000),))
        _con_fe(db, almacen, '003-PD-421', hace_h=2, consec='921', valor=900_000)
        paso = _inicio('PD422', 'PD', '422', co='003', almacen_id=almacen.id)
        assert paso.pasa, (paso.evaluacion.get('motivos'), paso.evaluacion.get('filas'))
        assert paso.evaluacion['consumo_wms'] == 0 and paso.evaluacion['saldo'] == 900_000

    def test_una_fe_sin_fecha_consume(self, db, fake, almacen):
        """No saber cuándo se emitió no prueba que se pagó."""
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-431', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-432', lineas=(('SKU1', 10, 900_000),))
        _tarea(db, almacen, '003-PD-431', estado='DESPACHADO', fe=('FE', '931'), valor=900_000)
        paso = _inicio('PD432', 'PD', '432', co='003', almacen_id=almacen.id)
        assert not paso.pasa

    def test_un_pedido_en_curso_sin_valor_retiene_no_suma_cero(self, db, fake, almacen):
        from app.models.pedido_historia import PedidoHistoria
        fake.cliente(cupo=10_000_000)
        _historia(db, '003-PD-441', lineas=(('SKU1', 10, 100_000),))
        for h in PedidoHistoria.query.filter_by(pedido_clave='003-PD-441'):
            h.vlr_neto = None
        db.session.commit()
        _historia(db, '003-PD-442', lineas=(('SKU1', 10, 100_000),))
        _tarea(db, almacen, '003-PD-441', estado='PENDIENTE')
        paso = _inicio('PD442', 'PD', '442', co='003', almacen_id=almacen.id)
        assert not paso.pasa
        assert _codigos(paso.evaluacion) == {cs.VALOR_DESCONOCIDO}
        assert paso.evaluacion['pedidos_wms_sin_valor'][0]['pedido'] == 'PD441'


class TestQuienPuedeDecidir:

    def test_retiene_sin_nadie_que_decida_lo_avisa(self, db, fake, monkeypatch):
        monkeypatch.delenv('CARTERA_GESTOR_TOKEN', raising=False)
        s = cs.salud()
        assert s['autorizadores']['n'] == 0
        assert any('NADIE puede decidir' in p for p in s['problemas'])

    def test_con_el_gestor_pero_sin_respaldo_avisa_distinto(self, db, fake):
        s = cs.salud()
        assert any('solo el Gestor' in p for p in s['problemas'])
        assert not any('NADIE' in p for p in s['problemas'])

    def test_con_un_autorizador_no_hay_problema(self, db, fake):
        u = _usuario(db, rol='admin', flag=True)
        s = cs.salud()
        assert s['autorizadores']['n'] == 1 and s['autorizadores']['usuarios'][0]['id'] == u.id
        assert not any('autorizar' in p.lower() for p in s['problemas'])

    def test_conductor_con_la_casilla_no_cuenta(self, db, fake):
        _usuario(db, rol='conductor', flag=True)
        assert cs.salud()['autorizadores']['n'] == 0

    def test_en_modo_informa_no_es_problema(self, db, fake, monkeypatch):
        monkeypatch.setenv('CARTERA_COMPUERTA', 'INFORMA')
        monkeypatch.delenv('CARTERA_GESTOR_TOKEN', raising=False)
        assert not any('NADIE' in p for p in cs.salud()['problemas'])

    def test_la_ruta_usa_la_misma_politica(self):
        """`_puede_autorizar_cartera` no decide por su cuenta (AST)."""
        import ast
        import inspect
        from app.routes import _auth_helpers
        src = inspect.getsource(_auth_helpers._puede_autorizar_cartera)
        llamadas = {n.func.id for n in ast.walk(ast.parse(src.strip()))
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert 'puede_autorizar' in llamadas
        assert 'puede_autorizar_cartera' not in {
            n.attr for n in ast.walk(ast.parse(src.strip())) if isinstance(n, ast.Attribute)}
