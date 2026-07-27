"""
Trinquete sobre la deuda legacy de SQLAlchemy.

No exige migrar las 224 llamadas: exige que NO CREZCAN. Igual que el guard de
endpoints huérfanos — pasa con lo que hay, revienta con lo nuevo.

Por qué importa más el trinquete que la migración: las llamadas legacy
funcionan y seguirán funcionando ("long term deprecation"). El daño real era
otro — 637 advertencias de una clase conocida hacían invisible cualquier
advertencia NUEVA y real. Eso se arregló declarando el filtro en pytest.ini.
Este archivo evita que la deuda siga creciendo mientras tanto.
"""
import os
import re

import pytest

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Conteo congelado al 2026-07-27. Solo puede BAJAR.
# Migración completa a db.session.get(Modelo, id): tarde tranquila después del
# comité del 7-ago-2026. Anotada como deuda con fecha, no como intención.
MAX_QUERY_GET_APP = 224


def _contar(directorio):
    total, por_archivo = 0, {}
    for raiz, _dirs, archivos in os.walk(os.path.join(_RAIZ, directorio)):
        if '__pycache__' in raiz or 'venv' in raiz:
            continue
        for a in archivos:
            if not a.endswith('.py'):
                continue
            ruta = os.path.join(raiz, a)
            with open(ruta, encoding='utf-8') as f:
                n = len(re.findall(r'\.query\.get\(', f.read()))
            if n:
                por_archivo[os.path.relpath(ruta, _RAIZ)] = n
                total += n
    return total, por_archivo


class TestTrinqueteQueryGet:
    """`Modelo.query.get(id)` es legacy en SQLAlchemy 2.0."""

    def test_no_crece_la_deuda(self):
        total, por_archivo = _contar('app')
        assert total <= MAX_QUERY_GET_APP, (
            f'\nLa deuda legacy creció: {total} llamadas a .query.get() '
            f'(tope {MAX_QUERY_GET_APP}).\n'
            f'Usa db.session.get(Modelo, id) en el código nuevo.\n'
            + '\n'.join(f'  {k}: {v}' for k, v in
                        sorted(por_archivo.items(), key=lambda x: -x[1])[:8])
        )

    def test_si_bajo_hay_que_bajar_el_tope(self):
        """Anti-podredumbre: si se migraron llamadas, el tope debe seguirlas.

        Sin esto el trinquete se afloja solo — un tope que quedó muy por encima
        del real vuelve a permitir que la deuda crezca sin avisar.
        """
        total, _ = _contar('app')
        assert total >= MAX_QUERY_GET_APP - 10, (
            f'Quedan {total} llamadas y el tope sigue en {MAX_QUERY_GET_APP}. '
            f'Baja MAX_QUERY_GET_APP a {total} para que el trinquete siga apretando.'
        )

    def test_el_filtro_de_warnings_esta_declarado_con_su_razon(self):
        """Silenciar sin explicar es esconder. El filtro lleva motivo y fecha."""
        ini = open(os.path.join(_RAIZ, 'pytest.ini'), encoding='utf-8').read()
        assert 'LegacyAPIWarning' in ini
        assert 'REVISAR' in ini, 'el filtro debe declarar cuándo se revisa'
        assert 'db.session.get' in ini, 'debe decir cuál es el reemplazo'


class TestCanalDeAdvertenciasLegible:
    """El canal solo sirve si está callado cuando no pasa nada."""

    def test_no_se_silencia_todo_en_bloque(self):
        """Cambiar un canal ruidoso por uno sordo no es un arreglo."""
        ini = open(os.path.join(_RAIZ, 'pytest.ini'), encoding='utf-8').read()
        assert 'ignore::DeprecationWarning' not in ini
        assert not re.search(r'^\s*ignore\s*$', ini, re.M), \
            'un ignore sin clase silenciaría TODO'

    def test_la_clave_de_test_no_dispara_avisos_de_criptografia(self):
        """Con clave corta PyJWT avisa por cada token: 183 avisos de ruido."""
        conftest = open(os.path.join(_RAIZ, 'tests', 'conftest.py'),
                        encoding='utf-8').read()
        m = re.search(r"os\.environ\['SECRET_KEY'\]\s*=\s*'([^']+)'", conftest)
        assert m, 'no se encontró SECRET_KEY en conftest'
        assert len(m.group(1).encode()) >= 32, \
            'la clave de tests debe tener 32+ bytes (RFC 7518)'


