"""
Sin saber qué viene en camino, la propuesta de contenedor NO es apta (P0-10).

`rop_dual` declaraba `insumo_en_camino.hay_dato=false` cuando ninguna fuente
de «en camino» tenía dato (el espejo de OCs nace apagado, `COMPRAS_OC_SYNC`),
pero `armar_contenedor` no lo propagaba y las dos pantallas mostraban la
propuesta como buena: el término en tránsito valía 0, el faltante salía
inflado y el contenedor —irreversible 120 días— de más.

Una política, `armador_service.aptitud_de_la_propuesta`; el contenedor la
publica (`apta`, `no_apta_por`), la bandeja la reenvía y las dos pantallas lo
dicen. Trinquete: nadie más decide la aptitud (AST).
"""
import ast
import pathlib
from unittest.mock import patch

from app.services.armador_service import ArmadorService, aptitud_de_la_propuesta
from tests.test_compras_bandeja_js import MALO, _node

RAIZ = pathlib.Path(__file__).resolve().parents[1]

SIN_DATO = {'hay_dato': False, 'fuentes': {}, 'fuentes_con_error': [],
            'nota': 'Ninguna fuente de «en camino» tiene datos.'}
COMPLETO = {'hay_dato': True, 'fuentes_con_error': [],
            'fuentes': {'OC_SIESA': {'espejo_completo': True}}}


class TestLaPolitica:

    def test_sin_dato_no_es_apta(self):
        a = aptitud_de_la_propuesta(SIN_DATO)
        assert a['apta'] is False and 'Ninguna fuente' in a['no_apta_por'][0]

    def test_una_fuente_que_fallo_no_es_apta(self):
        a = aptitud_de_la_propuesta(dict(COMPLETO, fuentes_con_error=[
            {'fuente': 'IMPORTACION', 'error': 'boom'}]))
        assert a['apta'] is False and 'IMPORTACION' in a['no_apta_por'][0]

    def test_contenedores_sin_espejo_de_ocs_no_es_apta(self):
        """Hay contenedores registrados (hay_dato) pero las OCs abiertas de
        Siesa nunca se sincronizaron: lo que viene por OC no se resta."""
        a = aptitud_de_la_propuesta({'hay_dato': True, 'fuentes_con_error': [],
                                     'fuentes': {'IMPORTACION': {'refs': 3}}})
        assert a['apta'] is False

    def test_con_todo_es_apta(self):
        assert aptitud_de_la_propuesta(COMPLETO) == {'apta': True, 'no_apta_por': []}

    def test_sin_insumo_no_es_apta(self):
        assert aptitud_de_la_propuesta(None)['apta'] is False


def _rop(insumo):
    return {'china': {'items': [], 'lt_dias': 105, 'sigma_lt': 15,
                      'sigma_lt_fuente': 'DEFAULT_CONSERVADOR'},
            'insumo_en_camino': insumo}


class TestElContenedorLaPropaga:

    def test_armar_contenedor_publica_no_apta(self, db):
        with patch.object(ArmadorService, 'rop_dual', return_value=_rop(SIN_DATO)), \
                patch.object(ArmadorService, 'calcular_sigma_lt_real', return_value={'n': 0}):
            p = ArmadorService.armar_contenedor('40STD')
        assert p['apta'] is False and p['no_apta_por']
        assert p['insumo_en_camino']['hay_dato'] is False

    def test_armar_contenedor_apta_con_dato(self, db):
        with patch.object(ArmadorService, 'rop_dual', return_value=_rop(COMPLETO)), \
                patch.object(ArmadorService, 'calcular_sigma_lt_real', return_value={'n': 0}):
            p = ArmadorService.armar_contenedor('40STD')
        assert p['apta'] is True and p['no_apta_por'] == []


def _propuesta(apta):
    return {'estado': 'PROPUESTA', 'tipos': [], 'tipo': '40STD', 'apta': apta,
            'no_apta_por': [MALO] if not apta else [], 'barras': {}, 'proveedores': [],
            'ventana_llegada': {}, 'lead_time': {}, 'modo': 'ACTIVO'}


class TestLasPantallasLoDicen:

    def test_bandeja(self, tmp_path):
        s = _node(tmp_path, pintar={
            'no': f'cmpContenedorHtml({__import__("json").dumps(_propuesta(False))})',
            'si': f'cmpContenedorHtml({__import__("json").dumps(_propuesta(True))})'})
        assert 'NO APTA' in s['no'] and MALO not in s['no']
        assert 'NO APTA' not in s['si']

    def test_armador_avanzado(self, tmp_path):
        import json
        prop = dict(_propuesta(False), gatillo='NINGUNO', items=[], excluidos=[])
        expr = ("(()=>{const el={innerHTML:''}; try{_renderArmador(el," + json.dumps(prop)
                + ",{},{},{china:{items:[]},nacional:{items:[]}})}catch(e){}; "
                "return el.innerHTML;})()")
        s = _node(tmp_path, pintar={'html': expr})
        assert 'NO APTA' in s['html'] and MALO not in s['html']


class TestNadieMasDecideLaAptitud:
    """Por AST: nadie LEE `hay_dato` (`x.get('hay_dato')` / `x['hay_dato']`)
    fuera de las funciones declaradas: decidir con él es decidir la aptitud,
    y eso lo hace `aptitud_de_la_propuesta` (la bandeja reenvía `apta`)."""

    #: `archivo::funcion` que leen `hay_dato`, con su porqué. Solo encoge.
    DECLARADOS = {
        'app/services/armador_service.py::aptitud_de_la_propuesta':
            'la política misma',
    }

    @staticmethod
    def _lee(n):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get' and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == 'hay_dato'):
            return True
        return (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load)
                and isinstance(n.slice, ast.Constant) and n.slice.value == 'hay_dato')

    def _lectores(self, base=RAIZ):
        out = set()
        for f in sorted((base / 'app').rglob('*.py')):
            arbol = ast.parse(f.read_text(encoding='utf-8'))
            for fn in ast.walk(arbol):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(self._lee(n) for n in ast.walk(fn)):
                        out.add(f'{f.relative_to(base)}::{fn.name}')
        return out

    def test_nadie_mas_lo_lee(self):
        extra = self._lectores() - set(self.DECLARADOS)
        assert not extra, extra

    def test_la_lista_solo_encoge(self):
        assert set(self.DECLARADOS) <= self._lectores()

    def test_el_detector_ve_las_dos_lecturas_y_no_la_escritura(self, tmp_path):
        d = tmp_path / 'app'
        d.mkdir()
        (d / 'x.py').write_text(
            "def a(i):\n    return i.get('hay_dato')\n"
            "def b(i):\n    return i['hay_dato']\n"
            "def c():\n    return {'hay_dato': False}\n"
            "def e():\n    \"\"\"i.get('hay_dato')\"\"\"\n", encoding='utf-8')
        assert self._lectores(tmp_path) == {'app/x.py::a', 'app/x.py::b'}
