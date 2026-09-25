"""
La cola de Siesa no postea fuera de la ventana (P1-10, parte liquidación,
2026-09-25).

Regla 14: Siesa no opera después de ~8 p. m. La DLQ corría 24/7: un recibo de
caja encolado a las 20:30 gastaba sus reintentos contra un Siesa que no
contesta (5 → 15 → 45 → 120 min) y amanecía FALLIDO. Decisión del dueño:
**Siesa caído = se para todo**, sin caminos alternativos. Fuera de la ventana
la DLQ no toma ningún job que vaya a Siesa (no gasta intentos); el correo, que
no va a Siesa, sí sale.

Una ventana, una función: `app/services/ventana_siesa.ventana_abierta`
(06:00–19:30 Bogotá). La usan la DLQ (`dlq_puede_postear`), la vista previa de
la liquidación, el cierre de caja y los crons; `analitica_salud.VENTANA_SIESA`
es la misma constante. Este frente traía su propia `fecha.en_ventana_siesa`
(07:00–20:00); se retiró al integrar los frentes (2026-09-25).
"""
import ast
import pathlib
from datetime import datetime
from unittest.mock import patch

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.usefixtures('ventana_qa')
class TestLaVentana:

    @pytest.mark.parametrize('hora,minuto,esperado', [
        (5, 59, False), (6, 0, True), (12, 0, True), (19, 30, True), (19, 45, False),
        (20, 0, False), (22, 0, False), (3, 0, False)])
    def test_bordes(self, hora, minuto, esperado):
        from app.services.ventana_siesa import ventana_abierta
        from app.utils.fecha import TZ_BOGOTA
        m = datetime(2026, 9, 25, hora, minuto, tzinfo=TZ_BOGOTA)
        assert ventana_abierta(m) is esperado

    def test_un_instante_utc_se_juzga_en_bogota(self):
        from zoneinfo import ZoneInfo
        from app.services.ventana_siesa import ventana_abierta
        # 01:30 UTC = 20:30 Bogotá del día anterior.
        assert ventana_abierta(datetime(2026, 9, 26, 1, 30, tzinfo=ZoneInfo('UTC'))) is False

    def test_la_salud_usa_la_misma(self):
        """La salud no tiene una ventana propia: `tiempo_operativo` pregunta
        a `ventana_siesa` (con ventana, la noche no cuenta)."""
        from datetime import timedelta
        from app.services import analitica_salud
        # 00:30 → 12:30 UTC = 19:30 → 07:30 Bogotá: solo cuenta 06:00–07:30.
        t = analitica_salud.tiempo_operativo(datetime(2026, 9, 26, 0, 30),
                                             datetime(2026, 9, 26, 12, 30))
        assert t == timedelta(hours=1, minutes=30)


class TestSinVentanaNoHayReloj:
    """Tanda 2 · H: sin `SIESA_VENTANA` (producción) la DLQ no frena por
    hora: un POST que falla de noche sigue el backoff y la jerarquía."""

    def test_la_dlq_postea_de_noche(self, app):
        from app.services import siesa_job_service as s
        from app.services.connekta_gateway import connekta
        from app.utils.fecha import TZ_BOGOTA
        with patch.object(connekta, 'modo_simulacion', False), \
                patch.object(connekta, 'modo_ensayo', False):
            assert s.dlq_puede_postear(datetime(2026, 9, 25, 23, 30, tzinfo=TZ_BOGOTA)) is True

    def test_la_salud_cuenta_la_noche(self):
        from datetime import timedelta
        from app.services import analitica_salud
        t = analitica_salud.tiempo_operativo(datetime(2026, 9, 26, 0, 30),
                                             datetime(2026, 9, 26, 12, 30))
        assert t == timedelta(hours=12)


@pytest.mark.usefixtures('ventana_qa')
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

    def test_en_simulacion_no_aplica(self, app):
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
        `ventana_abierta`. Quitar cualquiera de las dos es volver a postear
        de noche."""
        src = (RAIZ / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        arbol = ast.parse(src)

        def llamadas(nombre):
            fn = next(n for n in ast.walk(arbol)
                      if isinstance(n, ast.FunctionDef) and n.name == nombre)
            return {getattr(c.func, 'id', getattr(c.func, 'attr', None))
                    for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert 'dlq_puede_postear' in llamadas('_run_dlq_jobs')
        assert 'ventana_abierta' in llamadas('dlq_puede_postear')
