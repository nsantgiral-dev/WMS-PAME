"""
La cola de Siesa no postea fuera de la ventana (P1-10, parte liquidación,
2026-09-25).

Regla 14: Siesa no opera después de ~8 p. m. La DLQ corría 24/7: un recibo de
caja encolado a las 20:30 gastaba sus reintentos contra un Siesa que no
contesta (5 → 15 → 45 → 120 min) y amanecía FALLIDO. Decisión del dueño:
**Siesa caído = se para todo**, sin caminos alternativos. Fuera de la ventana
la DLQ no toma ningún job que vaya a Siesa (no gasta intentos); el correo, que
no va a Siesa, sí sale.

Una ventana, una función: `app/utils/fecha.en_ventana_siesa` (la usan la DLQ
y la vista previa de la liquidación; `analitica_salud.VENTANA_SIESA` es la
misma constante).
"""
import ast
import pathlib
from datetime import datetime
from unittest.mock import patch

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]


class TestLaVentana:

    @pytest.mark.parametrize('hora,esperado', [
        (6, False), (7, True), (12, True), (19, True), (20, False), (22, False), (3, False)])
    def test_bordes(self, hora, esperado):
        from app.utils.fecha import TZ_BOGOTA, en_ventana_siesa
        m = datetime(2026, 9, 25, hora, 0, tzinfo=TZ_BOGOTA)
        assert en_ventana_siesa(m) is esperado

    def test_un_instante_utc_se_juzga_en_bogota(self):
        from zoneinfo import ZoneInfo
        from app.utils.fecha import en_ventana_siesa
        # 01:30 UTC = 20:30 Bogotá del día anterior.
        assert en_ventana_siesa(datetime(2026, 9, 26, 1, 30, tzinfo=ZoneInfo('UTC'))) is False

    def test_la_salud_usa_la_misma(self):
        from app.services import analitica_salud
        from app.utils import fecha
        assert analitica_salud.VENTANA_SIESA is fecha.VENTANA_SIESA


class TestLaDLQNoPosteaDeNoche:

    def _jobs(self, db):
        from app.models.siesa_job import SiesaJob
        rc = SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': 999999})
        correo = SiesaJob.encolar('ALERTA_EMAIL', {'asunto': 'x'})
        db.session.commit()
        return rc, correo

    def test_de_noche_no_toma_el_recibo_ni_gasta_intentos(self, app, db):
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as s
        rc, correo = self._jobs(db)
        tomados = []
        with patch.object(s, 'dlq_puede_postear', return_value=False), \
                patch.object(s, '_ejecutar_job', side_effect=lambda j: tomados.append(j.tipo) or {}):
            s._run_dlq_jobs()
        rc = db.session.get(SiesaJob, rc.id)
        assert tomados == ['ALERTA_EMAIL']
        assert rc.estado == 'PENDIENTE' and rc.intentos == 0

    def test_de_dia_toma_todo(self, app, db):
        from app.services import siesa_job_service as s
        self._jobs(db)
        tomados = []
        with patch.object(s, 'dlq_puede_postear', return_value=True), \
                patch.object(s, '_ejecutar_job', side_effect=lambda j: tomados.append(j.tipo) or {}):
            s._run_dlq_jobs()
        assert sorted(tomados) == ['ALERTA_EMAIL', 'RECIBO_CAJA']

    def test_en_simulacion_o_ensayo_no_aplica(self, app):
        from app.services import siesa_job_service as s
        from app.services.connekta_gateway import connekta
        with patch.object(connekta, 'modo_simulacion', True):
            assert s.dlq_puede_postear() is True

    def test_con_post_real_manda_la_ventana(self, app):
        from app.services import siesa_job_service as s
        from app.services.connekta_gateway import connekta
        from app.utils.fecha import TZ_BOGOTA
        with patch.object(connekta, 'modo_simulacion', False), \
                patch.object(connekta, 'modo_ensayo', False):
            assert s.dlq_puede_postear(datetime(2026, 9, 25, 20, 30, tzinfo=TZ_BOGOTA)) is False
            assert s.dlq_puede_postear(datetime(2026, 9, 25, 9, 0, tzinfo=TZ_BOGOTA)) is True


class TestTrinquete:

    def test_la_dlq_pregunta_la_ventana_antes_de_tomar(self):
        """Por AST: `_run_dlq_jobs` llama `dlq_puede_postear`, y esa llama
        `en_ventana_siesa`. Quitar cualquiera de las dos es volver a postear
        de noche."""
        src = (RAIZ / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        arbol = ast.parse(src)

        def llamadas(nombre):
            fn = next(n for n in ast.walk(arbol)
                      if isinstance(n, ast.FunctionDef) and n.name == nombre)
            return {getattr(c.func, 'id', getattr(c.func, 'attr', None))
                    for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert 'dlq_puede_postear' in llamadas('_run_dlq_jobs')
        assert 'en_ventana_siesa' in llamadas('dlq_puede_postear')
