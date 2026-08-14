"""
Un respaldo que existe no es un respaldo: es un archivo.

Lo que hace falta saber es si, cuando haga falta, se puede volver a operar
desde él. Eso solo se sabe restaurándolo una vez en una base aparte y
mirándolo — y hasta el 2026-08-14 no había con qué mirarlo.

## Lo que la comparación tiene que distinguir

No toda diferencia entre la foto y la copia es un fallo. Un respaldo es de un
momento anterior, así que **es normal que le falten filas operativas**. Lo que
no puede faltar es lo que no se regenera:

    kardex_movimientos, serie_vigia, stock_diario…  → IRRECUPERABLE
    pedidos_siesa, stock_siesa, siesa_jobs…         → se recarga desde Siesa

Tratar las dos igual produce una de dos cosas malas: un respaldo bueno
rechazado por ruido, o uno malo aprobado porque «total, faltan pocas».
"""
import importlib.util
import pathlib

import pytest

_SCRIPT = (pathlib.Path(__file__).resolve().parents[1] / 'scripts'
           / 'verificar_restauracion.py')


@pytest.fixture(scope='module')
def mod():
    spec = importlib.util.spec_from_file_location('_vr', _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _foto(**tablas):
    base = {'kardex_movimientos': 4672, 'serie_vigia': 36,
            'productos': 26294, 'pedidos_siesa': 164}
    base.update(tablas)
    return {'tablas': base, 'head': 'm007retencionesporpuc'}


class TestUnaCopiaSanaPasa:

    def test_identica(self, mod, capsys):
        f = _foto()
        assert mod._comparar(f, f) == 0
        capsys.readouterr()

    def test_con_menos_filas_regenerables(self, mod, capsys):
        """El respaldo es de un momento anterior: que le falten pedidos es
        esperable, y se recargan desde Siesa."""
        assert mod._comparar(_foto(), _foto(pedidos_siesa=100)) == 0
        capsys.readouterr()

    def test_con_mas_filas_que_la_foto(self, mod, capsys):
        """El respaldo puede ser POSTERIOR a la foto. No es un fallo."""
        assert mod._comparar(_foto(), _foto(pedidos_siesa=900)) == 0
        capsys.readouterr()


class TestUnaCopiaQueNoSirveSeRechaza:

    def test_falta_memoria_irrecuperable(self, mod, capsys):
        """`kardex_movimientos` no se regenera: alimenta los cuatro modelos."""
        assert mod._comparar(_foto(), _foto(kardex_movimientos=10)) == 1
        assert 'IRRECUPERABLE' in capsys.readouterr().out

    def test_la_serie_del_cusum_vacia(self, mod, capsys):
        """Sin las 26 semanas de referencia el CUSUM queda ciego ~6 meses."""
        assert mod._comparar(_foto(), _foto(serie_vigia=0)) == 1
        assert 'memoria que no se puede reconstruir' in capsys.readouterr().out

    def test_una_tabla_que_no_existe(self, mod, capsys):
        """Una tabla ausente se lee como cero filas, y cero filas se lee como
        «esa parte del negocio no se usó»."""
        copia = _foto()
        copia['tablas']['productos'] = None
        assert mod._comparar(_foto(), copia) == 1
        assert 'NO EXISTE' in capsys.readouterr().out

    def test_cabeza_de_migraciones_distinta(self, mod, capsys):
        """Una copia en otra revisión no la levanta la app."""
        copia = _foto()
        copia['head'] = 'm003fepersistida'
        assert mod._comparar(_foto(), copia) == 1
        assert 'cabeza de migraciones' in capsys.readouterr().out

    def test_faltan_maestras_que_nadie_clasifico(self, mod, capsys):
        """`productos` no está en irrecuperables ni en regenerables: ante la
        duda, se rechaza. Aprobar por omisión es cómo se aprueba un respaldo
        malo."""
        assert mod._comparar(_foto(), _foto(productos=100)) == 1
        capsys.readouterr()


class TestElScriptNoEscribe:
    """Se corre contra producción para tomar la foto. Si escribiera algo, la
    verificación sería el riesgo."""

    def test_no_hay_escrituras_en_el_fuente(self):
        import ast
        arbol = ast.parse(_SCRIPT.read_text(encoding='utf-8'))
        sql = [n.value for n in ast.walk(arbol)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        for s in sql:
            alto = s.upper()
            for verbo in ('DELETE ', 'DROP ', 'TRUNCATE', 'UPDATE ', 'INSERT '):
                assert verbo not in alto, f'el script contiene {verbo!r}: {s[:80]}'

    def test_solo_hace_select_y_count(self):
        fuente = _SCRIPT.read_text(encoding='utf-8')
        assert 'SELECT count(*)' in fuente
        assert 'db.session.commit' not in fuente
