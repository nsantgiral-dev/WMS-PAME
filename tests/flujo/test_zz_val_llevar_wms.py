"""
Validación crítica de `ConteoService.llevar_wms_a_lo_contado` (qa 44c892f0),
con escenarios propios: movimientos concurrentes REALES entre el conteo y la
ejecución del ajuste, idempotencia, ensayo y la guarda de empacado.
"""
import json

import pytest

from tests.flujo.test_e2e_conteo_escenarios import (  # noqa: F401
    _pedido, _politica_por_defecto, m, siesa)


def _faltante_encolado(m, lugares, contado):
    """CC1 == CC2 = `contado` contra Siesa = suma de lugares: AJ-SAL automático
    encolado (bajo el tope), sin ejecutar todavía."""
    p = m.producto(lugares=lugares)
    sid = m.manual(m.sofi, p)
    m.cc1_cc2(sid, m.ana, m.beto, contado)
    (job,) = m.jobs(sid)
    assert json.loads(job.payload)['motivo_codigo'] == 'AJ-SAL'
    return p, sid, job


def _vendible(m, p):
    from app.models.inventario import UbicacionProducto
    from app.models.ubicacion import Ubicacion
    m.db.session.expire_all()
    return sum(f.cantidad for f in UbicacionProducto.query.join(Ubicacion)
               .filter(Ubicacion.almacen_id == m.nb1.id, UbicacionProducto.producto_id == p.id,
                       Ubicacion.tipo_zona != 'AVERIAS').all())


class TestMovimientosDespuesDelConteo:

    def test_picking_confirmado_entre_el_conteo_y_el_job(self, m):
        from tests.flujo.conductor_de_flujo import hacer_picking
        p, sid, _ = _faltante_encolado(m, {'PIK-1': 50}, 45)
        f = _pedido(m, p)
        with m.simulando():
            hacer_picking(m.db, f, 10, 10)            # 10 salen del estante (SALIDA)
        m.ejecutar_ajustes()
        assert m.wms(p) == 35, m.por_lugar(p)

    @pytest.mark.xfail(strict=True, reason=(
        'VAL-E1: SHORT_PICK escribe cantidad POSITIVA sin saldo, y el WMS baja. '
        'movido_despues_del_conteo la suma como entrada: el WMS termina +2×encontrado '
        '(48 en vez de 42).'))
    def test_short_pick_entre_el_conteo_y_el_job(self, m):
        from app.services.picking_service import PickingService
        p, sid, _ = _faltante_encolado(m, {'PIK-1': 50}, 45)
        f = _pedido(m, p)
        (t,) = PickingService.crear_tareas(producto_id=p.id, cantidad=10, almacen_id=m.nb1.id,
                                           referencia_documento=f.pedido, tipo_documento='PEDIDO')
        m.db.session.commit()
        PickingService.iniciar_picking(t.id, m.ana.id)
        PickingService.reportar_problema(t.id, m.ana.id, 'FALTANTE', 3)
        m.db.session.commit()
        m.ejecutar_ajustes()
        assert m.wms(p) == 42, m.por_lugar(p)          # 45 contadas − 3 que salieron

    @pytest.mark.xfail(strict=True, reason=(
        'VAL-E2: REPOSICION (RESERVA→PICKING) no cambia el total, pero escribe un solo '
        'movimiento +unidades sin saldo: el WMS termina con esas unidades de más.'))
    def test_reposicion_entre_el_conteo_y_el_job(self, m):
        from app.models.inventario import MovimientoInventario, UbicacionProducto
        p, sid, _ = _faltante_encolado(m, {'PIK-1': 20, 'RES-1': 30}, 45)
        # Lo que escribe `reposicion_service.confirmar_reposicion` (líneas 412-446).
        pik, res = m.ub(m.nb1, 'PIK-1'), m.ub(m.nb1, 'RES-1')
        UbicacionProducto.query.filter_by(ubicacion_id=pik.id, producto_id=p.id).one().cantidad += 10
        UbicacionProducto.query.filter_by(ubicacion_id=res.id, producto_id=p.id).one().cantidad -= 10
        m.db.session.add(MovimientoInventario(
            producto_id=p.id, ubicacion_id=pik.id, almacen_id=m.nb1.id, tipo='REPOSICION',
            cantidad=10, motivo='Reposición de prueba', idempotency_key=f'REP-VAL-{p.id}'))
        m.db.session.commit()
        m.ejecutar_ajustes()
        assert m.wms(p) == 45, m.por_lugar(p)


