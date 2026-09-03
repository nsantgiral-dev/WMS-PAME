"""
Dos defectos que empujan las dos hacia COMPRAR MÁS — y ninguno da error.

DEFECTO 1 — el costo promedio del kardex mezclaba todos los conceptos.
`_costos_kardex` ponderaba `costo_promedio * cantidad` sobre TODAS las filas
del SKU: saldos iniciales (699, costo viejo con cantidades enormes), ajustes
(603), ventas (501, la mayoría de las filas) y traslados (607, que aportan DOS
filas por las MISMAS unidades físicas — salida en origen y entrada en destino).
El costo promedio ponderado DE COMPRA sale del concepto de compra, y el repo ya
tiene la tabla que lo clasifica: `kardex_service.CONCEPTO_DEFINICION`.

En el caso construido de abajo el error medido es −34,1 % (659,09 en vez de
1.000). `KARDEX_PROMEDIO` es el nivel 3 de la jerarquía de costo y alimenta
Cu/Co del newsvendor: subestimar el costo infla Cu y desinfla Co, y las dos
empujan el ratio crítico hacia COMPRAR MÁS. Ese es el lado irreversible.

DEFECTO 2 — el ancla del kardex devolvía 0 en silencio.
`_obtener_saldo_actual` promete en su docstring dos fuentes (`stock_siesa` o
`ubicacion_producto`) y solo consulta una. Un SKU ausente de `stock_siesa`
ancla en 0, la reconstrucción hacia atrás lo deja con `tuvo_stock=False` todos
los días, y `tuvo_stock` es EL DENOMINADOR de la demanda descensurada → d_avg,
sigma_d, ROP, s_objetivo y el armado del contenedor. Se lee como «agotado» y
censura la demanda hacia abajo.

El guard que ya existía —`pct_negativo`/`dato_insuficiente`— mide una PROXY y
no puede verlo: solo dispara si el ancla queda demasiado BAJA y la serie se va
a negativo. Un ancla en 0 sobre un SKU con stock real produce una serie plana
en cero, indistinguible de un agotado legítimo. Aquí se ejerce en las dos
direcciones: que el contador nuevo lo declare, y que la proxy vieja siga sin
verlo (para que nadie crea que ya estaba cubierto).

Cada bloque prueba las DOS direcciones: que dispara sobre el caso roto y que
NO cambia nada sobre operación sana. Un detector que solo prueba que dispara,
prueba la mitad.
"""
import ast
from datetime import timedelta

import pytest

from tests.conftest import hoy_operativo as _hoy_operativo


@pytest.fixture
def hoy_operativo():
    """El día como lo ve el código (`dia_operativo()`), no como lo ve el contenedor.

    Regla 5: la suite corre en UTC y el código en Bogotá. Cinco horas al día en
    que `date.today()` miente, y de un solo lado — pasa local, rompe el deploy.
    """
    return _hoy_operativo()


# ═══════════════════════════════════════════════════════════════════════════
# DEFECTO 1 — el costo de compra sale del concepto de compra
# ═══════════════════════════════════════════════════════════════════════════

