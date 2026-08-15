"""
La reconciliación, ejercida sobre rutas construidas con los servicios reales.

Un reporte que cuenta mal es peor que no tenerlo: manda a alguien a buscar una
fuga que no existe, o —más caro— declara cuadrada una ruta que perdió plata.
Por eso cada tramo se prueba **rompiéndolo a propósito** y exigiendo que el
reporte lo vea. Sin eso, «0 fugas» no significa nada.
"""
import pytest

from tests.flujo import conductor_de_flujo as cf


@pytest.fixture
def actores(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('operario', 'op_rec@test.com'),
                       ('conductor', 'cond_rec@test.com')):
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


def _ruta(db, almacen, actores, cond='C02', **entrega):
    """Una ruta entregada, con la condición anotada en la parada."""
    from app.models.packing import TareaPacking
    base = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
            'monto_cobrado': 50000}
    base.update(entrega)
    flujo = cf.flujo_completo(db, almacen, actores['operario'].id,
                              actores['conductor'].id, **base)
    t = TareaPacking.query.get(flujo.packing_id)
    t.cond_pago, t.valor_factura = cond, 50000
    db.session.commit()
    return flujo


def _rc_completado(db, recaudo_id, monto=50000):
    """Simula que el recibo de caja llegó a Siesa."""
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    SiesaJob.encolar(tipo='RECIBO_CAJA', payload={'recaudo_id': recaudo_id,
                                                  'monto': monto},
                     referencia_tipo='RecaudoEntrega', referencia_id=recaudo_id)
    j = SiesaJob.query.filter_by(referencia_id=recaudo_id,
                                 tipo='RECIBO_CAJA').first()
    j.estado = EstadoSiesaJob.COMPLETADO
    db.session.commit()
    return j


class TestUnaRutaSanaCuadra:
    def test_los_tres_tramos_medibles_dan_cero(self, db, almacen, actores, codigos):
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        _rc_completado(db, f.recaudo_id)

        rep = reconciliar(f.ruta_id)
        c = rep['columnas']
        assert c['debian_cobrarse']['n'] == 1
        assert c['cobros_registrados']['n'] == 1
        assert c['rc_en_siesa']['n'] == 1
        assert rep['ciclos_rotos'] == 0


