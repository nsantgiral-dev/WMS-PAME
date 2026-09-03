"""
El Armador compraba contando las averías y el tránsito como stock vendible.

## Qué pasó

`armador_service.rop_dual` suma `StockSiesa` con `group_by(codigo_siesa)` y
**sin filtro de bodega**. Esa línea no cambió nunca — cambió lo que hay en la
tabla: un lote anterior agregó `AV1` (averías) y `TRA1` (tránsito) al universo
que se le pide a Siesa (`inventario_siesa_service._BODEGAS_SERVICIO`), y esas
filas ahora se **persisten** en `stock_siesa` igual que las de un punto de
venta.

Medido sobre las filas persistidas para un mismo SKU:

    {'NB1': 100.0, 'AV1': 40.0, 'TRA1': 25.0}
    stock_actual que ve el Armador: 165.0     ·     vendible real: 100

## Qué costaba

`posicion = stock_actual + transito - comprometido - salida_sin_conf` se infla,
y `deficit = max(0, s_objetivo - posicion)` sale corto **exactamente en las
averías más el tránsito**. El déficit es el número que arma el contenedor, y un
contenedor es irreversible 120 días (Regla 0): el error no se corrige el mes
que viene, se descubre agotado cuatro meses después.

Peor que un número inflado: es un número inflado **con lo que nunca se va a
vender**. Una avería no se despacha nunca — está en AV1 justo porque salió del
universo vendible.

Y `stock_siesa` es acumulativa (upsert sin borrado): persistidas una vez, esas
filas las devuelve `_leer_stock_de_bd` para siempre, aunque la API deje de
reportarlas.

## Y era la única de tres respuestas que las incluía

`dashboard_service` declara AVERIAS zona no vendible. `picking_service` la
excluye del FEFO. El Armador —el único de los tres que **compra**— las sumaba.
Tres respuestas a «¿cuánto hay vendible?» y la que más pesa era la equivocada.

## Detector en las dos direcciones

No alcanza con probar que el arreglo dispara. Se exige además que **no cambie
nada sobre operación sana**: un SKU con stock solo en bodegas operadas da la
misma `posicion`, el mismo `rop` y el mismo `deficit` que antes; y el stock
repartido entre varias bodegas operadas (NB1 + NC1 + PC1) se sigue sumando
completo. El arreglo excluye bodegas de servicio, **no reduce a una sola
bodega** — un guard que solo verifica que dispara prueba la mitad.
"""
import pytest


NIVEL = 0.95


def _z():
    from app.services.kardex_service import _norm_ppf
    return _norm_ppf(NIVEL)


def _demanda(**skus):
    """Payload de `demanda_descensurada` con las llaves que `rop_dual` lee."""
    return {
        ref: {
            'd_avg': d_avg,
            'sigma_d': 0.0,          # sin ruido de demanda: el ROP queda cerrado
            'dias_con_stock': 300,
            'dias_ventana': 360,
            'demanda_neta': d_avg * 300,
            'factor_censura': 1.0,
            'censurado': False,
        }
        for ref, d_avg in skus.items()
    }


@pytest.fixture
def sin_kardex(monkeypatch):
    """Inyecta la demanda sin tocar el kardex — acá se prueba de qué bodegas
    sale el stock, no la descensura."""
    from app.services.kardex_service import KardexService

    def _fijar(demanda):
        monkeypatch.setattr(KardexService, 'demanda_descensurada',
                            lambda *a, **k: demanda)
    return _fijar


def _producto(db, ref, origen):
    from app.models.producto import Producto
    p = Producto(codigo=ref, nombre=ref, codigo_siesa=ref,
                 origen=origen, activo=True)
    db.session.add(p)
    return p


def _stock(db, ref, existencia, comprometido=0, salida_sin_conf=0,
           bodega='NB1'):
    from app.models.stock_siesa import StockSiesa
    db.session.add(StockSiesa(
        bodega=bodega, codigo_siesa=ref, existencia=existencia,
        comprometido=comprometido, salida_sin_conf=salida_sin_conf))


def _fila(resultado, regimen, ref):
    return next(f for f in resultado[regimen]['items'] if f['referencia'] == ref)


# ── Los números, calculados aparte ──────────────────────────────────────────
# Con sigma_d = 0:
#   sigma_LTD = sqrt(LT*0 + d^2*sigma_LT^2) = d * sigma_LT
#   ROP       = d*LT + z*d*sigma_LT
# Con d = 6 y LT nacional: ROP = 30 + z*12 ≈ 49.7 → 50.

