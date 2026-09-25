"""
Web y worker con los mismos crons (P3, 2026-09-25).

Si los dos servicios tienen `HEAVY_SCHEDULERS=true`, dos crons corrían dos
veces: el reporte semanal de flota (dos correos) y el refresco de existencias
(dos descargas de ~2 min contra Siesa, cada 45 min, las 24 horas). Y el hook
`post_fork` de `wsgi.py` —que descarta las conexiones heredadas del maestro con
`--preload`— no corría nunca: Gunicorn solo lee hooks de su configuración.
"""
import ast
import pathlib
import runpy
from datetime import datetime, timedelta
from unittest.mock import patch

RAIZ = pathlib.Path(__file__).resolve().parents[1]


class TestElReporteSemanalSaleUnaVez:

    def _correr(self, app):
        from flota.adaptadores import reporte_semanal as rs
        with patch.object(rs, 'reporte_encendido', return_value=True), \
                patch('apscheduler.schedulers.background.BackgroundScheduler.start'):
            sch = rs.init_scheduler(app)
        return sch.get_jobs()[0].func

    def test_si_otro_servicio_ya_lo_mando_no_sale(self, app, db):
        from app.models.cron_latido import CronLatido
        db.session.add(CronLatido(nombre='flota_reporte_semanal', servicio='web', ultimo_ok=True,
                                  ultimo_ok_en=datetime.utcnow() - timedelta(minutes=2),
                                  corridas=1, fallos_seguidos=0))
        db.session.commit()
        job = self._correr(app)
        with patch('flota.adaptadores.reporte_semanal.enviar_reporte_semanal') as env:
            job()
        env.assert_not_called()

    def test_la_primera_de_la_semana_si_sale(self, app, db):
        job = self._correr(app)
        with patch('flota.adaptadores.reporte_semanal.enviar_reporte_semanal') as env:
            job()
        env.assert_called_once()

    def test_con_el_lock_tomado_no_sale(self, app, db):
        from contextlib import contextmanager
        @contextmanager
        def _tomado(*a, **k):
            yield False
        job = self._correr(app)
        with patch('app.utils.lock.advisory_lock', _tomado), \
                patch('flota.adaptadores.reporte_semanal.enviar_reporte_semanal') as env:
            job()
        env.assert_not_called()


class TestElRefrescoDeExistencias:

    def test_fuera_de_la_ventana_no_descarga(self, app, db):
        from app.services import inventario_siesa_service as iss, ventana_siesa
        ventana_siesa._RELOJ_FIJO['abierta'] = False
        capturado = {}
        with patch('threading.Thread') as T, patch('threading.Timer'):
            iss.iniciar_refresh_periodico(app)
            capturado['ciclo'] = T.call_args.kwargs['target']
        with patch.object(iss, '_descargar_inventario_siesa_raw') as raw, \
                patch('threading.Timer'):
            capturado['ciclo']()
        raw.assert_not_called()

    def test_con_el_lock_tomado_no_descarga(self, app, db):
        from contextlib import contextmanager
        from app.services import inventario_siesa_service as iss
        @contextmanager
        def _tomado(*a, **k):
            yield False
        with patch('threading.Thread') as T, patch('threading.Timer'):
            iss.iniciar_refresh_periodico(app)
            ciclo = T.call_args.kwargs['target']
        with patch('app.utils.lock.advisory_lock', _tomado), \
                patch.object(iss, '_descargar_inventario_siesa_raw') as raw, patch('threading.Timer'):
            ciclo()
        raw.assert_not_called()


class TestElHookDeGunicornCorre:

    def test_vive_en_la_configuracion_que_gunicorn_lee(self):
        from gunicorn.config import Config
        assert Config().settings['config'].default == './gunicorn.conf.py'
        ns = runpy.run_path(str(RAIZ / 'gunicorn.conf.py'))
        assert callable(ns.get('post_fork'))
        fn = next(n for n in ast.walk(ast.parse((RAIZ / 'gunicorn.conf.py').read_text()))
                  if isinstance(n, ast.FunctionDef) and n.name == 'post_fork')
        assert any(isinstance(n, ast.Attribute) and n.attr == 'dispose' for n in ast.walk(fn))

    def test_wsgi_no_tiene_hooks_muertos(self):
        arbol = ast.parse((RAIZ / 'wsgi.py').read_text(encoding='utf-8'))
        assert not [n.name for n in arbol.body if isinstance(n, ast.FunctionDef)]