class TestCadaTramoSeVeCuandoSeRompe:
    """Los detectores ciegos. Sin esto el reporte no prueba nada."""

    def test_entregado_sin_cobro(self, db, almacen, actores, codigos):
        """El defecto del clasificador, visto desde el reporte: la parada debía
        cobrarse y no hay monto."""
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, monto_cobrado=0, forma_pago='EXENTO')

        rep = reconciliar(f.ruta_id)
        fuga = next(x for x in rep['fugas'] if x['tramo'] == 'entrega_sin_cobro')
        assert fuga['documentos'] == 1 and fuga['valor'] == 50000
        assert rep['ciclos_rotos'] >= 1

    def test_cobrado_sin_recibo_en_siesa(self, db, almacen, actores, codigos):
        """Cobró y el RC no llegó: la plata está en la calle y el saldo abierto.
        No se crea el job — es el caso del 2026-08-11."""
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)

        rep = reconciliar(f.ruta_id)
        fuga = next(x for x in rep['fugas'] if x['tramo'] == 'cobro_sin_recibo')
        assert fuga['documentos'] == 1
        assert rep['detalle'][0]['tramo'] == 'cobro_sin_recibo'

    def test_un_job_fallido_no_cuenta_como_llegado(self, db, almacen, actores,
                                                   codigos):
        """`siesa_rc_triggered` se enciende ANTES del POST (Regla 6): un job
        que falló deja la bandera puesta. Si el reporte mirara la bandera,
        contaría como enviado algo que Siesa nunca vio."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        j = _rc_completado(db, f.recaudo_id)
        j.estado = EstadoSiesaJob.FALLIDO
        r = RecaudoEntrega.query.get(f.recaudo_id)
        r.siesa_rc_triggered = True          # la bandera SÍ quedó encendida
        db.session.commit()

        rep = reconciliar(f.ruta_id)
        assert rep['columnas']['rc_en_siesa']['n'] == 0, (
            'un job FALLIDO se contó como llegado a Siesa — el reporte está '
            'leyendo la bandera de pre-envío en vez del estado del job')

    def test_un_job_descartado_tampoco(self, db, almacen, actores, codigos):
        """`DESCARTADO` es «un humano decidió no intentarlo más». **Nada llegó
        a Siesa.** Ya costó una vez: 103 ajustes abandonados se contaban como
        enviados."""
        from app.models.siesa_job import EstadoSiesaJob
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        j = _rc_completado(db, f.recaudo_id)
        j.estado = EstadoSiesaJob.DESCARTADO
        db.session.commit()

        assert reconciliar(f.ruta_id)['columnas']['rc_en_siesa']['n'] == 0


class TestElDenominadorEsVisible:
    def test_el_credito_real_no_entra_al_denominador(self, db, almacen, actores,
                                                      codigos):
        """Una parada C04 no debía producir plata: contarla como fuga mandaría
        a cobrar algo que nadie debe en la puerta."""
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, cond='C04', monto_cobrado=0,
                  forma_pago='CREDITO')

        rep = reconciliar(f.ruta_id)
        assert rep['columnas']['debian_cobrarse']['n'] == 0
        assert rep['ciclos_rotos'] == 0

    def test_una_parada_sin_condicion_se_cuenta_aparte(self, db, almacen,
                                                        actores, codigos):
        """No está exenta: está **sin clasificar**. Sacarla en silencio encoge
        el universo sin que nadie lo note."""
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, cond=None, monto_cobrado=0,
                  forma_pago='EXENTO')

        rep = reconciliar(f.ruta_id)
        assert rep['columnas']['debian_cobrarse']['n'] == 0
        assert rep['paradas_sin_condicion'] == 1

    def test_el_denominador_del_defecto_no_depende_de_lo_que_marco_el_conductor(
            self, db, almacen, actores, codigos):
        """El test que impide que el defecto se borre de su propia medición.

        Si «debía cobrarse» se dedujera de `forma_pago`, una parada donde la
        pantalla no pidió cobrar saldría del denominador y la fuga sería
        invisible — el instrumento mediría su propia falla como éxito.
        """
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, monto_cobrado=0, forma_pago='EXENTO')
        assert reconciliar(f.ruta_id)['columnas']['debian_cobrarse']['n'] == 1

    def test_el_modo_de_pantalla_viaja_en_el_detalle(self, db, almacen, actores,
                                                     codigos):
        """El diagnóstico del primer tramo sin adivinar: si la pantalla estaba
        en CREDITO sobre una parada cobrable, la causa es el clasificador y no
        un conductor distraído."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, monto_cobrado=0, forma_pago='EXENTO')
        r = RecaudoEntrega.query.get(f.recaudo_id)
        r.modo_pantalla = 'CREDITO'
        db.session.commit()

        assert reconciliar(f.ruta_id)['detalle'][0]['modo_pantalla'] == 'CREDITO'


class TestLaCuartaColumnaSeDeclaraAusente:
    """Lo que separa este reporte de uno que miente."""

    def test_no_se_inventa_ni_se_deriva(self, db, almacen, actores, codigos):
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        _rc_completado(db, f.recaudo_id)

        col = reconciliar(f.ruta_id)['columnas']['recaudo_verificado']
        assert col['capturado'] is False
        assert col['n'] is None and col['valor'] is None, (
            'la cuarta columna trae un número. Si viene de las otras tres, se '
            'está contrastando contra sí misma; si es cero, es una alarma de '
            'faltante total que nadie midió.')

    def test_la_cobertura_dice_cuantos_tramos_se_miden(self, db, almacen,
                                                        actores, codigos):
        """«0 fugas» sobre tres tramos de cuatro no es lo mismo que «0 fugas».
        El denominador tiene que ser visible."""
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores)
        rep = reconciliar(f.ruta_id)
        assert rep['cobertura'] == {'tramos_medibles': 2, 'tramos_totales': 3}
        no_medible = [x for x in rep['fugas'] if not x['medible']]
        assert len(no_medible) == 1


class TestLaVara:
    def test_una_ruta_peor_que_el_proceso_viejo_se_declara(self, db, almacen,
                                                            actores, codigos):
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, monto_cobrado=0, forma_pago='EXENTO')
        rep = reconciliar(f.ruta_id)
        assert rep['tasa_ciclos_rotos'] == 1.0
        assert rep['peor_que_la_vara'] is True

    def test_sin_paradas_cobrables_no_se_afirma_que_salio_bien(
            self, db, almacen, actores, codigos):
        """Regla 0. Sin denominador no hay tasa, y `False` diría «cumplió»."""
        from app.services.reconciliacion_ruta import reconciliar
        f = _ruta(db, almacen, actores, cond='C04', monto_cobrado=0,
                  forma_pago='CREDITO')
        rep = reconciliar(f.ruta_id)
        assert rep['tasa_ciclos_rotos'] is None
        assert rep['peor_que_la_vara'] is None