class TestElCostoDeCompraNoMezclaConceptos:

    def _sembrar_caso_completo(self, db, hoy, ref='MIX1'):
        """601 compra · 699 saldo inicial · 607 ida y vuelta · 501 venta.

        Las cuatro clases de fila que conviven en el kardex real de un SKU. La
        única que dice qué costó comprarlo es la 601.
        """
        from app.services.kardex_service import KardexMovimiento
        filas = [
            # LA VERDAD: 100 unidades compradas a $1.000
            dict(concepto=601, naturaleza=1, cantidad=100, costo_promedio=1000),
            # Saldo inicial: costo viejo, cantidad grande — arrastra el promedio abajo
            dict(concepto=699, naturaleza=1, cantidad=500, costo_promedio=400),
            # Traslado: DOS filas por las MISMAS 100 unidades físicas
            dict(concepto=607, naturaleza=2, cantidad=100, costo_promedio=1000),
            dict(concepto=607, naturaleza=1, cantidad=100, costo_promedio=1000),
            # Venta: en el kardex real es la mayoría de las filas
            dict(concepto=501, naturaleza=2, cantidad=80, costo_promedio=1000),
        ]
        for f in filas:
            db.session.add(KardexMovimiento(
                referencia=ref, bodega='NB1', fecha=hoy - timedelta(days=5), **f))
        db.session.commit()

    def test_el_promedio_es_el_de_compra_no_el_de_todo(self, app, db, hoy_operativo):
        """El caso construido: −34,1 % de error. 659,09 en vez de 1.000."""
        from app.services.costo_service import resolver_costos
        self._sembrar_caso_completo(db, hoy_operativo)

        r = resolver_costos(['MIX1'])['MIX1']
        assert r['fuente'] == 'KARDEX_PROMEDIO'
        assert abs(r['costo'] - 1000) < 0.01, (
            f"costo={r['costo']:.2f} — el promedio se está calculando sobre "
            f"saldos iniciales, traslados y ventas. Solo el concepto de compra "
            f"dice qué costó comprarlo."
        )

    def test_el_saldo_inicial_solo_no_es_un_costo_de_compra(self, app, db, hoy_operativo):
        """699 arrastra costo viejo. Un SKU que solo lo tiene no tiene costo de compra.

        Regla 0: declararlo como SIN_COSTO —visible en la cobertura por fuente—
        es honesto; presentarlo como KARDEX_PROMEDIO es inventar procedencia.
        """
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        db.session.add(KardexMovimiento(
            referencia='SOLOINI', bodega='NB1', fecha=hoy_operativo - timedelta(days=3),
            concepto=699, naturaleza=1, cantidad=500, costo_promedio=400))
        db.session.commit()

        r = resolver_costos(['SOLOINI'])['SOLOINI']
        assert r['fuente'] != 'KARDEX_PROMEDIO', (
            'un saldo inicial se está presentando como costo promedio de compra')
        assert r['fuente'] == 'SIN_COSTO'

    def test_el_traslado_no_cuenta_dos_veces(self, app, db, hoy_operativo):
        """607 aporta dos filas por las mismas unidades: duplica su propio peso."""
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        hoy = hoy_operativo
        db.session.add(KardexMovimiento(
            referencia='TRAS1', bodega='NB1', fecha=hoy, concepto=601,
            naturaleza=1, cantidad=100, costo_promedio=1000))
        db.session.add(KardexMovimiento(
            referencia='TRAS1', bodega='NB1', fecha=hoy, concepto=607,
            naturaleza=2, cantidad=100, costo_promedio=500))
        db.session.add(KardexMovimiento(
            referencia='TRAS1', bodega='PC1', fecha=hoy, concepto=607,
            naturaleza=1, cantidad=100, costo_promedio=500))
        db.session.commit()

        assert abs(resolver_costos(['TRAS1'])['TRAS1']['costo'] - 1000) < 0.01

    # ── La otra dirección: NO cambia nada sobre operación sana ──────────────

    def test_un_sku_solo_con_compras_da_exactamente_lo_mismo(self, app, db, hoy_operativo):
        """Kardex de puras compras (601): mismo número antes y después del arreglo.

        Se compara contra el promedio ponderado calculado sobre TODAS las filas
        —la fórmula vieja— para que la igualdad sea una afirmación y no una
        constante copiada.
        """
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos
        hoy = hoy_operativo
        lotes = [(90, 100), (10, 200), (50, 150)]
        for cant, costo in lotes:
            db.session.add(KardexMovimiento(
                referencia='SANO1', bodega='NB1', fecha=hoy, concepto=601,
                naturaleza=1, cantidad=cant, costo_promedio=costo))
        db.session.commit()

        formula_vieja = sum(c * p for c, p in lotes) / sum(c for c, _ in lotes)
        r = resolver_costos(['SANO1'])['SANO1']
        assert r['fuente'] == 'KARDEX_PROMEDIO'
        assert abs(r['costo'] - formula_vieja) < 1e-6, (
            'sobre un kardex de puras compras el arreglo no puede mover el número')

    def test_la_antiguedad_sigue_siendo_la_de_la_ultima_compra(self, app, db, hoy_operativo):
        """La bandera `anejo` se conserva — y ahora fecha la COMPRA, no la venta."""
        from app.services.kardex_service import KardexMovimiento
        from app.services.costo_service import resolver_costos, DIAS_COSTO_ANEJO
        hoy = hoy_operativo
        db.session.add(KardexMovimiento(
            referencia='ANEJO1', bodega='NB1',
            fecha=hoy - timedelta(days=DIAS_COSTO_ANEJO + 60),
            concepto=601, naturaleza=1, cantidad=10, costo_promedio=1000))
        # Venta de ayer: no rejuvenece el costo de compra
        db.session.add(KardexMovimiento(
            referencia='ANEJO1', bodega='NB1', fecha=hoy - timedelta(days=1),
            concepto=501, naturaleza=2, cantidad=1, costo_promedio=1000))
        db.session.commit()

        r = resolver_costos(['ANEJO1'])['ANEJO1']
        assert r['anejo'] is True, (
            'una venta reciente está haciendo pasar por fresco un costo de hace '
            'año y medio')
        assert r['dias_antiguedad'] >= DIAS_COSTO_ANEJO


