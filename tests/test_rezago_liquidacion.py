"""
La alerta que el diagnóstico dice que no existe.

BK-OPS-01 v2.1 §4.2: *«Alerta de ruta entregada sin liquidar, con días
transcurridos. Hoy no existe ninguna: una ruta puede quedar sin liquidar
indefinidamente y nadie se entera.»*

Era literal aunque el número sí existiera. `rezago_liquidacion` estaba en el
desglose, pero un número en una pantalla que alguien tiene que abrir no es una
alerta — es un dato que se mira el día que ya es tarde. El mismo documento lo
dice de otra forma: «alguien que compare tres números una vez al mes deja de
hacerlo al tercero».
"""
from datetime import date, datetime, timedelta

import pytest

from app.services import rezago_liquidacion as rz


class _Ruta:
    """Lo mínimo que la política lee. Se usa un doble y no el modelo porque lo
    que se prueba acá es la regla de fechas, no el ORM — el modelo se ejercita
    en los tests del endpoint."""
    def __init__(self, id=1, entregada=None, programada=None):
        self.id = id
        self.fecha_entregada = entregada
        self.fecha_programada = programada
        self.estado_financiero = 'PENDIENTE'


HOY = date(2026, 8, 13)


class TestCuandoUnaRutaEstaAtrasada:

    def test_entregada_hoy_esta_al_dia(self):
        r = _Ruta(entregada=datetime(2026, 8, 13, 20, 0))
        assert rz.urgencia(r, HOY) == rz.OK

    def test_las_ocho_de_la_noche_siguen_siendo_hoy(self):
        """Regla 5. En UTC las 8 p.m. de Bogotá ya son el día siguiente, y esa
        ruta aparecería atrasada el mismo día en que se entregó."""
        r = _Ruta(entregada=datetime(2026, 8, 13, 23, 30))
        assert rz.dias_de_rezago(r, HOY) == 0

    def test_un_dia_ya_es_atraso(self):
        """Se liquida el mismo día. Un día no es tolerancia, es incumplimiento
        del proceso LOG-03."""
        r = _Ruta(entregada=datetime(2026, 8, 12, 9, 0))
        assert rz.urgencia(r, HOY) == rz.ATRASADA

    def test_sin_fecha_aparece_en_la_lista(self):
        """Regla 0: no saber cuándo se entregó no es evidencia de que fue hoy."""
        assert rz.urgencia(_Ruta(), HOY) == rz.ATRASADA
        assert rz.dias_de_rezago(_Ruta(), HOY) is None

    def test_cae_a_la_fecha_programada_si_no_hay_entrega(self):
        r = _Ruta(programada=date(2026, 8, 11))
        assert rz.dias_de_rezago(r, HOY) == 2


class TestLaQueCruzaElMes:
    """La regla de cierre de mes del diagnóstico: las rutas de fin de mes se
    liquidan el mismo día «para que el recaudo quede en el período que
    corresponde»."""

    def test_entregada_el_mes_pasado_es_su_propia_urgencia(self):
        r = _Ruta(entregada=datetime(2026, 7, 31, 18, 0))
        assert rz.urgencia(r, HOY) == rz.CRUZA_MES

    def test_no_se_confunde_con_una_atrasada_del_mismo_mes(self):
        """Liquidar rápido arregla la atrasada y NO arregla la que cruzó mes:
        el período contable no se mueve. Por eso son dos cosas."""
        r = _Ruta(entregada=datetime(2026, 8, 1, 9, 0))
        assert rz.urgencia(r, HOY) == rz.ATRASADA

    def test_un_ano_antes_del_mismo_mes_tambien_cruza(self):
        """Comparar solo el número de mes haría que agosto de 2025 pasara por
        el mes en curso."""
        r = _Ruta(entregada=datetime(2025, 8, 13, 9, 0))
        assert rz.urgencia(r, HOY) == rz.CRUZA_MES

    def test_el_primero_del_mes_deja_atras_todo_lo_de_ayer(self):
        r = _Ruta(entregada=datetime(2026, 8, 31, 18, 0))
        assert rz.urgencia(r, date(2026, 9, 1)) == rz.CRUZA_MES


