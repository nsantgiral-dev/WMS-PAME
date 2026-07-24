"""
Tests del módulo de compras inteligente — acuerdos marco, deriva, calendario.

Cisnes negros que previene:
- Proveedor sube 3% en factura sin que nadie cruce contra acuerdo → fuga silenciosa
- Acuerdo vence sin aviso → vuelve a cotización transaccional (40 min × 3 llamadas)
- SKU cola C se cotiza con 3 proveedores → destrucción de valor disfrazada de diligencia
- OC sin precio gobernado → compra a ciegas bajo Ley 1116
"""
import pytest
from datetime import date, timedelta


@pytest.fixture
def proveedor(db):
    from app.models.acuerdo_marco import Proveedor
    p = Proveedor(codigo='PROV-001', nombre='Distribuidora Test', nit='900123456',
                  pais='COLOMBIA', activo=True)
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def acuerdo_vigente(db, producto, proveedor):
    from app.models.acuerdo_marco import AcuerdoMarco
    a = AcuerdoMarco(
        producto_id=producto.id, proveedor_id=proveedor.id,
        precio_unitario=5000, moneda='COP',
        vigencia_desde=date.today() - timedelta(days=30),
        vigencia_hasta=date.today() + timedelta(days=60),
        negociado_por='Santiago', activo=True,
    )
    db.session.add(a)
    db.session.commit()
    return a


class TestClasificacionRama:

    def test_rama_1_con_acuerdo_vigente(self, app, db, producto, acuerdo_vigente):
        """SKU con acuerdo vigente → Rama 1, precio pactado."""
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.clasificar_sku_compra(producto.id)
        assert result['rama'] == 1
        assert result['precio_sugerido'] == 5000

    def test_rama_2_nucleo_a_sin_acuerdo(self, app, db, producto):
        """SKU clase A sin acuerdo → Rama 2, requiere cotización."""
        producto.clasificacion_abc = 'A'
        db.session.commit()
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.clasificar_sku_compra(producto.id)
        assert result['rama'] == 2
        assert result['rama_nombre'] == 'REQUIERE_COTIZACION'

    def test_rama_3_cola_c(self, app, db, producto):
        """SKU clase C → Rama 3, precio lista sin cotizar."""
        producto.clasificacion_abc = 'C'
        db.session.commit()
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.clasificar_sku_compra(producto.id)
        assert result['rama'] == 3


class TestDetectorDeriva:

    def test_sin_acuerdos_retorna_vacio(self, app, db):
        """Sin acuerdos vigentes, no hay deriva que detectar."""
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.detectar_deriva()
        assert result['total'] == 0

    def test_estructura_resultado(self, app, db):
        """El resultado tiene la estructura esperada."""
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.detectar_deriva()
        assert 'derivas' in result
        assert 'total' in result
        assert isinstance(result['derivas'], list)


class TestCalendarioVencimientos:

    def test_acuerdo_por_vencer_aparece(self, app, db, producto, proveedor):
        """Acuerdo que vence en 2 semanas aparece en la alerta."""
        from app.models.acuerdo_marco import AcuerdoMarco
        a = AcuerdoMarco(
            producto_id=producto.id, proveedor_id=proveedor.id,
            precio_unitario=3000,
            vigencia_desde=date.today() - timedelta(days=80),
            vigencia_hasta=date.today() + timedelta(days=14),
            activo=True,
        )
        db.session.add(a)
        db.session.commit()

        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.calendario_vencimientos()
        assert result['resumen']['acuerdos_por_vencer'] >= 1

    def test_calendario_sin_datos_no_crashea(self, app, db):
        """Sin acuerdos, calendario retorna listas vacías."""
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.calendario_vencimientos()
        assert isinstance(result['por_vencer'], list)
        assert isinstance(result['candidatos_acuerdo'], list)


class TestComparador:

    def test_comparador_con_acuerdo(self, app, db, producto, acuerdo_vigente):
        """Comparador muestra precio del acuerdo como opción."""
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        result = ComprasInteligenciaService.comparador_precios(producto.id)
        assert result['total_opciones'] >= 1
        assert result['mejor']['precio'] == 5000
        assert result['mejor']['fuente'] == 'ACUERDO_MARCO'


class TestEndpoints:

    def test_acuerdos_endpoint(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('acuerdos' in r for r in rules)

    def test_deriva_endpoint(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('deriva' in r for r in rules)

    def test_calendario_endpoint(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('calendario' in r for r in rules)

    def test_comparador_endpoint(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('comparador' in r for r in rules)