class TestLaDevolucionAlProveedorNoEsCostoDeCompra:
    """El segundo filtro de `_costos_kardex`: concepto de compra **Y** naturaleza ENTRADA.

    El bloque de arriba prueba el filtro por CONCEPTO. Éste prueba el otro, que
    hasta ahora no tenía una sola fila que lo ejerciera: entre todas las filas
    sembradas en este archivo, las de naturaleza SALIDA eran de 607 y de 501 —
    conceptos que el filtro anterior ya descarta. **Ninguna era 601 en salida**,
    que es exactamente lo único que este filtro existe para descartar. Revertir
    la guarda de naturaleza dejaba la suite entera en verde.

    Una fila de compra en SALIDA es una **devolución al proveedor**: describe
    mercancía que se devolvió, no lo que costó reponerla. Y como la consulta
    agrupa por naturaleza, sin la guarda el grupo de salida pisa al de entrada
    en el diccionario — el costo no se promedia mal, se REEMPLAZA por el de la
    devolución.

    La dirección del error es la peligrosa, la misma que el docstring del módulo
    nombra: costo bajo → Cu inflado y Co desinflado → ratio crítico arriba →
    COMPRAR MÁS. Un contenedor son 120 días irreversibles.
    """

    def test_una_devolucion_al_proveedor_no_reemplaza_el_costo_de_compra(
            self, app, db, hoy_operativo):
        """10 und compradas a $1.000 y 90 devueltas a $100 → el costo es $1.000.

        Sin la guarda de naturaleza el resultado es **100.0**: factor 10 hacia
        abajo, y hacia el lado que empuja a comprar.
        """
        from app.services.kardex_service import (
            KardexMovimiento, CONCEPTOS_COMPRA, NATURALEZA_ENTRADA, NATURALEZA_SALIDA)
        from app.services.costo_service import resolver_costos
        compra = min(CONCEPTOS_COMPRA)
        hoy = hoy_operativo
        # La verdad: 10 unidades compradas a $1.000
        db.session.add(KardexMovimiento(
            referencia='DEVPROV', bodega='NB1', fecha=hoy - timedelta(days=2),
            concepto=compra, naturaleza=NATURALEZA_ENTRADA,
            cantidad=10, costo_promedio=1000))
        # Devolución al proveedor: MISMO concepto de compra, naturaleza SALIDA
        db.session.add(KardexMovimiento(
            referencia='DEVPROV', bodega='NB1', fecha=hoy - timedelta(days=1),
            concepto=compra, naturaleza=NATURALEZA_SALIDA,
            cantidad=90, costo_promedio=100))
        db.session.commit()

        r = resolver_costos(['DEVPROV'])['DEVPROV']
        assert r['fuente'] == 'KARDEX_PROMEDIO'
        assert abs(r['costo'] - 1000) < 0.01, (
            f"costo={r['costo']:.2f} — una devolución al proveedor (concepto de "
            f"compra en naturaleza SALIDA) está entrando al costo promedio de "
            f"compra. Subestimar el costo infla Cu, desinfla Co y empuja el "
            f"ratio crítico hacia COMPRAR MÁS.")

    def test_la_devolucion_tampoco_se_promedia_con_la_compra(
            self, app, db, hoy_operativo):
        """Ni reemplaza NI promedia: el costo de compra sale solo de las entradas.

        Se afirma contra los dos números equivocados a la vez —el de la salida
        sola (100) y el promedio de las dos (190)— para que ningún arreglo
        futuro «mejore» la guarda mezclando las dos naturalezas.
        """
        from app.services.kardex_service import (
            KardexMovimiento, CONCEPTOS_COMPRA, NATURALEZA_ENTRADA, NATURALEZA_SALIDA)
        from app.services.costo_service import resolver_costos
        compra = min(CONCEPTOS_COMPRA)
        hoy = hoy_operativo
        for nat, cant, costo in ((NATURALEZA_ENTRADA, 10, 1000),
                                 (NATURALEZA_SALIDA, 90, 100)):
            db.session.add(KardexMovimiento(
                referencia='DEVPROV2', bodega='NB1', fecha=hoy,
                concepto=compra, naturaleza=nat, cantidad=cant, costo_promedio=costo))
        db.session.commit()

        costo = resolver_costos(['DEVPROV2'])['DEVPROV2']['costo']
        mezclado = (10 * 1000 + 90 * 100) / (10 + 90)      # 190.0
        solo_salida = (90 * 100) / 90                       # 100.0
        assert abs(costo - solo_salida) > 0.01, 'el grupo de salida pisó al de entrada'
        assert abs(costo - mezclado) > 0.01, (
            'las dos naturalezas se están promediando juntas: una devolución al '
            'proveedor no dice qué costó reponer')

    # ── La otra dirección: solo salidas → sin costo de kardex, y DECLARADO ──

    def test_un_sku_con_compras_solo_en_salida_no_tiene_costo_de_kardex(
            self, app, db, hoy_operativo, caplog):
        """Todo devuelto al proveedor: cae a SIN_COSTO y se declara en el log.

        Regla 0 — el denominador tiene que verse. Lo que NO puede pasar es que
        se invente un costo con las devoluciones: sería el mismo error de arriba
        sin ninguna entrada que lo contradiga.
        """
        import logging
        from app.services.kardex_service import (
            KardexMovimiento, CONCEPTOS_COMPRA, NATURALEZA_SALIDA)
        from app.services.costo_service import resolver_costos, resumen_por_fuente
        compra = min(CONCEPTOS_COMPRA)
        db.session.add(KardexMovimiento(
            referencia='TODODEV', bodega='NB1', fecha=hoy_operativo,
            concepto=compra, naturaleza=NATURALEZA_SALIDA,
            cantidad=50, costo_promedio=100))
        db.session.commit()

        with caplog.at_level(logging.WARNING, logger='app.services.costo_service'):
            costos = resolver_costos(['TODODEV'])
        r = costos['TODODEV']

        assert r['fuente'] == 'SIN_COSTO', (
            f"fuente={r['fuente']} — un SKU cuyas únicas filas de compra son "
            f"devoluciones al proveedor no tiene costo promedio DE COMPRA")
        assert r['costo'] == 0.0, 'se inventó un costo a partir de la devolución'
        assert resumen_por_fuente(costos)['sin_costo'] == 1, (
            'el SKU tiene que ser contable en la cobertura por fuente, no '
            'desaparecer en silencio')

        avisos = [x for x in caplog.messages if 'SOLO en salida' in x]
        assert avisos, (
            'la referencia se fue sin costo y sin dejar rastro: el módulo promete '
            'declararlo en el log en vez de irse en silencio')
        assert 'TODODEV' in avisos[0]

    def test_con_maestro_cae_a_maestro_y_no_a_la_devolucion(
            self, app, db, hoy_operativo):
        """La jerarquía sigue: sin kardex de compra manda `Producto.precio_compra`.

        Y el número del maestro tiene que ganarle al de la devolución — si
        alguna vez saliera 100, la guarda volvió a caerse.
        """
        from app.models.producto import Producto
        from app.services.kardex_service import (
            KardexMovimiento, CONCEPTOS_COMPRA, NATURALEZA_SALIDA)
        from app.services.costo_service import resolver_costos
        compra = min(CONCEPTOS_COMPRA)
        db.session.add(Producto(codigo='DEVMAE', nombre='Devuelto entero',
                                codigo_siesa='DEVMAE', activo=True,
                                precio_compra=1000))
        db.session.add(KardexMovimiento(
            referencia='DEVMAE', bodega='NB1', fecha=hoy_operativo,
            concepto=compra, naturaleza=NATURALEZA_SALIDA,
            cantidad=90, costo_promedio=100))
        db.session.commit()

        r = resolver_costos(['DEVMAE'])['DEVMAE']
        assert r['fuente'] == 'MAESTRO'
        assert abs(r['costo'] - 1000) < 0.01

    def test_una_compra_sana_no_cambia_por_tener_la_guarda(
            self, app, db, hoy_operativo):
        """La otra dirección del detector: sin devoluciones, el número es el de siempre."""
        from app.services.kardex_service import (
            KardexMovimiento, CONCEPTOS_COMPRA, NATURALEZA_ENTRADA)
        from app.services.costo_service import resolver_costos
        compra = min(CONCEPTOS_COMPRA)
        lotes = [(10, 1000), (90, 100)]     # las MISMAS cifras, las dos en ENTRADA
        for cant, costo in lotes:
            db.session.add(KardexMovimiento(
                referencia='SINDEV', bodega='NB1', fecha=hoy_operativo,
                concepto=compra, naturaleza=NATURALEZA_ENTRADA,
                cantidad=cant, costo_promedio=costo))
        db.session.commit()

        esperado = sum(c * p for c, p in lotes) / sum(c for c, _ in lotes)
        r = resolver_costos(['SINDEV'])['SINDEV']
        assert r['fuente'] == 'KARDEX_PROMEDIO'
        assert abs(r['costo'] - esperado) < 1e-6, (
            'la guarda de naturaleza está descartando entradas legítimas')


