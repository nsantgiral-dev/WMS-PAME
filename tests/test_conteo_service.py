"""
Test — ConteoService: conteo ciclico service logic.

Covers:
  - crear_conteo_manual: creates SesionConteo per ubicacion with stock
  - crear_conteo_manual no-dup: skips if active session exists
  - registrar_conteo MATCH: physical == expected (WMS fallback)
  - registrar_conteo DESCUADRE: physical != expected → SEGUNDO_CONTEO
  - cancelar_conteo: session → CANCELADO
  - generar_auditoria_por_excepcion: creates EXCEPCION_PICKING session
  - generar_auditoria_por_excepcion no-dup: returns existing active session

Siesa integration is NOT tested here (covered by test_siesa_contracts.py).
connekta is in modo_simulacion (consultar_existencia_siesa returns None),
so registrar_conteo falls back to UbicacionProducto.cantidad from WMS.
"""
import pytest
from unittest.mock import patch
from datetime import datetime


# ---------------------------------------------------------------------------
# Fixtures locales
# ---------------------------------------------------------------------------

@pytest.fixture
def tarea_picking(db, almacen, producto, ub_picking, usuario):
    """TareaPicking minima para tests de auditoria por excepcion."""
    from app.models.picking import TareaPicking
    t = TareaPicking(
        codigo='PICK-TEST-001',
        producto_id=producto.id,
        cantidad_solicitada=10,
        ubicacion_id=ub_picking.id,
        almacen_id=almacen.id,
        operario_id=usuario.id,
        estado='BLOQUEADO',
    )
    db.session.add(t)
    db.session.commit()
    return t


@pytest.fixture
def sesion_pendiente(db, almacen, producto, ub_picking, inv_picking):
    """SesionConteo PENDIENTE lista para registrar_conteo."""
    from app.models.conteo import SesionConteo
    s = SesionConteo(
        codigo='CC-TEST-PEND',
        tipo='MANUAL',
        clasificacion_abc='C',
        ubicacion_id=ub_picking.id,
        almacen_id=almacen.id,
        producto_id=producto.id,
        producto_codigo_siesa=producto.codigo_siesa,
        maneja_lote=False,
        estado='PENDIENTE',
    )
    db.session.add(s)
    db.session.commit()
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCrearConteoManual:

    def test_crear_conteo_manual(self, db, almacen, producto, ub_picking, inv_picking):
        """crear_conteo_manual creates one SesionConteo per ubicacion with stock."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo

        result = ConteoService.crear_conteo_manual(almacen.id, producto.codigo)

        assert result['tareas_creadas'] == 1
        assert result['omitidas_ya_activas'] == 0
        assert result['producto'] == producto.codigo.upper()
        assert len(result['codigos']) == 1

        sesion = SesionConteo.query.filter_by(codigo=result['codigos'][0]).first()
        assert sesion is not None
        assert sesion.tipo == 'MANUAL'
        assert sesion.estado == 'PENDIENTE'
        assert sesion.ubicacion_id == ub_picking.id
        assert sesion.almacen_id == almacen.id
        assert sesion.producto_id == producto.id
        assert sesion.producto_codigo_siesa == producto.codigo_siesa

    def test_crear_conteo_no_duplica(self, db, almacen, producto, ub_picking, inv_picking):
        """If an active session already exists for the same product+ubicacion, skip it."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo

        # Create first session
        first = ConteoService.crear_conteo_manual(almacen.id, producto.codigo)
        assert first['tareas_creadas'] == 1

        # Second call must skip (active session exists)
        second = ConteoService.crear_conteo_manual(almacen.id, producto.codigo)
        assert second['tareas_creadas'] == 0
        assert second['omitidas_ya_activas'] == 1

        # Only one session in DB
        total = SesionConteo.query.filter_by(
            producto_id=producto.id,
            ubicacion_id=ub_picking.id,
        ).count()
        assert total == 1


