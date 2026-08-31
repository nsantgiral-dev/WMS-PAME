"""
Tests de DevolucionClienteService — reemplazo de TareaDevolucion.

Cubre: buscar_pedido (ancla en TareaPacking + resolución del tipo/consec REAL
de la factura electrónica vía get_detalle_factura, nunca los del pedido),
crear_devolucion (tope contra lo facturado), confirmar_entrada_fisica
(parcial, exceso rechazado contra el re-GET, línea averiada → AVERIADOS +
flag en el payload de la NC).
"""
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def tarea_packing_despachada(db, almacen):
    from app.models.packing import TareaPacking
    tarea = TareaPacking(
        codigo='PK-DEVC-001', tipo_documento='PEDIDO', estado='DESPACHADO',
        almacen_id=almacen.id, numero_pedido_siesa='PD9001',
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='500',
        rm_tipo='RM', rm_consec=700, cliente='Cliente Test',
        siesa_triggered=True,
    )
    db.session.add(tarea)
    db.session.commit()
    return tarea


_FILA_FE_CABECERA = {
    'f350_id_tipo_docto': 'FEW',
    'f350_consec_docto': '5555',
}


def _fila_rowid(ref='PROD-001', rowid='999', cant=10, uom='UND', bodega='NB1'):
    return {
        'f470_rowid': rowid,
        'f120_referencia': ref,
        'f470_cant_base': cant,
        'f470_id_unidad_medida': uom,
        'f150_id': bodega,
    }


def _linea_input(producto, cantidad_facturada=10, cantidad_devuelta=4, es_averiado=False):
    return {
        'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
        'cantidad_facturada': cantidad_facturada, 'cantidad_devuelta': cantidad_devuelta,
        'es_averiado': es_averiado, 'f470_id_unidad_medida': 'UND',
        'f150_id_bodega': 'NB1', 'f470_rowid': '999',
    }


# ═══════════════════════════════════════════════════════════════════
# buscar_pedido
# ═══════════════════════════════════════════════════════════════════

class TestBuscarPedido:

    def test_sin_tarea_packing_falla(self, app, db, almacen):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        with pytest.raises(ValueError, match='No se encontró'):
            DevolucionClienteService.buscar_pedido('PD-INEXISTENTE')

    def test_sin_siesa_triggered_falla(self, app, db, almacen):
        from app.models.packing import TareaPacking
        from app.services.devolucion_cliente_service import DevolucionClienteService
        tarea = TareaPacking(
            codigo='PK-DEVC-002', tipo_documento='PEDIDO', estado='EN_PROCESO',
            almacen_id=almacen.id, numero_pedido_siesa='PD9002',
            siesa_triggered=False,
        )
        db.session.add(tarea)
        db.session.commit()
        with pytest.raises(ValueError, match='no ha sido facturado'):
            DevolucionClienteService.buscar_pedido('PD9002')

    @patch('app.services.devolucion_cliente_service.connekta')
    def test_happy_path_resuelve_fe_real_no_pedido(self, mock_connekta, app, db,
                                                     tarea_packing_despachada, producto):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        mock_connekta.get_detalle_factura.return_value = [dict(_FILA_FE_CABECERA)]
        mock_connekta.get_rowids_factura.return_value = [_fila_rowid(ref=producto.codigo_siesa)]

        resultado = DevolucionClienteService.buscar_pedido('PD9001')

        # tipo_docto_fe/consec_fe deben venir de get_detalle_factura (FE real),
        # NUNCA de tarea.tipo_docto_pedido_siesa ('PD'/'500') — ver hallazgo del
        # DOCX 142946 documentado en el plan aprobado.
        assert resultado['tipo_docto_fe'] == 'FEW'
        assert resultado['consec_fe'] == '5555'
        assert resultado['tipo_docto_fe'] != tarea_packing_despachada.tipo_docto_pedido_siesa
        assert resultado['consec_fe'] != tarea_packing_despachada.consec_docto_pedido_siesa
        mock_connekta.get_rowids_factura.assert_called_once_with('FEW', '5555')
        assert len(resultado['lineas']) == 1
        assert resultado['lineas'][0]['producto_id'] == producto.id
        assert resultado['lineas'][0]['cantidad_facturada'] == 10
        assert resultado['lineas'][0]['codigo_barras'] == producto.codigo_barras

    @patch('app.services.devolucion_cliente_service.connekta')
    def test_expone_codigo_barras_para_el_escaner(self, mock_connekta, app, db,
                                                   tarea_packing_despachada, producto):
        """Reportado el 2026-08-31: el conteo de recepción en Devoluciones era
        100% manual — sin código de barras en el payload, el escáner no tenía
        contra qué emparejar (solo quedaba codigo_siesa/producto_codigo, que
        no son lo que trae la etiqueta física del producto)."""
        from app.services.devolucion_cliente_service import DevolucionClienteService
        producto.codigo_barras = '7701234567890'
        db.session.commit()
        mock_connekta.get_detalle_factura.return_value = [dict(_FILA_FE_CABECERA)]
        mock_connekta.get_rowids_factura.return_value = [_fila_rowid(ref=producto.codigo_siesa)]

        resultado = DevolucionClienteService.buscar_pedido('PD9001')
        assert resultado['lineas'][0]['codigo_barras'] == '7701234567890'

    @patch('app.services.devolucion_cliente_service.connekta')
    def test_sin_fe_en_siesa_falla(self, mock_connekta, app, db, tarea_packing_despachada):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        mock_connekta.get_detalle_factura.return_value = []
        with pytest.raises(ValueError, match='factura electrónica'):
            DevolucionClienteService.buscar_pedido('PD9001')

    @patch('app.services.devolucion_cliente_service.connekta')
    def test_referencia_sin_producto_wms_se_omite(self, mock_connekta, app, db,
                                                    tarea_packing_despachada):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        mock_connekta.get_detalle_factura.return_value = [dict(_FILA_FE_CABECERA)]
        mock_connekta.get_rowids_factura.return_value = [_fila_rowid(ref='REF-DESCONOCIDA')]

        resultado = DevolucionClienteService.buscar_pedido('PD9001')
        assert resultado['lineas'] == []


