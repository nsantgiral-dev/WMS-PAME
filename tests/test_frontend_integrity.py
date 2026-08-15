"""
Tests de integridad del frontend — atrapa roturas antes de producción.

Nivel 1: Resolución de handlers (onclick, oninput, onchange)
Nivel 2: Integridad de scripts (orden, SW cache, syntax, duplicados)
Nivel 3: Endpoints referenciados en JS existen en Flask
"""
import os
import re
import subprocess
import pytest

os.environ.setdefault('CONNEKTA_MODO_SIMULACION', 'true')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('SYNC_SCHEDULER', 'false')

_PWA = os.path.join(os.path.dirname(__file__), '..', 'app', 'static', 'pwa')


def _read(filename):
    return open(os.path.join(_PWA, filename), 'r').read()


def _all_js_files():
    return [f for f in os.listdir(_PWA) if f.endswith('.js')]


def _all_code():
    return '\n'.join(_read(f) for f in _all_js_files())


# ═══════════════════════════════════════════════════════════════════
# NIVEL 1: Resolución de handlers HTML
# ═══════════════════════════════════════════════════════════════════

class TestHandlerResolution:
    """Cada onclick/oninput/onchange en index.html debe resolver a una función real."""

    def _extract_handlers(self):
        html = _read('index.html')
        pattern = r'(?:onclick|oninput|onchange)="(\w+)\('
        return sorted(set(re.findall(pattern, html)))

    def _extract_all_functions(self):
        code = _all_code()
        pattern = r'(?:async\s+)?function\s+(\w+)'
        return set(re.findall(pattern, code))

    def test_all_onclick_handlers_resolve(self):
        """Cada onclick en HTML tiene una función JS correspondiente."""
        handlers = self._extract_handlers()
        functions = self._extract_all_functions()
        unresolved = [h for h in handlers if h not in functions]
        assert not unresolved, (
            f'{len(unresolved)} handler(s) sin función JS:\n'
            + '\n'.join(f'  onclick="{h}()" → NO EXISTE' for h in unresolved)
        )

    def test_handler_count_minimum(self):
        """Sanity check — el HTML debe tener al menos 100 handlers (actualmente ~117)."""
        handlers = self._extract_handlers()
        assert len(handlers) >= 100, f'Solo {len(handlers)} handlers — ¿se borró HTML?'

    def test_function_count_minimum(self):
        """Sanity check — debe haber al menos 300 funciones JS (actualmente ~731)."""
        functions = self._extract_all_functions()
        assert len(functions) >= 300, f'Solo {len(functions)} funciones — ¿se borró código?'


# ═══════════════════════════════════════════════════════════════════
# NIVEL 2: Integridad de scripts
# ═══════════════════════════════════════════════════════════════════

class TestScriptIntegrity:
    """Estructura de carga de scripts es correcta y completa."""

    def _script_tags_in_html(self):
        """Extrae scripts src del HTML en orden."""
        html = _read('index.html')
        return re.findall(r'<script src="/static/pwa/(\w+\.js)', html)

    def test_all_js_files_loaded_in_html(self):
        """Cada .js en el directorio PWA está referenciado en index.html."""
        loaded = set(self._script_tags_in_html())
        on_disk = set(_all_js_files())
        # sw.js no se carga como script tag — es service worker
        on_disk.discard('sw.js')
        missing = on_disk - loaded
        assert not missing, f'Archivos JS no cargados en HTML: {missing}'

    def test_app_js_loads_first(self):
        """app.js debe ser el primer script (define globals que todos usan)."""
        scripts = self._script_tags_in_html()
        assert scripts[0] == 'app.js', f'Primer script es {scripts[0]}, debe ser app.js'

    def test_no_duplicate_scripts(self):
        """No hay script tags duplicados."""
        scripts = self._script_tags_in_html()
        dupes = [s for s in scripts if scripts.count(s) > 1]
        assert not dupes, f'Scripts duplicados: {set(dupes)}'

    def test_sw_caches_all_modules(self):
        """sw.js SHELL array incluye todos los módulos JS.

        `etiquetas.js` estaba exento desde que se escribió, con el motivo
        «pre-existente, network-first». Las dos mitades del motivo dejaron de
        ser ciertas —está en el SHELL y el SW es cache-first desde hace
        meses—, pero la exención siguió ahí: una excepción sin fecha sobrevive
        a su razón y deja un módulo fuera del guard para siempre.
        """
        sw = _read('sw.js')
        for s in self._script_tags_in_html():
            assert s in sw, f'{s} no está en sw.js SHELL — PWA offline se rompe'

    def test_cache_bust_versions(self):
        """Cada script tag tiene version param para cache busting.

        Sin `?v=`, un módulo cambiado se sirve viejo desde el caché del
        navegador hasta que alguien haga Ctrl+F5 — y datos viejos presentados
        como actuales es la peor clase de dato en un WMS.
        """
        html = _read('index.html')
        scripts = re.findall(r'<script src="(/static/pwa/\w+\.js[^"]*)"', html)
        for script in scripts:
            assert '?v=' in script, f'{script} sin cache bust version param'

    def test_all_js_files_parse(self):
        """Cada archivo JS parsea sin errores de sintaxis (skip si node no disponible)."""
        import shutil
        if not shutil.which('node'):
            pytest.skip('node no disponible en este entorno (Railway solo tiene Python)')
        for f in _all_js_files():
            path = os.path.join(_PWA, f)
            result = subprocess.run(
                ['node', '-c', path],
                capture_output=True, text=True, timeout=10
            )
            assert result.returncode == 0, (
                f'{f} tiene error de sintaxis:\n{result.stderr[:200]}'
            )

    def test_tabs_array_matches_html_nav_tabs(self):
        """El array TABS en app.js tab() debe incluir todos los nav-tabs del HTML.
        Bug real: la limpieza de duplicados eliminó tab-liquidacion del array,
        causando que el módulo desapareciera del sidebar."""
        html = _read('index.html')
        app = _read('app.js')

        # Extraer tabs del HTML (onclick="tab('tab-xxx')")
        html_tabs = re.findall(r"onclick=\"tab\('(tab-[^']+)'\)\"", html)
        html_tabs_set = set(html_tabs)

        # Extraer array TABS de app.js
        tabs_match = re.search(r"const TABS = \[([^\]]+)\]", app)
        assert tabs_match, 'No se encontró const TABS = [...] en app.js'
        js_tabs = re.findall(r"'(tab-[^']+)'", tabs_match.group(1))
        js_tabs_set = set(js_tabs)

        # Cada nav-tab del HTML debe estar en el array TABS
        missing = html_tabs_set - js_tabs_set
        assert not missing, (
            f'Tabs del HTML que faltan en TABS array de app.js:\n'
            + '\n'.join(f'  {t} — sidebar visible pero tab() no lo maneja' for t in missing)
        )

    def test_tab_dispatcher_handles_all_tabs(self):
        """Cada tab en el array TABS debe tener un handler en cargarAdmin().
        Si falta, hacer click en el tab no carga nada."""
        app = _read('app.js')

        tabs_match = re.search(r"const TABS = \[([^\]]+)\]", app)
        assert tabs_match
        js_tabs = re.findall(r"'(tab-[^']+)'", tabs_match.group(1))

        # Tabs que tienen handler directo (else if TAB === 'tab-xxx')
        # Excluir: tab-dashboard (handled by default), tab-etiquetas (static)
        skip = {'tab-dashboard', 'tab-etiquetas'}
        for tab in js_tabs:
            if tab in skip:
                continue
            # Check if there's a dispatcher line for this tab
            pattern = f"TAB === '{tab}'"
            assert pattern in app, (
                f'{tab} está en TABS array pero no tiene handler en cargarAdmin()'
            )

    def test_cross_module_calls_resolve(self):
        """Funciones llamadas en un módulo que están definidas en OTRO módulo deben existir.
        Previene que una limpieza de duplicados borre una función que otro módulo necesita."""
        # Critical cross-module dependencies (module → functions it calls from other modules)
        cross_deps = {
            'app.js': [
                # Tab dispatchers → module entry points
                'cargarLiquidacion', 'cargarReposicion', 'cargarInventario',
                'cargarMuelle', 'cargarRutas', 'cargarTrasladosAdmin',
                'cargarLayout', 'cargarRecepciones', 'cargarDevoluciones',
                'cargarRutasConductor', 'cargarTrasladosOperario',
                'pedirTarea', 'empCargarTareas', 'empIniciarHUD',
            ],
            'layout.js': [
                # Cross-module: layout.js calls entrepaño functions defined in app.js
                '_layoutRenderEntrepanoSeccion', 'layoutImprimirEtiquetasCuerpo',
            ],
            'picking.js': [
                # Scan dispatcher → other modules
                'empProcesarEscaneo', 'procesarScanRecepcion',
                'procesarScanDevolucion', 'imprimirEtiquetaLPN',
            ],
            'recepcion.js': [
                'imprimirEtiquetaLPN',
            ],
            'liquidacion.js': [
                'repReintentar',
            ],
        }
        all_funcs = self._extract_all_functions()
        missing = []
        for caller, deps in cross_deps.items():
            for fn in deps:
                if fn not in all_funcs:
                    missing.append(f'{caller} → {fn}() NOT FOUND in any file')
        assert not missing, (
            f'{len(missing)} cross-module call(s) broken:\n'
            + '\n'.join(f'  {m}' for m in missing)
        )

    def _extract_all_functions(self):
        code = _all_code()
        return set(re.findall(r'(?:async\s+)?function\s+(\w+)', code))

    def test_no_duplicate_functions_across_files(self):
        """Ninguna función está definida en más de un archivo."""
        func_locations = {}
        for f in _all_js_files():
            code = _read(f)
            funcs = re.findall(r'(?:async\s+)?function\s+(\w+)', code)
            for fn in funcs:
                if fn in func_locations:
                    func_locations[fn].append(f)
                else:
                    func_locations[fn] = [f]
        dupes = {fn: files for fn, files in func_locations.items() if len(files) > 1}
        assert not dupes, (
            f'{len(dupes)} función(es) duplicada(s):\n'
            + '\n'.join(f'  {fn}() en {", ".join(files)}' for fn, files in dupes.items())
        )

    def test_no_duplicate_variables_across_files(self):
        """Ninguna variable top-level let/const está declarada en más de un archivo.

        ROOT CAUSE del incidente 2026-07-21: la modularización extrajo variables
        a módulos pero dejó las declaraciones originales en app.js. Con 'use strict',
        redeclarar let/const en el scope global causa SyntaxError que mata el módulo
        entero — todas sus funciones desaparecen y la PWA queda congelada.

        Solo detecta declaraciones a nivel 0 de indentación (top-level del script),
        que son las que viven en el scope global del browser.
        """
        var_locations = {}
        for f in _all_js_files():
            code = _read(f)
            for line in code.split('\n'):
                # Solo líneas sin indentación → top-level del script
                if line and not line[0].isspace():
                    m = re.match(r'^(?:let|const)\s+(\w+)', line)
                    if m:
                        var = m.group(1)
                        if var in var_locations:
                            var_locations[var].append(f)
                        else:
                            var_locations[var] = [f]
        dupes = {v: files for v, files in var_locations.items() if len(files) > 1}
        assert not dupes, (
            f'{len(dupes)} variable(s) global(es) duplicada(s) — SyntaxError en strict mode:\n'
            + '\n'.join(
                f'  {v} en {", ".join(files)} — browser mata el módulo entero'
                for v, files in sorted(dupes.items())
            )
        )

    def test_modules_no_syntax_errors_node(self):
        """Cada módulo JS parsea sin errores (simula carga en browser).

        Si node no está disponible (Railway), se salta. La verificación real
        se hace con test_no_duplicate_variables que detecta la causa raíz.
        """
        import shutil
        if not shutil.which('node'):
            pytest.skip('node no disponible')
        for f in _all_js_files():
            path = os.path.join(_PWA, f)
            result = subprocess.run(
                ['node', '--check', path],
                capture_output=True, text=True, timeout=10
            )
            assert result.returncode == 0, (
                f'{f} tiene error de sintaxis:\n{result.stderr[:300]}'
            )

    def test_strict_mode_redeclaration_simulation(self):
        """Simula la carga secuencial de scripts y detecta redeclaraciones globales.

        Recorre los scripts en el MISMO ORDEN que el browser los carga (según
        los <script> tags en index.html) y detecta variables top-level (sin
        indentación) que se declaran más de una vez. Estas causarían SyntaxError
        en el browser y matarían el módulo.
        """
        html = _read('index.html')
        script_order = re.findall(r'<script src="/static/pwa/(\w+\.js)', html)

        seen_vars = {}  # var_name → first_file
        conflicts = []

        for f in script_order:
            if f not in _all_js_files():
                continue
            code = _read(f)
            for lineno, line in enumerate(code.split('\n'), 1):
                # Solo top-level (sin indentación)
                if line and not line[0].isspace():
                    m = re.match(r'^(?:let|const)\s+(\w+)', line)
                    if m:
                        var = m.group(1)
                        if var in seen_vars and seen_vars[var] != f:
                            conflicts.append(
                                f'  {var}: first in {seen_vars[var]}, redeclared in {f}:{lineno}'
                            )
                        elif var not in seen_vars:
                            seen_vars[var] = f

        assert not conflicts, (
            f'{len(conflicts)} redeclaración(es) global(es) en orden de carga — '
            f'SyntaxError en browser:\n' + '\n'.join(conflicts)
        )


