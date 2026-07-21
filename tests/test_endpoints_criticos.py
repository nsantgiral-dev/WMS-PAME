"""
Tests de endpoints HTTP criticos del WMS.

Valida status codes, estructura basica de respuesta y control de acceso
(autenticacion JWT y autorizacion por rol) en los endpoints mas importantes.
"""
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 1. POST /api/auth/login
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthLogin:

    def test_login_exitoso(self, client, usuario):
        resp = client.post('/api/auth/login', json={
            'email': 'op@test.com',
            'password': 'test123',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'token' in data
        assert 'usuario' in data
        assert data['usuario']['email'] == 'op@test.com'

    def test_login_password_invalido(self, client, usuario):
        resp = client.post('/api/auth/login', json={
            'email': 'op@test.com',
            'password': 'incorrecta',
        })
        assert resp.status_code == 401
        data = resp.get_json()
        assert 'error' in data

    def test_login_sin_campos(self, client):
        resp = client.post('/api/auth/login', json={})
        assert resp.status_code == 400

    def test_login_usuario_inexistente(self, client, db):
        resp = client.post('/api/auth/login', json={
            'email': 'no-existe@test.com',
            'password': 'test123',
        })
        assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# 2. GET /api/mobile/tarea-actual
# ─────────────────────────────────────────────────────────────────────────────

class TestMobileTareaActual:

    def test_sin_token_401(self, client):
        resp = client.get('/api/mobile/tarea-actual')
        assert resp.status_code == 401

    def test_con_token_valido(self, client, jwt_token):
        resp = client.get(
            '/api/mobile/tarea-actual',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        # Operario valido puede acceder — 200 con tarea o sin_tareas
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


# ─────────────────────────────────────────────────────────────────────────────
# 3. GET /api/packing/
# ─────────────────────────────────────────────────────────────────────────────

class TestPackingListar:

    def test_sin_token_401(self, client):
        resp = client.get('/api/packing/')
        assert resp.status_code == 401

    def test_listar_tareas_con_token(self, client, jwt_token_admin):
        resp = client.get(
            '/api/packing/',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, (dict, list))


# ─────────────────────────────────────────────────────────────────────────────
# 4. GET /api/conteo/
# ─────────────────────────────────────────────────────────────────────────────

class TestConteoListar:

    def test_sin_token_401(self, client):
        resp = client.get('/api/conteo/')
        assert resp.status_code == 401

    def test_listar_sesiones_admin(self, client, jwt_token_admin):
        resp = client.get(
            '/api/conteo/',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_operario_sin_permiso_403(self, client, jwt_token):
        """Operario comun no pertenece a Roles.SUPERVISION — debe recibir 403."""
        resp = client.get(
            '/api/conteo/',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 5. GET /api/reposicion/ubicaciones
# ─────────────────────────────────────────────────────────────────────────────

class TestReposicionUbicaciones:

    def test_sin_token_401(self, client):
        resp = client.get('/api/reposicion/ubicaciones')
        assert resp.status_code == 401

    def test_admin_obtiene_lista(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/reposicion/ubicaciones?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'ubicaciones' in data
        assert 'total' in data
        assert isinstance(data['ubicaciones'], list)

    def test_operario_sin_permiso_403(self, client, jwt_token):
        resp = client.get(
            '/api/reposicion/ubicaciones',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 6. GET /api/dashboard/resumen-completo
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardResumenCompleto:

    def test_sin_token_401(self, client):
        resp = client.get('/api/dashboard/resumen-completo')
        assert resp.status_code == 401

    def test_sin_almacen_id_400(self, client, jwt_token_admin):
        resp = client.get(
            '/api/dashboard/resumen-completo',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 400
        assert 'almacen_id' in resp.get_json()['error']

    def test_con_almacen_id_retorna_kpis(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/resumen-completo?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_operario_sin_permiso_403(self, client, jwt_token, almacen):
        resp = client.get(
            f'/api/dashboard/resumen-completo?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 7. GET /api/health/ping
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthPing:

    def test_publico_sin_token(self, client):
        resp = client.get('/api/health/ping')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        assert 'circuit_breaker' in data


# ─────────────────────────────────────────────────────────────────────────────
# 8. GET /api/health/siesa
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthSiesa:

    def test_sin_token_401(self, client):
        resp = client.get('/api/health/siesa')
        assert resp.status_code == 401

    def test_admin_accede(self, client, jwt_token_admin):
        resp = client.get(
            '/api/health/siesa',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        # 200 o 503 dependiendo de variables configuradas — ambos son validos
        assert resp.status_code in (200, 503)
        data = resp.get_json()
        assert 'variables' in data
        assert 'modo' in data
        assert 'circuit_breaker' in data

    def test_operario_rechazado_403(self, client, jwt_token):
        resp = client.get(
            '/api/health/siesa',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 9. Endpoints admin rechazan tokens de operario (403)
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminRechazaOperario:
    """Verifica que endpoints restringidos a gestion/admin devuelvan 403 con token operario."""

    def test_dashboard_kpis_rechaza_operario(self, client, jwt_token, almacen):
        resp = client.get(
            f'/api/dashboard/kpis?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_dashboard_productividad_rechaza_operario(self, client, jwt_token, almacen):
        resp = client.get(
            f'/api/dashboard/productividad?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_conteo_rechaza_operario(self, client, jwt_token):
        resp = client.get(
            '/api/conteo/',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_health_siesa_rechaza_operario(self, client, jwt_token):
        resp = client.get(
            '/api/health/siesa',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_reposicion_ubicaciones_rechaza_operario(self, client, jwt_token):
        resp = client.get(
            '/api/reposicion/ubicaciones',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_reposicion_pendientes_rechaza_operario(self, client, jwt_token):
        resp = client.get(
            '/api/reposicion/pendientes',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403