# ═══════════════════════════════════════════════════════════════════
# crear_devolucion
# ═══════════════════════════════════════════════════════════════════

class TestCrearDevolucion:

    def test_no_puede_devolver_mas_de_lo_facturado(self, app, db, tarea_packing_despachada,
                                                     producto, almacen):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        with pytest.raises(ValueError, match='no puede superar'):
            DevolucionClienteService.crear_devolucion(
                tarea_packing_id=tarea_packing_despachada.id,
                tipo_docto_fe='FEW', consec_fe='5555',
                almacen_id=almacen.id, recepcionista_id=None,
                lineas=[_linea_input(producto, cantidad_facturada=10, cantidad_devuelta=15)],
            )

    def test_sin_lineas_validas_falla(self, app, db, tarea_packing_despachada, producto, almacen):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        with pytest.raises(ValueError, match='al menos una línea'):
            DevolucionClienteService.crear_devolucion(
                tarea_packing_id=tarea_packing_despachada.id,
                tipo_docto_fe='FEW', consec_fe='5555',
                almacen_id=almacen.id, recepcionista_id=None,
                lineas=[_linea_input(producto, cantidad_devuelta=0)],
            )

    def test_to_dict_expone_codigo_barras(self, app, db, tarea_packing_despachada,
                                          producto, almacen):
        """El camino de "devoluciones pendientes de ruta" (Liquidación arma la
        devolución sola) lee las líneas ya guardadas vía to_dict() — sin este
        campo, esas devoluciones tampoco se podían escanear, aunque
        buscar_pedido() sí lo tuviera."""
        from app.services.devolucion_cliente_service import DevolucionClienteService
        producto.codigo_barras = '7701234567890'
        db.session.commit()
        devolucion = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=tarea_packing_despachada.id,
            tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=almacen.id, recepcionista_id=None,
            lineas=[_linea_input(producto, cantidad_facturada=10, cantidad_devuelta=4)],
        )
        assert devolucion.lineas[0].to_dict()['codigo_barras'] == '7701234567890'


# ═══════════════════════════════════════════════════════════════════
# confirmar_entrada_fisica
# ═══════════════════════════════════════════════════════════════════

