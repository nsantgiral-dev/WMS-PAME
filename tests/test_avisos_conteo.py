"""
Test — avisos_conteo_service: aviso de solo lectura al operario de CC2/CC3
cuando el ADI de conteo cíclico falla por disponible negativo en Siesa.
No debe mutar ninguna sesión ni job existente.
"""
import pytest


@pytest.fixture
def sesion_cc1(db, almacen, producto, ub_reserva, usuario):
    from app.models.conteo import SesionConteo
    s = SesionConteo(
        codigo='CC1-TEST', tipo='DIARIO_ABC',
        ubicacion_id=ub_reserva.id, almacen_id=almacen.id,
        producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
        operario_id=usuario.id, estado='AJUSTANDO',
        cantidad_fisica=15, existencia_siesa=110, diferencia=5,
        motivo_codigo='AJ-ENT',
    )
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def operario_cc2(db, almacen):
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(
        nombre='Operario CC2', email='cc2@test.com',
        password_hash=generate_password_hash('test123'),
        rol='operario', almacen_id=almacen.id, activo=True, puede_picar=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _crear_job_fallido(db, sesion_id, error_msg):
    from app.models.siesa_job import SiesaJob, EstadoSiesaJob
    job = SiesaJob.encolar('AJUSTE_CONTEO', {'sesion_id': sesion_id},
                            referencia_tipo='SesionConteo', referencia_id=sesion_id)
    job.estado = EstadoSiesaJob.FALLIDO
    job.error_ultimo = error_msg
    db.session.commit()
    return job


class TestObtenerAvisosPendientes:
    def test_sin_sesiones_cc2_retorna_vacio(self, db, usuario):
        from app.services.avisos_conteo_service import obtener_avisos_pendientes
        assert obtener_avisos_pendientes(usuario.id) == []

    def test_cc2_con_job_fallido_por_disponible_genera_aviso(
        self, db, sesion_cc1, operario_cc2, almacen, producto, ub_reserva
    ):
        from app.models.conteo import SesionConteo
        from app.services.avisos_conteo_service import obtener_avisos_pendientes

        cc2 = SesionConteo(
            codigo='CC2-TEST', tipo='DIARIO_ABC',
            ubicacion_id=ub_reserva.id, almacen_id=almacen.id,
            producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
            operario_id=operario_cc2.id, estado='DESCUADRE',
            es_segundo_conteo=True, sesion_origen_id=sesion_cc1.id,
            cantidad_fisica=15,
        )
        db.session.add(cc2)
        db.session.commit()

        error = (
            "Siesa rechazó el documento (HTTP 400): {'codigo': 1, 'detalle': "
            "[{'f_detalle': 'Movto Inventario: Item sin cantidad disponible "
            "Faltante Inv.: -8.000000'}]}"
        )
        _crear_job_fallido(db, sesion_cc1.id, error)

        avisos = obtener_avisos_pendientes(operario_cc2.id)
        assert len(avisos) == 1
        assert avisos[0]['sesion_codigo'] == 'CC1-TEST'
        assert avisos[0]['tipo'] == 'advertencia'
        assert 'supervisor' in avisos[0]['mensaje'].lower()
        # No debe exponer cifras de Siesa
        assert '-8' not in avisos[0]['mensaje']

    def test_cc2_con_job_fallido_por_otra_razon_no_genera_aviso(
        self, db, sesion_cc1, operario_cc2, almacen, producto, ub_reserva
    ):
        """Solo el sentinel de disponible negativo debe disparar el aviso."""
        from app.models.conteo import SesionConteo
        from app.services.avisos_conteo_service import obtener_avisos_pendientes

        cc2 = SesionConteo(
            codigo='CC2-TEST2', tipo='DIARIO_ABC',
            ubicacion_id=ub_reserva.id, almacen_id=almacen.id,
            producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
            operario_id=operario_cc2.id, estado='DESCUADRE',
            es_segundo_conteo=True, sesion_origen_id=sesion_cc1.id,
        )
        db.session.add(cc2)
        db.session.commit()

        _crear_job_fallido(
            db, sesion_cc1.id,
            "El dato es obligatorio. Posición inicial: 2673.Posición final: 2693"
        )

        assert obtener_avisos_pendientes(operario_cc2.id) == []

    def test_cc3_resuelve_hasta_la_raiz_cc1(
        self, db, sesion_cc1, operario_cc2, almacen, producto, ub_reserva
    ):
        """CC1(AJUSTANDO) -> CC2(TERCER_CONTEO) -> CC3(operario) debe llegar al job de CC1."""
        from app.models.conteo import SesionConteo
        from app.services.avisos_conteo_service import obtener_avisos_pendientes
        from app.models.usuario import Usuario
        from werkzeug.security import generate_password_hash

        operario_cc3 = Usuario(
            nombre='Operario CC3', email='cc3@test.com',
            password_hash=generate_password_hash('test123'),
            rol='operario', almacen_id=almacen.id, activo=True, puede_picar=True,
        )
        db.session.add(operario_cc3)
        db.session.commit()

        cc2 = SesionConteo(
            codigo='CC2-CHAIN', tipo='DIARIO_ABC',
            ubicacion_id=ub_reserva.id, almacen_id=almacen.id,
            producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
            operario_id=operario_cc2.id, estado='TERCER_CONTEO',
            es_segundo_conteo=True, sesion_origen_id=sesion_cc1.id,
        )
        db.session.add(cc2)
        db.session.commit()

        cc3 = SesionConteo(
            codigo='CC3-CHAIN', tipo='DIARIO_ABC',
            ubicacion_id=ub_reserva.id, almacen_id=almacen.id,
            producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
            operario_id=operario_cc3.id, estado='DESCUADRE',
            es_segundo_conteo=True, sesion_origen_id=cc2.id,
        )
        db.session.add(cc3)
        db.session.commit()

        _crear_job_fallido(
            db, sesion_cc1.id,
            "Siesa rechazó el documento (HTTP 400): Item sin cantidad disponible Faltante Inv.: -3.0"
        )

        avisos_cc3 = obtener_avisos_pendientes(operario_cc3.id)
        assert len(avisos_cc3) == 1
        assert avisos_cc3[0]['sesion_codigo'] == 'CC1-TEST'

    def test_sesion_raiz_resuelta_no_genera_aviso(
        self, db, sesion_cc1, operario_cc2, almacen, producto, ub_reserva
    ):
        """Si la raíz ya no está atascada (ej. AJUSTADO), no debe avisar."""
        from app.models.conteo import SesionConteo
        from app.services.avisos_conteo_service import obtener_avisos_pendientes

        sesion_cc1.estado = 'AJUSTADO'
        cc2 = SesionConteo(
            codigo='CC2-RESUELTO', tipo='DIARIO_ABC',
            ubicacion_id=ub_reserva.id, almacen_id=almacen.id,
            producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
            operario_id=operario_cc2.id, estado='MATCH',
            es_segundo_conteo=True, sesion_origen_id=sesion_cc1.id,
        )
        db.session.add(cc2)
        db.session.commit()

        _crear_job_fallido(
            db, sesion_cc1.id,
            "Item sin cantidad disponible Faltante Inv.: -5.0"
        )

        assert obtener_avisos_pendientes(operario_cc2.id) == []

    def test_no_muta_ningun_registro(self, db, sesion_cc1, operario_cc2, almacen, producto, ub_reserva):
        """La función debe ser puramente de lectura."""
        from app.models.conteo import SesionConteo
        from app.models.siesa_job import SiesaJob
        from app.services.avisos_conteo_service import obtener_avisos_pendientes

        cc2 = SesionConteo(
            codigo='CC2-READONLY', tipo='DIARIO_ABC',
            ubicacion_id=ub_reserva.id, almacen_id=almacen.id,
            producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
            operario_id=operario_cc2.id, estado='DESCUADRE',
            es_segundo_conteo=True, sesion_origen_id=sesion_cc1.id,
        )
        db.session.add(cc2)
        db.session.commit()
        job = _crear_job_fallido(db, sesion_cc1.id, "Item sin cantidad disponible")

        estado_raiz_antes = sesion_cc1.estado
        estado_job_antes = job.estado
        intentos_antes = job.intentos

        obtener_avisos_pendientes(operario_cc2.id)

        db.session.refresh(sesion_cc1)
        db.session.refresh(job)
        assert sesion_cc1.estado == estado_raiz_antes
        assert job.estado == estado_job_antes
        assert job.intentos == intentos_antes
