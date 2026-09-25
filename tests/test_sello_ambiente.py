"""
Una copia de producción restaurada en QA no postea a Siesa (P0-9).

La clase: **un proceso escribe en Siesa sobre una base que no es de su
ambiente**. La base se sella la primera vez que un proceso va a hablar con
Siesa; desde ahí, un proceso de otro ambiente no postea — ni la DLQ ni un POST
inline. Ver `app/services/sello_ambiente.py`.

Trinquete de clase (AST): todo `requests.post` de `app/` y `flota/` está
declarado, y el único que va a Siesa (`ConnektaGateway._post`) llama a la
pared antes de salir a la red; la DLQ la consulta antes de tocar un job.
"""
import ast
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import sello_ambiente

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Todo `requests.post` que no va a Siesa, con su porqué. Solo encoge.
POSTS_QUE_NO_VAN_A_SIESA = {
    'app/services/alertas_service.py': 'Resend (correo). Lleva el prefijo de ambiente en el asunto.',
    'flota/adaptadores/gupshup.py': 'WhatsApp (Gupshup). Nace apagado y exige FLOTA_AVISOS_REALES.',
}


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    sello_ambiente._olvidar_cache()
    yield
    sello_ambiente._olvidar_cache()


def _sellar(db, ambiente):
    from app.models.sello_ambiente import SelloAmbiente
    db.session.add(SelloAmbiente(ambiente=ambiente, sellado_en=datetime.utcnow(),
                                 sellado_por='test'))
    db.session.commit()


class TestLaDecision:

    def test_sin_variable_postea_y_no_sella(self, db, monkeypatch):
        from app.models.sello_ambiente import SelloAmbiente
        monkeypatch.delenv('RAILWAY_ENVIRONMENT_NAME', raising=False)
        assert sello_ambiente.puede_postear() == (True, '')
        assert SelloAmbiente.query.count() == 0
        assert sello_ambiente.estado()['bloquea'] is False

    def test_base_sin_sello_se_sella_con_el_ambiente_del_proceso(self, db, monkeypatch):
        from app.models.sello_ambiente import SelloAmbiente
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        assert sello_ambiente.puede_postear() == (True, '')
        assert SelloAmbiente.query.one().ambiente == 'QA'

    def test_el_mismo_ambiente_postea(self, db, monkeypatch):
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'production')
        _sellar(db, 'production')
        assert sello_ambiente.puede_postear()[0] is True

    def test_copia_de_produccion_en_qa_no_postea(self, db, monkeypatch):
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        _sellar(db, 'production')
        ok, motivo = sello_ambiente.puede_postear()
        assert ok is False and 'production' in motivo
        assert sello_ambiente.estado()['bloquea'] is True

    def test_sello_ilegible_no_postea(self, db, monkeypatch):
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        with patch.object(sello_ambiente, '_leer', side_effect=RuntimeError('base caída')):
            ok, motivo = sello_ambiente.puede_postear(usar_cache=False)
        assert ok is False and 'No se pudo leer' in motivo