class TestConfirmarEntradaFisica:

    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    @patch('app.services.devolucion_cliente_service.connekta')
    def test_parcial_sube_solo_cantidad_devuelta_y_encola_nc(self, mock_connekta, mock_dlq,
                                                              app, db, tarea_packing_despachada,
                                                              producto, almacen, ub_reserva):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.models.inventario import UbicacionProducto
        from app.models.siesa_job import SiesaJob

        mock_connekta.get_rowids_factura.return_value = [
            _fila_rowid(ref=producto.codigo_siesa, cant=10)
        ]

        devolucion = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=tarea_packing_despachada.id,
            tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=almacen.id, recepcionista_id=None,
            lineas=[_linea_input(producto, cantidad_facturada=10, cantidad_devuelta=4)],
        )

        DevolucionClienteService.confirmar_entrada_fisica(devolucion.id, recepcionista_id=1)

        reg = UbicacionProducto.query.filter_by(producto_id=producto.id).first()
        assert reg is not None
        assert reg.cantidad == 4

        db.session.refresh(devolucion)
        assert devolucion.estado == 'CONFIRMADA'

        job = SiesaJob.query.filter_by(
            referencia_tipo='DevolucionCliente', referencia_id=devolucion.id
        ).first()
        assert job is not None
        assert job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE'
        payload = job.get_payload()
        assert payload['tipo_docto_fe'] == 'FEW'
        assert payload['consec_fe'] == '5555'
        assert payload['items_devueltos'][0]['cantidad_devuelta'] == 4
        assert payload['items_devueltos'][0]['es_averiado'] is False

    @patch('app.services.devolucion_cliente_service.connekta')
    def test_exceso_contra_reget_de_siesa_es_rechazado(self, mock_connekta, app, db,
                                                        tarea_packing_despachada, producto,
                                                        almacen):
        from app.services.devolucion_cliente_service import DevolucionClienteService

        # crear_devolucion valida contra lo declarado (10 facturadas). Antes de
        # confirmar, Siesa ahora reporta solo 3 (el dato cambió) — debe fallar
        # explícito, nunca aceptar con un tope silenciosamente menor.
        devolucion = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=tarea_packing_despachada.id,
            tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=almacen.id, recepcionista_id=None,
            lineas=[_linea_input(producto, cantidad_facturada=10, cantidad_devuelta=8)],
        )

        mock_connekta.get_rowids_factura.return_value = [
            _fila_rowid(ref=producto.codigo_siesa, cant=3)
        ]

        with pytest.raises(ValueError, match='supera la cantidad facturada actual'):
            DevolucionClienteService.confirmar_entrada_fisica(devolucion.id, recepcionista_id=1)

    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    @patch('app.services.devolucion_cliente_service.connekta')
    def test_averiado_va_a_ubicacion_averiados_y_marca_payload(self, mock_connekta, mock_dlq,
                                                                app, db, tarea_packing_despachada,
                                                                producto, almacen):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.models.ubicacion import Ubicacion
        from app.models.siesa_job import SiesaJob

        mock_connekta.get_rowids_factura.return_value = [
            _fila_rowid(ref=producto.codigo_siesa, cant=10)
        ]

        devolucion = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=tarea_packing_despachada.id,
            tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=almacen.id, recepcionista_id=None,
            lineas=[_linea_input(producto, cantidad_facturada=10, cantidad_devuelta=2, es_averiado=True)],
        )

        DevolucionClienteService.confirmar_entrada_fisica(devolucion.id, recepcionista_id=1)

        db.session.refresh(devolucion)
        linea = devolucion.lineas[0]
        ub = Ubicacion.query.get(linea.ubicacion_id)
        assert ub.codigo == 'AVERIADOS'

        job = SiesaJob.query.filter_by(
            referencia_tipo='DevolucionCliente', referencia_id=devolucion.id
        ).first()
        assert job.get_payload()['items_devueltos'][0]['es_averiado'] is True

    @patch('app.services.devolucion_cliente_service.connekta')
    def test_idempotente_si_ya_confirmada_con_job_activo(self, mock_connekta, app, db,
                                                          tarea_packing_despachada, producto,
                                                          almacen):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.models.siesa_job import SiesaJob

        mock_connekta.get_rowids_factura.return_value = [
            _fila_rowid(ref=producto.codigo_siesa, cant=10)
        ]

        devolucion = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=tarea_packing_despachada.id,
            tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=almacen.id, recepcionista_id=None,
            lineas=[_linea_input(producto, cantidad_facturada=10, cantidad_devuelta=4)],
        )
        devolucion.estado = 'CONFIRMADA'
        db.session.commit()
        SiesaJob.encolar(
            tipo='NOTA_CREDITO_DEVOLUCION_CLIENTE', payload={},
            referencia_tipo='DevolucionCliente', referencia_id=devolucion.id,
        )
        db.session.commit()

        # No debe lanzar ni re-encolar un segundo job
        resultado = DevolucionClienteService.confirmar_entrada_fisica(devolucion.id, recepcionista_id=1)
        assert resultado.estado == 'CONFIRMADA'
        jobs = SiesaJob.query.filter_by(
            referencia_tipo='DevolucionCliente', referencia_id=devolucion.id
        ).count()
        assert jobs == 1

    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    def test_averiado_de_inicio_a_fin_wms_y_payload_real_a_siesa(
            self, mock_dlq, app, db, tarea_packing_despachada,
            producto, producto2, almacen, ub_reserva):
        """Simulación completa del checkbox «Averiado», en una sola devolución
        con DOS productos — uno marcado, uno no — para probar que es de
        verdad POR LÍNEA y no una bandera global:

        1. Recepción confirma entrada física (lo que hace este mismo test en
           versiones más chicas, arriba) — se verifica dónde aterriza cada
           producto en el WMS.
        2. Se toma el SiesaJob REAL que esa confirmación encoló (no uno
           fabricado a mano) y se ejecuta de verdad (`_ejecutar_job`) — se
           verifica el payload que de verdad viajaría a Siesa (251126),
           línea por línea.

        Si el checkbox no cumpliera su función, alguna de estas cuatro
        aserciones fallaría: el producto averiado terminaría en la ubicación
        normal, o el sano en AVERIADOS, o el payload a Siesa no
        distinguiría la bodega por línea."""
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from app.services.siesa_job_service import _ejecutar_job
        from app.models.inventario import UbicacionProducto, MovimientoInventario
        from app.models.ubicacion import Ubicacion
        from app.models.siesa_job import SiesaJob

        # Un solo mock para las dos rutas de import de `connekta` que este
        # flujo realmente atraviesa: devolucion_cliente_service lo importa a
        # nivel de módulo (para el re-GET de confirmar_entrada_fisica) y
        # siesa_job_service igual (para _ejecutar_job) — son dos referencias
        # distintas al mismo singleton, así que las dos hay que parchearlas.
        mock_connekta = MagicMock()
        mock_connekta.get_rowids_factura.return_value = [
            _fila_rowid(ref=producto.codigo_siesa, rowid='901', cant=10, bodega='NB1'),
            _fila_rowid(ref=producto2.codigo_siesa, rowid='902', cant=10, bodega='NB1'),
        ]

        with patch('app.services.devolucion_cliente_service.connekta', mock_connekta), \
             patch('app.services.connekta_gateway.connekta', mock_connekta):
            devolucion = DevolucionClienteService.crear_devolucion(
                tarea_packing_id=tarea_packing_despachada.id,
                tipo_docto_fe='FEW', consec_fe='5555',
                almacen_id=almacen.id, recepcionista_id=None,
                lineas=[
                    _linea_input(producto, cantidad_facturada=10, cantidad_devuelta=2,
                                es_averiado=True),
                    _linea_input(producto2, cantidad_facturada=10, cantidad_devuelta=3,
                                es_averiado=False),
                ],
            )

            # ── Paso 1: WMS — ¿cada producto aterriza donde debe? ──────────
            DevolucionClienteService.confirmar_entrada_fisica(devolucion.id, recepcionista_id=1)

            ub_averiados = Ubicacion.query.filter_by(
                codigo='AVERIADOS', almacen_id=almacen.id).first()
            assert ub_averiados is not None, (
                'el producto averiado debía crear la ubicación AVERIADOS')

            stock_averiado_en_averiados = UbicacionProducto.query.filter_by(
                ubicacion_id=ub_averiados.id, producto_id=producto.id).first()
            assert stock_averiado_en_averiados is not None
            assert stock_averiado_en_averiados.cantidad == 2

            stock_averiado_normal = UbicacionProducto.query.filter(
                UbicacionProducto.producto_id == producto.id,
                UbicacionProducto.ubicacion_id != ub_averiados.id,
            ).first()
            assert stock_averiado_normal is None, (
                'el producto marcado averiado NO debe aparecer en ninguna '
                'ubicación de stock vendible')

            stock_sano = UbicacionProducto.query.filter_by(producto_id=producto2.id).first()
            assert stock_sano is not None
            assert stock_sano.ubicacion_id != ub_averiados.id, (
                'el producto SIN marcar averiado no debe terminar en AVERIADOS')
            assert stock_sano.cantidad == 3

            mov_averiado = MovimientoInventario.query.filter_by(producto_id=producto.id).first()
            mov_sano = MovimientoInventario.query.filter_by(producto_id=producto2.id).first()
            assert mov_averiado.tipo == 'DEVOLUCION_CLIENTE_AVERIADO'
            assert mov_sano.tipo == 'DEVOLUCION_CLIENTE'

            # ── Paso 2: el job REAL que esa confirmación encoló, ejecutado
            # de verdad — el payload que realmente viajaría a Siesa ───────
            job = SiesaJob.query.filter_by(
                referencia_tipo='DevolucionCliente', referencia_id=devolucion.id
            ).first()
            assert job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE'

            mock_connekta.modo_simulacion = False
            mock_connekta.causal_devolucion_default = '01'
            mock_connekta.motivo_ventas = '01'
            mock_connekta.uom_default = 'UND'
            mock_connekta.bodega = 'NB1'
            mock_connekta.bodega_averias = 'AV1'
            mock_connekta.trigger_nota_factura_crear_cruzar.return_value = {'codigo': 0}

            _ejecutar_job(job)

        mock_connekta.trigger_nota_factura_crear_cruzar.assert_called_once()
        _, kwargs = mock_connekta.trigger_nota_factura_crear_cruzar.call_args
        lineas_por_ref = {ln['f120_referencia']: ln for ln in kwargs['lineas']}

        assert lineas_por_ref[producto.codigo_siesa]['f470_id_bodega'] == 'AV1', (
            'la línea averiada debe entrar a Siesa por la bodega de averías, '
            'no por la bodega real del pedido')
        assert lineas_por_ref[producto2.codigo_siesa]['f470_id_bodega'] == 'NB1', (
            'la línea sana NO debe verse afectada por el averiado de la otra '
            'línea de la misma devolución — es por línea, no global')

        db.session.refresh(devolucion)
        assert devolucion.siesa_nc_triggered is True


