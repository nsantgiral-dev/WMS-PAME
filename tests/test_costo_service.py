"""
Tests del resolvedor de costo y precio.

Cisne negro que previenen: COBERTURA CERO SILENCIOSA.

`Producto.precio_compra` nunca se puebla — siesa_sync_service crea productos
sin él y solo POST /api/productos lo escribe, con default 0. Todo SKU
sincronizado tenía costo 0, el newsvendor los excluía a todos por "costo
fantasma", y la cobertura del comité no era 40%: era CERO.
"""
from datetime import date, timedelta


class TestJerarquiaDeCosto:
    """Acuerdo vigente > cotización > kardex > maestro.

    Los dos primeros son costo HACIA ADELANTE: la plata en riesgo es la que se
    va a gastar, no la que se gastó.
    """

    def test_sin_ninguna_fuente_no_omite_en_silencio(self, app, db):
        """Un SKU sin costo sale marcado, no desaparece: la cobertura se cuenta."""
        from app.services.costo_service import resolver_costos
        r = resolver_costos(['FANTASMA'])
        assert r['FANTASMA']['fuente'] == 'SIN_COSTO'
        assert r['FANTASMA']['costo'] == 0

    def test_el_kardex_alimenta_el_costo_cuando_no_hay_acuerdo(self, app, db):
        """El caso real de hoy: sin acuerdos cargados, el kardex es la fuente."""
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        db.session.add(KardexMovimiento(
            referencia='K1', bodega='NB1', fecha=date.today() - timedelta(days=10),
            concepto=601, naturaleza=1, cantidad=100, costo_promedio=2500))
        db.session.commit()

        r = resolver_costos(['K1'])['K1']
        assert r['fuente'] == 'KARDEX_PROMEDIO'
        assert abs(r['costo'] - 2500) < 0.01
        assert r['confiable'] is False, 'el kardex mira hacia atrás: no es hacia adelante'

    def test_el_costo_del_kardex_es_promedio_ponderado(self, app, db):
        """Ponderado por cantidad, no media simple."""
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        db.session.add(KardexMovimiento(referencia='K2', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=90, costo_promedio=100))
        db.session.add(KardexMovimiento(referencia='K2', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=10, costo_promedio=200))
        db.session.commit()
        # (90*100 + 10*200) / 100 = 110, no (100+200)/2 = 150
        assert abs(resolver_costos(['K2'])['K2']['costo'] - 110) < 0.01

    def test_marca_el_costo_anejo(self, app, db):
        """Con 500 días de inventario el promedio refleja compras de ~1,5 años."""
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos, DIAS_COSTO_ANEJO
        db.session.add(KardexMovimiento(
            referencia='VIEJO', bodega='NB1',
            fecha=date.today() - timedelta(days=DIAS_COSTO_ANEJO + 60),
            concepto=601, naturaleza=1, cantidad=10, costo_promedio=1000))
        db.session.commit()
        assert resolver_costos(['VIEJO'])['VIEJO']['anejo'] is True


class TestSesgoConservador:
    """Regla 0: ante duda entre dos costos, el MÁS ALTO y MÁS RECIENTE.

    Subestimar el costo infla Cu y desinfla Co — ambas empujan el ratio crítico
    hacia arriba, es decir, hacia COMPRAR MÁS. Con 82% de excedente y caja
    comprometida, ese es el lado irreversible.
    """

    def test_entre_dos_acuerdos_vigentes_gana_el_mas_alto(self, app, db):
        from app.models.producto import Producto
        from app.models.acuerdo_marco import Proveedor, AcuerdoMarco
        from app.services.costo_service import resolver_costos

        p = Producto(codigo='A1', nombre='A1', codigo_siesa='A1')
        pr = Proveedor(codigo='PRV1', nombre='Prov')
        db.session.add_all([p, pr])
        db.session.flush()
        hoy = date.today()
        for precio in (800, 1200):
            db.session.add(AcuerdoMarco(
                producto_id=p.id, proveedor_id=pr.id, precio_unitario=precio,
                vigencia_desde=hoy - timedelta(days=5), vigencia_hasta=hoy + timedelta(days=30)))
        db.session.commit()

        r = resolver_costos(['A1'])['A1']
        assert r['costo'] == 1200, 'debe elegir el más alto, no el más barato'
        assert r['fuente'] == 'ACUERDO_VIGENTE'
        assert r['confiable'] is True

    def test_el_acuerdo_gana_al_kardex(self, app, db):
        """Costo hacia adelante manda sobre costo histórico."""
        from app.models.producto import Producto
        from app.models.acuerdo_marco import Proveedor, AcuerdoMarco
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos

        p = Producto(codigo='B1', nombre='B1', codigo_siesa='B1')
        pr = Proveedor(codigo='PRV2', nombre='P2')
        db.session.add_all([p, pr])
        db.session.flush()
        # `hoy_operativo()`: `resolver_costos` filtra con `dia_operativo()`, y
        # con un solo día de margen en `vigencia_desde` este test pasaba en CI
        # por casualidad — el desfase UTC/Bogotá lo consumía entero.
        from tests.conftest import hoy_operativo
        hoy = hoy_operativo()
        db.session.add(AcuerdoMarco(
            producto_id=p.id, proveedor_id=pr.id, precio_unitario=5000,
            vigencia_desde=hoy - timedelta(days=1), vigencia_hasta=hoy + timedelta(days=30)))
        db.session.add(KardexMovimiento(referencia='B1', bodega='NB1', fecha=hoy,
                                        concepto=601, naturaleza=1, cantidad=10,
                                        costo_promedio=3000))
        db.session.commit()
        assert resolver_costos(['B1'])['B1']['costo'] == 5000


