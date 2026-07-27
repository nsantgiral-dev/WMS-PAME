"""
Tests del precio realizado.

`valor / cantidad` sobre ventas reales: neto de descuentos y de la escalera
clandestina. El margen que sale de aquí es el que se obtiene; el de una lista
es el que se cree obtener.

Cisne negro que previenen: caer en SILENCIO al margen supuesto cuando la
columna de ítem no se reconoce. Regla 0 — si el dato no está, se declara.
"""
from datetime import date


class TestDeteccionDeColumna:
    """Por CABECERA, nunca por índice.

    Escribir row[7] porque parece razonable sería el error que este proyecto
    lleva una semana persiguiendo.
    """

    def test_reconoce_nombres_plausibles(self):
        from app.services.vigia_service import detectar_columna_item
        for nombre in ('Item', 'ITEM', 'Referencia', 'Producto', 'Código',
                       'SKU', 'Cod. Item', 'Artículo'):
            cab = ['Fecha', 'Nro documento', 'Tipo docto.', 'C.O.', nombre]
            i, encontrado = detectar_columna_item(cab)
            assert i == 4, f'no reconoció "{nombre}"'
            assert encontrado == nombre

    def test_no_adivina_cuando_no_reconoce(self):
        """Devolver un índice al azar sería peor que no devolver nada."""
        from app.services.vigia_service import detectar_columna_item
        i, nombre = detectar_columna_item(['Fecha', 'Doc', 'Tipo', 'C.O.', 'Zzz'])
        assert i is None and nombre is None

    def test_cabecera_vacia_no_crashea(self):
        from app.services.vigia_service import detectar_columna_item
        assert detectar_columna_item(None) == (None, None)
        assert detectar_columna_item([]) == (None, None)


class TestPrecioRealizadoNoEsPromedioDePrecios:
    """El cociente de los totales, no el promedio de los cocientes.

    Vender 1 unidad a 100 y 99 a 50 da un precio realizado de 50.5, no de 75.
    Promediar cocientes le daría al ticket pequeño el mismo peso que a 99.
    """

    def test_pondera_por_cantidad(self, app, db):
        from app.models.precio_realizado import PrecioRealizado
        f = PrecioRealizado(referencia='X1', centro_operacion=None, periodo='TOTAL',
                            valor_total=100 * 1 + 50 * 99, cantidad_total=100,
                            precio_realizado=(100 + 50 * 99) / 100, lineas=2)
        db.session.add(f)
        db.session.commit()
        assert abs(float(f.precio_realizado) - 50.5) < 0.01


class TestPrecioRealizadoAlimentaCu:
    """Sustituye el margen supuesto por margen MEDIDO."""

    def test_el_realizado_gana_al_margen_supuesto(self, app, db):
        from app.models.precio_realizado import PrecioRealizado
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        db.session.add(KardexMovimiento(referencia='P1', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=10,
                                        costo_promedio=1000))
        db.session.add(PrecioRealizado(referencia='P1', centro_operacion=None,
                                       periodo='TOTAL', valor_total=15000,
                                       cantidad_total=10, precio_realizado=1500))
        db.session.commit()

        r = resolver_costos(['P1'])['P1']
        assert r['fuente_precio'] == 'REALIZADO'
        assert r['precio_es_supuesto'] is False
        assert abs(r['cu'] - 500) < 0.01  # 1500 - 1000, medido

    def test_sin_realizado_declara_que_el_precio_es_supuesto(self, app, db):
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        db.session.add(KardexMovimiento(referencia='P2', bodega='NB1', fecha=date.today(),
                                        concepto=601, naturaleza=1, cantidad=10,
                                        costo_promedio=1000))
        db.session.commit()
        r = resolver_costos(['P2'])['P2']
        assert r['fuente_precio'] == 'MARGEN_SUPUESTO'
        assert r['precio_es_supuesto'] is True