D_AVG = 6.0
VENDIBLE = 100.0     # NB1 — lo único que se puede despachar
AVERIADO = 40.0      # AV1 — no se vende nunca
EN_TRANSITO = 25.0   # TRA1 — el limbo entre el STS y el ETS


def _rop_nacional_esperado(d_avg=D_AVG):
    from app.services.armador_service import LT_NACIONAL_DIAS, SIGMA_LT_NACIONAL
    return d_avg * LT_NACIONAL_DIAS + _z() * d_avg * SIGMA_LT_NACIONAL


def _s_objetivo_esperado(d_avg=D_AVG):
    from app.services.armador_service import (
        LT_CHINA_DIAS, SIGMA_LT_CHINA, R_CHINA_DIAS,
        MAX_COBERTURA_RELLENO_DIAS)
    s = d_avg * (LT_CHINA_DIAS + R_CHINA_DIAS) + _z() * d_avg * SIGMA_LT_CHINA
    return min(s, d_avg * MAX_COBERTURA_RELLENO_DIAS)


class TestLaAveriaNoEsStockVendible:
    """DISPARA: con filas en AV1/TRA1, la posición y el déficit cambian."""

    def test_el_stock_actual_no_incluye_averias_ni_transito(
            self, app, db, sin_kardex):
        """El caso medido, tal cual: NB1=100, AV1=40, TRA1=25 → 165 hoy, 100 con
        el arreglo."""
        _producto(db, 'NAC-SERV', 'NACIONAL')
        _stock(db, 'NAC-SERV', VENDIBLE, bodega='NB1')
        _stock(db, 'NAC-SERV', AVERIADO, bodega='AV1')
        _stock(db, 'NAC-SERV', EN_TRANSITO, bodega='TRA1')
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-SERV': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-SERV')

        assert fila['rop'] == round(_rop_nacional_esperado()), \
            'el ROP cambió: este test dejó de medir lo que dice medir'
        assert fila['stock_actual'] == round(VENDIBLE), (
            f"stock_actual = {fila['stock_actual']}: el Armador está contando "
            f'las {AVERIADO:.0f} unidades de AV1 y/o las {EN_TRANSITO:.0f} de '
            'TRA1 como si se pudieran despachar')
        assert fila['posicion'] == round(VENDIBLE), (
            f"posición {fila['posicion']} en vez de {VENDIBLE:.0f} — se compra "
            'sobre mercancía que nunca se va a vender')

    def test_el_deficit_del_contenedor_crece_al_excluir_servicio(
            self, app, db, sin_kardex):
        """El lado caro: `deficit` es lo que arma el contenedor.

        Dos SKU China idénticos en NB1. Al segundo se le agregan averías y
        tránsito. Si el Armador los sumara, pediría MENOS de un SKU que tiene
        exactamente el mismo inventario vendible — y el contenedor sale corto
        en esas unidades, irreversible 120 días.
        """
        _producto(db, 'CHI-LIMPIO', 'CHINA')
        _producto(db, 'CHI-SERV', 'CHINA')
        _stock(db, 'CHI-LIMPIO', VENDIBLE, bodega='NB1')
        _stock(db, 'CHI-SERV', VENDIBLE, bodega='NB1')
        _stock(db, 'CHI-SERV', AVERIADO, bodega='AV1')
        _stock(db, 'CHI-SERV', EN_TRANSITO, bodega='TRA1')
        db.session.commit()
        sin_kardex(_demanda(**{'CHI-LIMPIO': D_AVG, 'CHI-SERV': D_AVG}))

        from app.services.armador_service import ArmadorService
        r = ArmadorService.rop_dual(NIVEL)
        limpio = _fila(r, 'china', 'CHI-LIMPIO')
        servicio = _fila(r, 'china', 'CHI-SERV')

        assert servicio['deficit'] == limpio['deficit'], (
            'dos SKU con el mismo inventario vendible piden distinto: '
            f"{servicio['deficit']} vs {limpio['deficit']}. El contenedor sale "
            f'corto en {limpio["deficit"] - servicio["deficit"]} unidades '
            'porque las averías se contaron como stock')
        assert servicio['deficit'] == round(_s_objetivo_esperado() - VENDIBLE)

    def test_bc99_y_las_duplicadas_tampoco_cuentan(self, app, db, sin_kardex):
        """Lista blanca, no lista negra.

        `BC99` (Bodega Contratación, que el WMS no usa) y `FD1`/`ND1`/`PD1`
        («DUPLICADA» en Siesa) no están en la lista negra de nadie — y con una
        lista negra pasarían derecho. Este test es el que separa las dos
        políticas: si alguien cambia la lista blanca por un `notin_(['AV1',
        'TRA1'])`, se pone rojo.
        """
        _producto(db, 'NAC-OTRAS', 'NACIONAL')
        _stock(db, 'NAC-OTRAS', VENDIBLE, bodega='NB1')
        _stock(db, 'NAC-OTRAS', 30.0, bodega='BC99')
        _stock(db, 'NAC-OTRAS', 15.0, bodega='FD1')
        _stock(db, 'NAC-OTRAS', 15.0, bodega='ND1')
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-OTRAS': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-OTRAS')
        assert fila['posicion'] == round(VENDIBLE), (
            f"posición {fila['posicion']}: entró stock de una bodega que el "
            'WMS no opera. Una lista negra deja pasar la bodega de servicio '
            'que alguien agregue mañana')

    def test_el_comprometido_de_servicio_tampoco_entra(self, app, db, sin_kardex):
        """Las cuatro columnas salen de la MISMA consulta: si la existencia de
        AV1 queda fuera y su comprometido entra, el descuento se aplica sobre
        un stock que no está — la posición sale por debajo de la real y se
        compra de más. Un filtro parcial es peor que ninguno."""
        _producto(db, 'NAC-COMPSERV', 'NACIONAL')
        _stock(db, 'NAC-COMPSERV', VENDIBLE, comprometido=20.0, bodega='NB1')
        _stock(db, 'NAC-COMPSERV', AVERIADO, comprometido=35.0,
               salida_sin_conf=5.0, bodega='AV1')
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-COMPSERV': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-COMPSERV')
        assert fila['comprometido'] == 20, (
            f"comprometido {fila['comprometido']}: entró el de AV1")
        assert fila['salida_sin_conf'] == 0, (
            f"salida_sin_conf {fila['salida_sin_conf']}: entró el de AV1")
        assert fila['posicion'] == 80


