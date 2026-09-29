"""
Compras — tanda G (2026-09-29): una lectura de la venta por corrida y el
resultado del ROP guardado por día, invalidado al llegar datos nuevos.

La clase: **el motor leía la venta diaria entera más de una vez por pedido
y la recalculaba en cada clic.** `rop_dual` leía 12 meses y, aparte, ~420
días para el horizonte (filas crudas día × bodega × SKU a Python); la bandeja,
el porqué de un SKU, el contenedor y la temporada lo llamaban entero. Con la
mitad del volumen de producción: 24 s y 600 MB solo la demanda (validación del
2026-09-27, P1-F).

Ahora: `kardex_service.lectura_demanda` (una lectura, a nivel red sumada en
SQL por referencia y día) la comparten la demanda de 12 meses y la del
horizonte; `rop_dual` guarda su resultado por proceso con la clave
`(nivel, día, sello_de_datos_rop())`.
"""
import ast
import pathlib
from datetime import timedelta

import pytest

from app.utils.fecha import dia_operativo
from tests.test_demanda_horizonte import _cubrir, _serie, _vende

RAIZ = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def mundo(app, db, monkeypatch):
    for v in ('COMPRAS_CACHE_ROP', 'DEMANDA_BODEGAS_PROYECTO'):
        monkeypatch.delenv(v, raising=False)
    _cubrir(db, 430)
    _vende(db, 'A', _serie(lambda d: 10))
    _vende(db, 'B', _serie(lambda d: 30 if 337 <= d <= 364 else 10))
    _vende(db, 'B', {100: 999}, bod='NS2')
    from app.models.stock_siesa import StockSiesa
    for ref in ('A', 'B'):
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa=ref, existencia=50,
                                  comprometido=0, salida_sin_conf=0))
    db.session.commit()
    app.config['COMPRAS_CACHE_ROP_EN_TESTS'] = True
    from app.services import armador_service, kardex_service
    armador_service._CACHE_ROP.clear()
    kardex_service._CACHE_LECTURA.clear()
    yield
    app.config['COMPRAS_CACHE_ROP_EN_TESTS'] = False
    armador_service._CACHE_ROP.clear()
    kardex_service._CACHE_LECTURA.clear()


def _contar_calculos(monkeypatch):
    """Cuántas veces se recalcula la demanda del horizonte (una por ROP que
    no sale de la caché)."""
    from app.services import kardex_service
    real = kardex_service.demanda_para_horizonte
    n = {'calculos': 0}

    def contar(*a, **k):
        n['calculos'] += 1
        return real(*a, **k)
    monkeypatch.setattr(kardex_service, 'demanda_para_horizonte', contar)
    return n


def _contar_lecturas(monkeypatch):
    from app.services import kardex_service
    real = kardex_service.serie_demanda
    n = {'lecturas': 0}

    def contar(*a, **k):
        n['lecturas'] += 1
        return real(*a, **k)
    monkeypatch.setattr(kardex_service, 'serie_demanda', contar)
    return n


class TestUnaLectura:

    def test_rop_dual_lee_la_venta_una_vez(self, app, db, mundo, monkeypatch):
        from app.services.armador_service import ArmadorService
        n = _contar_lecturas(monkeypatch)
        app.config['COMPRAS_CACHE_ROP_EN_TESTS'] = False
        r = ArmadorService.rop_dual()
        assert n['lecturas'] == 1, 'la demanda de 12 meses y la del horizonte, de una lectura'
        filas = {f['referencia']: f for f in r['nacional']['items']}
        assert filas['B']['d_avg_diaria'] == pytest.approx(30.0)

    def test_la_suma_en_sql_da_lo_mismo_que_por_bodega(self, app, db, mundo):
        """La lectura de red (sumada en la base) contra la de bodega sumada a
        mano: misma venta repetible, la de NS2 aparte."""
        from app.services.kardex_service import serie_demanda
        from app.services.demanda_fuentes import fuente_de_demanda
        f = fuente_de_demanda()
        h, d = dia_operativo() - timedelta(days=1), dia_operativo() - timedelta(days=430)
        red = serie_demanda(d, h, 'red', fuente=f)
        bod = serie_demanda(d, h, 'bodega', fuente=f)
        for ref in ('A', 'B'):
            suma_b = sum(sum(v['por_dia'].values()) for k, v in bod.items()
                         if k.split('|')[0] == ref)
            assert sum(red[ref]['por_dia'].values()) == pytest.approx(suma_b)
        assert red['B']['venta_proyecto'] == 999

    def test_la_lectura_compartida_da_lo_mismo_que_la_propia(self, app, db, mundo):
        from app.services.kardex_service import KardexService, lectura_demanda
        propia = KardexService.demanda_descensurada(12, 'red')
        compartida = KardexService.demanda_descensurada(12, 'red', lectura=lectura_demanda())
        for ref in ('A', 'B'):
            assert compartida[ref]['d_avg'] == pytest.approx(propia[ref]['d_avg'])
            assert compartida[ref]['sigma_d'] == pytest.approx(propia[ref]['sigma_d'])


