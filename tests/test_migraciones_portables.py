"""
Las migraciones corren en Postgres, y ningún test las ejercita.

`conftest` usa `create_all()` sobre SQLite: **el esquema de los tests sale de
los modelos, no de las migraciones**. Eso está bien para lo que los tests
verifican, pero deja un hueco entero — el archivo de migración solo se ejecuta
de verdad en el `releaseCommand` del deploy, contra Postgres.

Y ahí las diferencias de dialecto se cobran caro: una migración que falla deja
el release caído y el contenedor viejo corriendo con el código viejo.

## El caso que originó este archivo

    UPDATE recaudos_entrega SET ... WHERE siesa_dc_triggered = 1

SQLite lo acepta —sus booleanos son enteros—. **Postgres lo rechaza**:
`operator does not exist: boolean = integer`. Se detectó revisando antes de
desplegar, no por un test.
"""
import pathlib
import re

import pytest

_MIGRACIONES = sorted(
    (pathlib.Path(__file__).resolve().parents[1] / 'migrations' / 'versions')
    .glob('*.py'))

#: Columnas booleanas conocidas del esquema. Compararlas contra un entero es
#: válido en SQLite y un error de tipo en Postgres.
_SUFIJOS_BOOLEANOS = ('_triggered', '_enviado', '_activo', '_completa',
                      'activo', 'verificado', 'es_total', 'es_parcial')


def _sql_crudo(fuente: str) -> list:
    """El SQL de los `op.execute(...)`, **leído por AST**.

    Ahí es donde el dialecto importa: los `batch_alter_table` los traduce
    Alembic; el SQL crudo no lo traduce nadie.

    La primera versión usaba una expresión regular `op\.execute\((.*?)\)` y
    **se atrapó en su propio comentario**: el docstring de esta clase menciona
    `create_all()`, el paréntesis de esa mención cerró la captura antes de
    tiempo, y el `= 1` que venía después quedó fuera. El detector daba verde
    sobre el archivo que contenía el defecto que buscaba.

    Es el tercer tropiezo igual de esta clase en el repo —CLAUDE.md lo
    documenta— y la respuesta es siempre la misma: **el árbol, no la cadena**.
    Al parsear, los comentarios desaparecen y las cadenas adyacentes ya vienen
    concatenadas por el propio parser.
    """
    import ast
    out = []
    for nodo in ast.walk(ast.parse(fuente)):
        if not isinstance(nodo, ast.Call):
            continue
        f = nodo.func
        if not (isinstance(f, ast.Attribute) and f.attr == 'execute'):
            continue
        for arg in nodo.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append(arg.value)
    return out


class TestNingunaMigracionComparaBooleanosConEnteros:

    @pytest.mark.parametrize('ruta', _MIGRACIONES, ids=lambda p: p.stem)
    def test_sin_comparaciones_de_tipo_incompatible(self, ruta):
        for bloque in _sql_crudo(ruta.read_text(encoding='utf-8')):
            for m in re.finditer(r'(\w+)\s*=\s*([01])\b', bloque):
                col, val = m.group(1), m.group(2)
                if any(col.endswith(s) for s in _SUFIJOS_BOOLEANOS):
                    pytest.fail(
                        f'\n{ruta.name}: `{col} = {val}` en SQL crudo.\n'
                        f'En Postgres `boolean = integer` es un error de tipo y '
                        f'esto corre en el releaseCommand: tumba el deploy.\n'
                        f'Usar `WHERE {col}` o `{col} IS TRUE`.')


class TestLaCadenaDeMigracionesEstaSana:
    """Lo que el deploy necesita para no fallar antes de arrancar."""

    def test_una_sola_cabeza(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        raiz = pathlib.Path(__file__).resolve().parents[1]
        cfg = Config(str(raiz / 'migrations' / 'alembic.ini'))
        cfg.set_main_option('script_location', str(raiz / 'migrations'))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, (
            f'{len(heads)} cabezas: {heads}. Con más de una, `flask db upgrade` '
            f'falla y el release no entra.')

    def test_toda_migracion_declara_su_antecesora(self):
        for ruta in _MIGRACIONES:
            fuente = ruta.read_text(encoding='utf-8')
            assert re.search(r'^down_revision\s*=', fuente, re.M), ruta.name
