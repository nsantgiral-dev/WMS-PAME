"""
Qué corre al CARGAR un módulo del PWA — y por qué eso rompe el build.

El 2026-08-06 agregué `tests/test_cliente_js.py`, que carga módulos del PWA en
Node para ejercitar su lógica. Tumbó el build de Railway dos veces:

  1. El contenedor de build no tenía `node` —solo se detecta Python, no hay
     `package.json`— y el test falla duro a propósito. Se arregló con
     `nixpacks.toml`.
  2. `rutas.js:138` tiene una IIFE de nivel de módulo que lee
     `navigator.userAgent` **al cargar el archivo**. En mi Node 22 `navigator`
     existe; en el Node 18 de Railway no. Pasaba en local y reventaba allá con
     `ReferenceError: navigator is not defined`.

La lección no es "faltaba mockear navigator". Es que **cada línea que se ejecuta
al cargar un módulo es una dependencia oculta del entorno**: obliga a que
cualquier arnés futuro adivine qué globals hacer aparecer, y el fallo llega en
el build, no en la máquina de quien lo escribió.

Los dos casos que existen hoy NO son bugs: los scripts van al final del `body`
(línea 3286 de `index.html`, después de todo el DOM), así que encuentran lo que
buscan. Están declarados para que sean visibles y para que el tercero no entre
sin que nadie lo decida.
"""
import re
from pathlib import Path

import pytest

_PWA = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa'

# ══════════════════════════════════════════════════════════════════════════
# INVENTARIO DECLARADO — no son excepciones, son deuda con nombre.
#
# Cada entrada es código que corre al cargar el archivo y que por lo tanto
# **cualquier arnés en Node tiene que anticipar**. Solo puede ENCOGER: hay un
# test abajo que lo obliga.
# ══════════════════════════════════════════════════════════════════════════
DECLARADOS = {
    # Aplica el tema guardado ANTES del primer render. Tiene que correr al
    # cargar: diferirlo produce un parpadeo de oscuro a claro en cada entrada.
    # Lee `localStorage` y `document.body`.
    ('app.js', '(function () {'),
    # Muestra el botón «tocar para escanear» solo en móvil. Lee
    # `navigator.userAgent` — el que rompió el build de Railway.
    # Podría vivir dentro de `cargarMuelle()`; no se movió porque funciona y
    # moverlo hoy es churn sin usuario que lo pida.
    ('rutas.js', '(function initMuelleUXMobile() {'),
}

#: Globals que el arnés de `tests/js/verificar_cliente.js` hace aparecer.
#: Si un efecto de carga nuevo necesita otro, el arnés hay que ampliarlo — y
#: este trinquete es el que avisa antes de que lo haga el build.
GLOBALS_DEL_ARNES = ('document', 'window', 'navigator', 'API', 'TOKEN', 'get', 'alerta')


def _efectos_de_carga(archivo):
    """Sentencias en columna 0 que EJECUTAN algo al cargar el archivo.

    Columna 0 y no cualquier indentación: lo que está dentro de una función solo
    corre cuando alguien la llama. Lo que está al margen izquierdo corre siempre
    que el archivo se cargue, en cualquier entorno.

    Se detectan dos formas: la IIFE —`(function...`, `(()...`, `!function`— y la
    llamada suelta —`algo();`—. Las declaraciones (`function`, `const`, `var`)
    no ejecutan nada.
    """
    salida = []
    for n, linea in enumerate(archivo.read_text(encoding='utf-8').split('\n'), 1):
        if not linea or linea[0].isspace():
            continue
        if (linea.startswith(('(function', '(()', '!function'))
                or re.match(r'^[a-zA-Z_$][\w$.]*\(\);?\s*$', linea)):
            salida.append((n, linea.rstrip()))
    return salida