class TestLaAlertaSale:

    def _ruta_entregada(self, db, dias_atras):
        from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
        from app.models.usuario import Usuario

        cond = Usuario.query.filter_by(email='cond_rezago@test.com').first()
        if not cond:
            cond = Usuario(email='cond_rezago@test.com', nombre='Conductor',
                           rol='conductor', activo=True)
            cond.set_password('test123')
            db.session.add(cond)
            db.session.flush()

        r = RutaDespacho(conductor_id=cond.id, tipo_ruta='Urbana',
                         estado='ENTREGADA',
                         estado_financiero=EstadoFinancieroRuta.PENDIENTE)
        r.fecha_entregada = datetime.utcnow() - timedelta(days=dias_atras)
        db.session.add(r)
        db.session.commit()
        return r

    def test_sin_rutas_pendientes_no_manda_correo(self, app, db, monkeypatch):
        enviados = []
        monkeypatch.setattr('app.services.alertas_service._enviar_email_con_dlq',
                            lambda *a, **k: enviados.append(a))
        from app.services.alertas_service import verificar_y_alertar_rutas_sin_liquidar
        verificar_y_alertar_rutas_sin_liquidar(app=app)
        assert not enviados, 'un aviso que sale siempre deja de ser un aviso'

    def test_una_ruta_atrasada_manda_correo(self, app, db, monkeypatch):
        self._ruta_entregada(db, 3)
        enviados = []
        monkeypatch.setattr('app.services.alertas_service._enviar_email_con_dlq',
                            lambda *a, **k: enviados.append(a))
        from app.services.alertas_service import verificar_y_alertar_rutas_sin_liquidar
        verificar_y_alertar_rutas_sin_liquidar(app=app)
        assert len(enviados) == 1
        assert 'sin liquidar' in enviados[0][0]

    def test_una_ruta_liquidada_no_aparece(self, app, db, monkeypatch):
        from app.models.ruta_despacho import EstadoFinancieroRuta
        r = self._ruta_entregada(db, 3)
        r.estado_financiero = EstadoFinancieroRuta.LIQUIDADA
        db.session.commit()
        enviados = []
        monkeypatch.setattr('app.services.alertas_service._enviar_email_con_dlq',
                            lambda *a, **k: enviados.append(a))
        from app.services.alertas_service import verificar_y_alertar_rutas_sin_liquidar
        verificar_y_alertar_rutas_sin_liquidar(app=app)
        assert not enviados

    def test_el_cron_esta_registrado(self):
        """Una alerta que nadie dispara es exactamente el problema que vino a
        resolver — código correcto, probado y sin caller."""
        import inspect

        from app.services import alertas_service
        fuente = inspect.getsource(alertas_service.init_scheduler)
        assert 'verificar_y_alertar_rutas_sin_liquidar' in fuente
        assert 'alertas_rutas_sin_liquidar' in fuente


class TestElDesgloseYElCronLeenLoMismo:

    def test_el_endpoint_usa_la_politica_compartida(self):
        """Si el endpoint volviera a calcular el rezago por su cuenta, el correo
        y la pantalla podrían discrepar — y el que nadie mira es el correo."""
        import pathlib
        todo = (pathlib.Path(__file__).resolve().parents[1]
                / 'app' / 'routes' / 'rutas.py').read_text(encoding='utf-8')
        i = todo.find('def liquidacion_desglose')
        assert i != -1, '¿se renombró el endpoint del desglose?'
        cuerpo = todo[i:todo.find('\n@rutas_bp.route', i)]
        assert 'rezago_liquidacion as _rz' in cuerpo
        assert 'EstadoFinancieroRuta' not in cuerpo, (
            'el desglose volvió a armar la consulta de rezago por su cuenta')
