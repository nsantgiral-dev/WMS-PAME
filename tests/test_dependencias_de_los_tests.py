"""Ningún test importa un paquete que el build de Railway no instala.

El 2026-09-24 el deploy de `0b1d9daa` falló en web y worker por un
`import docx` dentro de un test: `python-docx` estaba en el venv local y no en
`requirements.txt`. Local verde, CI rojo — y la suite local no lo puede ver,
porque en local el paquete sí existe.

La propiedad: todo import de primer nivel en `tests/` resuelve a la biblioteca
estándar, a un paquete del repo, o a una distribución que `requirements.txt`
instala (directa o como dependencia de otra). Por AST; el mapa import→
distribución sale de `importlib.metadata`, no de una lista escrita a mano.
"""
import ast
import importlib.metadata as md
import pathlib
import re
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Paquetes y módulos del propio repo (y los que pytest pone en el path).
LOCALES = {'app', 'flota', 'tests', 'scripts', 'migrations', 'conftest', 'config',
           'wsgi', 'run', 'manage', 'worker'}

#: Imports que se permiten sin estar en requirements, con su motivo. Solo encoge.
PERMITIDOS_SIN_REQUIREMENTS = {}


def _nombre_dist(linea: str):
    linea = linea.split('#', 1)[0].strip()
    if not linea or linea.startswith('-'):
        return None
    return re.split(r'[<>=!~\[; ]', linea, 1)[0].strip().lower().replace('_', '-')


def _distribuciones_instaladas_por_requirements():
    """Cierre transitivo de requirements.txt según los metadatos instalados."""
    pendientes = [_nombre_dist(l) for l in
                  (RAIZ / 'requirements.txt').read_text(encoding='utf-8').splitlines()]
    pendientes = [p for p in pendientes if p]
    vistos = set()
    while pendientes:
        d = pendientes.pop()
        if d in vistos:
            continue
        vistos.add(d)
        try:
            reqs = md.requires(d) or []
        except md.PackageNotFoundError:
            continue
        for r in reqs:
            if 'extra ==' in r:
                continue
            n = _nombre_dist(r)
            if n:
                pendientes.append(n)
    return vistos


def _imports_de_primer_nivel(ruta: pathlib.Path):
    arbol = ast.parse(ruta.read_text(encoding='utf-8'))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for a in nodo.names:
                yield a.name.split('.')[0], nodo.lineno
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
            yield nodo.module.split('.')[0], nodo.lineno


def _faltantes(archivos, permitidas):
    mapa = md.packages_distributions()
    malos = []
    for f in archivos:
        for mod, linea in _imports_de_primer_nivel(f):
            if (mod in sys.stdlib_module_names or mod in LOCALES
                    or mod in PERMITIDOS_SIN_REQUIREMENTS or mod.startswith('_')):
                continue
            dists = {d.lower().replace('_', '-') for d in mapa.get(mod, [])}
            if not dists or not (dists & permitidas):
                malos.append(f'{f.relative_to(RAIZ)}:{linea} import {mod} '
                             f'(distribución: {sorted(dists) or "desconocida"})')
    return malos


def _archivos_de_tests():
    return sorted(p for p in (RAIZ / 'tests').rglob('*.py'))


def test_ningun_test_importa_lo_que_el_build_no_instala():
    permitidas = _distribuciones_instaladas_por_requirements()
    malos = _faltantes(_archivos_de_tests(), permitidas)
    assert not malos, ('Estos imports no los instala requirements.txt: pasan en '
                       'local y rompen el build de Railway.\n  ' + '\n  '.join(malos))


class TestElGuardMuerde:

    def test_detecta_un_import_fuera_de_requirements(self, tmp_path, monkeypatch):
        f = tmp_path / 'tests' / 'test_x.py'
        f.parent.mkdir()
        f.write_text('import docx\nfrom docx import Document\n', encoding='utf-8')
        monkeypatch.setattr(sys.modules[__name__], 'RAIZ', tmp_path)
        # «docx» no está en el cierre de requirements: aunque no esté instalado
        # (distribución desconocida) tiene que marcarse.
        assert len(_faltantes([f], {'flask'})) == 2

    def test_no_marca_stdlib_locales_ni_requirements(self, tmp_path, monkeypatch):
        permitidas = _distribuciones_instaladas_por_requirements()
        f = tmp_path / 'tests' / 'test_y.py'
        f.parent.mkdir()
        f.write_text('import os, json\nfrom app import db\nimport flask\n'
                     'from . import algo\n', encoding='utf-8')
        monkeypatch.setattr(sys.modules[__name__], 'RAIZ', tmp_path)
        assert _faltantes([f], permitidas) == []

    def test_piso(self):
        assert len(_archivos_de_tests()) >= 300
        assert len(_distribuciones_instaladas_por_requirements()) >= 25
