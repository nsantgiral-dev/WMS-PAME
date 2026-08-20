"""
Las fugas de VALOR, que el conteo de documentos no puede ver.

Una ruta puede tener todos sus ciclos **completos** —cada entrega con su cobro,
cada cobro con su recibo en Siesa— y aun así faltarle plata. Contando
documentos esa ruta se declara limpia: `documentos = 0` en los tres tramos.

Se reprodujo sobre servicios reales antes de escribir el arreglo: parada de
$50.000 con el RC ya en Siesa por $50.000, reescrita después a $10.000, daba

    cobros_registrados 10.000 · rc_en_siesa 50.000
    ciclos_rotos=0  tasa=0.0  peor_que_la_vara=False

Los $40.000 estaban enteros en el dato y ausentes del veredicto.

## Por qué solo se miden dos cosas y no tres

`cobrado != esperado` **no es un defecto por sí solo**: en una entrega PARCIAL
cobrar menos que la factura es exactamente lo correcto. Marcarlo sería el falso
positivo que quema el canal entero, que es la lección de los 639 avisos
conocidos. Por eso `cobro_incompleto` solo mira entregas COMPLETAS y descuenta
el descuento declarado — y la parcial se deja sin medir en vez de medirse mal.
"""
import pytest

from tests.flujo import conductor_de_flujo as cf


@pytest.fixture
def actores(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('operario', 'op_val@test.com'),
                       ('conductor', 'cond_val@test.com')):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
            u.set_password('test123')
            db.session.add(u)
            db.session.flush()
        out[rol] = u
    db.session.commit()
    return out


@pytest.fixture
def codigos(monkeypatch):
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01')
    monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02')
    return connekta


def _ruta(db, almacen, actores, cond='C02', valor_factura=50000, **entrega):
    from app.models.packing import TareaPacking
    base = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
            'monto_cobrado': 50000}
    base.update(entrega)
    flujo = cf.flujo_completo(db, almacen, actores['operario'].id,
                              actores['conductor'].id, **base)
    t = TareaPacking.query.get(flujo.packing_id)
    t.cond_pago, t.valor_factura = cond, valor_factura
    db.session.commit()
    return flujo


def _rc_completado(db, recaudo_id, monto=50000):
    """El RC que llegó a Siesa, con el monto que llevó de verdad."""
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    SiesaJob.encolar(tipo='RECIBO_CAJA',
                     payload={'recaudo_id': recaudo_id, 'monto': monto},
                     referencia_tipo='RecaudoEntrega', referencia_id=recaudo_id)
    j = SiesaJob.query.filter_by(referencia_id=recaudo_id,
                                 tipo='RECIBO_CAJA').first()
    j.estado = EstadoSiesaJob.COMPLETADO
    db.session.commit()
    return j


class TestElMontoReescritoDespuesDelRecibo:
    """El caso reproducido. `confirmar_parada` acepta re-confirmar una parada
    y no mira `siesa_rc_triggered`: el número local se puede mover después de
    que la plata viajó."""

    def test_se_ve_aunque_los_documentos_cuadren(self, db, almacen, actores,
                                                  codigos):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        _rc_completado(db, f.recaudo_id, monto=50000)

        assert reconciliar(f.ruta_id)['ciclos_rotos'] == 0, 'la sana ya fallaba'

        r = RecaudoEntrega.query.get(f.recaudo_id)
        r.monto_cobrado = 10000          # lo que hace confirmar_parada
        db.session.commit()

        rep = reconciliar(f.ruta_id)
        assert rep['ciclos_rotos'] == 1, (
            f'la ruta se declaró limpia con $40.000 de diferencia entre lo que '
            f'el recibo llevó a Siesa y lo que el WMS dice hoy: {rep["fugas"]}')
        d = next(x for x in rep['detalle'] if x['tramo'] == 'monto_reescrito')
        assert d['cobrado'] == 10000 and d['en_el_recibo'] == 50000

    def test_trae_quien_lo_editó(self, db, almacen, actores, codigos):
        """El rastro no está en el recaudo (no guarda el valor anterior), así
        que el reporte tiene que traer al menos quién y cuándo."""
        from datetime import datetime

        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        _rc_completado(db, f.recaudo_id, monto=50000)
        r = RecaudoEntrega.query.get(f.recaudo_id)
        r.monto_cobrado = 10000
        r.editado_por, r.editado_en = actores['conductor'].id, datetime.utcnow()
        db.session.commit()

        d = next(x for x in reconciliar(f.ruta_id)['detalle']
                 if x['tramo'] == 'monto_reescrito')
        assert d['editado_por'] == actores['conductor'].id
        assert d['editado_en'] is not None

    def test_una_ruta_sana_no_lo_dispara(self, db, almacen, actores, codigos):
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        _rc_completado(db, f.recaudo_id, monto=50000)
        rep = reconciliar(f.ruta_id)
        assert rep['ciclos_rotos'] == 0
        assert not [x for x in rep['detalle'] if x['tramo'] == 'monto_reescrito']


