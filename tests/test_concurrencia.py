"""
Tests de concurrencia y guards de idempotencia del WMS.

SQLite no soporta `skip_locked` ni partial unique indexes, por lo que
estos tests verifican los guards a nivel de aplicacion:
  - confirmar_picking doble sobre la misma tarea
  - siesa_triggered como barrera de cierre packing
  - idempotency_key unico en MovimientoInventario
  - generar_auditoria_por_excepcion evita duplicados sin partial index
"""
import pytest
from sqlalchemy.exc import IntegrityError


# ═══════════════════════════════════════════════════════════════════
# 1. Doble confirmacion de picking
# ═══════════════════════════════════════════════════════════════════

class TestDobleConfirmacionPicking:

    def test_doble_confirmacion_picking_bloqueada(self, app, db, almacen,
                                                   producto, ub_picking,
                                                   inv_picking, usuario):
        """
        confirmar_picking dos veces sobre la misma tarea: la segunda debe
        lanzar ValueError('Tarea ya completada') porque el estado ya es
        COMPLETADO tras la primera confirmacion.
        """
        from app.services.picking_service import PickingService

        tareas = PickingService.crear_tareas(
            producto_id=producto.id,
            cantidad=5,
            almacen_id=almacen.id,
            referencia_documento='PED-CONC-001',
            tipo_documento='PEDIDO',
        )
        tarea = tareas[0]

        # Primera confirmacion — debe pasar limpio
        PickingService.confirmar_picking(
            tarea_id=tarea.id,
            cantidad_recogida=5,
            usuario_id=usuario.id,
        )

        # Segunda confirmacion — la tarea ya esta COMPLETADO
        with pytest.raises(ValueError, match='[Yy]a completada'):
            PickingService.confirmar_picking(
                tarea_id=tarea.id,
                cantidad_recogida=5,
                usuario_id=usuario.id,
            )


# ═══════════════════════════════════════════════════════════════════
# 2. siesa_triggered como barrera de cierre packing
# ═══════════════════════════════════════════════════════════════════

class TestDobleCierrePacking:

    def test_doble_cierre_packing_bloqueado(self, app, db, almacen,
                                             producto, usuario):
        """
        Si siesa_triggered=True en una TareaPacking, cerrar_packing debe
        rechazar la segunda invocacion con ValueError, sin importar el estado.
        Esto es el guard de idempotencia a nivel de aplicacion (linea 375 de
        packing_service.py).
        """
        from app.models.packing import TareaPacking, ItemPacking
        from app.services.packing_service import PackingService

        tarea = TareaPacking(
            codigo='PCK-CONC-001',
            numero_pedido_siesa='PED-999',
            tipo_documento='PEDIDO',
            almacen_id=almacen.id,
            empacador_id=usuario.id,
            estado='DESPACHADO',
            siesa_triggered=True,  # ya fue procesada por Siesa
        )
        db.session.add(tarea)
        db.session.commit()

        with pytest.raises(ValueError, match='[Ss]iesa ya proces'):
            PackingService.cerrar_packing(
                tarea_id=tarea.id,
                bultos_data=[{'tipo': 'Caja', 'cantidad': 1}],
            )

    def test_resetear_siesa_bloqueado_si_triggered(self, app, db, almacen,
                                                     producto, usuario):
        """
        resetear_siesa debe rechazar si siesa_triggered=True — no se
        puede retroceder una tarea que Siesa ya confirmo.
        """
        from app.models.packing import TareaPacking
        from app.services.packing_service import PackingService

        tarea = TareaPacking(
            codigo='PCK-CONC-002',
            numero_pedido_siesa='PED-888',
            tipo_documento='PEDIDO',
            almacen_id=almacen.id,
            estado='DESPACHADO',
            siesa_triggered=True,
        )
        db.session.add(tarea)
        db.session.commit()

        with pytest.raises(ValueError, match='[Ss]iesa ya proces'):
            PackingService.resetear_siesa(tarea_id=tarea.id)


# ═══════════════════════════════════════════════════════════════════
# 3. idempotency_key en MovimientoInventario
# ═══════════════════════════════════════════════════════════════════

