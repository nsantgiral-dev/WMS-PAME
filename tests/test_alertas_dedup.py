"""
Una alerta que no se pudo enviar queda VISIBLE en la cola. Siempre.

`_enviar_email_con_dlq` existe por una sola razón: cuando Resend falla, la
alerta no puede desaparecer. Se encola un `SiesaJob(ALERTA_EMAIL)` para que
quede en la cola del WMS y alguien la vea (SF_JOB_SILENCIOSO).

**Al mes de vida dejaba de cumplir esa función.** La consulta de deduplicación
incluía el estado `FALLIDO` —reintentos agotados, la fila queda para siempre— y
**no tenía ninguna cota temporal**:

    _idem = f'ALERTA-{tipo_alerta}-{date.today().isoformat()}'   # ← la fecha
    _ya_en_cola = SiesaJob.query.filter(
        estado.in_(['PENDIENTE', 'PROCESANDO', 'FALLIDO']),
        payload.contains(f'"tipo_alerta": "{tipo_alerta}"'),     # ← y no se usa
    ).first()

La clave con la fecha estaba escrita y la consulta la ignoraba. El primer fallo
definitivo de un tipo de alerta lo silenciaba **para siempre**: meses después,
con Resend caído otra vez, el job no se encolaba porque "ya había uno".

El bug no está en el filtro que falta — está en que la intención se escribió y
no se implementó.
"""
from datetime import timedelta

import pytest

from app.models.siesa_job import SiesaJob
from app.utils.fecha import dia_operativo


def _falla_resend(monkeypatch):
    def _explota(*a, **kw):
        raise RuntimeError('Resend caído')
    monkeypatch.setattr('app.services.alertas_service.enviar_email', _explota)


def _jobs():
    return SiesaJob.query.filter_by(tipo='ALERTA_EMAIL').all()


class TestUnFallidoViejoNoSilenciaLoDeHoy:

    def test_el_fallo_encola_un_job(self, app, db, monkeypatch):
        from app.services.alertas_service import _enviar_email_con_dlq

        _falla_resend(monkeypatch)
        _enviar_email_con_dlq('asunto', '<p>x</p>', 'x', 'stock_critico')
        assert len(_jobs()) == 1

    def test_dos_fallos_el_mismo_dia_no_duplican(self, app, db, monkeypatch):
        """Lo que la deduplicación SÍ tiene que seguir haciendo: un downtime de
        Resend que dura varios ciclos del scheduler no llena la cola."""
        from app.services.alertas_service import _enviar_email_con_dlq

        _falla_resend(monkeypatch)
        for _ in range(4):
            _enviar_email_con_dlq('asunto', '<p>x</p>', 'x', 'stock_critico')
        assert len(_jobs()) == 1

    def test_UN_FALLIDO_DE_OTRO_DIA_NO_BLOQUEA(self, app, db, monkeypatch):
        """EL bug. Un job que agotó reintentos hace un mes silenciaba ese tipo
        de alerta para siempre."""
        from app.services.alertas_service import _enviar_email_con_dlq

        viejo = dia_operativo() - timedelta(days=30)
        SiesaJob.encolar('ALERTA_EMAIL', {
            'tipo_alerta': 'stock_critico',
            'idem_key': f'ALERTA-stock_critico-{viejo.isoformat()}',
        })
        db.session.commit()
        SiesaJob.query.filter_by(tipo='ALERTA_EMAIL').first().estado = 'FALLIDO'
        db.session.commit()

        _falla_resend(monkeypatch)
        _enviar_email_con_dlq('asunto', '<p>x</p>', 'x', 'stock_critico')

        assert len(_jobs()) == 2, (
            'el FALLIDO de hace 30 días bloqueó el job de hoy: esa alerta queda '
            'invisible para siempre, que es justo lo que este wrapper existe '
            'para impedir')

    def test_tipos_distintos_no_se_pisan(self, app, db, monkeypatch):
        from app.services.alertas_service import _enviar_email_con_dlq

        _falla_resend(monkeypatch)
        _enviar_email_con_dlq('a', '<p>x</p>', 'x', 'stock_critico')
        _enviar_email_con_dlq('b', '<p>x</p>', 'x', 'ubicaciones_huerfanas')
        assert len(_jobs()) == 2

    def test_un_fallido_de_HOY_si_deduplica(self, app, db, monkeypatch):
        """La cota es el día, no "siempre" ni "nunca"."""
        from app.services.alertas_service import _enviar_email_con_dlq

        _falla_resend(monkeypatch)
        _enviar_email_con_dlq('a', '<p>x</p>', 'x', 'stock_critico')
        SiesaJob.query.filter_by(tipo='ALERTA_EMAIL').first().estado = 'FALLIDO'
        db.session.commit()

        _enviar_email_con_dlq('a', '<p>x</p>', 'x', 'stock_critico')
        assert len(_jobs()) == 1


