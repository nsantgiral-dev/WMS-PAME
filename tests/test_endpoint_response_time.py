"""
Test anti-freeze: endpoints críticos deben responder en < 3 segundos.

Previene la regresión donde un scheduler pesado o un query sin paginar
bloquea el único worker de gunicorn y el server entero devuelve 502.

Si este test falla, significa que un endpoint se cuelga — probablemente
un query sin límite, un N+1, o un deadlock de DB.
"""
import time
import pytest

MAX_RESPONSE_SECONDS = 3  # ningún endpoint debe tardar más de 3s


class TestEndpointResponseTime:
    """Cada endpoint crítico debe responder en < 3 segundos."""

    def _timed_get(self, client, url, headers):
        """GET con medición de tiempo. Falla si excede MAX_RESPONSE_SECONDS."""
        start = time.monotonic()
        resp = client.get(url, headers=headers)
        elapsed = time.monotonic() - start
        assert elapsed < MAX_RESPONSE_SECONDS, (
            f'{url} tardó {elapsed:.1f}s (máximo {MAX_RESPONSE_SECONDS}s) — '
            f'posible query sin paginar o deadlock'
        )
        return resp, elapsed

    # ── Públicos (sin auth) ─────────────────────────────────────────

    def test_health_ping(self, client):
        resp, t = self._timed_get(client, '/api/health/ping', {})
        assert resp.status_code == 200

    def test_health_main(self, client):
        resp, t = self._timed_get(client, '/health', {})
        assert resp.status_code == 200

    # ── Autenticados — los que se congelaban ────────────────────────

    def test_rutas_lista(self, client, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, '/api/rutas/', h)
        assert resp.status_code == 200

    def test_rutas_con_fecha(self, client, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, '/api/rutas/?fecha_desde=2026-07-21&fecha_hasta=2026-07-21', h)
        assert resp.status_code == 200

    def test_conteo_lista(self, client, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, '/api/conteo/?estados=PENDIENTE,EN_PROCESO&page=1', h)
        assert resp.status_code == 200

    def test_reposicion_ubicaciones(self, client, jwt_token_admin, almacen):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, f'/api/reposicion/ubicaciones?almacen_id={almacen.id}', h)
        assert resp.status_code == 200

    def test_packing_lista(self, client, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, '/api/packing/', h)
        assert resp.status_code == 200

    def test_muelle_listos(self, client, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, '/api/muelle/listos', h)
        assert resp.status_code == 200

    def test_dashboard(self, client, jwt_token_admin, almacen):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, f'/api/dashboard/resumen-completo?almacen_id={almacen.id}', h)
        assert resp.status_code == 200

    def test_liquidacion_dashboard(self, client, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp, t = self._timed_get(client, '/api/rutas/liquidacion/dashboard?fecha_desde=2026-07-21&fecha_hasta=2026-07-21', h)
        assert resp.status_code == 200


class TestGunicornConfig:
    """Valida que la configuración de gunicorn en railway.toml sea segura."""

    def test_workers_mayor_a_uno(self):
        """Con 1 worker, un scheduler bloqueado congela todo el server."""
        import re
        with open('railway.toml') as f:
            content = f.read()
        match = re.search(r'--workers[=\s]+(\d+)', content)
        assert match, 'No se encontró --workers en railway.toml'
        workers = int(match.group(1))
        assert workers >= 2, (
            f'gunicorn tiene --workers={workers} — DEBE ser >= 2. '
            'Con 1 worker, un scheduler bloqueado congela todo el server (502).'
        )

    def test_timeout_razonable(self):
        """Timeout > 120s permite que workers bloqueados vivan demasiado."""
        import re
        with open('railway.toml') as f:
            content = f.read()
        match = re.search(r'--timeout[=\s]+(\d+)', content)
        assert match, 'No se encontró --timeout en railway.toml'
        timeout = int(match.group(1))
        assert timeout <= 120, (
            f'gunicorn --timeout={timeout}s es demasiado alto. '
            'Workers bloqueados deben ser matados en <= 120s.'
        )

    def test_preload_activo(self):
        """--preload evita que schedulers se dupliquen por cada worker."""
        with open('railway.toml') as f:
            content = f.read()
        assert '--preload' in content, (
            'gunicorn sin --preload duplica schedulers por cada worker. '
            'Con N workers = N syncs simultáneos = N veces la carga en DB.'
        )
