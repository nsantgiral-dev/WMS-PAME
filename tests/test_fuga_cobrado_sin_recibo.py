"""
💸 «Cobrado sin recibo en Siesa» (2026-09-25).

Plata que el conductor cobró, en rutas **ya liquidadas**, cuyo recibo de caja
no consta en Siesa (`politica_cobro.rc_llegaron`). La fuga que faltaba: «Plata
en la calle» mira las rutas sin liquidar y «Documentos trabados» los FALLIDO;
una ruta liquidada cuyo RC nunca se encoló, se quedó días en la cola, se
descartó o quedó COMPLETADO sin verificar no estaba en ninguna.
"""
import uuid
from datetime import date, datetime, timedelta

import pytest


def _parada(db, almacen, liquidada_hace=1, forma='EFECTIVO', monto=50000, cond='C01'):
    from app.models.conductor import Conductor
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    c = Conductor(nombre='C', cedula=f'C{uuid.uuid4().hex[:8]}', activo=True)
    db.session.add(c)
    db.session.flush()
    cuando = datetime.utcnow() - timedelta(days=liquidada_hace)
    ruta = RutaDespacho(conductor_id=c.id, tipo_ruta='Urbana', estado='ENTREGADA',
                        estado_financiero='LIQUIDADA', liquidada_en=cuando,
                        fecha_entregada=cuando)
    db.session.add(ruta)
    db.session.flush()
    t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                     almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                     consec_docto_pedido_siesa=9, numero_pedido_siesa=f'PD{uuid.uuid4().hex[:5]}',
                     cond_pago=cond)
    db.session.add(t)
    db.session.flush()
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='ENTREGADO',
                       forma_pago=forma, monto_cobrado=monto)
    db.session.add(r)
    db.session.commit()
    return r


def _rc(db, r, estado, resultado=None, horas=0):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': r.id, 'monto': 1},
                         referencia_tipo='RecaudoEntrega', referencia_id=r.id)
    j.estado = estado
    if resultado is not None:
        j.resultado = resultado
    j.fecha_creacion = datetime.utcnow() - timedelta(hours=horas)
    db.session.commit()
    return j


def _fuga():
    from app.services import analitica_fugas as fg
    hoy = date.today()
    return fg.detalle_fuga('cobrado_sin_recibo', hoy - timedelta(days=10), hoy)


def _motivos(det):
    return sorted(c['motivo'] for c in det['casos'])


class TestLaFuga:

    def test_existe_en_el_catalogo_y_en_la_plata_en_riesgo(self):
        from app.services import analitica_fugas as fg
        from app.services.analitica_portada import FUGAS_PLATA_EN_RIESGO
        assert 'cobrado_sin_recibo' in fg.CLAVES
        assert 'cobrado_sin_recibo' in FUGAS_PLATA_EN_RIESGO

    def test_los_cuatro_motivos(self, db, almacen):
        _parada(db, almacen)                                           # nunca encolado
        _rc(db, _parada(db, almacen), 'PENDIENTE', horas=30)           # en cola, viejo
        _rc(db, _parada(db, almacen), 'DESCARTADO')
        _rc(db, _parada(db, almacen), 'COMPLETADO', '{"verificacion_imposible": true}')
        det = _fuga()
        assert _motivos(det) == sorted([
            'Sin recibo encolado', 'Recibo en la cola hace más de un día',
            'Recibo descartado sin constancia en Siesa', 'Recibo dado por hecho sin verificar'])

    def test_lo_que_no_es_fuga(self, db, almacen):
        r_ok = _parada(db, almacen)
        r_ok.siesa_rc_resultado = 'ENVIADO'
        db.session.commit()
        _rc(db, _parada(db, almacen), 'PENDIENTE', horas=1)            # saliendo
        _rc(db, _parada(db, almacen), 'COMPLETADO', '{"codigo": 0}')   # legado confirmado
        _parada(db, almacen, forma='CREDITO', monto=0, cond='C04')     # crédito real
        assert _fuga()['casos'] == []

    def test_el_fallido_esta_en_trabados_y_no_se_suma_dos_veces(self, db, almacen):
        from app.services import analitica_fugas as fg
        _rc(db, _parada(db, almacen), 'FALLIDO')
        hoy = date.today()
        res = fg.FUGAS['cobrado_sin_recibo'].fn(hoy - timedelta(days=10), hoy, fg._Ctx(None))
        assert res.casos == [] and res.extra['en_documentos_trabados'] == 1

    def test_el_valor_es_lo_cobrado(self, db, almacen):
        _parada(db, almacen, monto=73150)
        det = _fuga()
        assert [c['pesos'] for c in det['casos']] == [73150.0]


class TestLosDosNombres:
    """El KPI `rechazos_ruta` (tasa de paradas RECHAZADAS) y la fuga que se
    llamaba igual (plata de lo que volvió, total o en parte) medían distinto con
    el mismo nombre. La fuga pasó a `devoluciones_ruta`."""

    def test_ya_no_se_llaman_igual(self):
        from app.services import analitica_fugas as fg
        from app.services.analitica_kpi import METRICAS
        kpi = {k: m.nombre for k, m in METRICAS.items()}
        assert 'rechazos_ruta' not in fg.FUGAS and 'devoluciones_ruta' in fg.FUGAS
        assert fg.FUGAS['devoluciones_ruta'].titulo != kpi['rechazos_ruta']