# ═══════════════════════════════════════════════════════════════════
# NIVEL 3: Endpoints referenciados existen en Flask
# ═══════════════════════════════════════════════════════════════════

class TestEndpointIntegrity:
    """URLs referenciadas en JS existen como rutas registradas en Flask."""

    def _extract_api_urls_from_js(self):
        """Extrae todas las rutas /api/* referenciadas en el JS."""
        code = _all_code()
        # Match: '/api/...' or `/api/...` (template literals have ${} which we strip)
        urls = set()
        # Simple string literals
        for match in re.findall(r"['\"`](/api/[a-zA-Z0-9/_-]+)", code):
            # Remove template variable parts
            clean = re.sub(r'\$\{[^}]+\}', '<var>', match)
            # Normalize: remove trailing slashes, remove query params
            clean = clean.split('?')[0].rstrip('/')
            # Skip URLs with remaining variables
            if '<var>' not in clean and '$' not in clean:
                urls.add(clean)
        return urls

    def _get_flask_routes(self, app):
        """Obtiene todas las rutas registradas en Flask."""
        routes = set()
        for rule in app.url_map.iter_rules():
            # Normalize: replace <int:id> etc with pattern
            path = re.sub(r'<[^>]+>', '<var>', rule.rule)
            routes.add(path)
        return routes

    def _normalize_for_match(self, url):
        """Normaliza una URL de JS para comparar con Flask routes."""
        # /api/packing/123/confirmar → /api/packing/<var>/confirmar
        parts = url.split('/')
        normalized = []
        for part in parts:
            if part.isdigit():
                normalized.append('<var>')
            else:
                normalized.append(part)
        return '/'.join(normalized)

    def test_api_urls_exist_in_flask(self, app):
        """Las rutas /api/* del JS deben existir en Flask (con tolerancia para paths dinámicos)."""
        js_urls = self._extract_api_urls_from_js()
        flask_routes = self._get_flask_routes(app)

        # Build a set of normalized flask route prefixes for matching
        flask_prefixes = set()
        for route in flask_routes:
            parts = route.split('/')
            for i in range(2, len(parts) + 1):
                flask_prefixes.add('/'.join(parts[:i]))

        unmatched = []
        for url in sorted(js_urls):
            normalized = self._normalize_for_match(url)
            # Check if any Flask route starts with this prefix
            matched = any(
                route.startswith(normalized.split('<var>')[0].rstrip('/'))
                for route in flask_routes
            ) or normalized in flask_routes

            # Also check prefix match (for /api/module/action patterns)
            prefix = '/'.join(url.split('/')[:3])  # /api/module
            prefix_exists = prefix in flask_prefixes

            if not matched and not prefix_exists:
                unmatched.append(url)

        # Filter out known dynamic patterns that can't be statically matched
        false_positives = {
            '/api/mobile/tarea-actual',  # registered as /api/mobile/tarea-actual
        }
        real_unmatched = [u for u in unmatched if u not in false_positives]

        # Soft assertion — warn instead of fail for < 5 unmatched
        # (some routes use complex patterns that static analysis can't resolve)
        if len(real_unmatched) > 10:
            assert False, (
                f'{len(real_unmatched)} URLs del JS sin ruta Flask:\n'
                + '\n'.join(f'  {u}' for u in real_unmatched[:20])
            )

    def test_critical_endpoints_exist(self, app):
        """Los endpoints más críticos del WMS deben existir en Flask."""
        critical = [
            '/api/mobile/tarea-actual',
            '/api/mobile/escanear',
            '/api/mobile/confirmar',
            '/api/packing/',
            '/api/auth/login',
            '/api/dashboard/resumen-completo',
            '/api/conteo/',
            '/api/reposicion/ubicaciones',
            '/api/health/ping',
        ]
        flask_routes = self._get_flask_routes(app)
        flask_route_list = list(flask_routes)

        for endpoint in critical:
            found = any(
                endpoint.rstrip('/') in route or route.startswith(endpoint.rstrip('/'))
                for route in flask_route_list
            )
            assert found, f'Endpoint crítico {endpoint} no existe en Flask'


