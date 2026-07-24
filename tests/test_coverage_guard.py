"""
Test guardia de cobertura — BLOQUEA deploy si un servicio o ruta no tiene tests.

Este test escanea todos los archivos de servicios y rutas del proyecto
y verifica que CADA UNO tenga al menos un test asociado. Si se agrega
un servicio nuevo sin tests, el pipeline de CI falla automáticamente.

No mide % de líneas cubiertas — mide si EXISTE al menos un test que
importe o referencie el módulo. La diferencia: un servicio puede tener
1 solo test y pasar (mejor que 0), pero 0 tests es inaceptable.
"""
import os
import re
import pytest

_SERVICES_DIR = os.path.join(os.path.dirname(__file__), '..', 'app', 'services')
_ROUTES_DIR = os.path.join(os.path.dirname(__file__), '..', 'app', 'routes')
_TESTS_DIR = os.path.join(os.path.dirname(__file__))

# Servicios que son helpers internos o wrappers delgados (no necesitan test propio)
_SERVICES_EXCLUIDOS = {
    '__init__',
    'alertas_service',         # scheduler de emails — testeo manual
    'empaques_service',        # wrapper sync — cubierto por sync tests
    'empaques_sync_service',   # scheduler — testeo con datos reales
    'siesa_barcode_sync_service',  # scheduler nocturno
    'siesa_sync_service',      # scheduler — cubierto por test_siesa_*
    'ubicaciones_sync_service', # scheduler nocturno
    'packing_picking_sync_service',  # 89 líneas, bridge
    'faltante_reporte_service',     # 91 líneas, reporte simple
    'siesa_traslado_adapter',       # 146 líneas, adapter thin
    'pedidos_sync_service',    # scheduler — cubierto implícitamente
    'traslado_monitor_service', # scheduler monitoreo
}

# Rutas que son helpers o ya están cubiertas por tests de endpoints
_ROUTES_EXCLUIDOS = {
    '__init__',
    '_auth_helpers',           # cubierto por test_endpoints_criticos
    'config_siesa',            # admin config — testeo manual
    'remision_admin',          # 47 líneas, admin helper
    'factura_admin',           # admin helper
    'empaques',                # CRUD simple
}


def _all_test_content():
    """Lee todo el contenido de todos los archivos de test."""
    content = ''
    for f in os.listdir(_TESTS_DIR):
        if f.startswith('test_') and f.endswith('.py'):
            with open(os.path.join(_TESTS_DIR, f)) as fh:
                content += fh.read()
    return content


class TestCoverageGuard:
    """Cada servicio y ruta debe tener al menos un test."""

    def test_todos_los_servicios_tienen_tests(self):
        """Cada servicio en app/services/ debe estar referenciado en algún test."""
        all_tests = _all_test_content()

        servicios = []
        for f in os.listdir(_SERVICES_DIR):
            if f.endswith('.py') and not f.startswith('__'):
                nombre = f.replace('.py', '')
                if nombre in _SERVICES_EXCLUIDOS:
                    continue
                servicios.append(nombre)

        sin_tests = []
        for svc in servicios:
            # Buscar si algún test importa o referencia este servicio
            patterns = [
                svc,                           # nombre directo
                svc.replace('_', ' '),         # en comentarios
                'from app.services.' + svc,    # import directo
            ]
            encontrado = any(p in all_tests for p in patterns)
            if not encontrado:
                lines = 0
                try:
                    with open(os.path.join(_SERVICES_DIR, svc + '.py')) as f:
                        lines = len(f.readlines())
                except Exception:
                    pass
                sin_tests.append(f'{svc} ({lines} líneas)')

        assert not sin_tests, (
            f'{len(sin_tests)} servicio(s) sin test:\n'
            + '\n'.join(f'  ✗ {s}' for s in sin_tests)
            + '\n\nCada servicio nuevo DEBE tener al menos un test. '
            'Si es un helper interno, agrégalo a _SERVICES_EXCLUIDOS con justificación.'
        )

    def test_todas_las_rutas_tienen_tests(self):
        """Cada ruta en app/routes/ debe estar referenciada en algún test."""
        all_tests = _all_test_content()

        rutas = []
        for f in os.listdir(_ROUTES_DIR):
            if f.endswith('.py') and not f.startswith('__'):
                nombre = f.replace('.py', '')
                if nombre in _ROUTES_EXCLUIDOS:
                    continue
                rutas.append(nombre)

        sin_tests = []
        for ruta in rutas:
            patterns = [
                ruta,
                ruta.replace('_', '-'),
                ruta.replace('_', ' '),
                '/api/' + ruta.replace('_', '-'),
                '/api/' + ruta.replace('_', '/'),
            ]
            encontrado = any(p in all_tests for p in patterns)
            if not encontrado:
                lines = 0
                try:
                    with open(os.path.join(_ROUTES_DIR, ruta + '.py')) as f:
                        lines = len(f.readlines())
                except Exception:
                    pass
                sin_tests.append(f'{ruta} ({lines} líneas)')

        assert not sin_tests, (
            f'{len(sin_tests)} ruta(s) sin test:\n'
            + '\n'.join(f'  ✗ {s}' for s in sin_tests)
            + '\n\nCada ruta nueva DEBE tener al menos un test de endpoint. '
            'Si es un helper, agrégalo a _ROUTES_EXCLUIDOS con justificación.'
        )

    def test_count_minimo_tests(self):
        """Sanity check: el proyecto debe tener al menos 400 tests."""
        total = 0
        for f in os.listdir(_TESTS_DIR):
            if f.startswith('test_') and f.endswith('.py'):
                with open(os.path.join(_TESTS_DIR, f)) as fh:
                    total += len(re.findall(r'def test_', fh.read()))
        assert total >= 400, (
            f'Solo {total} tests — mínimo esperado 400. '
            'Los tests no son opcionales.'
        )
