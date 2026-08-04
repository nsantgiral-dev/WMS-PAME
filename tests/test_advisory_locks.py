"""
Todo advisory lock que se toma, se libera.

Los locks de sesión de PostgreSQL viven en la CONEXIÓN, no en la transacción.
Con pool de conexiones, un lock que no se libera **vuelve al pool tomado**: la
corrida siguiente del job pide el lock, se lo niegan, y hace `return`.

Y eso no es un error — es exactamente lo que debe hacer cuando otro worker lo
tiene. El job simplemente deja de correr, para siempre, sin una sola línea de
log. Un invariante ausente no falla: deja pasar.

Auditoría del 2026-08-04: 16 sitios toman locks, 14 los liberaban en `finally`
y **dos no los liberaban nunca**:

  · 2015 — liberación de tareas zombi. Sin ella las tareas quedan EN_PROCESO
    indefinidamente y el operario no puede retomarlas.
  · 2014 — sweep de reconciliación, que detecta tareas de packing que Siesa YA
    procesó y el WMS cree que no. Sin él, el WMS y el ERP divergen en silencio.

El review automático encontró **solo el 2015**. El 2014 salió de auditar el repo
entero en vez de quedarse en lo reportado.
"""
import ast
from pathlib import Path

import pytest

_SERVICIOS = Path(__file__).resolve().parents[1] / 'app' / 'services'
_ADQUIRIR = 'pg_try_advisory_lock'
_LIBERAR = 'pg_advisory_unlock'


def _lineas_de_docstring(arbol):
    """Rangos de los docstrings. Un docstring que CITA el patrón no lo ejecuta.

    `siesa_job_service.disparar_dlq_inmediato` explica en su docstring que el
    advisory lock protege la DLQ — y no toma ninguno. La primera versión de esta
    auditoría lo acusó por eso: buscaba el texto en `ast.dump()`, que incluye las
    constantes de cadena.
    """
    rangos = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cuerpo = getattr(nodo, 'body', [])
        if not cuerpo:
            continue
        primero = cuerpo[0]
        if (isinstance(primero, ast.Expr)
                and isinstance(primero.value, ast.Constant)
                and isinstance(primero.value.value, str)):
            ini = primero.value.lineno
            rangos.update(range(ini, (primero.value.end_lineno or ini) + 1))
    return rangos


def _codigo(nodo, docstrings, fuente):
    """El código de un nodo, sin sus docstrings."""
    ini, fin = nodo.lineno, (nodo.end_lineno or nodo.lineno)
    return '\n'.join(l for n, l in enumerate(fuente.split('\n'), 1)
                     if ini <= n <= fin and n not in docstrings)


def _adquisiciones_desprotegidas():
    """Funciones que TOMAN un lock sin garantizar que se libere.

    Tres formas válidas de garantizarlo:
      · `with advisory_lock(...)` — el context manager de `app.utils.lock`
      · un `try/finally` cuyo `finally` libera
      · delegar en otra función que haga una de las dos
    """
    malas = []
    for f in sorted(_SERVICIOS.glob('*.py')):
        fuente = f.read_text(encoding='utf-8')
        if _ADQUIRIR not in fuente and 'advisory_lock(' not in fuente:
            continue
        arbol = ast.parse(fuente)
        docs = _lineas_de_docstring(arbol)

        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            cuerpo = _codigo(nodo, docs, fuente)
            if _ADQUIRIR not in cuerpo:
                continue

            # ¿Está dentro de un `with advisory_lock(...)`?
            usa_helper = any(
                isinstance(n, ast.With)
                and 'advisory_lock' in ast.dump(ast.Module(body=list(n.items), type_ignores=[]))
                for n in ast.walk(nodo))

            # ¿Hay un `finally` que libera?
            en_finally = any(
                isinstance(n, ast.Try) and n.finalbody
                and _LIBERAR in ast.dump(ast.Module(body=n.finalbody, type_ignores=[]))
                for n in ast.walk(nodo))

            if not (usa_helper or en_finally):
                malas.append(f'{f.name}:{nodo.name}')
    return malas


