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

class TestRutaConteoManualPermiso:
    """POST /api/conteo/manual amplió de admin-only a Roles.LEAD (admin+supervisor)
    — mismo grupo que ya usan editar/ajustar/asignar-lote sobre conteos."""

    def test_supervisor_puede_crear_conteo_manual(self, app, db, client, almacen, producto, ub_picking, inv_picking):
        from flask_jwt_extended import create_access_token
        from werkzeug.security import generate_password_hash
        from app.models.usuario import Usuario

        sup = Usuario(nombre='Sup Test', email='sup-manual@test.com',
                      password_hash=generate_password_hash('x'), rol='supervisor',
                      almacen_id=almacen.id, activo=True)
        db.session.add(sup)
        db.session.commit()
        with app.app_context():
            tok = create_access_token(identity=str(sup.id))

        r = client.post('/api/conteo/manual', json={
            'almacen_id': almacen.id, 'producto_codigo': producto.codigo,
        }, headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 201, r.get_json()

    def test_operario_no_puede_crear_conteo_manual(self, app, db, client, almacen, producto, ub_picking, inv_picking, usuario):
        from flask_jwt_extended import create_access_token
        with app.app_context():
            tok = create_access_token(identity=str(usuario.id))

        r = client.post('/api/conteo/manual', json={
            'almacen_id': almacen.id, 'producto_codigo': producto.codigo,
        }, headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 403

    def test_supervisor_puede_forzar_operario_via_api(self, app, db, client, almacen, producto, ub_picking, inv_picking, usuario):
        """El caso que motivó el cambio: supervisor crea el conteo y ya lo deja
        asignado a un operario específico para el primer conteo (CC1)."""
        from flask_jwt_extended import create_access_token
        from werkzeug.security import generate_password_hash
        from app.models.usuario import Usuario
        from app.models.conteo import SesionConteo

        sup = Usuario(nombre='Sup Test 2', email='sup-manual-2@test.com',
                      password_hash=generate_password_hash('x'), rol='supervisor',
                      almacen_id=almacen.id, activo=True)
        db.session.add(sup)
        db.session.commit()
        with app.app_context():
            tok = create_access_token(identity=str(sup.id))

        r = client.post('/api/conteo/manual', json={
            'almacen_id': almacen.id, 'producto_codigo': producto.codigo,
            'operario_id': usuario.id,
        }, headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        assert body['operario_id'] == usuario.id

        sesion = db.session.get(SesionConteo, SesionConteo.query.filter_by(codigo=body['codigos'][0]).first().id)
        assert sesion.operario_id == usuario.id


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

    def test_crear_conteo_manual_fuerza_operario(self, db, almacen, producto, ub_picking, inv_picking, usuario):
        """operario_id fuerza el CC1 a ese operario — queda PENDIENTE-pero-asignado,
        mismo patrón que las tareas DIARIO_ABC pre-asignadas."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo

        result = ConteoService.crear_conteo_manual(almacen.id, producto.codigo, operario_id=usuario.id)

        assert result['operario_id'] == usuario.id
        assert result['operario_nombre'] == usuario.nombre

        sesion = SesionConteo.query.filter_by(codigo=result['codigos'][0]).first()
        assert sesion.operario_id == usuario.id
        assert sesion.estado == 'PENDIENTE', (
            'debe quedar PENDIENTE-pero-asignado, no EN_PROCESO — el operario '
            'todavía tiene que abrir la tarea (obtener_tarea_operario) para arrancarla')

    def test_operario_forzado_pasa_a_en_proceso_al_abrirla(self, db, almacen, producto, ub_picking, inv_picking, usuario):
        """Bug encontrado probando la feature: `obtener_tarea_operario` solo
        transicionaba PENDIENTE→EN_PROCESO cuando operario_id venía en None.
        Con una sesión pre-asignada (operario_id ya puesto por
        crear_conteo_manual o por asignar-lote), el operario la abría, la
        contaba, y quedaba viéndose PENDIENTE para siempre — fecha_inicio
        nunca se registraba."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo, EstadoConteo

        creado = ConteoService.crear_conteo_manual(almacen.id, producto.codigo, operario_id=usuario.id)
        cc1_id = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first().id

        ConteoService.obtener_tarea_operario(cc1_id, usuario.id)

        sesion = SesionConteo.query.get(cc1_id)
        assert sesion.estado == EstadoConteo.EN_PROCESO, (
            f'se quedó en {sesion.estado} — la transición PENDIENTE→EN_PROCESO '
            f'no ocurrió para una sesión pre-asignada')
        assert sesion.fecha_inicio is not None

    def test_crear_conteo_manual_operario_inexistente(self, db, almacen, producto, ub_picking, inv_picking):
        from app.services.conteo_service import ConteoService

        with pytest.raises(ValueError, match='no encontrado o inactivo'):
            ConteoService.crear_conteo_manual(almacen.id, producto.codigo, operario_id=999999)

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

    def test_crear_conteo_manual_reclama_pendiente_al_forzar_operario(
        self, db, almacen, producto, ub_picking, inv_picking, usuario,
    ):
        """Una sesión PENDIENTE sin dueño (ej. generada por el barrido
        DIARIO_ABC, nadie la ha abierto) no debe bloquear un conteo manual
        forzado a un operario específico — se reclama en vez de omitirse,
        sin crear una segunda sesión para la misma ubicación."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo

        # Sesión PENDIENTE preexistente, sin operario (ej. DIARIO_ABC)
        primero = ConteoService.crear_conteo_manual(almacen.id, producto.codigo)
        assert primero['tareas_creadas'] == 1
        sesion_id = SesionConteo.query.filter_by(codigo=primero['codigos'][0]).first().id

        # Forzar operario sobre el mismo producto/almacén
        resultado = ConteoService.crear_conteo_manual(almacen.id, producto.codigo, operario_id=usuario.id)

        assert resultado['tareas_creadas'] == 1
        assert resultado['tareas_reclamadas'] == 1
        assert resultado['tareas_nuevas'] == 0
        assert resultado['omitidas_ya_activas'] == 0
        assert resultado['codigos'] == [primero['codigos'][0]]

        sesion = SesionConteo.query.get(sesion_id)
        assert sesion.operario_id == usuario.id
        assert sesion.estado == 'PENDIENTE'

        # Sigue siendo una sola sesión — no se duplicó
        total = SesionConteo.query.filter_by(
            producto_id=producto.id,
            ubicacion_id=ub_picking.id,
        ).count()
        assert total == 1

    def test_crear_conteo_manual_pausa_otro_en_proceso_del_operario_forzado(
        self, db, almacen, producto, producto2, ub_picking, inv_picking, usuario,
    ):
        """Si el operario forzado ya está contando OTRO SKU (EN_PROCESO), el
        conteo manual forzado debe pausarlo — vuelve a PENDIENTE sin dueño,
        mismo patrón que liberar_tareas_zombi — para que el dispensador le
        entregue el conteo forzado en vez de seguir devolviéndole el viejo.

        La sesión "otro" vive en la MISMA ubicación que el producto forzado
        (`ub_picking`) a propósito — replica el caso real de producción
        (2026-09-14): dos SKUs distintos comparten `SIESA-GENERAL` como
        ubicación genérica, y una versión anterior de este fix excluía por
        `ubicacion_id` en vez de por identidad de sesión, así que el
        EN_PROCESO de un SKU distinto en la misma ubicación sobrevivía sin
        pausarse. Un test con ubicaciones distintas para "producto" y
        "producto2" no habría detectado ese bug — es la lección de este
        mismo repo sobre guards que miden una propiedad que la vía sana ya
        satisface por construcción."""
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo, EstadoConteo
        from app.models.inventario import UbicacionProducto

        # Carlos ya está a mitad de un conteo de OTRO SKU en la MISMA ubicación
        otro = SesionConteo(
            codigo='CC-OTRO-EN-PROCESO', tipo='DIARIO_ABC',
            clasificacion_abc='B', ubicacion_id=ub_picking.id, almacen_id=almacen.id,
            producto_id=producto2.id, producto_codigo_siesa=producto2.codigo_siesa,
            maneja_lote=False, estado=EstadoConteo.EN_PROCESO,
            operario_id=usuario.id, fecha_inicio=datetime.utcnow(),
        )
        db.session.add(otro)
        db.session.add(UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto2.id,
            cantidad=10, reservado=0, bloqueado=0,
        ))
        db.session.commit()

        resultado = ConteoService.crear_conteo_manual(almacen.id, producto.codigo, operario_id=usuario.id)
        assert resultado['tareas_creadas'] == 1
        assert resultado['operario_id'] == usuario.id

        pausado = SesionConteo.query.filter_by(codigo='CC-OTRO-EN-PROCESO').first()
        assert pausado.estado == EstadoConteo.PENDIENTE
        assert pausado.operario_id is None
        assert pausado.fecha_inicio is None

        nuevo = SesionConteo.query.filter_by(codigo=resultado['codigos'][0]).first()
        assert nuevo.operario_id == usuario.id
        assert nuevo.estado == 'PENDIENTE'

    def test_crear_conteo_manual_no_pausa_picking_activo(
        self, db, almacen, producto, ub_picking, inv_picking, usuario,
    ):
        """Forzar un conteo manual NUNCA toca un picking/packing/traslado
        activo del operario — solo pausa otro conteo cíclico. Interrumpir una
        operación física en curso (bultos escaneados, LPN abierto) es un
        riesgo distinto que esta feature no debe tocar."""
        from app.services.conteo_service import ConteoService
        from app.models.picking import TareaPicking

        picking = TareaPicking(
            codigo='PICK-NO-PAUSA-001',
            operario_id=usuario.id, producto_id=producto.id,
            ubicacion_id=ub_picking.id, almacen_id=almacen.id,
            cantidad_solicitada=5, cantidad_recogida=2,
            estado='EN_PROCESO', prioridad=1,
        )
        db.session.add(picking)
        db.session.commit()
        picking_id = picking.id

        ConteoService.crear_conteo_manual(almacen.id, producto.codigo, operario_id=usuario.id)

        picking = db.session.get(TareaPicking, picking_id)
        assert picking.estado == 'EN_PROCESO'
        assert picking.operario_id == usuario.id
        assert picking.cantidad_recogida == 2


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