class TestNoCambiaNadaSobreOperacionSana:
    """LA OTRA DIRECCIÓN. Los seis guards en verde de la auditoría del
    2026-08-15 tenían el test de «no dispara cuando está sano»; ninguno tenía
    el de «dispara cuando está roto». Acá hacen falta los dos, y este es el que
    prueba que el arreglo no se comió stock legítimo."""

    def test_un_sku_solo_en_bodegas_operadas_no_se_mueve(
            self, app, db, sin_kardex):
        """Mismos números que antes del arreglo, calculados a mano."""
        _producto(db, 'NAC-SANO', 'NACIONAL')
        _producto(db, 'CHI-SANO', 'CHINA')
        _stock(db, 'NAC-SANO', VENDIBLE, comprometido=20.0,
               salida_sin_conf=10.0, bodega='NB1')
        _stock(db, 'CHI-SANO', VENDIBLE, comprometido=20.0,
               salida_sin_conf=10.0, bodega='NB1')
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-SANO': D_AVG, 'CHI-SANO': D_AVG}))

        from app.services.armador_service import ArmadorService
        r = ArmadorService.rop_dual(NIVEL)
        nac = _fila(r, 'nacional', 'NAC-SANO')
        chi = _fila(r, 'china', 'CHI-SANO')

        assert nac['stock_actual'] == 100
        assert nac['comprometido'] == 20
        assert nac['salida_sin_conf'] == 10
        assert nac['posicion'] == 70
        assert nac['rop'] == round(_rop_nacional_esperado())
        assert nac['bajo_rop'] is False, '70 contra un ROP de 50 no está bajo'
        assert chi['posicion'] == 70
        assert chi['deficit'] == round(_s_objetivo_esperado() - 70), (
            'el déficit de un SKU sano cambió: el arreglo movió números de '
            'operación normal, no solo de las bodegas de servicio')

    def test_el_stock_repartido_entre_operadas_se_suma_completo(
            self, app, db, sin_kardex):
        """El arreglo excluye SERVICIO, no reduce a una sola bodega.

        Un filtro `== 'NB1'` pasaría todos los tests de arriba y dejaría
        invisible el stock de las otras nueve sedes: el Armador compraría para
        reponer inventario que ya está en Pitalito.
        """
        _producto(db, 'NAC-REPARTIDO', 'NACIONAL')
        _stock(db, 'NAC-REPARTIDO', 40.0, comprometido=5.0, bodega='NB1')
        _stock(db, 'NAC-REPARTIDO', 35.0, comprometido=5.0, bodega='NC1')
        _stock(db, 'NAC-REPARTIDO', 25.0, salida_sin_conf=10.0, bodega='PC1')
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-REPARTIDO': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-REPARTIDO')

        assert fila['stock_actual'] == 100, (
            f"stock_actual {fila['stock_actual']}: se perdió el stock de "
            'alguna sede. Excluir servicio no es quedarse con una bodega')
        assert fila['comprometido'] == 10
        assert fila['salida_sin_conf'] == 10
        assert fila['posicion'] == 80

    def test_las_diez_operadas_cuentan_una_por_una(self, app, db, sin_kardex):
        """Canario de la lista blanca: si alguien la recorta, este cae.

        NS2 es el caso que ya costó una vez — es bodega de parqueo de
        licitaciones, no punto de venta, y sacarla de las listas hizo invisible
        su inventario (6 SKU / 121 und). Acá tiene que contar: tiene stock.
        """
        from app.services.inventario_siesa_service import _BODEGAS_PV
        _producto(db, 'NAC-DIEZ', 'NACIONAL')
        for i, bod in enumerate(_BODEGAS_PV):
            _stock(db, 'NAC-DIEZ', 10.0, bodega=bod)
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-DIEZ': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-DIEZ')
        assert fila['stock_actual'] == 10 * len(_BODEGAS_PV), (
            f"stock_actual {fila['stock_actual']} con 10 unidades en cada una "
            f'de las {len(_BODEGAS_PV)} bodegas operadas — falta alguna en la '
            'lista blanca')