class TestNingunEfectoDeCargaNuevo:

    def test_solo_los_declarados(self):
        nuevos = []
        for archivo in sorted(_PWA.glob('*.js')):
            for n, linea in _efectos_de_carga(archivo):
                if (archivo.name, linea.strip()) in DECLARADOS:
                    continue
                nuevos.append(f'{archivo.name}:{n}  {linea.strip()[:60]}')
        assert not nuevos, (
            '\nCódigo nuevo que se ejecuta al CARGAR un módulo del PWA:\n'
            + '\n'.join(f'  · {x}' for x in nuevos)
            + '\n\nCada uno es una dependencia oculta del entorno: obliga a que '
              '`tests/js/verificar_cliente.js` haga aparecer los globals que use, '
              'y si falta uno el fallo aparece en el BUILD de Railway (Node 18), '
              'no en tu máquina.\n'
              'Si de verdad tiene que correr al cargar, agregalo a DECLARADOS '
              'con el motivo — y ampliá el arnés en el mismo commit.')

    def test_la_lista_solo_encoge(self):
        """Anti-podredumbre. Dos es lo que había el 2026-08-08."""
        assert len(DECLARADOS) <= 2, (
            'Creció la lista de efectos de carga. La pregunta no es cuál es el '
            'tope: es por qué un módulo nuevo necesita ejecutar algo antes de '
            'que alguien lo llame.')

    def test_los_declarados_siguen_existiendo(self):
        """Una entrada que ya no está en el código es una exención que protege
        a nadie — y peor: hace creer que el guard cubre algo que no."""
        reales = {(a.name, l.strip())
                  for a in _PWA.glob('*.js') for _, l in _efectos_de_carga(a)}
        fantasmas = DECLARADOS - reales
        assert not fantasmas, (
            f'declarados que ya no existen en el código: {fantasmas} — borrarlos')


class TestElDetectorNoEstaCiego:
    """Si el patrón deja de encontrar nada, el guard pasa vacío para siempre."""

    def _detecta(self, fuente, tmp_path):
        f = tmp_path / 'x.js'
        f.write_text(fuente, encoding='utf-8')
        return [l for _, l in _efectos_de_carga(f)]

    def test_ve_una_IIFE(self, tmp_path):
        assert self._detecta('(function () { hacerAlgo(); })();\n', tmp_path)

    def test_ve_una_IIFE_de_flecha(self, tmp_path):
        assert self._detecta('(() => { hacerAlgo(); })();\n', tmp_path)

    def test_ve_una_llamada_suelta(self, tmp_path):
        assert self._detecta('arrancar();\n', tmp_path)

    def test_NO_marca_una_declaracion(self, tmp_path):
        """`function f() {...}` no ejecuta nada. Marcarlo llenaría el guard de
        ruido y alguien lo desactivaría."""
        assert not self._detecta('function f() { hacerAlgo(); }\n', tmp_path)

    def test_NO_marca_lo_que_está_dentro_de_una_funcion(self, tmp_path):
        assert not self._detecta(
            'function f() {\n  arrancar();\n  navigator.userAgent;\n}\n', tmp_path)

    def test_NO_marca_una_constante(self, tmp_path):
        assert not self._detecta("const API = window.location.origin;\n", tmp_path)


class TestElArnesAnticipaLoQueLosModulosNecesitan:
    """El trinquete de arriba avisa; esto comprueba que el arnés esté al día."""

    _ARNES = Path(__file__).resolve().parents[1] / 'tests' / 'js' / 'verificar_cliente.js'

    def test_el_arnes_existe(self):
        assert self._ARNES.exists()

    def test_declara_los_globals_del_navegador_que_hacen_falta(self):
        """`navigator` está acá porque su ausencia tumbó el build. Los demás,
        porque los módulos los tocan al cargar o al primer render."""
        fuente = self._ARNES.read_text(encoding='utf-8')
        faltan = [g for g in GLOBALS_DEL_ARNES if f'global.{g}' not in fuente]
        assert not faltan, (
            f'el arnés no define {faltan} — un módulo que los use al cargar '
            f'revienta en Node antes de llegar a ninguna aserción')

    def test_node_del_build_esta_declarado(self):
        """El test de cliente falla duro si `node` falta, y el contenedor de
        build solo detecta Python. Sin esto el build no arranca."""
        nixpacks = Path(__file__).resolve().parents[1] / 'nixpacks.toml'
        assert nixpacks.exists(), (
            'falta nixpacks.toml: el build no tendría node y '
            'test_cliente_js.py lo hace fallar')
        t = nixpacks.read_text(encoding='utf-8')
        assert 'nodejs' in t
        assert 'aptPkgs' in t, (
            '`nixPkgs` REEMPLAZA el set de paquetes del provider en vez de '
            'sumarse: usarlo tumbó python del build. `aptPkgs` sí es aditivo.')
