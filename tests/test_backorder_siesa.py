"""
Backorder Siesa — líneas que Siesa cancela (f430_ind_backorder="despachar
disponible, cancelar el resto") ANTES de que el WMS las pickee.

Caso real que motivó esto (2026-08-24, verificado en vivo contra Siesa QA):
3 de 4 pedidos con la misma referencia perdieron esa línea en la factura
(cantidad_remisionada=0) mientras el WMS local creía tener stock suficiente
para los 4. `iniciar_despacho` ahora consulta los compromisos reales de
Siesa ANTES de dejar la tarea de picking abierta — si Siesa no la
comprometió, la tarea se crea igual (reserva local vía FEFO) pero se
bloquea de una vez, por el mismo camino que "el operario no lo encontró"
(`auditar_tarea(resultado='DISCREPANCIA_SIESA')`, que ya existía para esto).

Si este filtro se rompe o se borra, estos tests tienen que fallar — cada uno
construye el escenario que el filtro existe para atrapar.
"""
import pytest
from unittest.mock import patch


class TestReferenciasComprometidasPorSiesa:

    def test_devuelve_set_de_referencias_comprometidas(self, app, db):
        from app.services import backorder_service

        with patch(
            'app.services.despacho_parcial_service.DespachoParialService.obtener_compromisos',
            return_value=[
                {'f120_referencia': 'PAPELSP9218', 'f405_cant_por_remisionar_base': 4},
                {'f120_referencia': 'PAPELSP9830', 'f405_cant_por_remisionar_base': 4},
            ],
        ):
            resultado = backorder_service.referencias_comprometidas_por_siesa('PD', '1430')

        assert resultado == {'PAPELSP9218', 'PAPELSP9830'}
        # La referencia que Siesa canceló (BELLESB1382) simplemente no aparece —
        # get_compromisos_pedido ya la filtra por f405_cant_por_remisionar_base > 0.
        assert 'BELLESB1382' not in resultado

    def test_fallo_de_red_devuelve_none_no_set_vacio(self, app, db):
        """None = "no sé", no "ninguna está comprometida". Confundirlos bloquearía
        picking legítimo por un timeout de Siesa — el mismo error de
        CompromisosNoDisponibles que ya costó una vez (ver connekta_gateway.py)."""
        from app.services import backorder_service
        from app.services.connekta_gateway import CompromisosNoDisponibles

        with patch(
            'app.services.despacho_parcial_service.DespachoParialService.obtener_compromisos',
            side_effect=CompromisosNoDisponibles('timeout simulado'),
        ):
            resultado = backorder_service.referencias_comprometidas_por_siesa('PD', '1430')

        assert resultado is None


class TestObtenerCompromisosUsaRowid:
    """Bug real encontrado probando en vivo contra Siesa QA (2026-08-24):
    `API_v2_Ventas_Pedidos_Compromisos` filtrando por CO+tipo+consecutivo,
    sin `f430_rowid`, la rechaza — para un pedido despachado Y para uno
    recién sincronizado sin TareaPacking. Con `f430_rowid` (vía cabecera)
    responde bien. `obtener_compromisos` debe resolverlo sola, sin que cada
    caller tenga que descubrirlo — afecta tanto a esta feature como al
    endpoint preexistente `/api/despacho_parcial/<id>/compromisos`."""

    def test_pasa_f430_rowid_de_la_cabecera(self, app, db):
        from app.services.despacho_parcial_service import DespachoParialService

        with patch(
            'app.services.connekta_gateway.connekta.get_pedido_cabecera',
            return_value={'f430_rowid': 31986},
        ) as mock_cabecera, patch(
            'app.services.connekta_gateway.connekta.get_compromisos_pedido',
            return_value=[{'f120_referencia': 'ARTESA173'}],
        ) as mock_compromisos:
            resultado = DespachoParialService.obtener_compromisos('PD', '1357')

        mock_cabecera.assert_called_once_with('PD', '1357')
        mock_compromisos.assert_called_once_with('PD', '1357', 31986)
        assert resultado == [{'f120_referencia': 'ARTESA173'}]

    def test_sin_cabecera_cae_al_filtro_viejo_como_ultimo_recurso(self, app, db):
        from app.services.despacho_parcial_service import DespachoParialService

        with patch(
            'app.services.connekta_gateway.connekta.get_pedido_cabecera',
            return_value=None,
        ), patch(
            'app.services.connekta_gateway.connekta.get_compromisos_pedido',
            return_value=[],
        ) as mock_compromisos:
            DespachoParialService.obtener_compromisos('PD', '1357')

        mock_compromisos.assert_called_once_with('PD', '1357')