class TestMovimientoIdempotencyKey:

    def test_movimiento_idempotency_key_duplicada(self, app, db, almacen,
                                                    producto, ub_picking,
                                                    usuario):
        """
        Insertar dos MovimientoInventario con la misma idempotency_key
        debe causar IntegrityError — la columna tiene unique=True.
        """
        from app.models.inventario import MovimientoInventario

        m1 = MovimientoInventario(
            producto_id=producto.id,
            ubicacion_id=ub_picking.id,
            almacen_id=almacen.id,
            tipo='SALIDA',
            cantidad=5,
            motivo='Test concurrencia 1',
            usuario_id=usuario.id,
            idempotency_key='PICK-999',
        )
        db.session.add(m1)
        db.session.commit()

        m2 = MovimientoInventario(
            producto_id=producto.id,
            ubicacion_id=ub_picking.id,
            almacen_id=almacen.id,
            tipo='SALIDA',
            cantidad=5,
            motivo='Test concurrencia 2 — duplicado',
            usuario_id=usuario.id,
            idempotency_key='PICK-999',  # misma key
        )
        db.session.add(m2)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ═══════════════════════════════════════════════════════════════════
# 4. Guard de duplicados en generar_auditoria_por_excepcion
# ═══════════════════════════════════════════════════════════════════

class TestConteoUniqueGuard:

    def test_conteo_auditoria_duplicada_retorna_existente(self, app, db,
                                                           almacen, producto,
                                                           ub_picking,
                                                           inv_picking,
                                                           usuario):
        """
        generar_auditoria_por_excepcion debe retornar la sesion existente
        si ya hay un conteo activo para el mismo (producto, ubicacion).
        Este guard a nivel de aplicacion suple al partial unique index
        de PostgreSQL que SQLite no soporta.
        """
        from app.services.picking_service import PickingService
        from app.services.conteo_service import ConteoService

        # Crear una tarea de picking para referenciar
        tareas = PickingService.crear_tareas(
            producto_id=producto.id,
            cantidad=5,
            almacen_id=almacen.id,
            referencia_documento='PED-AUD-001',
            tipo_documento='PEDIDO',
        )
        tarea = tareas[0]

        # Primera invocacion — crea la sesion
        sesion1 = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tarea.id,
            ubicacion_id=ub_picking.id,
            producto_id=producto.id,
            almacen_id=almacen.id,
        )
        db.session.commit()

        # Segunda invocacion — debe retornar la misma sesion, no crear otra
        sesion2 = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tarea.id,
            ubicacion_id=ub_picking.id,
            producto_id=producto.id,
            almacen_id=almacen.id,
        )

        assert sesion1.id == sesion2.id
        assert sesion1.codigo == sesion2.codigo

    def test_conteo_auditoria_permite_otra_ubicacion(self, app, db, almacen,
                                                      producto, ub_picking,
                                                      ub_general, inv_picking,
                                                      usuario):
        """
        Para un producto distinto o ubicacion distinta, se debe crear una
        sesion nueva — el guard solo bloquea el par (producto, ubicacion).
        """
        from app.models.inventario import UbicacionProducto
        from app.services.picking_service import PickingService
        from app.services.conteo_service import ConteoService

        # Inventario en ubicacion general para poder crear picking ahi
        inv_gen = UbicacionProducto(
            ubicacion_id=ub_general.id,
            producto_id=producto.id,
            cantidad=20, reservado=0, bloqueado=0,
        )
        db.session.add(inv_gen)
        db.session.commit()

        tareas1 = PickingService.crear_tareas(
            producto_id=producto.id,
            cantidad=5,
            almacen_id=almacen.id,
            referencia_documento='PED-AUD-002',
            tipo_documento='PEDIDO',
        )

        # Auditoria en ub_picking
        sesion1 = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tareas1[0].id,
            ubicacion_id=ub_picking.id,
            producto_id=producto.id,
            almacen_id=almacen.id,
        )
        db.session.commit()

        # Auditoria en ub_general — ubicacion diferente, debe crear nueva
        sesion2 = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tareas1[0].id,
            ubicacion_id=ub_general.id,
            producto_id=producto.id,
            almacen_id=almacen.id,
        )

        assert sesion1.id != sesion2.id
