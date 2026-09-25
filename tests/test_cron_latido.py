"""
Nadie vigilaba a los crons (P1-11, 2026-09-25).

`SCHEDULERS_ACTIVOS` se escribe al arrancar y en memoria: la web no ve los
crons del worker (`alertas_por_correo` salía `false` siempre desde la web), y
un cron que revienta en cada corrida seguía «activo». Ahora cada corrida deja
su latido en la base (`cron_latido`) y el health y la 🩺 Salud lo leen.

Trinquete de clase (AST): todo `add_job` de `app/` y `flota/` pasa su función
por `con_latido(<el mismo id>, fn)`. Un cron nuevo sin latido es un cron que
se puede morir callado.
"""
import ast
import pathlib
from datetime import datetime, timedelta

import pytest

from app.services import cron_latido

RAIZ = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def con_app(app, db):
    cron_latido.usar_app(app)
    yield app


def _fila(nombre):
    from app.models.cron_latido import CronLatido
    from app.extensions import db
    db.session.expire_all()
    return CronLatido.query.filter_by(nombre=nombre).first()


class TestElLatido:

    def test_una_corrida_sana_deja_su_latido(self, con_app, monkeypatch):
        monkeypatch.setenv('RAILWAY_SERVICE_NAME', 'worker')
        f = cron_latido.con_latido('prueba_sana', lambda: 42)
        assert f() == 42
        fila = _fila('prueba_sana')
        assert fila.servicio == 'worker' and fila.ultimo_ok is True
        assert fila.corridas == 1 and fila.fallos_seguidos == 0 and fila.ultimo_ok_en

    def test_una_corrida_que_revienta_lo_dice_y_sigue_reventando(self, con_app):
        def _mal():
            raise RuntimeError('Siesa no responde')
        f = cron_latido.con_latido('prueba_mala', _mal)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                f()
        fila = _fila('prueba_mala')
        assert fila.ultimo_ok is False and fila.fallos_seguidos == 2
        assert 'Siesa no responde' in fila.ultimo_error
        assert 'prueba_mala' in cron_latido.estado()['fallando']

    def test_la_app_se_toma_de_los_argumentos(self, app, db):
        """Los jobs que reciben la app (`kwargs={'app': app}`) la usan."""
        cron_latido.usar_app(None)
        f = cron_latido.con_latido('prueba_kwargs', lambda app=None: 'ok')
        f(app=app)
        assert _fila('prueba_kwargs') is not None

    def test_callado(self, con_app):
        from app.extensions import db
        from app.models.cron_latido import CronLatido
        viejo = datetime.utcnow() - timedelta(hours=2)
        db.session.add(CronLatido(nombre='dlq_siesa_jobs', servicio='web', ultimo_inicio=viejo,
                                  ultimo_fin=viejo, ultimo_ok=True, corridas=5,
                                  fallos_seguidos=0))
        db.session.commit()
        assert 'dlq_siesa_jobs' in cron_latido.estado()['callados']


class TestElHealthLeeLaBase:

    def test_las_alertas_se_ven_aunque_las_corra_otro_servicio(
            self, client, db, jwt_token_admin, con_app):
        """La web no arrancó el cron de alertas; el worker lo corrió hace 1 h."""
        from app.models.cron_latido import CronLatido
        hace = datetime.utcnow() - timedelta(hours=1)
        db.session.add(CronLatido(nombre='resumen_operativo_diario', servicio='worker',
                                  ultimo_inicio=hace, ultimo_fin=hace, ultimo_ok=True,
                                  corridas=1, fallos_seguidos=0))
        db.session.commit()
        r = client.get('/api/health/siesa',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        s = r.get_json()['schedulers']
        assert s['alertas_por_correo'] is True
        assert '[ALERTAS_SCHEDULER]' not in s['activos_en_este_proceso']

    def test_sin_latido_de_alertas_dice_que_no_salen(self, client, db, jwt_token_admin, con_app):
        r = client.get('/api/health/siesa',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.get_json()['schedulers']['alertas_por_correo'] is False

    def test_la_salud_del_dato_lo_lee(self, con_app):
        from app.services.analitica_salud import crons_del_proceso
        assert 'latido' in crons_del_proceso()


# ─────────────────────────────────────────────────────────────────────────────
# La clase: todo cron deja latido
# ─────────────────────────────────────────────────────────────────────────────

def _jobs(base=None):
    """[(archivo, id, envuelto_con_el_mismo_id)] de todo `add_job`."""
    base = base or RAIZ
    out = []
    for carpeta in ('app', 'flota'):
        for f in sorted((base / carpeta).rglob('*.py')):
            for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'add_job'):
                    continue
                ident = next((k.value.value for k in n.keywords if k.arg == 'id'
                              and isinstance(k.value, ast.Constant)), None)
                fn = next((k.value for k in n.keywords if k.arg == 'func'), None) or (
                    n.args[0] if n.args else None)
                envuelto = (isinstance(fn, ast.Call) and isinstance(fn.func, ast.Name)
                            and fn.func.id == 'con_latido' and fn.args
                            and isinstance(fn.args[0], ast.Constant)
                            and fn.args[0].value == ident)
                out.append((str(f.relative_to(base)), ident, envuelto))
    return out


class TestTodoCronDejaLatido:

    def test_ninguno_sin_latido(self):
        sin = [(a, i) for a, i, e in _jobs() if not e]
        assert not sin, (f'add_job sin con_latido(<su id>, fn): {sin}. Un cron que no '
                         'deja latido se puede morir callado.')

    def test_todo_job_tiene_id(self):
        assert all(i for _a, i, _e in _jobs())

    def test_ve_el_job_sin_envolver_y_el_id_equivocado(self, tmp_path):
        (tmp_path / 'app').mkdir()
        (tmp_path / 'flota').mkdir()
        (tmp_path / 'app' / 'x.py').write_text(
            "def i(s):\n"
            "    s.add_job(func=f, id='a')\n"
            "    s.add_job(con_latido('otro', f), id='b')\n"
            "    s.add_job(func=con_latido('c', f), id='c')\n"
            "    '''s.add_job(func=f, id='d')'''\n", encoding='utf-8')
        assert _jobs(tmp_path) == [('app/x.py', 'a', False), ('app/x.py', 'b', False),
                                   ('app/x.py', 'c', True)]

    def test_piso(self):
        assert len(_jobs()) >= 25
