"""
Una fecha en un filtro de Siesa va entre comillas: `lit_fecha`. **Una función.**

Verificado en vivo contra Siesa QA el 2026-09-24 (CO 003,
`API_v2_Ventas_Facturas_DesdePedido`):

    f350_fecha >= 20260101 AND f350_fecha <= 20260923          →   0 filas
    f350_fecha >= ''20260101'' AND f350_fecha <= ''20260923''  → 100 filas

Sin comillas Siesa **no rechaza**: contesta «No se encontraron registros»
(HTTP 400 que `_get` convierte en tabla vacía). Un filtro mal armado se lee
igual que un día sin ventas. `vigia_service._facturas_de_semana` estaba así:
con `VIGIA_INGESTA_FACTURACION` encendida habría escrito cada semana en cero
—la alarma de colapso que la ingesta existe para no fabricar—, y la
«Verificación de ingesta» habría dicho DIFIERE sin decir por qué.

La clase: **una columna de fecha comparada contra un valor interpolado que no
pasó por `lit_fecha`**. Por AST sobre `app/` y `flota/`.
"""
import ast
import pathlib
import re
from datetime import date

import pytest

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_AUTORIZADO = 'app/services/siesa_filtro.py'
_CAMPO_FECHA = re.compile(r'f\d{3}_fecha\w*\s*(>=|<=|=|>|<|<>)\s*$')


def _es_lit_fecha(nodo) -> bool:
    return (isinstance(nodo, ast.Call)
            and getattr(nodo.func, 'id', getattr(nodo.func, 'attr', '')) in
            ('lit_fecha', '_lit_fecha'))


def _fechas_a_mano(fuente: str) -> list:
    hallados = []
    for nodo in ast.walk(ast.parse(fuente)):
        if not isinstance(nodo, ast.JoinedStr):
            continue
        v = nodo.values
        for i in range(len(v) - 1):
            izq, der = v[i], v[i + 1]
            if (isinstance(izq, ast.Constant) and isinstance(izq.value, str)
                    and _CAMPO_FECHA.search(izq.value)
                    and isinstance(der, ast.FormattedValue)
                    and not _es_lit_fecha(der.value)):
                hallados.append(nodo.lineno)
                break
    return hallados


def _fuentes():
    for base in ('app', 'flota'):
        for p in sorted((_RAIZ / base).rglob('*.py')):
            rel = p.relative_to(_RAIZ).as_posix()
            if rel != _AUTORIZADO:
                yield rel, p.read_text(encoding='utf-8')


class TestNingunaFechaSeInterpolaSinComillas:

    def test_ningun_filtro_de_fecha_a_mano(self):
        culpables = [f'{rel}:{ln}' for rel, src in _fuentes()
                     for ln in _fechas_a_mano(src)]
        assert not culpables, (
            f'Fechas en filtros de Siesa sin `lit_fecha`: {culpables}. Sin '
            f'comillas Siesa contesta «sin registros» y se lee como cero.')

    def test_el_detector_ve_la_forma_de_vigia(self):
        """Detector ciego: la línea exacta que había en `vigia_service`."""
        vieja = ("p = (f\"f350_id_co = {_lit(co)} \"\n"
                 "     f\"AND f350_fecha >= {desde.strftime('%Y%m%d')} \")\n")
        assert _fechas_a_mano(vieja) == [1]

    def test_no_marca_la_forma_buena(self):
        buena = "p = f\"AND f350_fecha >= {_lit_fecha(desde)} \"\n"
        assert _fechas_a_mano(buena) == []

    def test_no_marca_un_campo_que_no_es_fecha(self):
        assert _fechas_a_mano('p = f"f350_consec_docto = {n}"\n') == []

    def test_el_escaner_recorre_el_repo(self):
        """Piso: un escáner que no lee nada devuelve cero hallazgos igual."""
        assert sum(1 for _ in _fuentes()) > 150


class TestLitFecha:

    def test_comillas_y_formato(self):
        from app.services.siesa_filtro import lit_fecha
        assert lit_fecha(date(2026, 9, 22)) == "''20260922''"

    @pytest.mark.parametrize('malo', ['20260922', None, 20260922])
    def test_solo_fechas(self, malo):
        from app.services.siesa_filtro import lit_fecha
        with pytest.raises(ValueError):
            lit_fecha(malo)


class TestVigiaYaNoPreguntaConLaFechaDesnuda:

    def test_el_filtro_que_manda_vigia(self):
        from app.services.vigia_service import VigiaService
        vistos = []

        class _C:
            def _get(self, api, params=None, **k):
                vistos.append(params['parametros'])
                return {'detalle': {'Table': []}}

        VigiaService._facturas_de_semana(_C(), '003', date(2026, 9, 14), date(2026, 9, 20))
        assert "f350_fecha >= ''20260914''" in vistos[0]
        assert "f350_fecha <= ''20260920''" in vistos[0]