class TestElCobroIncompleto:
    """Entrega completa que cobró menos que la factura. Es el caso que importa
    para fraude y el que `documentos` nunca ve: hay cobro y hay recibo."""

    def test_cobrar_de_menos_en_una_entrega_completa_se_ve(self, db, almacen,
                                                            actores, codigos):
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, valor_factura=50000,
                  monto_cobrado=10000)
        _rc_completado(db, f.recaudo_id, monto=10000)   # el RC cuadra con el cobro

        rep = reconciliar(f.ruta_id)
        assert rep['ciclos_rotos'] == 1, (
            'cobró $10.000 de una factura de $50.000, el recibo cuadra contra '
            'sí mismo y la ruta salió limpia')
        d = next(x for x in rep['detalle'] if x['tramo'] == 'cobro_incompleto')
        assert d['faltante'] == 40000

    def test_el_descuento_declarado_no_es_un_faltante(self, db, almacen,
                                                      actores, codigos):
        """Un descuento con motivo válido es una decisión registrada, no una
        fuga. Contarlo mandaría a investigar algo que ya tiene explicación."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, valor_factura=50000,
                  monto_cobrado=45000)
        r = RecaudoEntrega.query.get(f.recaudo_id)
        r.monto_descuento = 5000
        db.session.commit()
        _rc_completado(db, f.recaudo_id, monto=45000)

        rep = reconciliar(f.ruta_id)
        assert not [x for x in rep['detalle'] if x['tramo'] == 'cobro_incompleto']
        assert rep['ciclos_rotos'] == 0

    def test_una_parcial_que_cobra_menos_no_es_una_fuga(self, db, almacen,
                                                        actores, codigos):
        """**El falso positivo que había que evitar.** En PARCIAL cobrar menos
        que la factura es lo correcto — el cliente se quedó con menos
        mercancía. Marcarlo quema el canal."""
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, valor_factura=50000,
                  estado_entrega='PARCIAL', monto_cobrado=20000,
                  observaciones='el cliente recibió 2 de 5 cajas')
        _rc_completado(db, f.recaudo_id, monto=20000)

        rep = reconciliar(f.ruta_id)
        assert not [x for x in rep['detalle'] if x['tramo'] == 'cobro_incompleto'], (
            'una entrega parcial que cobra menos se reportó como fuga')
        assert rep['ciclos_rotos'] == 0


class TestUnaParadaRotaCuentaUnaVez:
    def test_dos_sintomas_no_inflan_la_tasa(self, db, almacen, actores, codigos):
        """Cobró de menos **y** el monto se reescribió. Es una parada, no dos —
        si contara entradas de `detalle`, la tasa contra la vara saldría
        inflada y un solo caso pesaría doble."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, valor_factura=50000,
                  monto_cobrado=30000)
        _rc_completado(db, f.recaudo_id, monto=30000)
        r = RecaudoEntrega.query.get(f.recaudo_id)
        r.monto_cobrado = 5000           # ahora difiere del recibo Y de la factura
        db.session.commit()

        rep = reconciliar(f.ruta_id)
        tramos = {x['tramo'] for x in rep['detalle']}
        assert tramos == {'monto_reescrito', 'cobro_incompleto'}
        assert rep['ciclos_rotos'] == 1
        assert rep['tasa_ciclos_rotos'] == 1.0