# ═══════════════════════════════════════════════════════════════════
# _construir_lineas_nc (extraída en siesa_job_service.py) — función pura
# ═══════════════════════════════════════════════════════════════════

class TestConstruirLineasNC:

    def test_es_total_usa_fallbacks_del_conector(self):
        from app.services.siesa_job_service import _construir_lineas_nc
        rows = [_fila_rowid(ref='PROD-A', rowid='1', cant=5, uom=None, bodega=None)]
        lineas = _construir_lineas_nc(
            rows, es_total=True, items_devueltos=[], causal='01',
            motivo='02', uom_default='UND', bodega_default='NB1',
        )
        assert len(lineas) == 1
        assert lineas[0]['f470_id_bodega'] == 'NB1'
        assert lineas[0]['f470_id_motivo'] == '02'
        assert lineas[0]['f470_id_unidad_medida'] == 'UND'
        assert lineas[0]['f470_cant_base'] == 5

    def test_parcial_solo_incluye_items_declarados(self):
        from app.services.siesa_job_service import _construir_lineas_nc
        rows = [
            _fila_rowid(ref='PROD-A', rowid='1', cant=5),
            _fila_rowid(ref='PROD-B', rowid='2', cant=8),
        ]
        items = [{'codigo': 'PROD-A', 'cantidad_devuelta': 3}]
        lineas = _construir_lineas_nc(
            rows, es_total=False, items_devueltos=items, causal='01',
            motivo='02', uom_default='UND', bodega_default='NB1',
        )
        assert len(lineas) == 1
        assert lineas[0]['f470_rowid_movto'] == '1'
        assert lineas[0]['f470_cant_base'] == 3

    def test_averiado_usa_bodega_averias_en_vez_de_f150(self):
        from app.services.siesa_job_service import _construir_lineas_nc
        rows = [_fila_rowid(ref='PROD-A', rowid='1', cant=5, bodega='NB1')]
        items = [{'codigo': 'PROD-A', 'cantidad_devuelta': 2, 'es_averiado': True}]
        lineas = _construir_lineas_nc(
            rows, es_total=False, items_devueltos=items, causal='01',
            motivo='02', uom_default='UND', bodega_default='NB1',
            bodega_averias='AV1',
        )
        assert lineas[0]['f470_id_bodega'] == 'AV1'

    def test_sin_averiado_conserva_bodega_original_pese_a_bodega_averias(self):
        """Regresión: bodega_averias solo debe aplicar a items con es_averiado=True —
        el caso existente de Liquidación nunca manda ese flag y no debe cambiar."""
        from app.services.siesa_job_service import _construir_lineas_nc
        rows = [_fila_rowid(ref='PROD-A', rowid='1', cant=5, bodega='NB1')]
        items = [{'codigo': 'PROD-A', 'cantidad_devuelta': 2}]
        lineas = _construir_lineas_nc(
            rows, es_total=False, items_devueltos=items, causal='01',
            motivo='02', uom_default='UND', bodega_default='NB1',
            bodega_averias='AV1',
        )
        assert lineas[0]['f470_id_bodega'] == 'NB1'


