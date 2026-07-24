"""
Tests de PickingService — el flujo más crítico del CDI.
Si picking falla, nada sale del almacén.
"""
import pytest
from app.services.picking_service import PickingService


@pytest.fixture
def picking_setup(db, almacen, producto, ub_picking, inv_picking, usuario):
    """Setup completo para tests de picking."""
    return {
        'almacen': almacen,
        'producto': producto,
        'ubicacion': ub_picking,
        'inventario': inv_picking,
        'usuario': usuario,
    }


class TestCrearTareas:

    def test_crear_tareas_genera_picking(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id,
            cantidad=5,
            almacen_id=s['almacen'].id,
            referencia_documento='PED-001',
            tipo_documento='PEDIDO',
        )
        assert len(tareas) >= 1
        assert tareas[0].cantidad_solicitada == 5
        assert tareas[0].estado == 'PENDIENTE'
        assert tareas[0].referencia_documento == 'PED-001'

    def test_crear_tareas_reserva_stock(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        from app.models.inventario import UbicacionProducto
        antes = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado

        PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )

        despues = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado
        assert despues == antes + 5

    def test_crear_tareas_stock_insuficiente(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        with pytest.raises(ValueError, match='[Ss]tock insuficiente'):
            PickingService.crear_tareas(
                producto_id=s['producto'].id,
                cantidad=99999,  # más de lo disponible
                almacen_id=s['almacen'].id,
            )


class TestFefoPrefiereUbicacionReal:
    """
    Caso real PD1343 (2026-07-24): SIESA-GENERAL (bucket sin ubicación física
    del sync de Siesa) con stock viejo siempre ganaba contra un hueco de Layout
    recién asignado, porque FEFO desempataba solo por fecha_ingreso — y
    GENERAL, al llevar meses cargado, es casi siempre más "antiguo" que
    cualquier hueco recién organizado. calcular_fefo() ahora prioriza
    ubicaciones reales (PICKING/RESERVA/AVERIAS) sobre GENERAL sin importar
    la fecha.
    """

    def test_prefiere_hueco_real_aunque_general_sea_mas_antiguo_y_tenga_mas_stock(
        self, app, db, almacen, producto, ub_picking, ub_general,
    ):
        from datetime import datetime, timedelta
        from app.models.inventario import UbicacionProducto
        from app.services.picking_service import PickingService

        db.session.add(UbicacionProducto(
            ubicacion_id=ub_general.id, producto_id=producto.id,
            cantidad=4000, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow() - timedelta(days=120),  # viejo — caso PD1343
        ))
        db.session.add(UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            cantidad=10, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow(),  # recién asignado hoy
        ))
        db.session.commit()

        fefo = PickingService.calcular_fefo(producto.id, 3, almacen.id)

        assert fefo['completo'] is True
        assert len(fefo['asignaciones']) == 1
        assert fefo['asignaciones'][0]['ubicacion_id'] == ub_picking.id

    def test_general_sigue_como_respaldo_si_ubicacion_real_no_alcanza(
        self, app, db, almacen, producto, ub_picking, ub_general,
    ):
        from datetime import datetime, timedelta
        from app.models.inventario import UbicacionProducto
        from app.services.picking_service import PickingService

        db.session.add(UbicacionProducto(
            ubicacion_id=ub_general.id, producto_id=producto.id,
            cantidad=4000, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow() - timedelta(days=120),
        ))
        db.session.add(UbicacionProducto(
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            cantidad=10, reservado=0, bloqueado=0,
            fecha_ingreso=datetime.utcnow(),
        ))
        db.session.commit()

        # Pide más de lo que tiene el hueco real (10) — debe completar con GENERAL
        fefo = PickingService.calcular_fefo(producto.id, 15, almacen.id)

        assert fefo['completo'] is True
        assert len(fefo['asignaciones']) == 2
        assert fefo['asignaciones'][0]['ubicacion_id'] == ub_picking.id
        assert fefo['asignaciones'][0]['cantidad'] == 10
        assert fefo['asignaciones'][1]['ubicacion_id'] == ub_general.id
        assert fefo['asignaciones'][1]['cantidad'] == 5


