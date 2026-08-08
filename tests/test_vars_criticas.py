"""
Que el catálogo de variables críticas siga siendo UNO, y siga estando completo.

Contexto (2026-08-08): 159 jobs en FALLIDO, **93 de ellos** por
`SIESA_TIPO_DOCTO_AJUSTE no está configurado`. Esa variable tiene default
(`'ADI'`), así que su ausencia nunca habría roto nada — estaba **declarada con
valor vacío** en Railway, y el string vacío desactiva el default en silencio.

Nadie se enteró en dos meses porque había dos listas de "obligatorias" —una en
`connekta_gateway.__init__` con 4 variables, otra en `health.py` con 9, solo 4
en común— y esa variable no estaba en ninguna. Diez variables con guard de
fallo duro podían reventar con `/api/health/siesa` respondiendo `ok`.

Los tres trinquetes de acá, en orden de qué los hace no ser cosméticos:

  1. **Completitud** — toda variable nombrada en un `raise` del gateway está en
     el catálogo. Se detecta por AST, no por texto: en este repo los
     detectores de texto se atraparon a sí mismos en sus propios comentarios
     tres veces.
  2. **Vacía ≠ ausente** — el caso exacto que costó los 93 jobs.
  3. **Una sola lista** — que nadie vuelva a escribir un catálogo paralelo.
"""
import ast
import re
from pathlib import Path

import pytest

from app.services import vars_criticas as vc

_RAIZ = Path(__file__).resolve().parents[1]
_GATEWAY = _RAIZ / 'app' / 'services' / 'connekta_gateway.py'
_HEALTH = _RAIZ / 'app' / 'routes' / 'health.py'


def _vars_en_guards(archivo: Path) -> dict:
    """Variables `SIESA_*` nombradas dentro de un `raise`. Nombre → línea.

    Por AST y no por regex sobre el archivo entero: un comentario que MENCIONA
    una variable no es un guard, y contarlo obligaría a declarar variables que
    no fallan — ruido en un canal que solo sirve si se puede leer entero.
    """
    arbol = ast.parse(archivo.read_text(encoding='utf-8'))
    salida = {}
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Raise) or nodo.exc is None:
            continue
        for sub in ast.walk(nodo.exc):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                for nombre in re.findall(r'SIESA_[A-Z_]+', sub.value):
                    salida.setdefault(nombre, nodo.lineno)
    return salida


class TestElCatalogoEstaCompleto:

    def test_toda_variable_con_guard_esta_declarada(self):
        """El agujero por donde se coló `SIESA_TIPO_DOCTO_AJUSTE`.

        Si el gateway puede fallar duro por una variable, el health tiene que
        poder decirlo ANTES de que alguien la ejercite. Un guard sin entrada en
        el catálogo es exactamente el estado que tuvo el módulo de conteo
        cíclico entre abril y junio de 2026.
        """
        declaradas = {v.nombre for v in vc.VARS_CRITICAS}
        en_guards = _vars_en_guards(_GATEWAY)
        sin_declarar = {n: l for n, l in en_guards.items() if n not in declaradas}
        assert not sin_declarar, (
            '\nVariables que hacen fallar al gateway y NO están en '
            '`app/services/vars_criticas.py`:\n'
            + '\n'.join(f'  · {n}  (connekta_gateway.py:{l})'
                        for n, l in sorted(sin_declarar.items()))
            + '\n\nCada una puede reventar en producción con /api/health/siesa '
              'diciendo `ok`. Agregala al catálogo con qué rompe — y si su '
              'guard solo dispara cuando la API no trae el dato, marcala '
              '`condicional=True` en vez de forzarla a obligatoria.')

    def test_el_detector_no_esta_ciego(self):
        """Si el AST dejara de encontrar guards, el trinquete pasaría vacío
        para siempre y nadie lo notaría."""
        assert len(_vars_en_guards(_GATEWAY)) >= 15, (
            'el detector encontró menos guards de los que había el 2026-08-08 '
            '— o se borraron guards reales, o el patrón dejó de funcionar')

    def test_ninguna_entrada_fantasma(self):
        """Una variable declarada que ya nadie lee es una exención que no
        protege a nadie, y peor: hace creer que el health cubre algo."""
        fuente_app = ''
        for py in (_RAIZ / 'app').rglob('*.py'):
            if py.name == 'vars_criticas.py':
                continue                      # el catálogo no se cuenta a sí mismo
            fuente_app += py.read_text(encoding='utf-8')
        fantasmas = [v.nombre for v in vc.VARS_CRITICAS if v.nombre not in fuente_app]
        assert not fantasmas, (
            f'declaradas críticas pero que ningún código lee: {fantasmas} — '
            f'borrarlas del catálogo o empezar a usarlas')

    def test_toda_entrada_dice_que_rompe(self):
        """`rompe` lo lee quien mira el health a las 6 a.m. sin conocer el
        código. Un nombre de variable no le dice si puede despachar."""
        mudas = [v.nombre for v in vc.VARS_CRITICAS if len(v.rompe.strip()) < 20]
        assert not mudas, f'entradas sin explicación utilizable: {mudas}'