# ═══════════════════════════════════════════════════════════════════
# listar_pendientes_aprobacion_nc / marcar_nc_aprobada
#
# Seguimiento interno de aprobación contable (CLAUDE.md Regla #21): 142946
# se crea en Elaboración y ni crearla ni aprobarla desde el escritorio cruza
# sola la cartera — alguien en contabilidad debe marcarlo aquí manualmente.
# ═══════════════════════════════════════════════════════════════════

class TestPendientesAprobacionNC:

    @staticmethod
    def _make_devolucion(db, almacen, siesa_nc_triggered=False, nc_aprobada=False, codigo='DEVC-NC-001'):
        from app.models.packing import TareaPacking
        from app.models.devolucion_cliente import DevolucionCliente
        # Un pedido por tarea activa. Las tres devoluciones del test
        # compartían `numero_pedido_siesa='PD-NC'`, que es un estado que la
        # operación prohíbe (`packing_service.py:48`) y que desde
        # `uq_packing_pedido_activo` (2026-08-19) la base tampoco acepta.
        # Cada devolución es de un pedido distinto; eso es lo que el test
        # quería decir.
        tarea = TareaPacking(
            codigo=f'PK-{codigo}', tipo_documento='PEDIDO', estado='DESPACHADO',
            almacen_id=almacen.id, numero_pedido_siesa=f'PD-NC-{codigo}',
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='500',
            siesa_triggered=True,
        )
        db.session.add(tarea)
        db.session.flush()
        devolucion = DevolucionCliente(
            codigo=codigo, tarea_packing_id=tarea.id,
            numero_pedido_siesa=f'PD-NC-{codigo}', tipo_docto_fe='FEW', consec_fe='9999',
            almacen_id=almacen.id, estado='CONFIRMADA',
            siesa_nc_triggered=siesa_nc_triggered,
            nc_aprobada_siesa=nc_aprobada,
        )
        db.session.add(devolucion)
        db.session.commit()
        return devolucion

    def test_lista_solo_triggered_y_no_aprobadas(self, app, db, almacen):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        self._make_devolucion(db, almacen, siesa_nc_triggered=True, nc_aprobada=False, codigo='DEVC-NC-A')
        self._make_devolucion(db, almacen, siesa_nc_triggered=True, nc_aprobada=True, codigo='DEVC-NC-B')
        self._make_devolucion(db, almacen, siesa_nc_triggered=False, nc_aprobada=False, codigo='DEVC-NC-C')

        pendientes = DevolucionClienteService.listar_pendientes_aprobacion_nc()
        codigos = [p['codigo'] for p in pendientes]
        assert 'DEVC-NC-A' in codigos
        assert 'DEVC-NC-B' not in codigos, 'Ya aprobada — no debe listarse'
        assert 'DEVC-NC-C' not in codigos, 'Sin NC en Siesa todavía — no debe listarse'

    def test_marcar_nc_aprobada_la_saca_de_pendientes(self, app, db, almacen, usuario_admin):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        dev = self._make_devolucion(db, almacen, siesa_nc_triggered=True, nc_aprobada=False, codigo='DEVC-NC-D')

        DevolucionClienteService.marcar_nc_aprobada(dev.id, usuario_admin.id)

        db.session.refresh(dev)
        assert dev.nc_aprobada_siesa is True
        assert dev.nc_aprobada_siesa_at is not None
        assert dev.nc_aprobada_siesa_por == usuario_admin.id
        pendientes = DevolucionClienteService.listar_pendientes_aprobacion_nc()
        assert dev.codigo not in [p['codigo'] for p in pendientes]

    def test_marcar_nc_aprobada_falla_si_no_tiene_nc(self, app, db, almacen, usuario_admin):
        from app.services.devolucion_cliente_service import DevolucionClienteService
        dev = self._make_devolucion(db, almacen, siesa_nc_triggered=False, codigo='DEVC-NC-E')
        with pytest.raises(ValueError, match='no tiene una NC'):
            DevolucionClienteService.marcar_nc_aprobada(dev.id, usuario_admin.id)