class TestLaListaDeConceptosNoSeInventaDosVeces:
    """Una política, una función. La tabla de conceptos vive en kardex_service."""

    def test_existe_el_conjunto_de_conceptos_de_compra(self, app):
        from app.services.kardex_service import CONCEPTO_DEFINICION, CONCEPTOS_COMPRA
        assert CONCEPTOS_COMPRA, 'sin conjunto declarado, cada módulo inventa el suyo'
        assert set(CONCEPTOS_COMPRA) <= set(CONCEPTO_DEFINICION), (
            'un concepto de compra que no está en CONCEPTO_DEFINICION es un '
            'concepto que la compuerta de conceptos desconocidos no vigila')

    def test_ningun_concepto_de_compra_cuenta_como_demanda(self, app):
        """Compra es logística, no demanda. Si algún día se cruzan, hay que mirarlo."""
        from app.services.kardex_service import CONCEPTO_DEFINICION, CONCEPTOS_COMPRA
        for c in CONCEPTOS_COMPRA:
            assert CONCEPTO_DEFINICION[c][0] is False, (
                f'concepto {c} cuenta como demanda Y como compra')

    def test_costo_service_no_escribe_su_propia_lista(self, app):
        """Por AST: ningún número de concepto literal en costo_service.py.

        Un detector de texto se atraparía en este mismo docstring — ya pasó
        siete veces en una semana en este repo.
        """
        import app.services.costo_service as mod
        with open(mod.__file__, encoding='utf-8') as fh:
            arbol = ast.parse(fh.read())
        from app.services.kardex_service import CONCEPTO_DEFINICION
        conceptos = set(CONCEPTO_DEFINICION)
        literales = [n.value for n in ast.walk(arbol)
                     if isinstance(n, ast.Constant) and isinstance(n.value, int)
                     and n.value in conceptos]
        assert not literales, (
            f'costo_service.py escribe los conceptos {sorted(set(literales))} a mano. '
            f'La tabla es CONCEPTO_DEFINICION en kardex_service — una lista copiada '
            f'diverge de la original y esa vez nadie estará comparando.')