class TestNadaCriticoQuedaFueraDelRepo:
    """El test corre contra GIT, no contra el disco.

    Un test que verifica `Path.exists()` pasa en local aunque el archivo nunca
    haya entrado al repo — y revienta el build. Pasó: `.gitignore` tenía
    `reset_*.py` sin anclar a la raíz y se tragó scripts/reset_transaccional.py.
    `git add -A` no dijo nada; promete "añade todo" y significa "añade todo lo
    no ignorado". Éxito parcial reportado como éxito.

    Estos tests usan `git ls-files` para preguntar qué hay DE VERDAD en el repo.
    """

    def _tracked(self):
        """Ficheros que git conoce de verdad.

        Es un guard de TIEMPO DE DESARROLLO: atrapa lo que existe en la máquina
        pero no en el repo. En el contenedor de build no aplica y no puede
        correr — Railway copia el árbol de archivos, no el repositorio, así que
        no hay .git y `git ls-files` sale con 128.

        Y ahí está la ironía: este test nació para arreglar un test que
        verificaba la máquina en vez del entregable, y cometió la misma falta en
        otra forma. La pregunta "¿está en el repo?" solo tiene sentido donde hay
        repo; donde no lo hay, los archivos presentes SON los del repo por
        construcción.

        El patrón de skip ya existía en test_frontend_integrity (node); faltaba
        aplicarlo aquí.
        """
        import shutil
        import subprocess
        if not shutil.which('git') or not os.path.isdir(os.path.join(_RAIZ, '.git')):
            pytest.skip('sin repositorio git — guard de desarrollo, no aplica al build')
        out = subprocess.run(['git', 'ls-files'], cwd=_RAIZ,
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            pytest.skip(f'git ls-files no disponible: {out.stderr.strip()[:80]}')
        return set(out.stdout.split())

    def test_todo_scripts_esta_en_el_repo(self):
        """scripts/ es código versionado, no borrador local."""
        tracked = self._tracked()
        en_disco = {f'scripts/{a}' for a in os.listdir(os.path.join(_RAIZ, 'scripts'))
                    if a.endswith('.py')}
        fuera = sorted(en_disco - tracked)
        assert not fuera, (
            f'\n{len(fuera)} script(s) existen en disco pero NO en el repo:\n'
            + '\n'.join(f'  · {f}' for f in fuera)
            + '\n\nProbablemente los tragó .gitignore. Comprueba con:\n'
              '  git check-ignore -v <archivo>'
        )

    def test_todos_los_canones_estan_en_el_repo(self):
        """Un canon fuera del repo es un canon que no certifica nada."""
        tracked = self._tracked()
        d = os.path.join(_RAIZ, 'docs', 'canones')
        en_disco = {f'docs/canones/{a}' for a in os.listdir(d) if a.endswith('.json')}
        fuera = sorted(en_disco - tracked)
        assert not fuera, f'canones fuera del repo: {fuera}'

    def test_los_patrones_operacionales_estan_anclados_a_la_raiz(self):
        """Sin barra inicial, `reset_*.py` aplica a cualquier profundidad."""
        gi = open(os.path.join(_RAIZ, '.gitignore'), encoding='utf-8').read()
        for patron in ('fix_*.py', 'debug_*.py', 'diag_*.py', 'reset_*.py',
                       'check_*.py'):
            assert f'\n{patron}' not in gi, (
                f'"{patron}" sin anclar: se tragará cualquier archivo con ese '
                f'nombre en subdirectorios. Usa "/{patron}".'
            )


class TestCanonZonaHoraria:
    """'Semana' es un número con nombre, así que tiene canon.

    Una serie semanal sin zona de corte declarada no es una serie: son dos
    series distintas según dónde corra el servidor.
    """

    def _canon(self):
        import json
        ruta = os.path.join(_RAIZ, 'docs', 'canones', 'zona_horaria.json')
        assert os.path.exists(ruta), 'falta docs/canones/zona_horaria.json'
        with open(ruta, encoding='utf-8') as f:
            return json.load(f)

    def test_declara_la_zona_de_negocio(self):
        c = self._canon()
        assert c['politica']['zona_de_negocio'] == 'America/Bogota'

    def test_responde_las_dos_preguntas(self):
        """Corte de periodos y sellado de timestamps: ambas, explícitas."""
        p = self._canon()['politica']
        assert 'corte_de_periodos' in p and 'almacenamiento_de_timestamps' in p
        assert 'utcnow' in ' '.join(p['almacenamiento_de_timestamps']), \
            'debe prohibir explícitamente datetime.utcnow()'

    def test_declara_que_el_codigo_todavia_no_cumple(self):
        """Declarar la política sin declarar la brecha sería simular que está resuelta."""
        e = self._canon()['estado_actual']
        assert 'NO cumple' in e['_ADVERTENCIA']
        assert len(e['hallazgos']) >= 3
        assert any(not h.get('correcto', True) for h in e['hallazgos'])

    def test_la_serie_reina_referencia_la_politica(self):
        """El empalme solo es comparable si ambas fuentes cortan igual."""
        import json
        ruta = os.path.join(_RAIZ, 'docs', 'canones', 'facturas_co.json')
        with open(ruta, encoding='utf-8') as f:
            c = json.load(f)
        z = c['serie']['zona_de_corte']
        assert z['zona'] == 'America/Bogota'
        assert 'zona_horaria.json' in z['declarada_en']

    def test_la_plantilla_obliga_a_declarar_zona(self):
        """Toda serie periódica futura nace declarando su zona."""
        import json
        ruta = os.path.join(_RAIZ, 'docs', 'canon_PLANTILLA.json')
        with open(ruta, encoding='utf-8') as f:
            c = json.load(f)
        assert 'zona_de_corte' in c['serie']


class TestHermetismoDeLosTests:
    """Ningún test puede llamar al ERP de producción.

    Pasó: el contenedor de build de Railway tiene credenciales Connekta, el
    gateway decide el modo SOLO por `not all([ikey, itoken])`, y
    CONNEKTA_MODO_SIMULACION —que conftest fijaba— no la lee nadie. Resultado:
    un test de descarga pidió 18 páginas reales contra el Siesa que factura en
    siete puntos de venta, durante un build.
    """

    def test_el_gateway_esta_en_simulacion(self, app):
        from app.services.connekta_gateway import connekta
        assert connekta.modo_simulacion is True, (
            'El gateway NO está en simulación: los tests golpearían el ERP real. '
            'Revisa que conftest borre CONNEKTA_IKEY y CONNEKTA_ITOKEN.'
        )

    def test_conftest_borra_las_credenciales_no_solo_pone_una_bandera(self):
        """Poner una bandera que nadie lee es peor que no poner nada."""
        conftest = open(os.path.join(_RAIZ, 'tests', 'conftest.py'),
                        encoding='utf-8').read()
        assert "pop('CONNEKTA_IKEY'" in conftest
        assert "pop('CONNEKTA_ITOKEN'" in conftest

    def test_no_hay_credenciales_en_el_entorno_de_test(self):
        assert not os.environ.get('CONNEKTA_IKEY')
        assert not os.environ.get('CONNEKTA_ITOKEN')