class TestListarPendientesDeRuta:
    """El panel de Recepción → Devoluciones (`recepcion.js::cargarPendientesDeRuta`)
    pinta el array en el orden que llega del API, sin reordenar por su cuenta
    — el orden real lo decide `listar_pendientes_de_ruta()`."""

    @staticmethod
    def _make_pendiente(db, almacen, codigo, fecha_creacion):
        from datetime import datetime
        from app.models.packing import TareaPacking
        from app.models.ruta_despacho import RutaDespacho
        from app.models.recaudo_entrega import RecaudoEntrega, EstadoEntrega
        from app.models.usuario import Usuario
        from app.models.devolucion_cliente import DevolucionCliente

        conductor = Usuario.query.filter_by(email='cond_pend_ruta@test.com').first()
        if not conductor:
            conductor = Usuario(email='cond_pend_ruta@test.com', nombre='Conductor Pend',
                                rol='conductor', activo=True)
            conductor.set_password('test123')
            db.session.add(conductor)
            db.session.flush()

        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado='ENTREGADA')
        db.session.add(ruta)
        db.session.flush()

        tarea = TareaPacking(
            codigo=f'PK-{codigo}', tipo_documento='PEDIDO', estado='DESPACHADO',
            almacen_id=almacen.id, numero_pedido_siesa=codigo,
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='500',
            siesa_triggered=True,
        )
        db.session.add(tarea)
        db.session.flush()

        recaudo = RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega=EstadoEntrega.PARCIAL, forma_pago='EFECTIVO', monto_cobrado=1000,
        )
        db.session.add(recaudo)
        db.session.flush()

        devolucion = DevolucionCliente(
            codigo=f'DEVC-{codigo}', tarea_packing_id=tarea.id,
            numero_pedido_siesa=codigo, tipo_docto_fe='FEW', consec_fe='9999',
            almacen_id=almacen.id, estado='ABIERTA',
            recaudo_entrega_id=recaudo.id,
            fecha_creacion=datetime.fromisoformat(fecha_creacion),
        )
        db.session.add(devolucion)
        db.session.commit()
        return devolucion

    def test_la_ultima_creada_sale_primero(self, app, db, almacen):
        """El caso real que lo destapó: PD1350 (2026-07-28, la más vieja del
        panel) salía de primero y las recién liquidadas quedaban al fondo —
        la recepcionista tenía que hacer scroll para llegar a lo urgente del
        día. `fecha_creacion.asc()` → `desc()`."""
        from app.services.devolucion_cliente_service import DevolucionClienteService
        self._make_pendiente(db, almacen, 'PD-VIEJO', '2026-07-28T10:00:00')
        self._make_pendiente(db, almacen, 'PD-MEDIO', '2026-08-15T10:00:00')
        self._make_pendiente(db, almacen, 'PD-NUEVO', '2026-08-20T17:11:39')

        pendientes = DevolucionClienteService.listar_pendientes_de_ruta()
        codigos = [p['codigo'] for p in pendientes]
        assert codigos == ['DEVC-PD-NUEVO', 'DEVC-PD-MEDIO', 'DEVC-PD-VIEJO']