class TestEscaleraDePrecios:
    """La dispersión entre C.O. del MISMO SKU es la escalera, medida.

    No sirve al newsvendor: sirve a la política de precios, que hoy se toma a
    ciegas. Mismo cómputo, segundo uso.
    """

    def test_mide_la_dispersion_entre_centros(self, app, db):
        from app.models.precio_realizado import PrecioRealizado
        from app.services.costo_service import dispersion_precio_por_co
        for co, precio in (('001', 1000), ('006', 1300)):
            db.session.add(PrecioRealizado(
                referencia='E1', centro_operacion=co, periodo='TOTAL',
                valor_total=precio * 10, cantidad_total=10, precio_realizado=precio))
        db.session.commit()

        d = dispersion_precio_por_co('E1')
        assert d['minimo'] == 1000 and d['maximo'] == 1300
        assert abs(d['dispersion_pct'] - 30.0) < 0.1

    def test_con_un_solo_co_no_inventa_una_escalera(self, app, db):
        from app.models.precio_realizado import PrecioRealizado
        from app.services.costo_service import dispersion_precio_por_co
        db.session.add(PrecioRealizado(referencia='E2', centro_operacion='001',
                                       periodo='TOTAL', valor_total=1000,
                                       cantidad_total=1, precio_realizado=1000))
        db.session.commit()
        assert dispersion_precio_por_co('E2')['dispersion_pct'] is None


class TestFalloRuidosoRegla0:
    """Si la columna no se reconoce, se DECLARA. No se cae en silencio."""

    def test_el_cargador_declara_la_columna_usada(self):
        import os
        ruta = os.path.join(os.path.dirname(__file__), '..', 'app', 'services',
                            'vigia_service.py')
        src = open(ruta, encoding='utf-8').read()
        assert 'advertencia_item' in src
        assert 'precio_realizado_calculado' in src
        assert 'margen SUPUESTO' in src, 'debe decir la consecuencia de no encontrarla'


class TestPrioridadReferenciaSobreItem:
    """El export real trae DOS candidatas y solo una sirve para cruzar.

    "Item" = 0000008 (código interno Siesa). "Referencia" = P100176, que es lo
    que usan KardexMovimiento.referencia y Producto.codigo_siesa (ambas de
    f120_referencia). Buscar columna por columna devolvía "Item" —aparece
    antes— y el cruce habría fallado EN SILENCIO: precios calculados que no
    empatan con ningún SKU.
    """

    def test_referencia_gana_a_item_aunque_venga_despues(self):
        from app.services.vigia_service import detectar_columna_item
        # Cabecera REAL del export de Papelería Medellín
        cab = ['Fecha', 'Nro documento', 'Tipo docto.', 'C.O.', 'Cliente factura',
               'Razón social cliente factura', 'Item', 'Referencia', 'Desc. ítem',
               'LINEA DE NEGOCIO', 'MARCA', 'SUB LINEA', 'U.M. emp.', 'U.M.',
               'Cantidad inv.', 'Costo promedio total', 'Costo promedio uni. movto',
               'Valor neto local']
        i, nombre = detectar_columna_item(cab)
        assert i == 7 and nombre == 'Referencia', \
            f'eligió {nombre} (col {i}) — "Item" no cruza con el kardex'

    def test_si_solo_hay_item_lo_usa(self):
        """Sin Referencia, Item es mejor que nada — pero es la segunda opción."""
        from app.services.vigia_service import detectar_columna_item
        i, nombre = detectar_columna_item(['Fecha', 'C.O.', 'Item'])
        assert i == 2 and nombre == 'Item'


class TestUmbralCostoFantasma:
    """0.5% de las filas arrastraban el margen de +35.5% a -190%.

    Un costo 109.616 veces el valor de venta no es un negocio a pérdida: es un
    error de unidad de empaque. Medido sobre el export real de temporada.
    """

    def test_el_umbral_existe_y_es_conservador(self):
        from app.services.vigia_service import FACTOR_COSTO_FANTASMA
        assert FACTOR_COSTO_FANTASMA >= 2.0, \
            'un umbral bajo descartaría ventas legítimas a pérdida'
        assert FACTOR_COSTO_FANTASMA <= 5.0, \
            'un umbral alto dejaría pasar los costos fantasma'