class TestPrecioSupuesto:
    """El precio de venta NO existe en ninguna fuente. Se declara, no se disfraza."""

    def test_sin_precio_de_venta_cu_es_supuesto_y_lo_dice(self, app, db):
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        db.session.add(KardexMovimiento(referencia='S1', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=10,
                                        costo_promedio=1000))
        db.session.commit()
        r = resolver_costos(['S1'], margen_supuesto=0.40)['S1']
        assert r['precio_es_supuesto'] is True
        assert r['margen_supuesto'] == 0.40
        # MARGEN SOBRE PRECIO: precio = 1000/(1-0.40) = 1666.67 → Cu = 666.67.
        # Este assert decía 400 (= 1000*1.40 - 1000), que es MARKUP sobre costo:
        # el test codificaba el bug en vez de detectarlo. Un test escrito contra
        # la implementación acompaña al error hasta producción.
        assert abs(r['cu'] - 666.67) < 0.01

    def test_con_precio_real_no_se_marca_como_supuesto(self, app, db):
        from app.models.producto import Producto
        from app.services.costo_service import resolver_costos
        db.session.add(Producto(codigo='C1', nombre='C1', codigo_siesa='C1',
                                precio_compra=1000, precio_venta=1800))
        db.session.commit()
        r = resolver_costos(['C1'])['C1']
        assert r['precio_es_supuesto'] is False
        assert abs(r['cu'] - 800) < 0.01


class TestCoberturaPorFuente:
    """No binaria: un Q* sobre cotización y uno sobre promedio de 18 meses
    no tienen la misma calidad, y el comité tiene derecho a ver cuál es cuál."""

    def test_cuenta_por_fuente_y_no_solo_si_hay_costo(self, app, db):
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos, resumen_por_fuente
        db.session.add(KardexMovimiento(referencia='D1', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=5,
                                        costo_promedio=700))
        db.session.commit()
        res = resumen_por_fuente(resolver_costos(['D1', 'D2']))
        assert res['total_skus'] == 2
        assert res['con_costo'] == 1 and res['sin_costo'] == 1
        assert res['por_fuente']['KARDEX_PROMEDIO'] == 1
        assert res['costo_hacia_adelante'] == 0, 'el kardex no cuenta como hacia adelante'

    def test_advierte_de_los_precios_supuestos(self, app, db):
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos, resumen_por_fuente
        db.session.add(KardexMovimiento(referencia='E1', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=5,
                                        costo_promedio=700))
        db.session.commit()
        res = resumen_por_fuente(resolver_costos(['E1']))
        assert res['precio_supuesto'] == 1
        assert 'hipótesis' in res['advertencia_precio']