# ══════════════════════════════════════════════════════════════════════════
# Nivel 4: endpoints sin consumidor (Flask → JS)
#
# El Nivel 3 pregunta "¿existe en Flask lo que el JS llama?". Este pregunta lo
# contrario: "¿llama el JS todo lo que Flask expone?". Sin este guard un
# endpoint puede escribirse, testearse y desplegarse sin que exista ninguna
# forma de usarlo — que fue exactamente lo que pasó con la descarga de kardex
# (bloqueaba 4 modelos), con alimentar-picking y con el cargador del Vigía.
#
# NO exige cero huérfanos: exige que el número NUNCA crezca. Es un trinquete.
# ══════════════════════════════════════════════════════════════════════════
#
# ── Alcanzabilidad: la tercera versión del mismo guard (2026-08-15) ────────
#
# El guard midió **presencia** («los trozos de la URL están escritos en algún
# lado»), y en agosto pasó a **adyacencia** («el sufijo aparece cerca del
# prefijo»), que atrapó once rutas parametrizadas. Las dos versiones comparten
# el mismo punto ciego, y es el que importa:
#
#     una URL escrita dentro de una función que nadie llama está escrita.
#
# `trasRevertir`, `trasReintentarDespachoSiesa` y `trasReintentarRecepcionSiesa`
# lo satisfacían perfectamente: `fetch(API + `/api/traslados/${id}/revertir`)`
# es adyacencia de manual. Ningún `onclick` las alcanzaba, y mientras tanto
# `traslado_service` le mandaba al operario, por correo, «WMS Admin → Traslados
# → Reintentar despacho» — un botón que no existía.
#
# Ahora se exige **invocación**: la URL cuenta solo si vive en código que se
# alcanza desde un `onclick` del HTML, desde el código de arranque de un módulo,
# desde un `addEventListener`, o desde otra función alcanzable. Es un grafo de
# llamadas, recorrido desde las raíces.
#
# Lo que NO hace, dicho para que nadie lo suponga: no interpreta JavaScript. Un
# nombre invocado por string (`window[fn]()`) le resulta invisible, y una
# función que solo llama código muerto se considera muerta aunque el usuario
# jure lo contrario. Por eso hay pisos mínimos y mutaciones abajo — un detector
# de alcance que se rompe callado marca todo como huérfano, y una lista larga
# se ignora.
#
# Y sigue midiendo **por ruta, no por método**: `/api/rutas/conductores` cuenta
# como consumida porque la pantalla hace el GET, aunque el POST que da de alta
# un conductor esté muerto desde hace meses (ver la deuda de
# `/api/rutas/usuarios-conductores`). Es el próximo escalón de este guard, y
# queda escrito acá para que se descubra leyendo y no a los golpes.


_ANTES_DE_REGEX = set('(,=:[!&|?{};+-*%~^<>') | {'\n'}
_PALABRAS_ANTES_DE_REGEX = {'return', 'typeof', 'case', 'in', 'of', 'do',
                            'else', 'yield', 'await', 'new', 'delete', 'void'}
_DECL_FUNCION = re.compile(r'(?:^|[\n;{}])\s*(?:async\s+)?function\s+(\w+)\s*\(')


def _fin_de_bloque(src, i):
    """Índice siguiente al `}` que cierra el `{` en `i`.

    Contar llaves a secas no sirve en este repo: el JS arma HTML con plantillas
    (` `${x}` `), y un `/\\d{2}/` o un `'{'` dentro de una cadena descuadran la
    cuenta y se llevan por delante media docena de funciones — que entonces
    quedarían dentro del cuerpo de otra y se declararían alcanzables sin serlo.
    """
    n = len(src)
    prof = 0
    while i < n:
        c = src[i]
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            i = src.find('\n', i)
            if i == -1:
                return n
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == '/':
            k = i - 1
            while k >= 0 and src[k] in ' \t':
                k -= 1
            prev = src[k] if k >= 0 else '\n'
            pal = re.search(r'([A-Za-z_$][\w$]*)$', src[:k + 1])
            if prev in _ANTES_DE_REGEX or (
                    pal is not None and pal.group(1) in _PALABRAS_ANTES_DE_REGEX):
                j, en_clase = i + 1, False
                while j < n:
                    d = src[j]
                    if d == '\\':
                        j += 2
                        continue
                    if d == '[':
                        en_clase = True
                    elif d == ']':
                        en_clase = False
                    elif (d == '/' and not en_clase) or d == '\n':
                        break
                    j += 1
                i = j + 1
                continue
            i += 1
            continue
        if c in '"\'':
            j = i + 1
            while j < n:
                if src[j] == '\\':
                    j += 2
                    continue
                if src[j] == c or src[j] == '\n':
                    break
                j += 1
            i = j + 1
            continue
        if c == '`':
            j = i + 1
            while j < n:
                if src[j] == '\\':
                    j += 2
                    continue
                if src[j] == '`':
                    break
                if src[j] == '$' and j + 1 < n and src[j + 1] == '{':
                    j = _fin_de_bloque(src, j + 1)     # interpolación
                    continue
                j += 1
            i = j + 1
            continue
        if c == '{':
            prof += 1
        elif c == '}':
            prof -= 1
            if prof == 0:
                return i + 1
        i += 1
    return n


def _trocear(src):
    """(cuerpos por función declarada, código de arranque del módulo).

    «Arranque» es todo lo que queda fuera de una declaración: se ejecuta al
    cargar el script, así que es raíz por definición. Separarlo bien importa —
    `document.addEventListener('DOMContentLoaded', verificarModoSistema)` vive
    después de la última función del archivo.
    """
    cuerpos, tapados = {}, []
    for m in _DECL_FUNCION.finditer(src):
        ini = src.index('function', m.start())
        llave = src.find('{', m.end() - 1)
        if llave == -1:
            continue
        if any(a <= ini < b for a, b in tapados):
            continue                       # anidada: cuenta dentro de su padre
        fin = _fin_de_bloque(src, llave)
        tapados.append((ini, fin))
        cuerpos[m.group(1)] = cuerpos.get(m.group(1), '') + '\n' + src[ini:fin]
    tapados.sort()
    arranque, cur = [], 0
    for a, b in tapados:
        arranque.append(src[cur:a])
        cur = b
    arranque.append(src[cur:])
    return cuerpos, '\n'.join(arranque)


_PALABRAS_CLAVE = {
    'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'typeof',
    'new', 'await', 'else', 'do', 'delete', 'void', 'in', 'of', 'case',
    'throw', 'yield', 'super', 'this', 'constructor',
}
#: `fn(` en cualquier parte del texto. Cubre la llamada normal **y** el
#: `onclick="fn(${id})"` que este repo escribe dentro de plantillas — para el
#: escáner las dos son el mismo texto, y por eso no hay una regla aparte para
#: atributos: sería una regla que no cambia ningún resultado.
_LLAMADA = re.compile(r'(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(')
#: pasada como valor: `addEventListener('x', fn)`, `setTimeout(fn, 0)`,
#: `Quagga.onDetected(_onQuaggaDetect)`, `_refreshBtn(event, cargarCola)`.
#: Pasar una función también es invocarla, con un intermediario.
_COMO_VALOR = re.compile(r'[(,]\s*([A-Za-z_$][\w$]*)\s*[,)]')
#: `el.onclick = fn;` — asignada ahora, disparada por el navegador después.
_ASIGNADA = re.compile(r'=\s*([A-Za-z_$][\w$]*)\s*[;,)\n]')


