"""
Tests mínimos de cobertura — cada servicio crítico tiene al menos un test
que verifica importación + lógica básica. Previene servicios muertos.
"""
import pytest


class TestDashboardService:
    def test_import_y_metodo(self, app):
        from app.services.dashboard_service import DashboardService
        assert hasattr(DashboardService, 'kpis_operativos')
        assert hasattr(DashboardService, 'productividad_operarios')


class TestDespachoParialService:
    def test_import_y_metodo(self, app):
        from app.services.despacho_parcial_service import DespachoParialService
        assert hasattr(DespachoParialService, 'despachar_parcial')
        assert hasattr(DespachoParialService, 'obtener_compromisos')


class TestDevolucionService:
    def test_import_y_funciones(self, app):
        from app.services.devolucion_service import (
            crear_tareas_desde_discrepancias, listar_pendientes,
            confirmar_ubicacion, descartar,
        )
        assert callable(listar_pendientes)

    def test_listar_pendientes_vacio(self, app, db):
        from app.services.devolucion_service import listar_pendientes
        result = listar_pendientes()
        assert isinstance(result, list)


class TestMuelleService:
    def test_listar_bultos_vacio(self, app, db):
        from app.services.muelle_service import MuelleService
        result = MuelleService.listar_bultos_listos()
        assert 'grupos' in result
        assert result['total_bultos'] == 0


class TestReconciliacionService:
    def test_import_y_metodo(self, app):
        from app.services.reconciliacion_service import ReconciliacionService
        assert hasattr(ReconciliacionService, 'reconciliar_despacho')
        assert hasattr(ReconciliacionService, 'sweep_despachos_pendientes')


class TestFacturaFeService:
    def test_import_y_metodo(self, app):
        from app.services.factura_fe_service import FacturaFEService
        assert hasattr(FacturaFEService, 'obtener_lineas')
        assert hasattr(FacturaFEService, 'puede_generar')


class TestRemisionService:
    def test_import_y_metodo(self, app):
        from app.services.remision_service import FacturaService
        assert hasattr(FacturaService, 'puede_generar')
        assert hasattr(FacturaService, 'generar_html')


class TestAbcService:
    def test_import_y_metodo(self, app):
        from app.services.abc_service import ABCService
        assert hasattr(ABCService, 'generar_tareas_conteo_diario')
        assert hasattr(ABCService, 'init_scheduler')
        assert hasattr(ABCService, 'watchdog_anomalias')


class TestInventarioSiesaService:
    def test_import_funciones(self, app):
        from app.services.inventario_siesa_service import iniciar_refresh_periodico
        assert callable(iniciar_refresh_periodico)


class TestTiendaOcService:
    def test_import_y_metodo(self, app):
        from app.services.tienda_oc_service import TiendaOCService
        assert hasattr(TiendaOCService, 'listar_ocs')
        assert hasattr(TiendaOCService, 'iniciar_recepcion')


class TestDespachoParialRoute:
    def test_endpoint_registrado(self, app):
        """La ruta de despacho parcial está registrada en Flask."""
        rules = [r.rule for r in app.url_map.iter_rules()]
        despacho_rules = [r for r in rules if 'despacho' in r]
        assert len(despacho_rules) > 0, 'No hay rutas de despacho_parcial registradas'


class TestTiendaOcRoute:
    def test_endpoint_registrado(self, app):
        """La ruta de tienda-oc está registrada en Flask."""
        rules = [r.rule for r in app.url_map.iter_rules()]
        tienda_rules = [r for r in rules if 'tienda' in r]
        assert len(tienda_rules) > 0, 'No hay rutas de tienda-oc registradas'