class TestVaciaNoEsLoMismoQueAusente:
    """El caso exacto de los 93 jobs."""

    _CON_DEFAULT = vc.VarCritica('SIESA_TIPO_DOCTO_AJUSTE', 'ADI', 'x' * 30)
    _SIN_DEFAULT = vc.VarCritica('SIESA_MOTIVO_TRASLADO', None, 'x' * 30)

    def test_declarada_vacia_es_error_aunque_tenga_default(self, monkeypatch):
        """El corazón del incidente. `os.getenv(X, 'ADI')` devuelve `''` —no
        `'ADI'`— cuando X existe vacía, y el guard dispara."""
        monkeypatch.setenv('SIESA_TIPO_DOCTO_AJUSTE', '')
        assert vc.estado(self._CON_DEFAULT) == vc.VACIA

    def test_solo_espacios_cuenta_como_vacia(self, monkeypatch):
        monkeypatch.setenv('SIESA_TIPO_DOCTO_AJUSTE', '   ')
        assert vc.estado(self._CON_DEFAULT) == vc.VACIA

    def test_ausente_con_default_es_ok(self, monkeypatch):
        """No reportar esto es deliberado: el default funciona. Avisar igual
        llenaría el canal de ruido conocido, que es cómo se vuelve invisible
        el aviso que sí importa."""
        monkeypatch.delenv('SIESA_TIPO_DOCTO_AJUSTE', raising=False)
        assert vc.estado(self._CON_DEFAULT) == vc.OK

    def test_ausente_sin_default_es_falta(self, monkeypatch):
        monkeypatch.delenv('SIESA_MOTIVO_TRASLADO', raising=False)
        assert vc.estado(self._SIN_DEFAULT) == vc.FALTA

    def test_con_valor_es_ok(self, monkeypatch):
        monkeypatch.setenv('SIESA_MOTIVO_TRASLADO', '01')
        assert vc.estado(self._SIN_DEFAULT) == vc.OK

    def test_condicional_ausente_no_alarma(self, monkeypatch):
        v = vc.VarCritica('SIESA_PUNTO_ENVIO_DEFAULT', None, 'x' * 30, condicional=True)
        monkeypatch.delenv('SIESA_PUNTO_ENVIO_DEFAULT', raising=False)
        assert vc.estado(v) == vc.OK

    def test_condicional_vacia_si_alarma(self, monkeypatch):
        """Ser condicional no salva de estar declarada en blanco: eso sigue
        siendo alguien que quiso poner un valor y no lo puso."""
        v = vc.VarCritica('SIESA_PUNTO_ENVIO_DEFAULT', None, 'x' * 30, condicional=True)
        monkeypatch.setenv('SIESA_PUNTO_ENVIO_DEFAULT', '')
        assert vc.estado(v) == vc.VACIA


class TestElProblemaSeExplicaSolo:

    def test_una_vacia_dice_que_el_default_quedo_desactivado(self, monkeypatch):
        """El mensaje tiene que contener el diagnóstico, no el síntoma. Quien
        vea 'FALTA' va a agregar la variable — que ya está — y no va a
        entender por qué sigue roto."""
        monkeypatch.setenv('SIESA_TIPO_DOCTO_AJUSTE', '')
        p = [x for x in vc.problemas() if x['variable'] == 'SIESA_TIPO_DOCTO_AJUSTE']
        assert len(p) == 1
        assert p[0]['estado'] == vc.VACIA
        assert 'VACÍO' in p[0]['detalle']
        assert 'ADI' in p[0]['detalle'], 'no dice cuál era el default que se perdió'

    def test_valor_efectivo_muestra_el_default_heredado(self, monkeypatch):
        monkeypatch.delenv('SIESA_TIPO_DOCTO_AJUSTE', raising=False)
        v = vc.VarCritica('SIESA_TIPO_DOCTO_AJUSTE', 'ADI', 'x' * 30)
        assert vc.valor_efectivo(v) == 'ADI'

    def test_todo_bien_no_reporta_nada(self, monkeypatch):
        for v in vc.VARS_CRITICAS:
            monkeypatch.setenv(v.nombre, v.default or 'X')
        assert vc.problemas() == []


class TestUnaSolaLista:
    """Anti-divergencia. El defecto original no fue un bug: fue existir dos
    veces. Un fix futuro aplicado a una sola copia reproduce el incidente."""

    def test_health_no_define_su_propio_catalogo(self):
        fuente = _HEALTH.read_text(encoding='utf-8')
        assert '_VARS_CRITICAS = [' not in fuente, (
            'volvió a aparecer una lista de variables dentro de health.py — '
            'esa es exactamente la divergencia que costó 93 jobs')

    def test_health_consume_el_catalogo(self):
        assert 'vars_criticas' in _HEALTH.read_text(encoding='utf-8')

    def test_el_gateway_consume_el_catalogo(self):
        fuente = _GATEWAY.read_text(encoding='utf-8')
        assert 'vars_criticas' in fuente
        assert "_faltantes.append(" not in fuente, (
            'el gateway volvió a armar su propia lista de obligatorias')

    def test_no_hay_una_tercera_implementacion(self):
        """Contar sitios que declaran variables obligatorias, no confiar en que
        los dos conocidos sigan siendo los únicos.

        Este test existe porque un trinquete anterior de este repo medía una
        proxy —contaba llamadas en UN archivo— y se le escaparon dos
        reimplementaciones.
        """
        sospechosos = []
        for py in (_RAIZ / 'app').rglob('*.py'):
            if py.name == 'vars_criticas.py':
                continue
            texto = py.read_text(encoding='utf-8')
            for n, linea in enumerate(texto.split('\n'), 1):
                # Una lista/tupla literal con 3+ nombres de variables SIESA es
                # un catálogo paralelo naciendo.
                if len(re.findall(r"'SIESA_[A-Z_]+'", linea)) >= 3:
                    sospechosos.append(f'{py.relative_to(_RAIZ)}:{n}')
        assert not sospechosos, (
            'posible catálogo paralelo de variables:\n'
            + '\n'.join(f'  · {s}' for s in sospechosos)
            + '\n\nSi es legítimo, que consuma `vars_criticas.VARS_CRITICAS`.')
