"""
La plata que ya se registró en Siesa no se reescribe desde el WMS.

`confirmar_parada` admite re-confirmar una parada a propósito —un dedazo se
corrige— y anota `editado_por`/`editado_en`. Lo que no tenía era **ninguna
guardia sobre si el recibo ya había salido**: once validaciones, todas sobre la
forma del dato, ninguna sobre la procedencia.

El recibo de caja se arma desde `monto_cobrado`. Movido el campo después del
POST, el WMS y Siesa quedan diciendo cifras distintas y ningún reintento lo
reconcilia: el RC ya existe, es un documento financiero, y corregirlo es una
nota crédito.

## Por qué `siesa_rc_triggered` y no el job COMPLETADO

Es la bandera de **pre-envío** (Regla 6), encendida ANTES del POST y revertida
sola si el POST falla. Congelar recién con el job en COMPLETADO dejaría abierta
la ventana en que el POST está en vuelo — que es justo cuando la divergencia se
vuelve irrecuperable. Regla 0: el lado conservador.

## Por qué el descuento también

`monto_descuento` no viaja en el RC, pero la reconciliación lo **resta** para
decidir si hubo cobro incompleto. Dejarlo abierto permitiría tapar un faltante
de $40.000 escribiendo un descuento de $40.000 después. Ver
`tests/flujo/test_reconciliacion_valor.py`.
"""
import pytest

from tests.flujo import conductor_de_flujo as cf


@pytest.fixture
def actores(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('operario', 'op_frz@test.com'),
                       ('conductor', 'cond_frz@test.com')):
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


def _parada(db, almacen, actores, **entrega):
    from app.models.packing import TareaPacking
    base = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
            'monto_cobrado': 50000}
    base.update(entrega)
    f = cf.flujo_completo(db, almacen, actores['operario'].id,
                          actores['conductor'].id, **base)
    t = TareaPacking.query.get(f.packing_id)
    t.cond_pago, t.valor_factura = 'C02', 50000
    db.session.commit()
    return f


def _rc_salio(db, recaudo_id):
    """El pre-flag de la Regla 6: el POST se intentó y no se revirtió."""
    from app.models.recaudo_entrega import RecaudoEntrega
    r = RecaudoEntrega.query.get(recaudo_id)
    r.siesa_rc_triggered = True
    db.session.commit()
    return r


class TestConElReciboAfuera:
    def test_el_monto_no_se_puede_cambiar(self, db, almacen, actores, codigos):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        f = _parada(db, almacen, actores)
        _rc_salio(db, f.recaudo_id)

        with pytest.raises(ValueError, match='ya se registró en Siesa'):
            RutaService.confirmar_parada(
                f.ruta_id, f.packing_id, actores['conductor'].id,
                {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                 'monto_cobrado': 10000})

        assert float(RecaudoEntrega.query.get(f.recaudo_id).monto_cobrado) == 50000

    def test_el_descuento_tampoco(self, db, almacen, actores, codigos):
        """El agujero por el que se taparía un faltante después del hecho."""
        from app.services.ruta_service import RutaService
        f = _parada(db, almacen, actores)
        _rc_salio(db, f.recaudo_id)

        with pytest.raises(ValueError, match='ya se registró en Siesa'):
            RutaService.confirmar_parada(
                f.ruta_id, f.packing_id, actores['conductor'].id,
                {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                 'monto_cobrado': 50000, 'monto_descuento': 40000,
                 'motivo_descuento': 'RETEFUENTE_2.5'})

    def test_el_mensaje_dice_qué_hacer(self, db, almacen, actores, codigos):
        """Un rechazo sin salida se resuelve por fuera del sistema."""
        from app.services.ruta_service import RutaService
        f = _parada(db, almacen, actores)
        _rc_salio(db, f.recaudo_id)

        with pytest.raises(ValueError) as e:
            RutaService.confirmar_parada(
                f.ruta_id, f.packing_id, actores['conductor'].id,
                {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                 'monto_cobrado': 1})
        assert 'nota crédito' in str(e.value)
        assert '50000' in str(e.value) or '50000.0' in str(e.value)

    def test_lo_que_NO_toca_plata_sigue_editable(self, db, almacen, actores,
                                                  codigos):
        """Congelar de más es su propio daño: el conductor queda trabado en la
        calle por querer corregir una observación."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        f = _parada(db, almacen, actores)
        _rc_salio(db, f.recaudo_id)

        _, es_edicion = RutaService.confirmar_parada(
            f.ruta_id, f.packing_id, actores['conductor'].id,
            {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
             'monto_cobrado': 50000,
             'observaciones': 'el cliente pidió factura por correo'})

        assert es_edicion
        r = RecaudoEntrega.query.get(f.recaudo_id)
        assert r.observaciones == 'el cliente pidió factura por correo'
        assert float(r.monto_cobrado) == 50000


class TestAntesDeQueElReciboSalga:
    def test_corregir_un_dedazo_sigue_funcionando(self, db, almacen, actores,
                                                   codigos):
        """La función que el congelamiento NO puede romper: mientras la plata
        no viajó, el conductor corrige."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        f = _parada(db, almacen, actores, monto_cobrado=5000)

        RutaService.confirmar_parada(
            f.ruta_id, f.packing_id, actores['conductor'].id,
            {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
             'monto_cobrado': 50000})

        assert float(RecaudoEntrega.query.get(f.recaudo_id).monto_cobrado) == 50000

    def test_un_rc_que_fallo_y_revirtio_no_congela(self, db, almacen, actores,
                                                   codigos):
        """La Regla 6 revierte el pre-flag si el POST falla. Con la bandera
        abajo **nada llegó a Siesa**, así que congelar sería trabar una parada
        por un error del ERP."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        f = _parada(db, almacen, actores)
        r = _rc_salio(db, f.recaudo_id)
        r.siesa_rc_triggered = False          # el POST falló y se revirtió
        db.session.commit()

        RutaService.confirmar_parada(
            f.ruta_id, f.packing_id, actores['conductor'].id,
            {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
             'monto_cobrado': 10000})
        assert float(RecaudoEntrega.query.get(f.recaudo_id).monto_cobrado) == 10000
