"""
Test 05 — Dead Letter Queue: reintentos, backoff, alerta admin.
Garantiza que ninguna transferencia a Siesa se pierda silenciosamente.
"""
import pytest
import unittest.mock as mock
from datetime import datetime, timedelta


class TestProcesarJobsPendientes:

    def test_procesa_job_exitoso(self, app, db):
        """Un job PENDIENTE se procesa y pasa a COMPLETADO."""
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import procesar_jobs_pendientes

        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {
            'bodega_id': 'NB1', 'ubicacion_origen': 'RES-01',
            'ubicacion_destino': 'PIK-01', 'referencia_item': 'PROD-001',
            'cantidad': 100, 'nota': 'test',
        })
        db.session.commit()

        respuesta_siesa = {'consecutivo': '12345', 'mensaje': 'OK'}
        with mock.patch(
            'app.services.siesa_job_service._ejecutar_job',
            return_value=respuesta_siesa
        ):
            procesados = procesar_jobs_pendientes(app=app)

        assert procesados == 1
        db.session.refresh(job)
        assert job.estado == 'COMPLETADO'
        assert '12345' in job.resultado

    def test_job_fallido_reintenta_con_backoff(self, app, db):
        """Un fallo marca el job para reintento con tiempo de espera."""
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import procesar_jobs_pendientes

        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {
            'bodega_id': 'NB1', 'ubicacion_origen': 'RES-01',
            'ubicacion_destino': 'PIK-01', 'referencia_item': 'PROD-001',
            'cantidad': 100, 'nota': '',
        })
        db.session.commit()

        with mock.patch(
            'app.services.siesa_job_service._ejecutar_job',
            side_effect=Exception('Connekta timeout')
        ):
            procesar_jobs_pendientes(app=app)

        db.session.refresh(job)
        assert job.estado == 'PENDIENTE'  # reintentable
        assert job.intentos == 1
        assert job.proximo_intento > datetime.utcnow()

    def test_tres_fallos_pasan_a_fallido(self, app, db):
        """Tras max_intentos fallidos → FALLIDO."""
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import procesar_jobs_pendientes

        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {
            'bodega_id': 'NB1', 'ubicacion_origen': 'RES-01',
            'ubicacion_destino': 'PIK-01', 'referencia_item': 'PROD-001',
            'cantidad': 100, 'nota': '',
        })
        db.session.commit()
        max_intentos = job.max_intentos  # 5 por defecto

        for i in range(max_intentos):
            # Resetear proximo_intento para que el procesador lo tome
            job.proximo_intento = None
            db.session.commit()

            with mock.patch(
                'app.services.siesa_job_service._ejecutar_job',
                side_effect=Exception('Periodo cerrado')
            ):
                procesar_jobs_pendientes(app=app)

        db.session.refresh(job)
        assert job.estado == 'FALLIDO'
        assert job.intentos == max_intentos

    def test_no_procesa_job_con_proximo_intento_futuro(self, app, db):
        """Un job con proximo_intento en el futuro NO se procesa ahora."""
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import procesar_jobs_pendientes

        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        job.proximo_intento = datetime.utcnow() + timedelta(hours=1)
        db.session.commit()

        with mock.patch('app.services.siesa_job_service._ejecutar_job') as m:
            procesar_jobs_pendientes(app=app)
            m.assert_not_called()

        db.session.refresh(job)
        assert job.estado == 'PENDIENTE'

    def test_no_procesa_jobs_ya_completados(self, app, db):
        """Jobs COMPLETADO o FALLIDO no se tocan."""
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import procesar_jobs_pendientes

        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        job.estado = 'COMPLETADO'
        db.session.commit()

        with mock.patch('app.services.siesa_job_service._ejecutar_job') as m:
            procesar_jobs_pendientes(app=app)
            m.assert_not_called()


class TestGetJobsFallidos:

    def test_retorna_solo_fallidos(self, app, db):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import get_jobs_fallidos

        j1 = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        j1.estado = 'FALLIDO'
        j2 = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 2})
        j2.estado = 'COMPLETADO'
        j3 = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 3})
        # j3 queda PENDIENTE
        db.session.commit()

        fallidos = get_jobs_fallidos()
        assert len(fallidos) == 1
        assert fallidos[0].id == j1.id


class TestReintentar:

    def test_reintentar_job_fallido(self, app, db):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import reintentar_job

        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        job.estado = 'FALLIDO'
        job.intentos = 3
        job.error_ultimo = 'Periodo cerrado'
        db.session.commit()

        resultado = reintentar_job(job.id)
        assert resultado['estado'] == 'PENDIENTE'
        assert resultado['intentos'] == 0

    def test_no_puede_reintentar_pendiente(self, app, db):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import reintentar_job

        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        db.session.commit()

        with pytest.raises(ValueError, match='no está en estado FALLIDO'):
            reintentar_job(job.id)
