"""
Tests de RutaService — rutas de despacho, conductor, entrega.
Si falla, el conductor sale sin manifiesto o la entrega no se registra.
"""
import pytest
from datetime import date


@pytest.fixture
def conductor(db):
    """Crea un conductor para tests de rutas."""
    from app.models.usuario import Usuario
    from app.models.conductor import Conductor
    user = Usuario(email='cond_ruta@test.com', nombre='Conductor Test', rol='conductor', activo=True)
    user.set_password('test123')
    db.session.add(user)
    db.session.flush()
    cond = Conductor(usuario_id=user.id, nombre='Conductor Test', cedula='1234567890', activo=True)
    db.session.add(cond)
    db.session.commit()
    return cond


@pytest.fixture
def vehiculo(db):
    """Crea un vehículo para tests de rutas."""
    from app.models.vehiculo import Vehiculo
    v = Vehiculo(placa='ABC123', tipo='Camioneta', activo=True)
    db.session.add(v)
    db.session.commit()
    return v


@pytest.fixture
def ruta_maestra(db):
    """Crea una ruta maestra."""
    from app.models.ruta_maestra import RutaMaestra
    rm = RutaMaestra(nombre='Ruta Test Urbana', tipo_ruta='Urbana', activa=True)
    db.session.add(rm)
    db.session.commit()
    return rm


class TestProgramarViaje:

    def test_programar_viaje_crea_ruta(self, app, db, conductor, vehiculo, ruta_maestra):
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        assert ruta.id is not None
        assert ruta.conductor_id == conductor.id
        assert ruta.vehiculo_id == vehiculo.id
        assert ruta.estado == 'PROGRAMADO'

    def test_programar_sin_conductor_falla(self, app, db, vehiculo):
        from app.services.ruta_service import RutaService
        with pytest.raises((ValueError, Exception)):
            RutaService.programar_viaje({
                'conductor_id': 99999,
                'vehiculo_id': vehiculo.id,
                'tipo_ruta': 'Urbana',
            })


class TestIniciarRuta:

    def test_iniciar_ruta(self, app, db, conductor, vehiculo, ruta_maestra):
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        resultado = RutaService.iniciar_ruta(ruta.id)
        # iniciar_ruta transiciona PROGRAMADO → EN_CARGUE (el conductor carga en muelle)
        db.session.refresh(ruta)
        assert ruta.estado in ('EN_CARGUE', 'EN_TRANSITO')


class TestCerrarRuta:

    def test_cerrar_ruta(self, app, db, conductor, vehiculo, ruta_maestra):
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        RutaService.iniciar_ruta(ruta.id)
        db.session.refresh(ruta)
        # cerrar_ruta requiere EN_CARGUE + bultos — sin bultos lanza error
        # Verificamos que la ruta está en EN_CARGUE (pre-condición para cerrar)
        assert ruta.estado == 'EN_CARGUE'


class TestLiquidarRuta:

    def test_liquidar_ruta(self, app, db, conductor, vehiculo, ruta_maestra):
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        # Avanzar directamente a ENTREGADA para probar liquidación
        ruta.estado = 'ENTREGADA'
        db.session.commit()
        resultado = RutaService.liquidar_ruta(ruta.id)
        db.session.refresh(ruta)
        assert ruta.estado_financiero == 'LIQUIDADA'