class TestNingunLockSeQuedaTomado:

    def test_todo_lock_se_libera(self):
        malas = _adquisiciones_desprotegidas()
        assert not malas, (
            '\nToman un advisory lock sin garantizar que se libere. La conexión '
            'vuelve al pool con el lock puesto y el job DEJA DE CORRER, en '
            'silencio:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nUsar `with advisory_lock(clave)` — app/utils/lock.py.')

    def test_la_auditoria_ve_los_locks(self):
        """Si el detector deja de encontrarlos, el test de arriba pasa vacío.

        La primera versión buscaba el texto dentro de `ast.dump()` y acusaba a
        una función que solo lo MENCIONA en su docstring. Un detector que mide
        el texto y no la propiedad da falsos en las dos direcciones.
        """
        con_lock = [f.name for f in _SERVICIOS.glob('*.py')
                    if _ADQUIRIR in f.read_text(encoding='utf-8')]
        assert len(con_lock) >= 8, f'solo {len(con_lock)} servicios con locks'

    def test_el_filtro_de_docstrings_no_apaga_el_guard(self):
        modulo = ast.parse(
            'def f():\n'
            '    """el advisory lock pg_try_advisory_lock protege esto"""\n'
            '    db.execute("SELECT pg_try_advisory_lock(1)")\n'
        )
        docs = _lineas_de_docstring(modulo)
        assert 2 in docs, 'el docstring no se excluye'
        assert 3 not in docs, 'la línea de código se excluye — guard ciego'


class TestElContextManager:

    def test_libera_aunque_el_trabajo_levante(self, app, db):
        """La razón de existir del helper.

        Si el trabajo levanta y el unlock queda fuera del `finally`, la conexión
        vuelve al pool con el lock y el job no corre nunca más.
        """
        from unittest.mock import MagicMock, patch

        from app.utils.lock import advisory_lock

        sesion = MagicMock()
        sesion.execute.return_value.scalar.return_value = True
        with patch('app.extensions.db.session', sesion):
            with pytest.raises(ValueError):
                with advisory_lock(9999):
                    raise ValueError('el trabajo falló')

        sqls = ' '.join(str(c.args[0]) for c in sesion.execute.call_args_list)
        assert 'pg_advisory_unlock' in sqls, 'no liberó tras la excepción'

    def test_no_libera_lo_que_no_tomo(self, app, db):
        """Si otro worker lo tiene, este proceso no debe llamar a unlock:
        liberaría un lock ajeno del mismo… o nada, y confundiría el log."""
        from unittest.mock import MagicMock, patch

        from app.utils.lock import advisory_lock

        sesion = MagicMock()
        sesion.execute.return_value.scalar.return_value = False
        with patch('app.extensions.db.session', sesion):
            with advisory_lock(9999) as tomado:
                assert tomado is False

        sqls = ' '.join(str(c.args[0]) for c in sesion.execute.call_args_list)
        assert 'pg_advisory_unlock' not in sqls

    def test_no_levanta_cuando_otro_lo_tiene(self, app, db):
        """Que otro worker corra el mismo job es NORMAL con varios Gunicorn,
        no una falla. Levantar ahí llenaría el log de errores que no lo son."""
        from unittest.mock import MagicMock, patch

        from app.utils.lock import advisory_lock

        sesion = MagicMock()
        sesion.execute.return_value.scalar.return_value = None
        with patch('app.extensions.db.session', sesion):
            with advisory_lock(9999) as tomado:
                assert tomado is False


class TestLosDosQueSeArreglaronHoy:

    @pytest.mark.parametrize('archivo,funcion', [
        ('abc_service.py', '_liberar_zombis'),
        ('reconciliacion_service.py', '_ejecutar_sweep'),
    ])
    def test_siguen_liberando(self, archivo, funcion):
        assert f'{archivo}:{funcion}' not in _adquisiciones_desprotegidas()
