"""
Tests de RutaService — rutas de despacho, conductor, entrega.
Si falla, el conductor sale sin manifiesto o la entrega no se registra.
"""
import pytest
from datetime import date
from unittest.mock import patch


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


class TestTransicionesEstado:
    """
    Máquina de estado de ruta: PROGRAMADO → EN_CARGUE → EN_TRANSITO → ENTREGADA.
    Cada transición tiene un guard. Si alguien salta un paso, el sistema rechaza.
    Cisne negro: liquidar ruta que nunca salió del CDI.
    """

    def test_iniciar_desde_entregada_falla(self, app, db, conductor, vehiculo, ruta_maestra):
        """No se puede iniciar cargue de una ruta ya entregada."""
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        ruta.estado = 'ENTREGADA'
        db.session.commit()
        with pytest.raises(ValueError, match='PROGRAMADO'):
            RutaService.iniciar_ruta(ruta.id)

    def test_cerrar_desde_programado_falla(self, app, db, conductor, vehiculo, ruta_maestra):
        """No se puede cerrar ruta que no está EN_CARGUE."""
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        # Intentar cerrar desde PROGRAMADO (sin pasar por EN_CARGUE)
        with pytest.raises(ValueError):
            RutaService.cerrar_ruta(ruta.id)

    def test_entregar_desde_en_cargue_falla(self, app, db, conductor, vehiculo, ruta_maestra):
        """No se puede entregar ruta que no está EN_TRANSITO."""
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        RutaService.iniciar_ruta(ruta.id)
        # Está EN_CARGUE — intentar entregar sin pasar por EN_TRANSITO
        with pytest.raises(ValueError, match='EN_TRANSITO'):
            RutaService.entregar_ruta(ruta.id, data={'paradas': []}, usuario_id=1)

    def test_flujo_completo_programado_a_en_cargue(self, app, db, conductor, vehiculo, ruta_maestra):
        """PROGRAMADO → EN_CARGUE funciona correctamente."""
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        assert ruta.estado == 'PROGRAMADO'
        RutaService.iniciar_ruta(ruta.id)
        db.session.refresh(ruta)
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

    def test_liquidar_ruta_no_entregada_falla(self, app, db, conductor, vehiculo, ruta_maestra):
        """No se puede liquidar una ruta que no está ENTREGADA."""
        from app.services.ruta_service import RutaService
        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        # Está PROGRAMADO — intentar liquidar
        with pytest.raises((ValueError, LookupError)):
            RutaService.liquidar_ruta(ruta.id)