class TestConvencionDeMargen:
    """'Margen' prometía una afirmación y significaba la vecina.

    precio = costo*(1+m) es MARKUP SOBRE COSTO. Con m=0.435 y Co=0.55*costo el
    ratio caía de 0.5833 a 0.4416, z de +0.2104 a -0.1469, y el modelo
    recomendaba pedir POR DEBAJO de la media. La palabra decidiendo la compra.
    """

    TOL = 0.001

    def _canon(self):
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / 'docs' / 'canones' / 'margen.json'
        assert p.exists(), 'falta docs/canones/margen.json'
        return json.loads(p.read_text(encoding='utf-8'))

    def _caso(self, prefijo):
        return next(c for c in self._canon()['casos'] if c['nombre'].startswith(prefijo))

    def test_reproduce_el_canon_al_43_5(self):
        from app.services.costo_service import precio_desde_margen
        c = self._caso('Margen 43.5%')
        p = precio_desde_margen(c['entrada']['costo'], c['entrada']['margen_sobre_precio'])
        assert abs(p - c['esperado']['precio']) < self.TOL
        assert abs((p - c['entrada']['costo']) - c['esperado']['cu']) < self.TOL

    def test_el_ratio_y_la_z_salen_del_canon(self):
        from statistics import NormalDist
        from app.services.costo_service import precio_desde_margen
        for prefijo in ('Margen 43.5%', 'Margen 33.5%', 'Margen 53.5%'):
            c = self._caso(prefijo)
            e, esp = c['entrada'], c['esperado']
            cu = precio_desde_margen(e['costo'], e['margen_sobre_precio']) - e['costo']
            co = e['co_fraccion_costo'] * e['costo']
            ratio = cu / (cu + co)
            assert abs(ratio - esp['ratio_critico']) < self.TOL, prefijo
            assert abs(NormalDist().inv_cdf(ratio) - esp['z']) < self.TOL, prefijo

    def test_no_reproduce_los_numeros_del_bug(self):
        """Caso NEGATIVO: si vuelven estos números, la convención se rompió."""
        from app.services.costo_service import precio_desde_margen
        malo = self._caso('EL BUG')['esperado_incorrecto']
        p = precio_desde_margen(100.0, 0.435)
        assert abs(p - malo['precio']) > 1.0, \
            'el precio coincide con el markup: la convención volvió a invertirse'

    def test_margen_sobre_precio_no_es_markup(self):
        """43.5% de margen == 77% de markup. No son el mismo número."""
        from app.services.costo_service import precio_desde_margen
        costo = 100.0
        precio = precio_desde_margen(costo, 0.435)
        markup = (precio - costo) / costo
        assert abs(markup - 0.770) < 0.005

    def test_un_markup_disfrazado_de_margen_revienta(self):
        """Un valor >=1 solo puede ser un markup mal pasado."""
        import pytest
        from app.services.costo_service import precio_desde_margen
        with pytest.raises(ValueError, match='markup'):
            precio_desde_margen(100.0, 1.435)

    def test_margen_por_origen(self, app, db):
        """Un solo margen global miente sobre catálogos distintos."""
        from app.models.producto import Producto
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        from datetime import date
        db.session.add(Producto(codigo='CH1', nombre='CH1', codigo_siesa='CH1', origen='CHINA'))
        db.session.add(Producto(codigo='NA1', nombre='NA1', codigo_siesa='NA1', origen='NACIONAL'))
        for ref in ('CH1', 'NA1'):
            db.session.add(KardexMovimiento(referencia=ref, bodega='NB1', fecha=date.today(),
                                            concepto=601, naturaleza=1, cantidad=10,
                                            costo_promedio=1000))
        db.session.commit()
        r = resolver_costos(['CH1', 'NA1'])
        assert r['CH1']['margen_supuesto'] == 0.435
        assert r['NA1']['margen_supuesto'] == 0.30
        assert r['CH1']['cu'] > r['NA1']['cu'], 'mayor margen debe dar mayor Cu'

    def test_las_filas_supuestas_traen_rango_no_punto(self, app, db):
        """±10 puntos de margen mueven Q* ~24%. El comité ve el rango."""
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        from datetime import date
        db.session.add(KardexMovimiento(referencia='RG1', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=10,
                                        costo_promedio=1000))
        db.session.commit()
        r = resolver_costos(['RG1'])['RG1']
        assert r['cu_rango'] is not None
        bajo, alto = r['cu_rango']
        assert bajo < r['cu'] < alto, 'el rango debe contener el punto'

    def test_la_convencion_esta_declarada_en_la_fila(self, app, db):
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        from datetime import date
        db.session.add(KardexMovimiento(referencia='CV1', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=10,
                                        costo_promedio=500))
        db.session.commit()
        assert resolver_costos(['CV1'])['CV1']['convencion_margen'] == 'SOBRE_PRECIO'