class TestRegistrarConteo:

    def test_registrar_conteo_match(
        self, db, almacen, producto, ub_picking, inv_picking, usuario, sesion_pendiente,
    ):
        """Physical count == WMS stock (30) → MATCH, diferencia 0."""
        from app.services.conteo_service import ConteoService

        # inv_picking has cantidad=30; Siesa is in simulation mode so it falls
        # back to WMS. Counting 30 must produce a MATCH.
        result = ConteoService.registrar_conteo(
            sesion_id=sesion_pendiente.id,
            operario_id=usuario.id,
            cantidad_fisica=30,
        )

        assert result['resultado'] == 'MATCH'
        db.session.refresh(sesion_pendiente)
        assert sesion_pendiente.estado == 'MATCH'
        assert sesion_pendiente.diferencia == 0
        assert sesion_pendiente.cantidad_fisica == 30
        assert sesion_pendiente.existencia_siesa == 30.0
        assert sesion_pendiente.fecha_cierre is not None

    def test_registrar_conteo_descuadre(
        self, db, almacen, producto, ub_picking, inv_picking, usuario, sesion_pendiente,
    ):
        """Physical count != WMS stock → SEGUNDO_CONTEO generated."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo

        # Count 25 but WMS says 30 → difference of -5
        result = ConteoService.registrar_conteo(
            sesion_id=sesion_pendiente.id,
            operario_id=usuario.id,
            cantidad_fisica=25,
        )

        assert result['resultado'] == 'SEGUNDO_CONTEO'
        assert 'segundo_conteo_id' in result

        db.session.refresh(sesion_pendiente)
        assert sesion_pendiente.estado == 'SEGUNDO_CONTEO'
        assert sesion_pendiente.diferencia == -5

        # A CC2 session must have been created
        cc2 = SesionConteo.query.get(result['segundo_conteo_id'])
        assert cc2 is not None
        assert cc2.es_segundo_conteo is True
        assert cc2.sesion_origen_id == sesion_pendiente.id
        assert cc2.estado == 'PENDIENTE'
        assert cc2.ubicacion_id == ub_picking.id
        assert cc2.producto_id == producto.id

    def test_registrar_conteo_match_with_mocked_siesa(
        self, db, almacen, producto, ub_picking, inv_picking, usuario, sesion_pendiente,
    ):
        """When consultar_existencia_siesa returns a known value, MATCH uses that value."""
        from app.services.conteo_service import ConteoService

        with patch.object(ConteoService, 'consultar_existencia_siesa', return_value=50.0):
            result = ConteoService.registrar_conteo(
                sesion_id=sesion_pendiente.id,
                operario_id=usuario.id,
                cantidad_fisica=50,
            )

        assert result['resultado'] == 'MATCH'
        db.session.refresh(sesion_pendiente)
        assert sesion_pendiente.existencia_siesa == 50.0

    def test_registrar_conteo_descuadre_with_mocked_siesa(
        self, db, almacen, producto, ub_picking, inv_picking, usuario, sesion_pendiente,
    ):
        """When Siesa says 100 but operator counts 90 → SEGUNDO_CONTEO."""
        from app.services.conteo_service import ConteoService

        with patch.object(ConteoService, 'consultar_existencia_siesa', return_value=100.0):
            result = ConteoService.registrar_conteo(
                sesion_id=sesion_pendiente.id,
                operario_id=usuario.id,
                cantidad_fisica=90,
            )

        assert result['resultado'] == 'SEGUNDO_CONTEO'
        db.session.refresh(sesion_pendiente)
        assert sesion_pendiente.diferencia == -10
        assert sesion_pendiente.existencia_siesa == 100.0


class TestCancelarConteo:

    def test_cancelar_conteo(self, db, almacen, producto, ub_picking, sesion_pendiente, usuario_admin):
        """Cancelling a PENDIENTE session sets CANCELADO + motivo."""
        from app.models.conteo import EstadoConteo

        sesion_pendiente.estado = EstadoConteo.PENDIENTE
        db.session.commit()

        # Reproduce the cancellation logic from the route (no service method exists)
        sesion_pendiente.estado = EstadoConteo.CANCELADO
        sesion_pendiente.fecha_cierre = datetime.utcnow()
        sesion_pendiente.motivo_edicion = 'CANCELADO: Test de cancelacion'
        sesion_pendiente.editado_por = usuario_admin.id
        sesion_pendiente.editado_en = datetime.utcnow()
        db.session.commit()

        db.session.refresh(sesion_pendiente)
        assert sesion_pendiente.estado == 'CANCELADO'
        assert sesion_pendiente.fecha_cierre is not None
        assert 'Test de cancelacion' in sesion_pendiente.motivo_edicion
        assert sesion_pendiente.editado_por == usuario_admin.id

    def test_cancelar_conteo_con_hijo(
        self, db, almacen, producto, ub_picking, inv_picking, usuario, usuario_admin,
    ):
        """Cancelling a parent also cancels its child CC2 session."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo, EstadoConteo

        # Create parent via manual conteo
        result = ConteoService.crear_conteo_manual(almacen.id, producto.codigo)
        padre = SesionConteo.query.filter_by(codigo=result['codigos'][0]).first()

        # Register a mismatch to create CC2
        padre_result = ConteoService.registrar_conteo(
            sesion_id=padre.id,
            operario_id=usuario.id,
            cantidad_fisica=25,
        )
        assert padre_result['resultado'] == 'SEGUNDO_CONTEO'
        cc2 = SesionConteo.query.get(padre_result['segundo_conteo_id'])

        # Cancel parent — child should also get cancelled
        estados_cancelables = [
            EstadoConteo.PENDIENTE, EstadoConteo.EN_PROCESO,
            EstadoConteo.SEGUNDO_CONTEO, EstadoConteo.DESCUADRE,
        ]

        db.session.refresh(padre)
        assert padre.estado in estados_cancelables

        padre.estado = EstadoConteo.CANCELADO
        padre.fecha_cierre = datetime.utcnow()
        padre.motivo_edicion = 'CANCELADO: Test padre-hijo'
        padre.editado_por = usuario_admin.id

        if padre.hijo_conteo and padre.hijo_conteo.estado in estados_cancelables:
            padre.hijo_conteo.estado = EstadoConteo.CANCELADO
            padre.hijo_conteo.fecha_cierre = datetime.utcnow()
            padre.hijo_conteo.motivo_edicion = 'CANCELADO (padre): Test padre-hijo'
            padre.hijo_conteo.editado_por = usuario_admin.id

        db.session.commit()

        db.session.refresh(cc2)
        assert cc2.estado == 'CANCELADO'
        assert 'padre' in cc2.motivo_edicion.lower()