class TestAveriasEnElAlmacen:

    @pytest.mark.xfail(strict=True, reason=(
        'VAL-E3: existencia_wms_del_sku suma la zona AVERIAS (que Siesa ya pasó a AV1). '
        'Un MATCH del estante (100 = Siesa NB1) "cuadra" el WMS a 100 incluyendo las 5 '
        'averiadas: descuenta 5 del stock vendible.'))
    def test_match_no_descuenta_lo_averiado_del_vendible(self, m):
        m.ub(m.nb1, 'AVE-1', 'AVERIAS')
        p = m.producto(lugares={'SIESA-GENERAL': 100, 'AVE-1': 5}, existencia=100)
        sid = m.manual(m.sofi, p)
        r = m.contar(m.ana, sid, 100)
        assert r['resultado'] == 'MATCH', r
        assert _vendible(m, p) == 100, m.por_lugar(p)


class TestIdempotencia:

    def test_recuperacion_despues_de_ejecutar_no_mueve_de_nuevo(self, m):
        from app.models.inventario import MovimientoInventario
        from app.services.siesa_job_service import _ejecutar_job
        from tests.flujo.conductor_de_flujo import hacer_picking
        p, sid, job = _faltante_encolado(m, {'PIK-1': 50}, 45)
        m.ejecutar_ajustes()
        assert m.wms(p) == 45
        f = _pedido(m, p)
        with m.simulando():
            hacer_picking(m.db, f, 10, 10)            # movimiento real después
        n_mov = MovimientoInventario.query.filter_by(producto_id=p.id).count()
        s = m.s(sid)                                  # crash simulado: AJUSTANDO con bandera
        s.estado = 'AJUSTANDO'
        m.db.session.commit()
        _ejecutar_job(job)
        m.db.session.commit()
        assert len(m.siesa.posts) == 1
        assert m.wms(p) == 35
        assert MovimientoInventario.query.filter_by(producto_id=p.id).count() == n_mov


class TestEnsayoYProduccion:

    def test_ensayo_no_cierra_ni_mueve_y_con_ensayo_apagado_si(self, m, monkeypatch):
        from app.models.siesa_job import SiesaJob
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.siesa_job_service import AjusteNoEnviadoEnEnsayo, _ejecutar_job
        p, sid, job = _faltante_encolado(m, {'PIK-1': 50}, 45)
        real_post = ConnektaGateway._post
        monkeypatch.setattr(ConnektaGateway, '_post', lambda self, *a, **k: {'modo_ensayo': True})
        with pytest.raises(AjusteNoEnviadoEnEnsayo):
            _ejecutar_job(job)
        m.db.session.rollback()
        s = m.s(sid)
        assert (s.estado, bool(s.siesa_triggered)) == ('AJUSTANDO', False)
        assert m.wms(p) == 50
        monkeypatch.setattr(ConnektaGateway, '_post', real_post)
        _ejecutar_job(m.db.session.get(SiesaJob, job.id))
        m.db.session.commit()
        s = m.s(sid)
        assert (s.estado, bool(s.siesa_triggered)) == ('AJUSTADO', True)
        assert m.wms(p) == 45 and len(m.siesa.posts) == 1


class TestGuardaEmpacado:

    def test_sobrante_sin_cajas_sale_solo(self, m):
        p = m.producto(lugares={'PIK-1': 50})
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 52)
        assert len(m.jobs(sid)) == 1

    def test_faltante_con_cajas_por_salir_sale_solo(self, m):
        from tests.flujo.conductor_de_flujo import hacer_packing, hacer_picking, siesa_emitio
        p = m.producto(lugares={'PIK-1': 50})
        f = _pedido(m, p)
        with m.simulando():
            hacer_picking(m.db, f, 10, 10)
            hacer_packing(m.db, f)
            siesa_emitio(m.db, f.packing_id)
        m.siesa.poner(p.codigo, 40)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 38)
        assert len(m.jobs(sid)) == 1
