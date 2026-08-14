"""
Un endpoint compuesto no puede exigir menos que sus partes.

`/liquidar-completo` es el «one-click»: hace lo mismo que `/liquidar` y
`/liquidar-siesa` —las dos `_solo_admin`— y encima encola las retenciones.
Exigía `_es_admin_o_jefe`.

**Un jefe de almacén recibía 403 en las dos operaciones granulares y 200 en la
que las ejecuta a las dos.** El atajo de conveniencia era la escalada.

Verificado ejecutando la aplicación con credenciales de cada rol.

## Por qué esto es un trinquete y no solo un arreglo

La forma se repite sola: alguien agrega un endpoint que agrupa pasos para que
la pantalla haga una sola llamada, y le pone el permiso de quien va a usar la
pantalla — no el de lo que el endpoint ejecuta. El permiso correcto es el del
más estricto de los pasos.
"""
import re

import pytest

_RUTAS = __import__('pathlib').Path(__file__).resolve().parents[1] / 'app' / 'routes' / 'rutas.py'


def _guard_de(nombre_ruta: str) -> str:
    """El primer guard que aplica el handler de esa ruta."""
    fuente = _RUTAS.read_text(encoding='utf-8')
    i = fuente.find(f"@rutas_bp.route('{nombre_ruta}'")
    assert i != -1, f'no existe la ruta {nombre_ruta}'
    j = fuente.find('@rutas_bp.route(', i + 10)
    cuerpo = fuente[i:j if j > 0 else i + 3000]
    m = re.search(r'if not (_solo_admin|_es_admin_o_jefe|_es_gestion)\(\)', cuerpo)
    return m.group(1) if m else '(sin guard)'


class TestElAtajoNoRelajaElPermiso:

    def test_liquidar_completo_exige_admin(self):
        assert _guard_de('/<int:id>/liquidar-completo') == '_solo_admin'

    @pytest.mark.parametrize('ruta', ['/<int:id>/liquidar', '/<int:id>/liquidar-siesa'])
    def test_las_granulares_siguen_exigiendo_admin(self, ruta):
        """Si alguna se relajara, el invariante se cumpliría bajando el listón
        en vez de subiéndolo — que es la otra forma de romperlo."""
        assert _guard_de(ruta) == '_solo_admin'

    def test_el_compuesto_no_es_mas_flojo_que_sus_partes(self):
        """El invariante, expresado directamente.

        `_solo_admin` ⊂ `_es_admin_o_jefe` ⊂ `_es_gestion`: cuanto más abajo en
        la lista, más gente pasa.
        """
        orden = {'_solo_admin': 0, '_es_admin_o_jefe': 1, '_es_gestion': 2,
                 '(sin guard)': 3}
        compuesto = orden[_guard_de('/<int:id>/liquidar-completo')]
        partes = [orden[_guard_de(r)] for r in
                  ('/<int:id>/liquidar', '/<int:id>/liquidar-siesa')]
        assert compuesto <= min(partes), (
            '\n`/liquidar-completo` exige menos que alguna de las operaciones '
            'que ejecuta. Un jefe de almacén recibiría 403 en la parte y 200 '
            'en el todo.')