class TestLaListaBlancaEsLaDeLasOperadas:
    """El Armador no puede tener su propia lista de bodegas.

    Tres `_BODEGA_CO_MAP` con 10, 9 y 8 entradas ya costaron una vez: mientras
    haya copias, una diverge. La lista blanca del Armador tiene que ser la
    constante que ya existe y que `tests/test_bodegas_coherentes.py` vigila
    contra el maestro de Siesa.
    """

    def test_el_armador_no_escribe_su_propia_lista_de_bodegas(self):
        """Por AST, no por texto: los detectores de texto de este repo se
        atraparon en sus propios docstrings siete veces en una semana."""
        import ast
        from pathlib import Path
        import re

        ruta = (Path(__file__).resolve().parents[1] / 'app' / 'services'
                / 'armador_service.py')
        arbol = ast.parse(ruta.read_text(encoding='utf-8'))
        forma_bodega = re.compile(r'^[A-Z]{2,3}\d{1,2}$')

        literales = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.List, ast.Tuple, ast.Set)):
                continue
            vals = [e.value for e in nodo.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(vals) >= 3 and all(forma_bodega.match(v) for v in vals):
                literales.append(f'línea {nodo.lineno}: {vals}')

        assert not literales, (
            '\nHay una lista de bodegas escrita a mano en armador_service.py:\n'
            + '\n'.join(f'  · {x}' for x in literales)
            + '\n\nUsar `inventario_siesa_service._BODEGAS_PV`. Una copia '
              'diverge sin que nadie lo note.')

    def test_la_lista_blanca_es_exactamente_las_operadas(self):
        """El otro lado del detector de arriba: que NO haya lista propia no
        prueba que use la correcta. Se compara contra el maestro escrito a
        mano — un test que importa la constante que verifica no verifica nada.
        """
        from app.services.inventario_siesa_service import _BODEGAS_PV
        assert set(_BODEGAS_PV) == {
            'NB1', 'NS1', 'NS2', 'NC1', 'FC1', 'PC1', 'PT1', 'FF1', 'FN1', 'FP1',
        }, ('`_BODEGAS_PV` dejó de ser el conjunto de bodegas operadas: el '
            'Armador la usa como lista blanca y el cambio le mueve la compra')

    def test_las_de_servicio_no_estan_en_la_lista_blanca(self):
        """AV1/TRA1 viven en `_BODEGAS_SERVICIO`, aparte y a propósito. Si
        alguien las fusionara, el Armador volvería a comprar sobre averías sin
        que ninguna línea de este archivo cambie."""
        from app.services.inventario_siesa_service import (
            _BODEGAS_PV, _BODEGAS_SERVICIO)
        assert set(_BODEGAS_SERVICIO) == {'AV1', 'TRA1'}
        assert not (set(_BODEGAS_PV) & set(_BODEGAS_SERVICIO))