class TestElROPSeGuardaPorDia:

    def test_la_segunda_vez_sale_de_la_cache(self, app, db, mundo, monkeypatch):
        from app.services.armador_service import ArmadorService
        n = _contar_lecturas(monkeypatch)
        a = ArmadorService.rop_dual()
        b = ArmadorService.rop_dual()
        assert n['lecturas'] == 1 and b['cache']['de_cache'] is True
        assert b['nacional']['items'] == a['nacional']['items']

    @pytest.mark.parametrize('cambio', ['existencias', 'decision', 'oc', 'registro', 'venta',
                                        'contenedor', 'variable', 'catalogo', 'registro_ajeno'])
    def test_un_dato_nuevo_la_invalida(self, app, db, mundo, monkeypatch, cambio):
        from datetime import datetime
        from app.services.armador_service import ArmadorService
        n = _contar_calculos(monkeypatch)
        lect = _contar_lecturas(monkeypatch)
        ArmadorService.rop_dual()
        if cambio == 'existencias':
            from app.models.stock_siesa import StockSiesa
            StockSiesa.query.filter_by(codigo_siesa='A').update({'existencia': 49})
        elif cambio == 'decision':
            from app.models.decision_compra import DecisionCompra
            db.session.add(DecisionCompra(referencia='A', accion='PEDIDO', cantidad_decidida=5,
                                          dia=dia_operativo(), vigente_hasta=dia_operativo(),
                                          creada_en=datetime.utcnow()))
        elif cambio == 'oc':
            from app.models.compras_fuentes import OcLineaSiesa
            db.session.add(OcLineaSiesa(rowid_linea=1, referencia='A', bodega='NB1',
                                        pendiente_base=5, abierta=True))
        elif cambio == 'registro':
            from app.models.registro_sync import RegistroSync
            db.session.add(RegistroSync(tipo='demanda_siesa'))
        elif cambio == 'registro_ajeno':
            from app.models.registro_sync import RegistroSync
            db.session.add(RegistroSync(tipo='compras_oc'))
        elif cambio == 'venta':
            from app.models.demanda_siesa import DemandaDiaCubierto
            db.session.query(DemandaDiaCubierto).filter(
                DemandaDiaCubierto.fecha == dia_operativo() - timedelta(days=430)).delete()
        elif cambio == 'contenedor':
            from app.models.importacion import Contenedor
            db.session.add(Contenedor(numero='C1', estado='NAVEGANDO'))
        elif cambio == 'variable':
            monkeypatch.setenv('ROP_CICLO_NACIONAL_DIAS', '14')
        elif cambio == 'catalogo':
            from app.models.producto import Producto
            db.session.add(Producto(codigo='N', nombre='N', codigo_siesa='N', origen='CHINA',
                                    activo=True))
        db.session.commit()
        r = ArmadorService.rop_dual()
        assert n['calculos'] == 2, f'{cambio}: el ROP guardado siguió vivo'
        assert 'cache' not in r
        # La venta se vuelve a leer SOLO si llegó venta nueva: el sync de OCs,
        # las existencias o una decisión no la releen.
        assert lect['lecturas'] == (2 if cambio in ('registro', 'venta') else 1), cambio

    def test_apagada_por_variable(self, app, db, mundo, monkeypatch):
        from app.services.armador_service import ArmadorService
        monkeypatch.setenv('COMPRAS_CACHE_ROP', 'false')
        n = _contar_lecturas(monkeypatch)
        ArmadorService.rop_dual()
        ArmadorService.rop_dual()
        assert n['lecturas'] == 2

    def test_otro_dia_otra_cuenta(self, app, db, mundo, monkeypatch):
        from app.services import armador_service
        n = _contar_calculos(monkeypatch)
        armador_service.ArmadorService.rop_dual()
        manana = dia_operativo() + timedelta(days=1)
        monkeypatch.setattr(armador_service, '_dia_operativo', lambda: manana)
        armador_service.ArmadorService.rop_dual()
        assert n['calculos'] == 2