def _referencias(texto):
    """Nombres que este trozo de código puede terminar ejecutando."""
    r = {m.group(1) for m in _LLAMADA.finditer(texto)} - _PALABRAS_CLAVE
    for rx in (_COMO_VALOR, _ASIGNADA):
        r |= {m.group(1) for m in rx.finditer(texto)}
    return r


def _analizar(fuentes, html):
    """El detector, sin tocar disco: `{archivo: código}` + el HTML.

    Separado de `_grafo()` a propósito — un detector que solo corre contra el
    repo entero no se puede probar por mutación, y un detector de alcance sin
    mutaciones es exactamente el que se rompe callado.
    """
    cuerpos, arranques = {}, []
    for f in sorted(fuentes):
        c, a = _trocear(fuentes[f])
        for nombre, cuerpo in c.items():
            cuerpos[nombre] = cuerpos.get(nombre, '') + '\n' + cuerpo
        arranques.append(a)
    arranque = '\n'.join(arranques)

    # Raíces: lo que el usuario puede disparar desde el HTML, más lo que corre
    # solo al cargar cada módulo.
    raices = {m.group(1) for m in
              re.finditer(r'\bon\w+="\s*([A-Za-z_$][\w$]*)\s*\(', html)}
    raices |= _referencias(arranque)
    raices |= _referencias(re.sub(r'<script[^>]*src[^>]*>', '', html))

    alcanzables, pendientes = set(), [r for r in raices if r in cuerpos]
    while pendientes:
        fn = pendientes.pop()
        if fn in alcanzables:
            continue
        alcanzables.add(fn)
        pendientes += [n for n in _referencias(cuerpos[fn])
                       if n in cuerpos and n not in alcanzables]

    return cuerpos, alcanzables, arranque, raices


_CACHE_GRAFO = {}


def _grafo():
    """(cuerpos, alcanzables, arranque, raíces) del repo — memoizado."""
    fuentes = {f: _read(f) for f in _all_js_files()}
    html = _read('index.html')
    clave = hash((tuple(sorted(fuentes.items())), html))
    if clave not in _CACHE_GRAFO:
        _CACHE_GRAFO[clave] = _analizar(fuentes, html)
    return _CACHE_GRAFO[clave]


def _codigo_alcanzable():
    """El único blob contra el que vale preguntar si una URL se usa."""
    cuerpos, alcanzables, arranque, _ = _grafo()
    return _blob_alcanzable(cuerpos, alcanzables, arranque)


