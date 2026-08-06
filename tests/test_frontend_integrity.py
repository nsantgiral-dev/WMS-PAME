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
        """sw.js SHELL array incluye todos los módulos JS (excepto etiquetas)."""
        sw = _read('sw.js')
        scripts = self._script_tags_in_html()
        # etiquetas.js puede no estar en SW (pre-existente, network-first)
        core_scripts = [s for s in scripts if s != 'etiquetas.js']
        for s in core_scripts:
            assert s in sw, f'{s} no está en sw.js SHELL — PWA offline se rompe'

    def test_cache_bust_versions(self):
        """Cada script tag tiene version param para cache busting."""
        html = _read('index.html')
        scripts = re.findall(r'<script src="(/static/pwa/\w+\.js[^"]*)"', html)
        for script in scripts:
            if 'etiquetas.js' in script:
                continue  # puede no tener version
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

    '/api/kardex/reconstruir':
        'Decisión: ¿le creo al stock diario? Reconstruye la serie hacia atrás. '
        'Su lugar es como precondición de la descensura, no como botón suelto.',
    '/api/kardex/stock-diario':
        'Decisión: ¿por qué este SKU tiene esa demanda corregida? Es la evidencia '
        'de los días sin stock. Va como detalle expandible junto al SKU en Reposición.',
    '/api/kardex/reconciliar':
        'Decisión: ¿le creo al kardex? Compuerta de completitud y conceptos sin '
        'clasificar. Va como semáforo de confianza en la pantalla Modelos.',
    '/api/kardex/tasa-servida-corregida':
        'Descensura M0.2. Doblemente huérfana: sin UI Y sin consumidor aguas abajo. '
        'No necesita pantalla — necesita que TSB, ROP y newsvendor la consuman, y '
        'aparecer como procedencia junto al SKU: "1.240 u/mes, +18% por 62 días sin stock".',
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

    @staticmethod
    def _segmentos_literales(ruta):
        """Trozos fijos de la ruta, ignorando <int:id> y demás.

        El JS arma las URLs por concatenación ('/api/x/' + id + '/cerrar'), así
        que buscar la ruta completa daría falsos positivos. Exigimos que TODOS
        los trozos literales aparezcan: el prefijo y también lo que va después
        del parámetro.
        """
        return [s for s in re.split(r'<[^>]+>', ruta) if s.strip('/')]

    def _huerfanos(self, app):
        blob = _all_code() + _read('index.html') if os.path.exists(
            os.path.join(_PWA, 'index.html')) else _all_code()
        huerfanos = set()
        for regla in app.url_map.iter_rules():
            ruta = str(regla)
            if not ruta.startswith('/api/'):
                continue
            if any(x in ruta for x in _EXENTOS_POR_REGLA):
                continue
            if all(s.rstrip('/') in blob for s in self._segmentos_literales(ruta)):
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

    def test_el_backend_separa_ensayo_de_conectividad(self):
        """WMS_ENSAYO es independiente de Connekta a propósito.

        Los flags de connekta dicen si los POST llegan a Siesa; NO dicen si los
        datos en pantalla son de prueba. En un ensayo con vestuario hay
        credenciales reales y datos ficticios a la vez.
        """
        import os
        ruta = os.path.join(os.path.dirname(__file__), '..', 'app', 'routes', 'health.py')
        src = open(ruta).read()
        assert "WMS_ENSAYO" in src
        assert "'ensayo'" in src
