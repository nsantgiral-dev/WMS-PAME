"""
El día operativo, sin depender de la hora a la que corra la suite.

## Qué rompió el build

`52c0e4de`, 2026-08-13 20:41 Bogotá. `test_desbloqueo_con_vigencia_expira`
ponía la vigencia en `date.today() - 1` y el modelo la comparaba contra
`dia_operativo()`. En el contenedor de CI —que corre en UTC— ya era el 14, así
que «ayer» daba 13; en Bogotá `dia_operativo()` también daba 13. **«Ayer» y
«hoy» eran el mismo día**, el desbloqueo seguía vigente y el test falló.

El defecto estaba en el test, no en el modelo: el modelo hace lo correcto —usa
el día operativo, Regla 5— y el test usaba el reloj del contenedor.

## Por qué es peor que un test flaky común

Falla **de un solo lado**: pasa en la máquina de quien lo escribe y falla en el
deploy. Y solo cinco horas al día, así que reintentar «arregla» el síntoma y
esconde la causa. Un build que se arregla esperando es un build en el que nadie
vuelve a confiar.

## Qué hace este archivo

Fija el comportamiento del borde **inyectando el día**, sin leer ningún reloj.
Así el resultado es el mismo a las 3 a.m. y a las 11 p.m., en Bogotá o en UTC.
"""
from datetime import date, timedelta

import pytest

_AYER = date(2026, 8, 12)
_HOY = date(2026, 8, 13)
_MANANA = date(2026, 8, 14)


def _bloqueo(vigencia):
    from app.models.producto_bloqueado import ProductoBloqueado
    b = ProductoBloqueado(
        producto_id=1, motivo='TEST', bloqueado_por_sistema=True,
        desbloqueado_por_id=1, motivo_desbloqueo='urgente',
        cantidad_autorizada=50, vigencia_desbloqueo=vigencia,
    )
    b.activo = True
    return b


@pytest.fixture
def hoy_fijo(app, monkeypatch):
    """Congela el día operativo. Ningún reloj interviene.

    Depende de `app` porque instanciar el modelo configura los mappers de
    SQLAlchemy, y sin la app registrada eso revienta con un `KeyError` sobre
    una clase que no tiene nada que ver.
    """
    monkeypatch.setattr('app.models.producto_bloqueado._dia_operativo',
                        lambda: _HOY)


class TestElBordeDeLaVigencia:

    def test_vigencia_de_ayer_vuelve_a_bloquear(self, hoy_fijo):
        assert _bloqueo(_AYER).esta_bloqueado() is True

    def test_vigencia_de_hoy_sigue_desbloqueado(self, hoy_fijo):
        """El último día cuenta: `<=`. Un desbloqueo «hasta hoy» sirve hoy."""
        assert _bloqueo(_HOY).esta_bloqueado() is False

    def test_vigencia_de_manana_sigue_desbloqueado(self, hoy_fijo):
        assert _bloqueo(_MANANA).esta_bloqueado() is False

    def test_sin_vigencia_es_permanente(self, hoy_fijo):
        assert _bloqueo(None).esta_bloqueado() is False


class TestElModeloNoPuedeVolverAlRelojDelContenedor:
    """Trinquete sobre la causa, no sobre el síntoma.

    Si alguien «simplifica» el modelo a `date.today()`, los tests de arriba
    siguen pasando —inyectan `_dia_operativo`— pero producción empieza a
    desbloquear un día de más entre las 7 p.m. y la medianoche.
    """

    def test_esta_bloqueado_compara_contra_el_dia_operativo(self):
        import inspect

        from app.models.producto_bloqueado import ProductoBloqueado
        fuente = inspect.getsource(ProductoBloqueado.esta_bloqueado)
        assert '_dia_operativo()' in fuente
        assert 'date.today()' not in fuente, (
            'la vigencia volvió a compararse contra el reloj del servidor. '
            'En UTC eso adelanta el día a las 7 p.m. de Bogotá y el desbloqueo '
            'dura un día más de lo autorizado.')


class TestElHelperExisteYEsElCorrecto:

    def test_devuelve_el_mismo_dia_que_usa_el_codigo(self):
        from app.utils.fecha import dia_operativo
        from tests.conftest import hoy_operativo
        assert hoy_operativo() == dia_operativo()