class TestBloquearPorBackorderSiesa:
    """Unit test contra DB real de la transición reservado→bloqueado —
    exactamente la misma aritmética que ya usa reportar_problema()."""

    def test_mueve_reservado_a_bloqueado_y_marca_motivo(
        self, app, db, almacen, producto, usuario_admin,
    ):
        from app.models.picking import TareaPicking, EstadoPicking
        from app.models.inventario import UbicacionProducto
        from app.models.ubicacion import Ubicacion
        from app.services.picking_service import PickingService

        ub = Ubicacion(codigo='UB-TEST-01', almacen_id=almacen.id, zona='A', tipo='estanteria', activo=True)
        db.session.add(ub)
        db.session.flush()

        inv = UbicacionProducto(
            ubicacion_id=ub.id, producto_id=producto.id,
            cantidad=10, reservado=4, bloqueado=0,
        )
        db.session.add(inv)

        tarea = TareaPicking(
            codigo='PICK-TEST-BACKORDER', producto_id=producto.id,
            cantidad_solicitada=4, ubicacion_id=ub.id, almacen_id=almacen.id,
            estado=EstadoPicking.PENDIENTE,
        )
        db.session.add(tarea)
        db.session.commit()

        PickingService.bloquear_por_backorder_siesa([tarea], detalle='test backorder')

        db.session.refresh(inv)
        db.session.refresh(tarea)

        assert tarea.estado == EstadoPicking.BLOQUEADO
        assert tarea.motivo_bloqueo == 'BACKORDER_SIESA'
        assert tarea.observaciones_bloqueo == 'test backorder'
        assert tarea.operario_id is None
        assert inv.reservado == 0
        assert inv.bloqueado == 4

        # Y el ciclo de auditoría existente sabe cerrarla — no hace falta
        # nada nuevo para resolverla.
        cerrada = PickingService.auditar_tarea(
            tarea.id, admin_id=usuario_admin.id, resultado='DISCREPANCIA_SIESA',
        )
        assert cerrada.estado == EstadoPicking.CANCELADO
        db.session.refresh(inv)
        assert inv.bloqueado == 0