class TestLaLecturaSeGuardaHastaQueLlegueVenta:
    """La lectura de la venta (lo caro: ~13 s en Postgres a volumen de
    producción) se guarda hasta que llegue venta nueva. Un sync de OCs o de
    existencias recalcula el ROP pero no vuelve a leer 420 días de venta."""

    def test_sobrevive_a_oc_y_existencias(self, app, db, mundo):
        from app.services import kardex_service
        from app.models.stock_siesa import StockSiesa
        from app.models.compras_fuentes import OcLineaSiesa
        a = kardex_service.lectura_demanda()
        StockSiesa.query.filter_by(codigo_siesa='A').update({'existencia': 1})
        db.session.add(OcLineaSiesa(rowid_linea=7, referencia='A', bodega='NB1',
                                    pendiente_base=5, abierta=True))
        db.session.commit()
        assert kardex_service.lectura_demanda() is a

    def test_la_venta_nueva_la_invalida(self, app, db, mundo):
        from app.services import kardex_service
        from app.models.registro_sync import RegistroSync
        a = kardex_service.lectura_demanda()
        db.session.add(RegistroSync(tipo='demanda_siesa'))
        db.session.commit()
        assert kardex_service.lectura_demanda() is not a

    def test_otra_fuente_otra_lectura(self, app, db, mundo, monkeypatch):
        from app.services import kardex_service
        a = kardex_service.lectura_demanda()
        monkeypatch.setenv('DEMANDA_BODEGAS_PROYECTO', 'NS2,NB1')
        assert kardex_service.lectura_demanda() is not a

    def test_apagada_en_tests_por_defecto(self, app, db, mundo):
        from app.services import kardex_service
        app.config['COMPRAS_CACHE_ROP_EN_TESTS'] = False
        assert kardex_service.lectura_demanda() is not kardex_service.lectura_demanda()


class TestUnaLecturaPorAST:
    """`rop_dual` no llama a `serie_demanda` y pasa la lectura a las dos
    demandas: si alguien vuelve a leer por su cuenta, se ve acá."""

    def _rop(self):
        src = (RAIZ / 'app/services/armador_service.py').read_text(encoding='utf-8')
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.FunctionDef) and n.name == 'rop_dual':
                return n
        raise AssertionError('no encontré rop_dual')

    def _llamadas(self, fn):
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
                out.append((nombre, {k.arg for k in n.keywords}))
        return out

    def test_una_lectura_compartida(self):
        ll = self._llamadas(self._rop())
        nombres = [x[0] for x in ll]
        assert nombres.count('lectura_demanda') == 1
        assert 'serie_demanda' not in nombres
        kw = dict((x[0], x[1]) for x in ll)
        assert 'lectura' in kw['demanda_descensurada']
        assert 'serie' in kw['demanda_para_horizonte']

    def test_el_detector_ve_una_segunda_lectura(self):
        fn = ast.parse('def rop_dual():\n    a = lectura_demanda()\n'
                       '    b = serie_demanda(1, 2)\n    KardexService.demanda_descensurada(12)\n')
        nombres = [x[0] for x in self._llamadas(fn)]
        assert 'serie_demanda' in nombres
        kw = dict(self._llamadas(fn))
        assert 'lectura' not in kw['demanda_descensurada']