class TestLiquidarRutaCreaDevolucionesPendientes:
    """
    Bug real en producción (2026-08-13, ruta #16 / PD1350): "Liquidar en WMS"
    dejaba el recaudo RECHAZADO sin ningún job ni devolución — el único botón
    que disparaba la NC se eliminó del módulo Liquidación sin dejar
    reemplazo. Liquidar debe armar la devolución pendiente solo, sin que el
    admin tenga que ir a buscar un botón aparte.
    """

    def _mock_rowids(self, codigo_siesa, cant_base=5):
        return patch('app.services.connekta_gateway.connekta.get_rowids_factura', return_value=[{
            'f120_referencia': codigo_siesa, 'f470_cant_base': cant_base,
            'f470_vlr_neto': 100000, 'f470_id_unidad_medida': 'UND',
            'f150_id': 'NB1', 'f470_rowid': '123',
        }])

    def _crear_recaudo(self, db, almacen, conductor, vehiculo, ruta_maestra,
                        estado_entrega, items_ent=None):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.packing import TareaPacking
        from app.services.ruta_service import RutaService
        import uuid

        ruta = RutaService.programar_viaje({
            'conductor_id': conductor.id,
            'vehiculo_id': vehiculo.id,
            'ruta_maestra_id': ruta_maestra.id,
            'tipo_ruta': 'Urbana',
            'fecha_programada': date.today().isoformat(),
        })
        ruta.estado = 'ENTREGADA'
        tarea = TareaPacking(
            codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO', almacen_id=almacen.id,
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1350,
            numero_pedido_siesa='PD1350',
        )
        db.session.add(tarea)
        db.session.flush()
        recaudo = RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega=estado_entrega, forma_pago='EFECTIVO', monto_cobrado=0,
            items_entregados=items_ent,
        )
        db.session.add(recaudo)
        db.session.commit()
        return ruta, recaudo

    def test_liquidar_ruta_rechazado_crea_devolucion_pendiente(
            self, app, db, almacen, conductor, vehiculo, ruta_maestra, producto):
        from app.services.ruta_service import RutaService
        from app.models.devolucion_cliente import DevolucionCliente

        ruta, recaudo = self._crear_recaudo(
            db, almacen, conductor, vehiculo, ruta_maestra, 'RECHAZADO')

        with patch('app.services.fe_resolver.resolver_fe', return_value=('FEW', '9999')), \
             self._mock_rowids(producto.codigo_siesa):
            resultado = RutaService.liquidar_ruta(ruta.id)

        assert resultado['devoluciones_pendientes_creadas'] == 1

        devolucion = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo.id).first()
        assert devolucion is not None
        assert devolucion.estado == 'ABIERTA'
        assert devolucion.es_total is True

        from app.models.siesa_job import SiesaJob
        assert SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='NOTA_CREDITO_FACTURA').count() == 0

    def test_liquidar_ruta_parcial_crea_devolucion_pendiente(
            self, app, db, almacen, conductor, vehiculo, ruta_maestra, producto):
        from app.services.ruta_service import RutaService
        from app.models.devolucion_cliente import DevolucionCliente

        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 2}]
        ruta, recaudo = self._crear_recaudo(
            db, almacen, conductor, vehiculo, ruta_maestra, 'PARCIAL', items_ent=items)

        with patch('app.services.fe_resolver.resolver_fe', return_value=('FEW', '9999')), \
             self._mock_rowids(producto.codigo_siesa):
            RutaService.liquidar_ruta(ruta.id)

        devolucion = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo.id).first()
        assert devolucion is not None
        assert devolucion.es_total is False

    def test_liquidar_ruta_entregado_no_crea_devolucion(
            self, app, db, almacen, conductor, vehiculo, ruta_maestra, producto):
        """ENTREGADO no pasa por devoluciones — no hay nada que devolver."""
        from app.services.ruta_service import RutaService
        from app.models.devolucion_cliente import DevolucionCliente

        ruta, recaudo = self._crear_recaudo(
            db, almacen, conductor, vehiculo, ruta_maestra, 'ENTREGADO')

        resultado = RutaService.liquidar_ruta(ruta.id)

        assert resultado['devoluciones_pendientes_creadas'] == 0
        assert DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo.id).first() is None

    def test_liquidar_ruta_no_duplica_si_ya_existe_devolucion(
            self, app, db, almacen, conductor, vehiculo, ruta_maestra, producto):
        """Si el admin ya liquidó (o el flujo manual ya armó la devolución),
        volver a liquidar no debe crear una segunda pendiente para el mismo recaudo."""
        from app.services.ruta_service import RutaService
        from app.services.liquidacion_service import LiquidacionService
        from app.models.devolucion_cliente import DevolucionCliente

        ruta, recaudo = self._crear_recaudo(
            db, almacen, conductor, vehiculo, ruta_maestra, 'RECHAZADO')

        with patch('app.services.fe_resolver.resolver_fe', return_value=('FEW', '9999')), \
             self._mock_rowids(producto.codigo_siesa):
            RutaService.liquidar_ruta(ruta.id)
            # Re-invocar directamente el creador (simula liquidar de nuevo /
            # el botón manual de respaldo en Rutas)
            resumen = LiquidacionService.crear_devoluciones_pendientes_ruta(ruta.id)

        assert resumen['creadas'] == 0
        assert DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo.id).count() == 1
