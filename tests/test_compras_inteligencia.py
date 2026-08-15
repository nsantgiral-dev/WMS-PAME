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


class TestElTodoClaroNoSeFabrica:
    """`total: 0` significa dos cosas y solo una es buena noticia.

    `detectar_deriva_precios` devuelve `{'derivas': [], 'total': 0, 'nota':
    'Sin acuerdos vigentes para comparar'}` cuando no hay con qué comparar — y
    hoy es siempre, porque `POST /api/compras/acuerdos` no tiene pantalla y no
    hay un solo acuerdo marco registrado.

    La pantalla ignoraba `nota` y pintaba en verde «Sin derivas detectadas —
    precios facturados coinciden con acuerdos». Cero derivas porque nadie
    comparó, presentado como que los precios coinciden.

    Es `[]` con dos significados —el mismo patrón que ya costó una vez con
    `get_compromisos_pedido`— y acá el caro es el que se veía.
    """

    def test_el_servicio_declara_que_no_comparo(self, db):
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        r = ComprasInteligenciaService.detectar_deriva()
        assert r['total'] == 0
        assert r.get('nota'), (
            'el servicio dejó de declarar por qué no hay derivas. Sin `nota`, '
            'la pantalla no puede distinguir «no hay desvíos» de «no se comparó».')

    def test_la_pantalla_consume_la_nota(self):
        """Por AST sobre el JS: que `nota` se lea, no solo que esté escrita.

        Un `grep` acá se atraparía en este propio docstring — pasó cinco veces
        en este repo.
        """
        import pathlib
        import re

        js = pathlib.Path('app/static/pwa/compras_ia.js').read_text()
        cuerpo = re.search(r'function _renderDeriva\(.*?\n}\n', js, re.S)
        assert cuerpo, 'ya no existe _renderDeriva'
        src = cuerpo.group(0)
        assert 'data.nota' in src, (
            'la pantalla de deriva volvió a ignorar `nota`: pinta el todo-claro '
            'verde sobre una comparación que nunca ocurrió.')
        # Y el verde solo puede salir cuando NO hay nota.
        i_nota = src.index('data.nota')
        i_verde = src.index('Sin derivas detectadas')
        assert i_nota < i_verde, (
            'el mensaje verde se evalúa antes que la nota — el todo-claro falso '
            'vuelve a ganar.')