class TestIniciarDespachoExcluyeLineaSinCompromiso:
    """Detector ciego del caso real: un pedido de 2 líneas donde Siesa solo
    comprometió 1 debe bloquear la tarea de la otra (sin mandarla al
    empacador) y no romper el resto del flujo."""

    def test_linea_sin_compromiso_se_crea_bloqueada_y_no_entra_al_packing(
        self, app, db, client, jwt_token_admin, almacen, producto, producto2,
    ):
        from app.models.picking import TareaPicking

        producto.codigo_siesa = 'PAPELSP9218'
        producto2.codigo_siesa = 'BELLESB1382'
        db.session.commit()

        # Siesa solo comprometió PAPELSP9218 — BELLESB1382 quedó en backorder.
        compromisos_mock = [
            {'f120_referencia': 'PAPELSP9218', 'f405_cant_por_remisionar_base': 4},
        ]

        tarea_comprometida = TareaPicking(
            producto_id=producto.id, cantidad_solicitada=4,
            ubicacion_id=None, almacen_id=almacen.id, estado='PENDIENTE',
        )
        tarea_backorder = TareaPicking(
            producto_id=producto2.id, cantidad_solicitada=4,
            ubicacion_id=None, almacen_id=almacen.id, estado='PENDIENTE',
        )

        with patch(
            'app.services.despacho_parcial_service.DespachoParialService.obtener_compromisos',
            return_value=compromisos_mock,
        ), patch(
            'app.services.picking_service.PickingService.crear_tareas'
        ) as mock_crear_tareas, patch(
            'app.services.picking_service.PickingService.bloquear_por_backorder_siesa'
        ) as mock_bloquear, patch(
            'app.services.packing_service.PackingService.crear_manual'
        ) as mock_crear_manual:

            def _crear_tareas_side_effect(*, producto_id, **kwargs):
                return [tarea_comprometida] if producto_id == producto.id else [tarea_backorder]
            mock_crear_tareas.side_effect = _crear_tareas_side_effect

            mock_crear_manual.return_value = type('P', (), {
                'to_dict': lambda self: {}, 'id': 1, 'codigo': 'PACK-TEST',
            })()

            resp = client.post(
                '/api/siesa/iniciar-despacho',
                json={
                    'numero_pedido': 'PD9999',
                    'tipo_docto': 'PD',
                    'consec_docto': '9999',
                    'almacen_id': almacen.id,
                    'items': [
                        {
                            'producto_id': producto.id, 'item_codigo': 'PAPELSP9218',
                            'cantidad_pendiente': 4, 'producto_nombre_wms': 'Resma',
                        },
                        {
                            'producto_id': producto2.id, 'item_codigo': 'BELLESB1382',
                            'cantidad_pendiente': 4, 'producto_nombre_wms': 'Peinilla',
                        },
                    ],
                },
                headers={'Authorization': f'Bearer {jwt_token_admin}'},
            )

        assert resp.status_code in (200, 201, 207), resp.get_json()
        data = resp.get_json()

        # Las dos tareas se crean (las dos reservan stock local) — pero solo
        # la de backorder se manda a bloquear.
        assert mock_crear_tareas.call_count == 2
        mock_bloquear.assert_called_once()
        assert mock_bloquear.call_args.args[0] == [tarea_backorder]

        # El packing solo debe esperar la línea comprometida — nunca la
        # bloqueada, o el empacador quedaría esperando algo que no se pickeó.
        items_packing_enviados = mock_crear_manual.call_args.kwargs['items']
        productos_en_packing = {i['producto_id'] for i in items_packing_enviados}
        assert productos_en_packing == {producto.id}

        codigos_error = [e['item_codigo'] for e in data.get('errores', [])]
        assert 'BELLESB1382' in codigos_error

    def test_fallo_al_consultar_compromisos_no_bloquea_ningun_item(
        self, app, db, client, jwt_token_admin, almacen, producto,
    ):
        """Regla 0: si no se pudo preguntar, no se filtra nada — se comporta
        exactamente como antes de este cambio."""
        from app.models.picking import TareaPicking
        from app.services.connekta_gateway import CompromisosNoDisponibles

        producto.codigo_siesa = 'PAPELSP9218'
        db.session.commit()

        tarea = TareaPicking(
            producto_id=producto.id, cantidad_solicitada=4,
            ubicacion_id=None, almacen_id=almacen.id, estado='PENDIENTE',
        )

        with patch(
            'app.services.despacho_parcial_service.DespachoParialService.obtener_compromisos',
            side_effect=CompromisosNoDisponibles('timeout simulado'),
        ), patch(
            'app.services.picking_service.PickingService.crear_tareas',
            return_value=[tarea],
        ) as mock_crear_tareas, patch(
            'app.services.picking_service.PickingService.bloquear_por_backorder_siesa'
        ) as mock_bloquear, patch(
            'app.services.packing_service.PackingService.crear_manual'
        ) as mock_crear_manual:

            mock_crear_manual.return_value = type('P', (), {
                'to_dict': lambda self: {}, 'id': 1, 'codigo': 'PACK-TEST',
            })()

            resp = client.post(
                '/api/siesa/iniciar-despacho',
                json={
                    'numero_pedido': 'PD9998',
                    'tipo_docto': 'PD',
                    'consec_docto': '9998',
                    'almacen_id': almacen.id,
                    'items': [
                        {
                            'producto_id': producto.id, 'item_codigo': 'PAPELSP9218',
                            'cantidad_pendiente': 4, 'producto_nombre_wms': 'Resma',
                        },
                    ],
                },
                headers={'Authorization': f'Bearer {jwt_token_admin}'},
            )

        assert resp.status_code in (200, 201, 207), resp.get_json()
        assert mock_crear_tareas.call_count == 1
        mock_bloquear.assert_not_called()