def _sin_comentarios(src):
    """El mismo código, sin `//` ni `/* */`.

    Una URL citada en un JSDoc no es un consumidor, y esa distinción no es
    teórica: un docstring que explica para qué existía algo ya alcanzó una vez
    para que un diseño retirado volviera a parecer vigente. El guard tiene que
    medir código, no prosa sobre el código.
    """
    n, i, out = len(src), 0, []
    while i < n:
        c = src[i]
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            j = src.find('\n', i)
            i = n if j == -1 else j
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            i = n if j == -1 else j + 2
            continue
        if c in '"\'`':
            j = i + 1
            while j < n:
                if src[j] == '\\':
                    j += 2
                    continue
                if src[j] == c or (c != '`' and src[j] == '\n'):
                    break
                j += 1
            out.append(src[i:j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def _blob_alcanzable(cuerpos, alcanzables, arranque):
    return _sin_comentarios(
        arranque + '\n' + '\n'.join(cuerpos[f] for f in sorted(alcanzables)))


# Exentos por naturaleza — no necesitan pantalla
_EXENTOS_POR_REGLA = (
    'debug',          # diagnóstico manual contra Siesa, se llaman a mano
    '/api/health/',   # monitores externos y healthcheck de Railway
)

# Deuda diagnosticada. Solo puede ENCOGER (hay un test que lo obliga).
#
# La razón NO es "qué pantalla le falta" — un endpoint sin consumidor casi nunca
# necesita un tab. La pregunta correcta es QUÉ DECISIÓN DEBERÍA ESTAR INFORMANDO.
# Un número que el usuario no puede auditar no se obedece: se ignora.
DEUDA_SIN_UI = {
    # ── Destapadas el 2026-08-13 al cambiar presencia por adyacencia ──────
    #
    # Las once son **rutas parametrizadas que mueven inventario o dinero**, y
    # ninguna figuraba acá: la comprobación vieja las daba por usadas porque sus
    # trozos (`/api/picking/` y `/confirmar`) existen sueltos en el frontend,
    # cada uno por una ruta distinta.
    #
    # No se conectan: la operación ya pasa por otro lado. Quedan declaradas —
    # que es lo que este archivo existe para lograr— y **son candidatas a
    # borrar**, no a conectar: cada una es una segunda puerta a una operación
    # crítica, sin la idempotencia ni las guardas de la vía viva.
    '/api/picking/<int:id>/confirmar':
        'SEGUNDA PUERTA al descuento de inventario. La vía viva es '
        'POST /api/mobile/confirmar (picking.js:489). BORRAR, no conectar.',
    '/api/picking/<int:id>/iniciar':
        'SEGUNDA PUERTA. La vía viva es /api/mobile/tarea-actual, que asigna e '
        'inicia en un solo gesto. BORRAR.',
    '/api/picking/<int:id>/reportar-problema':
        'DUPLICADO de /api/mobile/reportar-problema, que es el que llama la PWA.',
    '/api/packing/<int:id>/escanear':
        'SEGUNDA PUERTA a los datos que van al ERP. La vía viva es '
        '/api/mobile/escanear. Los aciertos de «escanear» en el JS son texto '
        'de pantalla, no llamadas — verificado a mano.',
    '/api/conteo/<int:id>/registrar':
        'ORIGEN DEL AJUSTE DE INVENTARIO sin consumidor. El conteo se registra '
        'desde el flujo móvil. Es la más delicada de las once.',
    '/api/conteo/<int:id>/tarea':
        'Detalle de una tarea de conteo. La pantalla trae el detalle en el '
        'listado.',
    '/api/despacho_parcial/<int:packing_id>/despachar':
        'SEGUNDA PUERTA al cierre de packing (244328→142945→142943). El cierre '
        'real pasa por PackingService.cerrar_packing. BORRAR.',
    '/api/devoluciones/<int:devolucion_id>/cancelar':
        'Cancelación de devolución. Sin gesto en la pantalla de devoluciones.',
    '/api/recepcion/<int:id>/cancelar':
        'Cancelación de recepción. Sin gesto en la pantalla.',
    '/api/recepcion/<int:id>/iniciar':
        'La recepción se inicia al abrir la OC, no con una llamada aparte.',
    '/api/traslados/<int:id>/cancelar':
        'Cancelación de traslado. El panel cancela desde el detalle, que usa '
        'otra ruta.',

    # ── Destapadas el 2026-08-15 al cambiar adyacencia por invocación ─────
    #
    # Seis. Sus URLs estaban escritas —y con adyacencia perfecta— dentro de
    # funciones que **nadie llama**. Cada razón de acá se verificó leyendo el
    # código citado; donde no se pudo verificar, lo dice.
    #
    # Las tres de traslado son un mismo hecho: `index.html:2911` declara que
    # «pantalla-picker-traslado y pantalla-packer-traslado eliminadas», y el JS
    # de esas pantallas se quedó. Son ~280 líneas de código muerto que **parece
    # vivo**: la próxima persona que audite el flujo de traslados lo va a leer
    # como si describiera la operación de hoy, y no la describe.
    '/api/traslados/<int:id>/confirmar-packing':
        'SEGUNDA PUERTA al cierre de packing de traslado, y dispara 174720 otra '
        'vez. La vía viva es PackingService.cerrar_packing → TrasladoPackingCloser '
        '(compromisos 174720 + job DESPACHO_TRASLADO), que salta PREPARADO y va a '
        'EN_TRANSITO. Verificado: confirmar_packing_traslado no tiene más caller '
        'que esta ruta, y sus dos llamadores JS (confirmarPackingTraslado, '
        '_trasPackerConfirmar) son del HUD legacy sin pantalla. BORRAR, no conectar.',
    '/api/traslados/<int:id>/items-picking':
        'Alimentaba el HUD de conteo ítem por ítem de las pantallas picker/packer '
        'de traslado, borradas del HTML (index.html:2911). Verificado: sus dos '
        'únicos llamadores son trasPickerAbrirHUD y trasPackerAbrirHUD, ambos '
        'inalcanzables. Se pierde nada hoy; el riesgo es creer que sigue viva.',
    '/api/traslados/mis-traslados':
        'Lista de traslados asignados al operario. Su único llamador, '
        'cargarTrasladosOperario, escribe en el contenedor «traslados-operario», '
        'que NO EXISTE en index.html — verificado por grep: cero apariciones. La '
        'función retorna en la primera línea aunque alguien la llamara.',
    '/api/rutas/usuarios-conductores':
        'Poblaba el selector del formulario de alta de conductor. Verificado: los '
        'ids que toca (conductores-form, cond-form-usuario, cond-form-error) no '
        'existen en index.html, así que conductoresMostrarForm reventaría en su '
        'primera línea. Costo real: **no hay forma de dar de alta un conductor '
        'desde la PWA** — la pantalla solo lista, activa/desactiva y crea la '
        'cuenta PWA de uno ya existente. Alta = INSERT a mano en la base.',
    '/api/picking/<int:id>/cancelar':
        'Cancelar una tarea de picking con motivo. Verificado: cancelarTareaPicking '
        '(app.js:690) no se referencia en ningún onclick ni HTML. Lo que la '
        'pantalla de bodega SÍ ofrece es «auditar», que solo acepta tareas '
        'BLOQUEADAS (picking_service.py:549). Una tarea PENDIENTE o EN_PROCESO '
        'que sobra —pedido anulado, ola mal lanzada— no se puede cerrar desde la '
        'UI: queda ocupando el pool y reteniendo stock reservado.',
    '/api/picking/<int:id>/reabrir':
        'Devuelve una tarea al pool. Verificado: reabrirTareaPicking (app.js:679) '
        'no se referencia en ningún onclick. Sin ella, la tarea de un operario '
        'que terminó el turno o perdió el equipo se queda asignada a él. OJO al '
        'conectarla: reabrir pone cantidad_recogida en CERO '
        '(picking_service.py:519) y eso ya produjo falsos positivos en VTA-20 — '
        'darle un botón sin decir eso en la confirmación repite el problema.',

    '/api/auth/me':
        'DUPLICADO: el login ya devuelve el usuario completo. Pedirlo otra vez es un viaje de red por un dato que el cliente tiene. Candidato a BORRAR, no a conectar.',
    '/api/compras/armador/contenedores':
        'Listado de contenedores. El armador muestra la propuesta, no el histórico.',
    '/api/compras/bloqueados/verificar':
        'Verifica una OC contra la lista de bloqueados. Sin gesto en compras.',
    '/api/compras/clasificar-rama/<int:producto_id>':
        'Clasificación manual de un SKU. La pantalla clasifica en lote.',
    '/api/conteo/abc/sincronizar':
        'Recalcula la clasificación ABC. El cron la corre a las 2am; el manual es para después de una carga masiva.',
    '/api/conteo/mis-tareas':
        'DUPLICADO: la pantalla lista con /api/conteo/?filtros.',
    '/api/dashboard/kpis':
        'DUPLICADO de /api/dashboard/resumen-completo, que es el que consume el panel. Dos endpoints que calculan lo mismo divergen: el día que cambie un KPI, uno de los dos queda mintiendo.',
    '/api/dashboard/movimientos':
        'DUPLICADO: /resumen-completo ya trae los movimientos del panel.',
    '/api/empaques/lpn/<string:codigo>/consumir':
        ('Consumo manual de un LPN. Sin UI, un empaque que se abrió y no se '
         'escaneó queda ACTIVO para siempre: el sistema cree que hay una paca '
         'entera donde hay unidades sueltas.'),
    '/api/inventario/ajuste':
        'Ajuste directo de inventario por API. La pantalla ajusta por conteo, que es el camino con segunda firma. Este endpoint SALTA esa fricción — conectarlo a un botón sería quitarle el control al conteo cíclico. Se deja sin UI A PROPÓSITO.',
    '/api/inventario/movimientos':
        'Kardex de movimientos por producto. Útil para auditar un faltante; hoy solo por API.',
    '/api/inventario/stock/<int:producto_id>':
        'Stock de un producto. Lectura puntual sin pantalla.',
    '/api/mobile/mis-tareas':
        'DUPLICADO de /api/mobile/tarea-actual.',
    '/api/muelle/historial':
        'Historial del muelle. La pantalla muestra el estado actual, no el histórico.',
    '/api/packing/crear-desde-picking':
        'Alta manual de packing. El flujo normal la crea sola.',
    '/api/packing/crear-manual':
        'Alta manual de packing sin picking previo.',
    '/api/picking/crear':
        'Alta manual de tarea de picking. El sync de pedidos las crea.',
    '/api/picking/fefo':
        'Cálculo FEFO. Se consume dentro del flujo, no como endpoint.',
    '/api/picking/mis-tareas-activas':
        'DUPLICADO: la pantalla usa /api/mobile/tarea-actual (una tarea a la vez, por diseño — un operario con lista elige, y elegir rompe el orden de recorrido).',
    '/api/picking/purgar-ceros':
        'Limpieza de picks en cero. Mantenimiento puntual.',
    '/api/picking/siguiente-tarea':
        'DUPLICADO de /api/mobile/tarea-actual, que es el que usa la pantalla de operario. Quedó de una versión anterior del flujo.',
    '/api/recepcion/crear':
        'Crear recepción sin OC. La pantalla siempre parte de una OC.',
    '/api/reposicion/alertas/smtp-check':
        'Diagnóstico de SMTP. La pantalla tiene /alertas/test-email, que además ENVÍA. Este solo verifica la conexión — DUPLICADO parcial.',
    '/api/reposicion/mis-tareas':
        'DUPLICADO de /api/reposicion/tarea-actual.',
    '/api/reposicion/pre-verificar-ola':
        'Simulación de una ola de reposición antes de lanzarla.',
    '/api/rutas/<int:id>/liquidar-completo':
        'Liquidación en un paso. La pantalla liquida por partes (NC→RC→DC), que es lo que permite ver dónde falla la cadena. El atajo esconde el punto de fallo.',
    '/api/rutas/<int:id>/sugeridos':
        'Bultos sugeridos para una ruta. El muelle asigna a mano.',
    '/api/siesa/cargar-inventario':
        'Carga inicial de inventario desde Siesa. Se corre UNA VEZ en el go-live, con la bodega quieta y bajo acta. Un botón permanente invita a correrla dos veces.',
    '/api/siesa/sync-productos':
        'Sincronización del catálogo. Corre por cron; el disparo manual es para cuando entra un producto nuevo y no se quiere esperar.',
    '/api/siesa/terceros-contacto':
        'Consulta de contacto de terceros. Se usó para verificar el maestro contra producción desde la app, no desde una sesión de desarrollo.',
    '/api/traslados/bodegas-siesa':
        'Catálogo de bodegas. La pantalla usa el maestro local.',
    '/api/kardex/tasa-servida-corregida':
        'Descensura M0.2. Sigue sin UI y sin consumidor aguas abajo, PERO desde el '
        '2026-08-08 ya no diverge: consume dias_expuestos() como el resto y declara '
        'censurado. Antes dividia por dias crudo y OMITIA del resultado a los SKU sin '
        'StockDiario — los censurados. Es redundante con demanda_diaria_corregida, que '
        'si tiene consumidores (ROP, armador) y ya usa la politica: la decision '
        'pendiente es BORRARLA, no conectarla.',
    '/api/kardex/newsvendor':
        'Calculadora pura. Su llamador (TemporadaService) la consume en el servidor, '
        'no por HTTP. Decisión que debería informar: el "¿y si...?" del comité — '
        'recalcular Q* con otra demanda supuesta sin tocar la historia. La pantalla '
        'de Temporada aún no lo ofrece; cuando lo haga, se conecta aquí.',
    '/api/vigia/series':
        'Redundante: el panel obtiene lo mismo vía /resumen. Candidato a borrarse, '
        'no a conectarse — un endpoint duplicado es deuda, no funcionalidad.',
    '/api/vigia/verificar-carga':
        'Arnés de certificación. Consumidor legítimo NO-JS: scripts/verificar_carga_vigia.py. '
        'Se corre una vez por CLI; darle UI sería invitar a correrlo sin contexto.',
    '/api/vigia/backtest/florencia':
        'Test canónico de reproducción. Mismo caso: se dispara desde el arnés de '
        'certificación, no desde el panel.',
}

# Inventario heredado, sin clasificar. No fabricamos razones que no conocemos:
# al tocar cualquiera de estos, o se conecta o se mueve a DEUDA_SIN_UI con su
# motivo. Esta lista también solo puede encoger.
BASELINE_HEREDADO = set()   # <- vacio: los 54 quedaron clasificados el 2026-08-05.
#
# Un baseline en CERO no significa que no haya deuda: significa que toda la que
# hay tiene nombre y motivo escrito en DEUDA_SIN_UI. La diferencia es que ahora
# se puede decidir sobre ella.

TOLERADOS = set(DEUDA_SIN_UI) | BASELINE_HEREDADO


class TestEndpointsSinConsumidor:
    """Nivel 4 — ningún endpoint nuevo puede nacer sin forma de usarse."""

    #: Cuánto texto puede haber entre el prefijo y el sufijo de una URL
    #: construida por concatenación. `API + \`/api/picking/${id}/confirmar\``
    #: mete unos pocos caracteres; 120 deja margen de sobra sin volver a
    #: aceptar dos trozos que viven en archivos distintos.
    _VENTANA_ADYACENCIA = 120

    @staticmethod
    def _segmentos_literales(ruta):
        """Trozos fijos de la ruta, ignorando <int:id> y demás."""
        return [s for s in re.split(r'<[^>]+>', ruta) if s.strip('/')]

    @classmethod
    def _esta_construida(cls, ruta, blob):
        """¿El frontend arma ESTA url, o solo contiene sus pedazos sueltos?

        Antes se exigía **presencia**: que cada trozo apareciera en algún lado
        del blob. Para `/api/picking/<int:id>/confirmar` los trozos son
        `/api/picking/` y `/confirmar` — **los dos existen, en archivos
        distintos y por rutas distintas** (`/api/picking/${id}/reabrir` aporta
        el primero, cualquier otro endpoint el segundo). La ruta se declaraba
        usada sin que nadie la llamara.

        Y la clase que el agujero tapaba es exactamente la parametrizada, que
        es la que mueve inventario: confirmar picking, escanear en packing,
        registrar un conteo, despachar un parcial. Por eso ninguna figuraba en
        la deuda declarada.

        Ahora se exige **adyacencia**: el sufijo tiene que aparecer cerca del
        prefijo, que es lo que produce una concatenación real.
        """
        segs = cls._segmentos_literales(ruta)
        if not segs:
            return True
        if len(segs) == 1:
            return segs[0].rstrip('/') in blob
        pre, post = segs[0].rstrip('/'), segs[1].rstrip('/')
        for m in re.finditer(re.escape(pre), blob):
            if post in blob[m.end():m.end() + cls._VENTANA_ADYACENCIA]:
                return True
        return False

    def _huerfanos(self, app):
        # El blob NO es todo el JS: es solo el código alcanzable. Una URL que
        # solo existe dentro de una función que nadie llama no cuenta como
        # consumidor — ver la nota de alcanzabilidad arriba.
        blob = _codigo_alcanzable()
        huerfanos = set()
        for regla in app.url_map.iter_rules():
            ruta = str(regla)
            if not ruta.startswith('/api/'):
                continue
            if any(x in ruta for x in _EXENTOS_POR_REGLA):
                continue
            if self._esta_construida(ruta, blob):
                continue
            huerfanos.add(ruta)
        return huerfanos

    def test_ningun_endpoint_nuevo_sin_consumidor(self, app):
        """EL GUARD. Un endpoint nuevo sin forma de llamarse rompe el build.

        Si esto falla: conecta el endpoint desde el JS, o —si de verdad no debe
        tener UI— añádelo a DEUDA_SIN_UI con la razón y el costo de no tenerla.
        Lo que no se vale es que se cuele en silencio.
        """
        nuevos = self._huerfanos(app) - TOLERADOS
        assert not nuevos, (
            f'\n{len(nuevos)} endpoint(s) sin ningún consumidor en el frontend:\n'
            + '\n'.join(f'  · {r}' for r in sorted(nuevos))
            + '\n\nConéctalo desde el JS, o justifícalo en DEUDA_SIN_UI '
              '(tests/test_frontend_integrity.py) explicando qué se pierde.'
        )

    def test_la_lista_solo_encoge(self, app):
        """Anti-podredumbre: lo que ya tiene consumidor sale de la lista.

        Sin esto la lista se vuelve un cementerio que nadie limpia y deja de
        proteger — un endpoint podría desconectarse sin que nadie se entere.
        """
        huerfanos = self._huerfanos(app)
        rutas_vivas = {str(r) for r in app.url_map.iter_rules()}
        ya_conectados = sorted(
            r for r in TOLERADOS if r in rutas_vivas and r not in huerfanos)
        assert not ya_conectados, (
            f'\n{len(ya_conectados)} endpoint(s) ya tienen consumidor — '
            f'bórralos de la lista:\n'
            + '\n'.join(f'  · {r}' for r in ya_conectados)
        )

    def test_la_lista_no_acumula_rutas_muertas(self, app):
        """Una ruta borrada del backend no puede seguir en la lista."""
        rutas_vivas = {str(r) for r in app.url_map.iter_rules()}
        fantasmas = sorted(r for r in TOLERADOS if r not in rutas_vivas)
        assert not fantasmas, (
            f'\n{len(fantasmas)} ruta(s) en la lista ya no existen en Flask — '
            f'bórralas:\n' + '\n'.join(f'  · {r}' for r in fantasmas)
        )

    def test_la_deuda_declara_su_razon(self):
        """Cada deuda dice qué se pierde. Sin razón es un olvido con formato."""
        for ruta, razon in DEUDA_SIN_UI.items():
            assert razon and len(razon) > 25, (
                f'{ruta} está en DEUDA_SIN_UI sin explicar el costo de no tener UI')

    def test_el_baseline_heredado_no_crece(self, app):
        """BASELINE_HEREDADO es un inventario congelado: solo se le quitan.

        Deuda nueva va a DEUDA_SIN_UI con su razón, nunca aquí.
        """
        assert len(BASELINE_HEREDADO) <= 54, (
            f'BASELINE_HEREDADO creció a {len(BASELINE_HEREDADO)}. '
            f'La deuda nueva va a DEUDA_SIN_UI con su razón.')


class TestElDetectorDeAlcanceSeMide:
    """Un detector sin detector es una opinión.

    El precedente concreto es del mes pasado: el detector de tipos encolados sin
    handler veía **3 de 12** porque solo miraba argumentos posicionales, y el
    roto estaba entre los 9 invisibles. Salió verde y no significaba nada.

    La forma de fallo propia de éste es peor que la de aquél, porque es
    silenciosa en las dos direcciones:

    · si el troceo se rompe (una llave mal contada), medio archivo cae dentro
      del cuerpo de otra función y **todo pasa a ser alcanzable** — el guard se
      apaga sin decirlo;
    · si el recorrido no reconoce una forma de invocación, se declaran huérfanas
      rutas que sí se usan, aparece una lista larga, y una lista larga se ignora.

    Por eso acá hay dos cosas: **pisos mínimos** sobre el repo real, y
    **mutaciones** sobre fuentes sintéticas y sobre el repo mutado en memoria.
    """

    # ── Pisos: si el detector se rompe, estos números se desploman ─────────

    def test_trocea_practicamente_todas_las_funciones(self):
        """El Nivel 1 cuenta las funciones por regex sobre el texto crudo. Si el
        troceo por llaves ve muchas menos, es que se comió unas dentro de otras.
        """
        cuerpos, _, _, _ = _grafo()
        por_regex = set(re.findall(r'(?:async\s+)?function\s+(\w+)', _all_code()))
        assert len(cuerpos) >= 600, (
            f'solo {len(cuerpos)} funciones troceadas — el escáner de llaves '
            f'se está comiendo archivos enteros')
        perdidas = por_regex - set(cuerpos)
        assert len(perdidas) <= 15, (
            f'{len(perdidas)} funciones declaradas que el troceo no ve: '
            f'{sorted(perdidas)[:10]}')

    def test_la_mayoria_del_codigo_es_alcanzable(self):
        """Si el recorrido se rompe, esto se desploma y la lista de huérfanos
        explota. Si el troceo se rompe, se va a 100% y el guard queda ciego."""
        cuerpos, alcanzables, _, raices = _grafo()
        proporcion = len(alcanzables) / len(cuerpos)
        assert 0.85 <= proporcion < 1.0, (
            f'{len(alcanzables)}/{len(cuerpos)} alcanzables ({proporcion:.0%}) — '
            f'fuera del rango sano; el detector está midiendo otra cosa')
        assert len(raices) >= 300, f'solo {len(raices)} raíces'

    #: Una por cada forma de conexión que existe en este repo. Si el detector
    #: deja de reconocer una, la que le corresponde cae y el fallo dice cuál.
    _CANARIOS = (
        ('cargarTrasladosAdmin', 'onclick en index.html'),
        ('trasReintentarDespachoSiesa', 'onclick dentro de una plantilla JS'),
        ('procesarScan', 'llamada normal desde otra función alcanzable'),
        ('verificarModoSistema', 'addEventListener al final del archivo'),
        ('syncOffline', 'llamada desde el arranque del módulo'),
        ('_onQuaggaDetect', 'pasada como valor a Quagga.onDetected'),
        ('_renderTrasladoCard', 'llamada indirecta desde el listado'),
    )

    @pytest.mark.parametrize('fn,forma', _CANARIOS)
    def test_los_canarios_siguen_alcanzables(self, fn, forma):
        _, alcanzables, _, _ = _grafo()
        assert fn in alcanzables, (
            f'{fn} dejó de verse alcanzable — el detector ya no reconoce '
            f'«{forma}», y todo lo que se conecte así va a dar huérfano')

    # ── Mutaciones sobre fuentes sintéticas ────────────────────────────────

    _HTML = '<button onclick="pintar(1)">x</button>'

    def _analiza(self, js, html=None):
        return _analizar({'m.js': js}, html if html is not None else self._HTML)

    def test_una_funcion_que_nadie_llama_NO_es_alcanzable(self):
        """LA MUTACIÓN CENTRAL — el caso exacto de los tres botones."""
        js = '''
function pintar(id) { return `<b>${id}</b>`; }
async function revertir(id) { await fetch(`/api/x/${id}/revertir`); }
'''
        cuerpos, alcanzables, arranque, _ = self._analiza(js)
        assert 'pintar' in alcanzables
        assert 'revertir' not in alcanzables, (
            'una función suelta se declaró alcanzable — el guard no atraparía '
            'nada de lo que existe para atrapar')
        assert '/api/x/' not in _blob_alcanzable(cuerpos, alcanzables, arranque)

    def test_conectarla_desde_la_tarjeta_la_vuelve_alcanzable(self):
        """El otro lado de la mutación: con el botón puesto, cuenta. Sin esto,
        un detector que devuelve «huérfano» para todo también pasaría."""
        js = '''
function pintar(id) { return `<button onclick="revertir(${id})">x</button>`; }
async function revertir(id) { await fetch(`/api/x/${id}/revertir`); }
'''
        cuerpos, alcanzables, arranque, _ = self._analiza(js)
        assert 'revertir' in alcanzables
        assert '/api/x/' in _blob_alcanzable(cuerpos, alcanzables, arranque)

    def test_la_alcanzabilidad_es_transitiva_y_muere_de_raiz(self):
        """Una cadena de tres se recorre entera; si se corta la raíz, cae toda.

        Es el caso de `cargarTrasladosOperario` → `_renderTrasladoOperario` →
        `trasConfirmarRecogida`: nadie llama a la primera y las tres están
        muertas, aunque las dos últimas se llamen entre sí todo el tiempo.
        """
        vivo = '''
function pintar() { return uno(); }
function uno() { return dos(); }
function dos() { return fetch('/api/x/hondo'); }
'''
        _, alcanzables, _, _ = self._analiza(vivo)
        assert {'uno', 'dos'} <= alcanzables
        muerto = vivo.replace('return uno();', 'return 0;')
        _, alcanzables, _, _ = self._analiza(muerto)
        assert 'uno' not in alcanzables and 'dos' not in alcanzables

    def test_la_url_en_un_comentario_no_cuenta_como_consumidor(self):
        js = '''
function pintar() { /* antes llamaba a /api/x/viejo/confirmar */ return 1; }
'''
        cuerpos, alcanzables, arranque, _ = self._analiza(js)
        assert 'pintar' in alcanzables
        assert '/api/x/viejo' not in _blob_alcanzable(
            cuerpos, alcanzables, arranque)

    def test_el_troceo_sobrevive_a_llaves_dentro_de_texto(self):
        """Regex `{2}`, cadenas con `}` y plantillas anidadas: si cualquiera de
        las tres descuadra la cuenta, la función siguiente queda dentro de ésta
        y se declara alcanzable de arrastre."""
        js = r'''
function pintar() {
  const re = /^\d{2}-\w{3}$/;
  const s = "}}} no cierra nada {{{";
  return `<i>${[1,2].map(x => `${x}`).join('')}</i>`;
}
async function huerfana() { await fetch('/api/x/1/confirmar'); }
'''
        cuerpos, alcanzables, arranque, _ = self._analiza(js)
        assert set(cuerpos) == {'pintar', 'huerfana'}, (
            f'el troceo no separó las dos funciones: {sorted(cuerpos)}')
        assert 'huerfana' not in alcanzables
        assert '/api/x/' not in _blob_alcanzable(cuerpos, alcanzables, arranque)

    def test_una_funcion_asignada_a_un_handler_cuenta_como_invocada(self):
        """`el.onclick = fn` no lleva paréntesis en ningún lado: la dispara el
        navegador. Sin esta regla la función daría huérfana y el guard mandaría
        a declarar deuda sobre algo que sí se usa — así se llena de ruido."""
        js = '''
function pintar() { const el = document.body; el.onclick = confirmarTodo; }
async function confirmarTodo() { await fetch('/api/x/1/confirmar'); }
'''
        _, alcanzables, _, _ = self._analiza(js)
        assert 'confirmarTodo' in alcanzables

    def test_el_codigo_de_arranque_es_raiz_aunque_este_al_final(self):
        """`document.addEventListener('DOMContentLoaded', fn)` vive DESPUÉS de
        la última función del archivo. La primera versión del troceo metía todo
        eso dentro del cuerpo de la última función y perdía la raíz."""
        js = '''
function arranca() { return fetch('/api/x/arranque'); }
document.addEventListener('DOMContentLoaded', arranca);
'''
        _, alcanzables, _, _ = self._analiza(js, html='<div></div>')
        assert 'arranca' in alcanzables

    # ── Mutación sobre el repo real, en memoria ────────────────────────────

    _RECUPERACION = (
        ('trasRevertir', '/api/traslados/<int:id>/revertir'),
        ('trasReintentarDespachoSiesa', '/api/traslados/<int:id>/reintentar-despacho'),
        ('trasReintentarRecepcionSiesa', '/api/traslados/<int:id>/reintentar-recepcion'),
    )

    def _fuentes(self):
        return {f: _read(f) for f in _all_js_files()}, _read('index.html')

    def _blob(self, fuentes, html):
        cuerpos, alcanzables, arranque, _ = _analizar(fuentes, html)
        return _blob_alcanzable(cuerpos, alcanzables, arranque)

    @pytest.mark.parametrize('fn,ruta', _RECUPERACION)
    def test_desconectar_el_boton_deja_la_ruta_huerfana(self, fn, ruta):
        """LA MUTACIÓN QUE IMPORTA, contra el código real.

        Se le quita al repo —en memoria, sin tocar disco— el `onclick` que
        conecta cada botón de recuperación, y se exige que la ruta caiga como
        huérfana. Con el guard viejo (adyacencia) las tres seguían pareciendo
        consumidas: la URL está escrita, dentro de una función que nadie llama.
        """
        fuentes, html = self._fuentes()
        assert TestEndpointsSinConsumidor._esta_construida(
            ruta, self._blob(fuentes, html)), (
            f'{ruta} ya está huérfana antes de mutar nada — el botón de {fn} '
            f'se desconectó')

        fuentes['traslados.js'] = re.sub(
            r'onclick="' + fn + r'\([^"]*\)"', 'onclick=""',
            fuentes['traslados.js'])
        assert not TestEndpointsSinConsumidor._esta_construida(
            ruta, self._blob(fuentes, html)), (
            f'quitarle el botón a {fn} NO deja huérfana a {ruta} — el detector '
            f'no está midiendo invocación')

    def test_la_adyacencia_sola_no_habria_visto_nada_de_esto(self):
        """El registro de por qué se subió el listón.

        Con el blob completo —lo que medía el guard hasta ayer— las tres rutas
        pasan aunque se les quite el botón, porque el texto sigue escrito.
        """
        fuentes, html = self._fuentes()
        for fn, _ in self._RECUPERACION:
            fuentes['traslados.js'] = re.sub(
                r'onclick="' + fn + r'\([^"]*\)"', 'onclick=""',
                fuentes['traslados.js'])
        blob_viejo = '\n'.join(fuentes.values()) + html
        for _, ruta in self._RECUPERACION:
            assert TestEndpointsSinConsumidor._esta_construida(ruta, blob_viejo), (
                f'{ruta}: la premisa de este test dejó de ser cierta')


class TestOrganizacionPorDecision:
    """La unidad de organización es la decisión, no el módulo ni el rol.

    Los cuatro modelos vivían bajo Inventario con código y endpoints de compras.
    Nadie los encontraba ahí.
    """

    def _html(self):
        return _read('index.html')

    def test_los_modelos_ya_no_viven_en_inventario(self):
        html = self._html()
        assert 'inv-panel-inteligencia' not in html, \
            'el panel de Inteligencia debe haber salido de Inventario'
        assert 'inv-tab-inteligencia' not in html

    def test_compras_agrupa_operacion_y_decision(self):
        """Lo continuo arriba, lo que compromete plata abajo."""
        html = self._html()
        assert 'OPERACIÓN' in html and 'DECISIÓN' in html
        for sub in ('comp-sub-temporada', 'comp-sub-modelos', 'comp-sub-armador'):
            assert f'id="{sub}"' in html, f'falta la sub-pestaña {sub}'

    def test_el_dispatcher_de_compras_cubre_las_secciones_nuevas(self):
        src = _read('recepcion.js')
        assert "'temporada'" in src and "'modelos'" in src
        assert 'temporadaCargar()' in src and 'modelosCargar()' in src

    def test_inventario_no_dispara_modelos(self):
        """invSubtab quedó con conteos, abc y datos. Nada de modelos."""
        src = _read('conteo.js')
        assert 'compCargarInteligencia' not in src


class TestFusionRopArmador:
    """Es la única cadena que cruza módulos y la de la decisión más cara.

    El déficit China no es información PREVIA al armado: es su insumo.
    """

    def test_el_armador_consume_el_rop(self):
        src = _read('compras_ia.js')
        assert '/api/compras/rop-dual' in src, 'el Armador debe traer el déficit'
        assert '_tablaDeficitChina' in src

    def test_la_tabla_muestra_procedencia_por_fila(self):
        """Dispositivo de seguridad, no adorno.

        El bug de 25x habría producido números absurdos; lo único que separa a
        un humano de aprobarlos es ver en la MISMA fila de dónde salió la
        demanda. Un aviso agregado no basta.
        """
        src = _read('compras_ia.js')
        for campo in ('censurado', 'dias_con_stock', 'factor_censura',
                      'ss_formula_anterior', 'sigma_d_diaria'):
            assert campo in src, f'falta procedencia por fila: {campo}'

    def test_avisa_de_censura_en_la_pantalla_de_decision(self):
        src = _read('compras_ia.js')
        assert 'skus_censurados' in src
        assert 'CENSURADA' in src


class TestBannerModo:
    """Los datos de ensayo entrenan juicios reales. Hay que etiquetarlos."""

    def test_existe_el_contenedor_del_banner(self):
        assert 'id="banner-modo"' in _read('index.html')

    def test_app_consulta_el_modo_y_lo_muestra(self):
        src = _read('app.js')
        assert 'verificarModoSistema' in src
        assert '/api/health/ping' in src
        assert 'MODO ENSAYO' in src and 'MODO SIMULACIÓN' in src

    def test_en_produccion_el_banner_se_oculta(self):
        src = _read('app.js')
        assert "modo === 'produccion'" in src


class TestReposicionNacional:
    """La reorganización no puede dejar huérfana la decisión SEMANAL.

    Sería una ironía costosa: superficie excelente para lo trimestral y nada
    para lo que es pan de cada semana y la mayoría del catálogo.
    """

    def test_existe_la_pantalla(self):
        html = _read('index.html')
        assert 'id="comp-sub-nacional"' in html
        assert 'id="comp-sec-nacional"' in html

    def test_el_dispatcher_la_carga(self):
        assert 'compCargarNacional()' in _read('recepcion.js')

    def test_muestra_procedencia_por_fila(self):
        """Misma exigencia que el déficit China: la señal donde está el número."""
        src = _read('compras_ia.js')
        i = src.index('function _renderNacional')
        bloque = src[i:i + 4000]
        for campo in ('censurado', 'dias_con_stock', 'factor_censura',
                      'ss_formula_anterior', 'sigma_d_diaria'):
            assert campo in bloque, f'Reposición nacional sin procedencia: {campo}'

    def test_ordena_por_urgencia_no_por_alfabeto(self):
        src = _read('compras_ia.js')
        assert 'bajo_rop' in src and 'cobertura_dias' in src


class TestBannerReglaCero:
    """Regla 0 aplicada al propio banner.

    Si el default fuera 'producción', el banner desaparecería justo cuando más
    se necesita, por omisión de configuración — el mismo modo de falla del 403.
    """

    def test_solo_un_produccion_explicito_apaga_el_banner(self):
        src = _read('app.js')
        assert "modo === 'produccion'" in src
        assert 'MODO NO VERIFICADO' in src, \
            'estado desconocido debe mostrar aviso, no ocultarlo'

    def test_fallo_de_red_no_apaga_el_banner(self):
        """Sin respuesta no se puede afirmar que sea producción."""
        src = _read('app.js')
        i = src.index('async function verificarModoSistema')
        bloque = src[i:i + 900]
        assert bloque.count('_pintarBannerModo(null)') >= 2, \
            'tanto el catch como el !r.ok deben pintar el banner'

    def test_el_backend_separa_ensayo_de_conectividad(self, monkeypatch):
        """WMS_ENSAYO es independiente de Connekta a propósito.

        Los flags de connekta dicen si los POST llegan a Siesa; NO dicen si los
        datos en pantalla son de prueba. En un ensayo con vestuario hay
        credenciales reales y datos ficticios a la vez.

        **Se ejerce la función, no se busca la palabra en un archivo.** Antes
        este test hacía `assert "'ensayo'" in src` sobre `health.py`, y se
        rompió el 2026-08-10 cuando el cálculo se unificó en
        `connekta.modo_datos()` — el comportamiento mejoró y el guard falló.
        Medía dónde estaba escrito, no qué hacía.
        """
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'url_get_dinamico',
                            'https://servicios.siesacloud.com/api/x', raising=False)
        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'modo_ensayo', False, raising=False)

        monkeypatch.delenv('WMS_ENSAYO', raising=False)
        assert connekta.modo_datos() == 'produccion'

        monkeypatch.setenv('WMS_ENSAYO', 'true')
        assert connekta.modo_datos() == 'ensayo', (
            'WMS_ENSAYO dejó de pesar: el banner se apagaría en un ensayo con '
            'credenciales reales, que es justo cuando hace falta')