class TestLaClaveSeUsaDeVerdad:
    """TRINQUETE — la clave se construía y no se consultaba.

    Es la forma más silenciosa de este error: el código *parece* deduplicar por
    día porque la variable existe y lleva la fecha. Nadie leyendo por encima ve
    que la consulta filtra por otra cosa.
    """

    def test_la_consulta_filtra_por_la_clave_con_fecha(self):
        import ast
        from pathlib import Path

        fuente = (Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'alertas_service.py').read_text(encoding='utf-8')
        i = fuente.index('def _enviar_email_con_dlq')
        cuerpo = fuente[i:i + 2600]
        assert 'idem_key' in cuerpo and '_idem' in cuerpo
        assert '"tipo_alerta": "{tipo_alerta}"' not in cuerpo, (
            'la deduplicación volvió a filtrar por tipo sin cota temporal: un '
            'FALLIDO viejo silencia ese tipo para siempre')

    def test_la_fecha_es_el_dia_operativo(self):
        """`date.today()` en Railway es UTC: el día de la alerta cambiaría a
        las 7 p.m. Colombia, igual que todo lo demás que se corrigió hoy."""
        from pathlib import Path

        fuente = (Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'alertas_service.py').read_text(encoding='utf-8')
        i = fuente.index('def _enviar_email_con_dlq')
        cuerpo = fuente[i:i + 2600]
        assert 'dia_operativo' in cuerpo
        assert 'date.today()' not in cuerpo


# ══════════════════════════════════════════════════════════════════════════
# Paginación de stock: un inventario parcial NO se devuelve como completo
# ══════════════════════════════════════════════════════════════════════════

class TestLaPaginacionNoDevuelveInventarioIncompleto:
    """`_fetch_stock_pages` se tragaba los fallos por página.

    El `except` convertía cualquier error en `{'_error': True}` y el bucle hacía
    `continue`: sin log, sin contador y sin abortar. **Las filas de esa página
    quedaban afuera del resultado**, y quien lo recibe no puede distinguir un
    inventario completo de uno al que le faltan doscientos productos — los
    ausentes se leen como inexistentes, no como desconocidos.

    Y había un tercero que nadie reportó: `self._get()` devuelve `None` cuando
    el circuit breaker bloquea. El `res.get('_error')` del bucle reventaba con
    `AttributeError` sobre `None`.
    """

    def _gw(self):
        from app.services.connekta_gateway import ConnektaGateway

        g = ConnektaGateway()
        g.modo_simulacion = False
        g._cb_state = 'CLOSED'
        return g

    def test_una_pagina_que_falla_aborta_en_vez_de_truncar(self):
        from unittest.mock import patch

        from app.services.connekta_gateway import ConnektaPaginacionError

        gw = self._gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('timeout')):
            with pytest.raises(ConnektaPaginacionError, match='página'):
                gw._fetch_stock_pages('NB1')

    def test_una_respuesta_None_no_revienta_con_AttributeError(self):
        """El breaker abierto devuelve None. Antes: AttributeError críptico."""
        from unittest.mock import patch

        from app.services.connekta_gateway import ConnektaPaginacionError

        gw = self._gw()
        with patch.object(gw, '_get', return_value=None):
            with pytest.raises(ConnektaPaginacionError, match='sin respuesta'):
                gw._fetch_stock_pages('NB1')

    def test_el_error_dice_que_pagina_y_por_que(self):
        from unittest.mock import patch

        from app.services.connekta_gateway import ConnektaPaginacionError

        gw = self._gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('conexión rota')):
            with pytest.raises(ConnektaPaginacionError) as exc:
                gw._fetch_stock_pages('NB1')
        assert 'conexión rota' in str(exc.value)

    def test_el_camino_feliz_sigue_funcionando(self):
        """El arreglo no puede haber roto la consulta normal."""
        from unittest.mock import patch

        gw = self._gw()
        pagina = {'detalle': {'Table': [{'f120_id': 'A', 'x': 1}]}}
        with patch.object(gw, '_get', return_value=pagina):
            filas = gw._fetch_stock_pages('NB1')
        assert len(filas) == 1 and filas[0]['f120_id'] == 'A'