class TestGenerarAuditoriaPorExcepcion:

    def test_generar_auditoria(
        self, db, almacen, producto, ub_picking, tarea_picking,
    ):
        """Creates an EXCEPCION_PICKING session from a picking exception."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo

        sesion = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tarea_picking.id,
            ubicacion_id=ub_picking.id,
            producto_id=producto.id,
            almacen_id=almacen.id,
        )

        assert sesion is not None
        assert sesion.id is not None
        assert sesion.tipo == 'EXCEPCION_PICKING'
        assert sesion.estado == 'PENDIENTE'
        assert sesion.ubicacion_id == ub_picking.id
        assert sesion.producto_id == producto.id
        assert sesion.almacen_id == almacen.id
        assert sesion.tarea_picking_id == tarea_picking.id
        assert sesion.producto_codigo_siesa == producto.codigo_siesa
        assert sesion.codigo.startswith('AUD-')

    def test_generar_auditoria_no_duplica(
        self, db, almacen, producto, ub_picking, tarea_picking,
    ):
        """If an active audit session exists for the same product+ubicacion, return existing."""
        from app.services.conteo_service import ConteoService

        first = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tarea_picking.id,
            ubicacion_id=ub_picking.id,
            producto_id=producto.id,
            almacen_id=almacen.id,
        )

        second = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tarea_picking.id,
            ubicacion_id=ub_picking.id,
            producto_id=producto.id,
            almacen_id=almacen.id,
        )

        # Same session returned — no duplicate
        assert second.id == first.id
        assert second.codigo == first.codigo