class TestConfirmarPicking:

    def test_confirmar_decrementa_stock(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        from app.models.inventario import UbicacionProducto

        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        stock_antes = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad

        PickingService.confirmar_picking(
            tarea_id=tarea.id,
            cantidad_recogida=5,
            usuario_id=s['usuario'].id,
        )

        stock_despues = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().cantidad
        assert stock_despues == stock_antes - 5

    def test_confirmar_tarea_completada(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.confirmar_picking(tarea.id, 5, s['usuario'].id)
        db.session.refresh(tarea)
        assert tarea.estado == 'COMPLETADO'
        assert tarea.cantidad_recogida == 5

    def test_confirmar_doble_no_decrementa_doble(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.confirmar_picking(tarea.id, 5, s['usuario'].id)
        with pytest.raises(ValueError, match='[Yy]a completada'):
            PickingService.confirmar_picking(tarea.id, 5, s['usuario'].id)

    def test_confirmar_cantidad_parcial(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=10,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.confirmar_picking(tarea.id, 7, s['usuario'].id)
        db.session.refresh(tarea)
        assert tarea.cantidad_recogida == 7
        assert tarea.estado == 'COMPLETADO'

    def test_confirmar_exceso_rechaza(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        with pytest.raises(ValueError, match='[Ss]upera'):
            PickingService.confirmar_picking(tarea.id, 999, s['usuario'].id)


class TestIniciarCancelarReabrir:

    def test_iniciar_picking(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        PickingService.iniciar_picking(tarea.id, s['usuario'].id)
        db.session.refresh(tarea)
        assert tarea.estado == 'EN_PROCESO'
        assert tarea.operario_id == s['usuario'].id

    def test_cancelar_picking_libera_reserva(self, app, db, picking_setup):
        s = picking_setup
        from app.services.picking_service import PickingService
        from app.models.inventario import UbicacionProducto

        tareas = PickingService.crear_tareas(
            producto_id=s['producto'].id, cantidad=5,
            almacen_id=s['almacen'].id,
        )
        tarea = tareas[0]

        reservado_antes = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado

        PickingService.cancelar_picking(tarea.id, motivo='Test cancel')

        reservado_despues = UbicacionProducto.query.filter_by(
            ubicacion_id=s['ubicacion'].id, producto_id=s['producto'].id
        ).first().reservado

        db.session.refresh(tarea)
        assert tarea.estado == 'CANCELADO'
        assert reservado_despues == reservado_antes - 5


class TestSiguienteTareaPara:
    """
    Dispensador de tareas al operario — aplica igual a pedidos y traslados
    (comparten esta cola vía referencia_documento). Orden: prioridad estricta
    -> mismo documento en curso -> ruta física (pasillo/fila/cuerpo/nivel/
    hueco) -> fecha_creacion.
    """

    @staticmethod
    def _ub(almacen_id, codigo, pasillo=None, fila=None, cuerpo=None, nivel=None, hueco=None):
        from app.models.ubicacion import Ubicacion
        from app.extensions import db as _db
        u = Ubicacion(
            codigo=codigo, almacen_id=almacen_id, tipo_zona='PICKING', tipo='estanteria',
            pasillo=pasillo, fila=fila, cuerpo=cuerpo, nivel=nivel, hueco=hueco,
            origen='MANUAL', activo=True,
        )
        _db.session.add(u)
        _db.session.flush()
        return u

    @staticmethod
    def _tarea(almacen_id, producto_id, ubicacion_id, codigo, prioridad=1,
              referencia_documento=None, estado='PENDIENTE', operario_id=None):
        from app.models.picking import TareaPicking
        from app.extensions import db as _db
        t = TareaPicking(
            codigo=codigo, producto_id=producto_id, cantidad_solicitada=1,
            ubicacion_id=ubicacion_id, almacen_id=almacen_id, estado=estado,
            prioridad=prioridad, referencia_documento=referencia_documento,
            operario_id=operario_id,
        )
        _db.session.add(t)
        _db.session.flush()
        return t

    def test_prefiere_ubicacion_fisicamente_mas_cercana(self, app, db, almacen, producto, usuario):
        ub_lejos = self._ub(almacen.id, 'PIK-B1-C01-E01-H01', pasillo='B', fila=1, cuerpo=1, nivel=1, hueco=1)
        ub_cerca = self._ub(almacen.id, 'PIK-A1-C01-E01-H01', pasillo='A', fila=1, cuerpo=1, nivel=1, hueco=1)
        self._tarea(almacen.id, producto.id, ub_lejos.id, 'T-LEJOS')
        self._tarea(almacen.id, producto.id, ub_cerca.id, 'T-CERCA')
        db.session.commit()

        siguiente = PickingService.siguiente_tarea_para(usuario.id)
        assert siguiente.codigo == 'T-CERCA'

    def test_pasillo_z_antes_que_aa_pese_al_orden_alfabetico(self, app, db, almacen, producto, usuario):
        ub_aa = self._ub(almacen.id, 'PIK-AA1-C01-E01-H01', pasillo='AA', fila=1, cuerpo=1, nivel=1, hueco=1)
        ub_z = self._ub(almacen.id, 'PIK-Z1-C01-E01-H01', pasillo='Z', fila=1, cuerpo=1, nivel=1, hueco=1)
        self._tarea(almacen.id, producto.id, ub_aa.id, 'T-AA')
        self._tarea(almacen.id, producto.id, ub_z.id, 'T-Z')
        db.session.commit()

        siguiente = PickingService.siguiente_tarea_para(usuario.id)
        assert siguiente.codigo == 'T-Z'  # pasillo 26, físicamente antes que AA (27)

    def test_prefiere_terminar_documento_en_curso_sobre_ubicacion_mas_cercana(self, app, db, almacen, producto, usuario):
        ub_actual = self._ub(almacen.id, 'PIK-A1-C01-E01-H01', pasillo='A', fila=1, cuerpo=1, nivel=1, hueco=1)
        ub_lejos_mismo_doc = self._ub(almacen.id, 'PIK-Z1-C01-E01-H01', pasillo='Z', fila=1, cuerpo=1, nivel=1, hueco=1)
        ub_cerca_otro_doc = self._ub(almacen.id, 'PIK-A1-C01-E01-H02', pasillo='A', fila=1, cuerpo=1, nivel=1, hueco=2)

        # El operario ya completó una tarea de PD-100 — sigue trabajando ese documento.
        self._tarea(almacen.id, producto.id, ub_actual.id, 'T-YA-HECHA',
                    referencia_documento='PD-100', estado='COMPLETADO', operario_id=usuario.id)
        db.session.commit()

        self._tarea(almacen.id, producto.id, ub_lejos_mismo_doc.id, 'T-MISMO-DOC', referencia_documento='PD-100')
        self._tarea(almacen.id, producto.id, ub_cerca_otro_doc.id, 'T-OTRO-DOC', referencia_documento='PD-200')
        db.session.commit()

        siguiente = PickingService.siguiente_tarea_para(usuario.id)
        # Aunque T-OTRO-DOC está más cerca, se prioriza terminar PD-100 primero.
        assert siguiente.codigo == 'T-MISMO-DOC'

    def test_prioridad_mas_alta_gana_aunque_este_trabajando_otro_documento(self, app, db, almacen, producto, usuario):
        ub_actual = self._ub(almacen.id, 'PIK-A1-C01-E01-H01', pasillo='A', fila=1, cuerpo=1, nivel=1, hueco=1)
        ub_mismo_doc = self._ub(almacen.id, 'PIK-A1-C01-E01-H02', pasillo='A', fila=1, cuerpo=1, nivel=1, hueco=2)
        ub_urgente = self._ub(almacen.id, 'PIK-Z1-C01-E01-H01', pasillo='Z', fila=1, cuerpo=1, nivel=1, hueco=1)

        self._tarea(almacen.id, producto.id, ub_actual.id, 'T-YA-HECHA',
                    referencia_documento='PD-100', estado='COMPLETADO', operario_id=usuario.id)
        db.session.commit()

        self._tarea(almacen.id, producto.id, ub_mismo_doc.id, 'T-MISMO-DOC-P1',
                    referencia_documento='PD-100', prioridad=1)
        self._tarea(almacen.id, producto.id, ub_urgente.id, 'T-URGENTE-P2',
                    referencia_documento='PD-999', prioridad=2)
        db.session.commit()

        siguiente = PickingService.siguiente_tarea_para(usuario.id)
        # Prioridad 2 gana pese a que PD-100 (prioridad 1) es el documento en curso.
        assert siguiente.codigo == 'T-URGENTE-P2'

    def test_sin_tareas_pendientes_retorna_none(self, app, db, almacen, usuario):
        assert PickingService.siguiente_tarea_para(usuario.id) is None

    def test_ubicaciones_sin_direccion_fisica_van_al_final(self, app, db, almacen, producto, usuario):
        ub_general = self._ub(almacen.id, 'SIESA-GENERAL')  # sin pasillo/fila/cuerpo/nivel/hueco
        ub_real = self._ub(almacen.id, 'PIK-Z1-C01-E01-H01', pasillo='Z', fila=1, cuerpo=1, nivel=1, hueco=1)
        self._tarea(almacen.id, producto.id, ub_general.id, 'T-GENERAL')
        self._tarea(almacen.id, producto.id, ub_real.id, 'T-REAL')
        db.session.commit()

        siguiente = PickingService.siguiente_tarea_para(usuario.id)
        assert siguiente.codigo == 'T-REAL'
