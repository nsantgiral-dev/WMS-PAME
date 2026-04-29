"""
Test 07 — Endpoints HTTP de la API de Reposición.
Valida status codes, estructura de respuesta y autenticación.
"""
import pytest
import unittest.mock as mock


class TestEndpointsReposicion:

    def test_tarea_actual_sin_tareas(self, app, db, client, jwt_token_abastecedor):
        response = client.get(
            '/api/reposicion/tarea-actual',
            headers={'Authorization': f'Bearer {jwt_token_abastecedor}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['sin_tareas'] is True

    def test_mis_tareas_vacio(self, app, db, client, jwt_token_abastecedor):
        response = client.get(
            '/api/reposicion/mis-tareas',
            headers={'Authorization': f'Bearer {jwt_token_abastecedor}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['total'] == 0
        assert data['tareas'] == []

    def test_pendientes_admin(self, app, db, client, jwt_token_admin,
                               producto, almacen, ub_reserva, ub_picking):
        from app.models.tarea_reposicion import TareaReposicion
        t = TareaReposicion(
            codigo='REP-TEST-001',
            producto_id=producto.id,
            almacen_id=almacen.id,
            cantidad_unidades=100,
            ubicacion_reserva_id=ub_reserva.id,
            ubicacion_picking_id=ub_picking.id,
            estado='PENDIENTE',
        )
        db.session.add(t)
        db.session.commit()

        response = client.get(
            '/api/reposicion/pendientes',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['total'] == 1

    def test_cancelar_tarea(self, app, db, client, jwt_token_admin,
                             producto, almacen, ub_reserva, ub_picking):
        from app.models.tarea_reposicion import TareaReposicion
        t = TareaReposicion(
            codigo='REP-TEST-002',
            producto_id=producto.id,
            almacen_id=almacen.id,
            cantidad_unidades=50,
            ubicacion_reserva_id=ub_reserva.id,
            ubicacion_picking_id=ub_picking.id,
            estado='PENDIENTE',
        )
        db.session.add(t)
        db.session.commit()

        response = client.post(
            f'/api/reposicion/cancelar/{t.id}',
            json={'motivo': 'LPN dañado'},
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['ok'] is True
        assert data['tarea']['estado'] == 'CANCELADA'

    def test_cancelar_tarea_inexistente_404(self, app, db, client, jwt_token_admin):
        response = client.post(
            '/api/reposicion/cancelar/99999',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert response.status_code == 404

    def test_verificar_stock_endpoint(self, app, db, client, jwt_token_admin, almacen):
        with mock.patch('app.routes.reposicion.verificar_stock_picking', return_value=2):
            response = client.post(
                '/api/reposicion/verificar-stock',
                json={'almacen_id': almacen.id},
                headers={'Authorization': f'Bearer {jwt_token_admin}'},
            )
        assert response.status_code == 200
        data = response.get_json()
        assert data['tareas_generadas'] == 2

    def test_jobs_fallidos_vacio(self, app, db, client, jwt_token_admin):
        response = client.get(
            '/api/reposicion/siesa-jobs/fallidos',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['total'] == 0
        assert data['alerta'] is None

    def test_jobs_fallidos_muestra_alerta(self, app, db, client, jwt_token_admin):
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        job.estado = 'FALLIDO'
        job.intentos = 3
        db.session.commit()

        response = client.get(
            '/api/reposicion/siesa-jobs/fallidos',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['total'] == 1
        assert data['alerta'] is not None
        assert 'Siesa' in data['alerta']

    def test_pre_verificar_ola_endpoint(self, app, db, client, jwt_token_admin,
                                         producto, almacen):
        with mock.patch(
            'app.routes.reposicion.pre_verificar_ola',
            return_value={
                'items_ok': [], 'items_con_alerta': [],
                'reposiciones_generadas': 0, 'hay_deficit': False,
            }
        ):
            response = client.post(
                '/api/reposicion/pre-verificar-ola',
                json={
                    'almacen_id': almacen.id,
                    'items': [{'producto_id': producto.id, 'cantidad': 10}],
                },
                headers={'Authorization': f'Bearer {jwt_token_admin}'},
            )
        assert response.status_code == 200

    def test_requiere_jwt(self, app, db, client):
        """Sin token → 401."""
        response = client.get('/api/reposicion/tarea-actual')
        assert response.status_code == 401

    def test_confirmar_sin_tarea_id_retorna_400(self, app, db, client, jwt_token_abastecedor):
        response = client.post(
            '/api/reposicion/confirmar',
            json={},
            headers={'Authorization': f'Bearer {jwt_token_abastecedor}'},
        )
        assert response.status_code == 400