class TestLasDosParedes:

    def test_la_dlq_no_toca_los_jobs_de_la_copia(self, db, monkeypatch):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _run_dlq_jobs
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        _sellar(db, 'production')
        j = SiesaJob.encolar('ALERTA_EMAIL', {'asunto': 'x'})
        atascado = SiesaJob.encolar('ALERTA_EMAIL', {'asunto': 'y'})
        atascado.estado = 'PROCESANDO'
        atascado.fecha_procesando = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        assert _run_dlq_jobs() == 0
        db.session.expire_all()
        assert db.session.get(SiesaJob, j.id).estado == 'PENDIENTE'
        assert db.session.get(SiesaJob, j.id).intentos == 0
        assert db.session.get(SiesaJob, atascado.id).estado == 'PROCESANDO', (
            'el ciclo bloqueado igual reseteó un atascado de la copia')

    def test_un_post_inline_no_sale_a_la_red(self, db, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.sello_ambiente import AmbienteNoCoincide
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        _sellar(db, 'production')
        gw = ConnektaGateway()
        gw.modo_simulacion = False
        gw.modo_ensayo = False
        gw._cb_state = 'CLOSED'
        with patch('app.services.connekta_gateway.requests.post') as red:
            with pytest.raises(AmbienteNoCoincide):
                gw._post('173076', 'X', {'a': 1})
        red.assert_not_called()

    def test_con_el_mismo_ambiente_el_post_sale(self, db, monkeypatch):
        """La otra dirección: la pared no puede tapar el POST legítimo."""
        from app.services.connekta_gateway import ConnektaGateway
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        _sellar(db, 'QA')
        gw = ConnektaGateway()
        gw.modo_simulacion = False
        gw.modo_ensayo = False
        gw._cb_state = 'CLOSED'

        class _R:
            status_code = 200
            text = '{"codigo":0}'
            def json(self):
                return {'codigo': 0, 'mensaje': 'ok'}
            def raise_for_status(self):
                pass
        with patch('app.services.connekta_gateway.requests.post', return_value=_R()) as red:
            try:
                gw._post('173076', 'X', {'a': 1})
            except Exception:
                pass
        red.assert_called_once()


class TestHealthYResellar:

    def test_el_health_lo_declara(self, client, db, jwt_token_admin, monkeypatch):
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        _sellar(db, 'production')
        r = client.get('/api/health/siesa',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d = r.get_json()
        assert d['sello_ambiente']['bloquea'] is True
        assert any('Sello de ambiente' in a for a in d['advertencias'])

    def test_resellar_exige_el_nombre_y_motivo(self, client, db, jwt_token_admin, monkeypatch):
        from app.models.bitacora import BitacoraAccion
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        _sellar(db, 'production')
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.post('/api/health/sello-ambiente', json={'ambiente': 'production',
                                                           'motivo': 'x'}, headers=h)
        assert r.status_code == 400
        r = client.post('/api/health/sello-ambiente', json={'ambiente': 'QA'}, headers=h)
        assert r.status_code == 400
        r = client.post('/api/health/sello-ambiente',
                        json={'ambiente': 'qa', 'motivo': 'copia adoptada tras vaciar jobs'},
                        headers=h)
        assert r.status_code == 200
        assert sello_ambiente.puede_postear()[0] is True
        assert BitacoraAccion.query.filter_by(entidad='SelloAmbiente').count() == 1

    def test_resellar_solo_admin(self, client, db, jwt_token, monkeypatch):
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        r = client.post('/api/health/sello-ambiente', json={'ambiente': 'QA', 'motivo': 'x'},
                        headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# La clase: nadie escribe en Siesa sin pasar por la pared
# ─────────────────────────────────────────────────────────────────────────────

def _llamadas(fn, attr, mod=None):
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == attr and (
                    mod is None or (isinstance(f.value, ast.Name) and f.value.id in mod)):
                out.append(n)
            elif isinstance(f, ast.Name) and f.id == attr and mod is None:
                out.append(n)
    return out


def _posts_http(base=None):
    """{archivo: [(funcion, linea)]} de todo `requests.post(...)`."""
    base = base or RAIZ
    out = {}
    for carpeta in ('app', 'flota'):
        for f in sorted((base / carpeta).rglob('*.py')):
            arbol = ast.parse(f.read_text(encoding='utf-8'))
            for fn in ast.walk(arbol):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for c in _llamadas(fn, 'post', mod={'requests', '_requests'}):
                    out.setdefault(str(f.relative_to(base)), []).append((fn, c.lineno))
    return out


class TestNingunPostASiesaSinLaPared:

    def test_todo_post_http_esta_declarado(self):
        hallados = _posts_http()
        nuevos = set(hallados) - set(POSTS_QUE_NO_VAN_A_SIESA) - {'app/services/connekta_gateway.py'}
        assert not nuevos, (
            f'POST HTTP nuevos sin declarar: {sorted(nuevos)}. Si va a Siesa, tiene '
            'que pasar por ConnektaGateway._post (y su pared de ambiente).')

    def test_la_lista_solo_encoge(self):
        hallados = _posts_http()
        assert set(POSTS_QUE_NO_VAN_A_SIESA) <= set(hallados)

    def test_el_post_a_siesa_llama_a_la_pared_antes_de_la_red(self):
        hallados = _posts_http()['app/services/connekta_gateway.py']
        assert len(hallados) == 1
        fn, linea_red = hallados[0]
        assert fn.name == '_post'
        paredes = [c.lineno for c in _llamadas(fn, 'exigir_para_postear')]
        assert paredes and min(paredes) < linea_red

    def test_la_dlq_consulta_la_pared(self):
        arbol = ast.parse((RAIZ / 'app/services/siesa_job_service.py').read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == '_run_dlq_jobs')
        assert _llamadas(fn, 'puede_postear')

    def test_el_detector_ve_un_post_nuevo(self, tmp_path):
        (tmp_path / 'app').mkdir()
        (tmp_path / 'flota').mkdir()
        (tmp_path / 'app' / 'x.py').write_text(
            'import requests\ndef f():\n    """requests.post(no)"""\n    requests.post("u")\n',
            encoding='utf-8')
        (tmp_path / 'app' / 'y.py').write_text(
            'def g():\n    # requests.post("u")\n    return 1\n', encoding='utf-8')
        assert set(_posts_http(tmp_path)) == {'app/x.py'}

    def test_piso(self):
        assert len(_posts_http()) >= 3