# ═══════════════════════════════════════════════════════════════════════════
# DEFECTO 2 — el ancla ausente se declara
# ═══════════════════════════════════════════════════════════════════════════

class TestElAnclaAusenteSeDeclara:

    def test_un_sku_sin_fila_en_stock_siesa_queda_contado(self, app, db, hoy_operativo):
        """Ancla en 0 → serie plana en cero → demanda censurada hacia abajo.

        El denominador tiene que ser visible: el módulo ya declara
        `factor_censura` y `censurado` por fila. Este es el mismo patrón.
        """
        from app.services.kardex_service import KardexMovimiento, KardexService
        hoy = hoy_operativo
        db.session.add(KardexMovimiento(
            fecha=hoy, tipo_docto='RM', bodega='NB1', referencia='SINANCLA',
            concepto=501, naturaleza=2, cantidad=10, costo_promedio=50))
        db.session.commit()

        r = KardexService.reconstruir_stock_diario('NB1')
        assert 'refs_sin_ancla' in r, (
            'el return 0 del ancla no lo declara nadie: un SKU sin fila en '
            'stock_siesa se lee como agotado todos los días')
        assert r['refs_sin_ancla']['cantidad'] == 1
        assert 'SINANCLA' in [d['referencia'] for d in r['refs_sin_ancla']['detalle']]

    def test_la_proxy_vieja_no_puede_verlo(self, app, db, hoy_operativo):
        """`pct_negativo` mide otra cosa: sobre un ancla en 0 no da un negativo.

        Se afirma para que nadie concluya que el guard existente ya cubría esto
        y borre el contador nuevo por redundante.
        """
        from app.services.kardex_service import KardexMovimiento, KardexService
        db.session.add(KardexMovimiento(
            fecha=hoy_operativo, tipo_docto='EA', bodega='NB1', referencia='PLANA',
            concepto=601, naturaleza=1, cantidad=40, costo_promedio=50))
        db.session.commit()

        r = KardexService.reconstruir_stock_diario('NB1')
        assert r['reporte_calidad']['total_con_negativos'] == 0
        assert r['reporte_calidad']['dato_insuficiente'] == 0
        assert r['refs_sin_ancla']['cantidad'] == 1, (
            'la proxy de negativos no dispara y el ancla ausente tampoco se '
            'declara: el SKU desaparece del canal por completo')

    # ── La otra dirección: un SKU anclado no entra, y su serie no cambia ────

    def test_un_sku_anclado_no_entra_en_el_contador(self, app, db, hoy_operativo):
        from app.services.kardex_service import KardexMovimiento, KardexService
        from app.models.stock_siesa import StockSiesa
        hoy = hoy_operativo
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='CONANCLA',
                                  existencia=100, comprometido=0, salida_sin_conf=0))
        db.session.add(KardexMovimiento(
            fecha=hoy, tipo_docto='RM', bodega='NB1', referencia='CONANCLA',
            concepto=501, naturaleza=2, cantidad=10, costo_promedio=50))
        db.session.commit()

        r = KardexService.reconstruir_stock_diario('NB1')
        assert r['refs_sin_ancla']['cantidad'] == 0
        assert r['refs_sin_ancla']['detalle'] == []

    def test_la_serie_de_un_sku_anclado_no_se_mueve(self, app, db, hoy_operativo):
        """Declarar no reconstruye distinto: los mismos números de siempre.

        Ancla 100, entrada de 30 hoy y salida de 10 ayer →
        hoy cierra en 100; ayer cierra en 100−30 = 70.
        """
        from app.services.kardex_service import (
            KardexMovimiento, KardexService, StockDiario)
        from app.models.stock_siesa import StockSiesa
        hoy = hoy_operativo
        ayer = hoy - timedelta(days=1)
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='SERIE1',
                                  existencia=100, comprometido=0, salida_sin_conf=0))
        db.session.add(KardexMovimiento(
            fecha=hoy, tipo_docto='EA', bodega='NB1', referencia='SERIE1',
            concepto=601, naturaleza=1, cantidad=30, costo_promedio=50))
        db.session.add(KardexMovimiento(
            fecha=ayer, tipo_docto='RM', bodega='NB1', referencia='SERIE1',
            concepto=501, naturaleza=2, cantidad=10, costo_promedio=50))
        db.session.commit()

        r = KardexService.reconstruir_stock_diario('NB1')
        assert r['referencias_procesadas'] == 1
        assert r['dias_generados'] == 2

        def _cierre(f):
            fila = StockDiario.query.filter_by(
                referencia='SERIE1', bodega='NB1', fecha=f).first()
            return (float(fila.stock_cierre), fila.tuvo_stock)

        assert _cierre(hoy) == (100.0, True)
        assert _cierre(ayer) == (70.0, True)

    def test_el_ancla_declara_su_fuente(self, app, db, hoy_operativo):
        """«No hay ancla» necesita su propio valor: 0 no puede significar las dos cosas.

        Un saldo real de 0 y la ausencia de fila son hechos distintos y hoy
        devolvían lo mismo.
        """
        from app.services.kardex_service import KardexService
        from app.models.stock_siesa import StockSiesa
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='CERO_REAL',
                                  existencia=0, comprometido=0, salida_sin_conf=0))
        db.session.commit()

        saldo_real, fuente_real = KardexService._obtener_saldo_actual('CERO_REAL', 'NB1')
        saldo_aus, fuente_aus = KardexService._obtener_saldo_actual('NO_EXISTE', 'NB1')
        assert (saldo_real, saldo_aus) == (0.0, 0.0)
        assert fuente_real == 'STOCK_SIESA'
        assert fuente_aus is None, 'la ausencia de ancla no se distingue de un saldo 0'
