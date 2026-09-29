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
horizonte. Esa demanda la calcula el worker y se guarda (`compras_rop`,
m052comprasg); la bandeja la lee y le suma la posición de ahora. Ningún
request calcula lo caro.
"""
import ast
import json
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
    from app.services import kardex_service
    kardex_service._CACHE_LECTURA.clear()
    yield
    app.config['COMPRAS_CACHE_ROP_EN_TESTS'] = False
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

    def test_por_tramos_da_lo_mismo_que_de_una_vez(self, app, db, mundo, monkeypatch):
        """La lectura de red va por tramos de fechas (cada sentencia bajo el
        corte de 25 s de producción): ningún día se pierde ni se cuenta dos
        veces en el borde de un tramo."""
        from app.services import kardex_service
        from app.services.demanda_fuentes import fuente_de_demanda
        f = fuente_de_demanda()
        h, d = dia_operativo() - timedelta(days=1), dia_operativo() - timedelta(days=430)
        monkeypatch.setattr(kardex_service, 'DIAS_POR_TRAMO_DE_LECTURA', 10_000)
        entera = kardex_service.serie_demanda(d, h, 'red', fuente=f)
        monkeypatch.setattr(kardex_service, 'DIAS_POR_TRAMO_DE_LECTURA', 7)
        partida = kardex_service.serie_demanda(d, h, 'red', fuente=f)
        for ref in ('A', 'B'):
            assert partida[ref]['por_dia'] == entera[ref]['por_dia']
            assert partida[ref]['venta_proyecto'] == entera[ref]['venta_proyecto']
        assert len(entera['A']['por_dia']) == 430

    def test_la_lectura_compartida_da_lo_mismo_que_la_propia(self, app, db, mundo):
        from app.services.kardex_service import KardexService, lectura_demanda
        propia = KardexService.demanda_descensurada(12, 'red')
        compartida = KardexService.demanda_descensurada(12, 'red', lectura=lectura_demanda())
        for ref in ('A', 'B'):
            assert compartida[ref]['d_avg'] == pytest.approx(propia[ref]['d_avg'])
            assert compartida[ref]['sigma_d'] == pytest.approx(propia[ref]['sigma_d'])


class TestLaDemandaSeGuarda:
    """Tanda G (2026-09-29, m052comprasg): la demanda —lo caro— la calcula el
    worker y se guarda; lo que cambia durante el día (existencias, OCs, lo que
    decidió el comprador) se suma al leer; ningún request calcula lo caro."""

    @pytest.fixture
    def guardada(self, app, mundo, monkeypatch):
        from app.services import armador_service, compras_rop
        app.config['COMPRAS_ROP_PERSISTIDO_EN_TESTS'] = True
        compras_rop._PARSEADO.clear()
        compras_rop.PEDIDOS_EN_TESTS.clear()
        real = armador_service.ArmadorService.calcular_demanda_rop
        n = {'calculos': 0}

        def contar(*a, **k):
            n['calculos'] += 1
            return real(*a, **k)
        monkeypatch.setattr(armador_service.ArmadorService, 'calcular_demanda_rop',
                            staticmethod(contar))
        yield n
        app.config['COMPRAS_ROP_PERSISTIDO_EN_TESTS'] = False
        compras_rop._PARSEADO.clear()
        compras_rop.PEDIDOS_EN_TESTS.clear()

    @staticmethod
    def _fila(r, ref):
        return {f['referencia']: f for f in r['nacional']['items']}[ref]

    def test_el_worker_calcula_una_vez_y_despues_lee(self, app, db, guardada):
        from app.services import compras_rop
        from app.services.armador_service import ArmadorService
        compras_rop.recalcular_si_hace_falta()                  # el worker
        a = ArmadorService.rop_dual()
        b = ArmadorService.rop_dual()
        assert guardada['calculos'] == 1
        assert b['calculo']['estado'] == 'AL_DIA' and b['calculo']['de_hoy'] is True
        assert b['nacional']['items'] == a['nacional']['items']
        assert 'Demanda calculada el' in b['calculo']['texto']

    def test_existencias_oc_y_decisiones_se_ven_sin_recalcular(self, app, db, guardada):
        from datetime import datetime
        from app.models.compras_fuentes import OcLineaSiesa
        from app.models.decision_compra import DecisionCompra
        from app.models.stock_siesa import StockSiesa
        from app.services import compras_rop
        from app.services.armador_service import ArmadorService
        compras_rop.recalcular_si_hace_falta()
        antes = self._fila(ArmadorService.rop_dual(), 'A')
        StockSiesa.query.filter_by(codigo_siesa='A').update({'existencia': 20})
        db.session.add(OcLineaSiesa(rowid_linea=1, referencia='A', bodega='NB1',
                                    pendiente_base=7, abierta=True))
        db.session.add(DecisionCompra(referencia='B', accion='PEDIDO', cantidad_decidida=11,
                                      dia=dia_operativo(), vigente_hasta=dia_operativo(),
                                      creada_en=datetime.utcnow()))
        db.session.commit()
        r = ArmadorService.rop_dual()
        assert guardada['calculos'] == 1, 'una OC, una existencia o una decisión no recalculan la demanda'
        a = self._fila(r, 'A')
        assert a['stock_actual'] == 20 and antes['stock_actual'] == 50
        assert a['posicion'] == antes['posicion'] - 30 + 7
        assert self._fila(r, 'B')['en_transito'] == 11
        assert r['calculo']['estado'] == 'AL_DIA'

    def test_venta_nueva_la_deja_vieja(self, app, db, guardada):
        from app.models.registro_sync import RegistroSync
        from app.services import compras_rop
        from app.services.armador_service import ArmadorService
        compras_rop.recalcular_si_hace_falta()
        db.session.add(RegistroSync(tipo='demanda_siesa'))
        db.session.commit()
        assert ArmadorService.rop_dual()['calculo']['estado'] == 'DESACTUALIZADO'
        assert compras_rop.recalcular_si_hace_falta()['hecho'] is True
        assert ArmadorService.rop_dual()['calculo']['estado'] == 'AL_DIA'
        assert guardada['calculos'] == 2

    @pytest.mark.parametrize('tipo', ['pedidos', 'compras_oc', 'carga_fisica'])
    def test_otros_registros_no_la_dejan_vieja(self, app, db, guardada, tipo):
        """El sync de pedidos abre un registro por minuto: si cualquier
        registro dejara vieja la demanda, en producción se recalcularía cada
        minuto (validación del 2026-09-29)."""
        from app.models.registro_sync import RegistroSync
        from app.services import compras_rop
        from app.services.armador_service import ArmadorService
        compras_rop.recalcular_si_hace_falta()
        db.session.add(RegistroSync(tipo=tipo))
        db.session.commit()
        assert ArmadorService.rop_dual()['calculo']['estado'] == 'AL_DIA'
        assert guardada['calculos'] == 1

    def test_otro_dia_la_deja_vieja(self, app, db, guardada, monkeypatch):
        from app.services import compras_rop
        from app.services.armador_service import ArmadorService
        compras_rop.recalcular_si_hace_falta()
        manana = dia_operativo() + timedelta(days=1)
        monkeypatch.setattr(compras_rop, 'dia_operativo', lambda: manana)
        r = ArmadorService.rop_dual()
        assert r['calculo']['estado'] == 'DESACTUALIZADO' and r['calculo']['de_hoy'] is False
        assert compras_rop.recalcular_si_hace_falta()['hecho'] is True
        assert guardada['calculos'] == 2

    def test_un_request_no_calcula_y_muestra_lo_viejo_con_su_fecha(self, app, db, guardada):
        from app.models.registro_sync import RegistroSync
        from app.services import compras_rop
        from app.services.armador_service import ArmadorService
        compras_rop.recalcular_si_hace_falta()                  # el worker
        db.session.add(RegistroSync(tipo='demanda_siesa'))
        db.session.commit()
        with app.test_request_context('/api/compras/bandeja'):
            r = ArmadorService.rop_dual()
        assert guardada['calculos'] == 1, 'un request calculó la demanda en caliente'
        assert r['calculo']['estado'] == 'DESACTUALIZADO' and r['calculo']['recalculando'] is True
        assert 'se está recalculando' in r['calculo']['texto']
        assert r['nacional']['items'], 'se muestra lo último guardado'
        assert compras_rop.PEDIDOS_EN_TESTS == [0.95], 'y se encola el recálculo'

    def test_sin_calculo_la_bandeja_lo_dice_sin_lineas(self, app, db, guardada):
        from app.services import compras_bandeja, compras_rop
        with app.test_request_context('/api/compras/bandeja'):
            b = compras_bandeja.bandeja()
        assert guardada['calculos'] == 0
        assert b['estado'] == 'RECALCULANDO' and b['resumen']['lineas'] == 0
        assert b['calculo']['estado'] == 'SIN_CALCULO'
        assert compras_rop.PEDIDOS_EN_TESTS == [0.95]

    def test_el_contenedor_sin_calculo_no_es_apto(self, app, db, guardada):
        from app.services.armador_service import ArmadorService
        with app.test_request_context('/api/compras/bandeja/contenedor'):
            r = ArmadorService.armar_contenedor()
        assert guardada['calculos'] == 0
        assert r.get('apta') is False
        assert 'todavía no se calculó' in str(r.get('no_apta_por')), r.get('no_apta_por')

    def test_recalcular_si_hace_falta(self, app, db, guardada):
        from app.models.registro_sync import RegistroSync
        from app.services import compras_rop
        assert compras_rop.recalcular_si_hace_falta()['hecho'] is True
        assert compras_rop.recalcular_si_hace_falta() == {'hecho': False, 'motivo': 'AL_DIA'}
        db.session.add(RegistroSync(tipo='demanda_siesa'))
        db.session.commit()
        assert compras_rop.recalcular_si_hace_falta()['hecho'] is True
        assert guardada['calculos'] == 2

    def test_un_recalculo_que_falla_queda_escrito_y_no_borra_lo_guardado(
            self, app, db, guardada, monkeypatch):
        from app.models.registro_sync import RegistroSync
        from app.services import armador_service, compras_rop
        compras_rop.recalcular_si_hace_falta()
        db.session.add(RegistroSync(tipo='demanda_siesa'))
        db.session.commit()

        def revienta(*a, **k):
            raise RuntimeError('la base se cayó')
        monkeypatch.setattr(armador_service.ArmadorService, 'calcular_demanda_rop',
                            staticmethod(revienta))
        r = compras_rop.recalcular_si_hace_falta()
        assert r['hecho'] is False and r['motivo'] == 'ERROR'
        with app.test_request_context('/api/compras/bandeja'):
            rop = armador_service.ArmadorService.rop_dual()
        assert rop['nacional']['items'], 'lo guardado antes sigue sirviendo'
        assert 'El último recálculo falló' in rop['calculo']['texto']
        assert 'la base se cayó' in rop['calculo']['texto']

    def test_lo_guardado_es_json_y_se_lee_igual(self, app, db, mundo):
        """En los tests (modo SIEMPRE) cada llamada calcula, guarda y lee de
        vuelta el JSON: lo que el worker guarda es lo que la bandeja usa."""
        import json
        from app.services.armador_service import ArmadorService
        from app.services.compras_rop import guardado
        r = ArmadorService.rop_dual()
        fila = guardado(0.95)
        assert fila is not None and json.loads(fila.demanda)['skus']
        assert r['calculo']['estado'] == 'AL_DIA'


class TestLaPantallaDiceDeCuandoEs:

    def _node(self, tmp_path, expr):
        from tests.test_compras_bandeja_js import _node
        return _node(tmp_path, pintar={'h': expr})['h']

    def test_recalculando_sin_lineas(self, tmp_path):
        d = {'estado': 'RECALCULANDO', 'calculo': {
            'estado': 'SIN_CALCULO', 'texto': 'La demanda de compras todavía no se calculó: se '
                                             'está calculando, vuelva a cargar en unos minutos.'}}
        html = self._node(tmp_path, f'cmpBandejaHtml({json.dumps(d)}, null)')
        assert 'Calculando la demanda' in html and 'vuelva a cargar' in html

    def test_el_texto_va_escapado(self, tmp_path):
        c = {'estado': 'DESACTUALIZADO', 'texto': '<img src=x onerror=1>'}
        html = self._node(tmp_path, f'cmpCalculoHtml({json.dumps(c)})')
        assert '<img' not in html and '&lt;img' in html


class TestUnaLecturaPorAST:
    """`rop_dual` no llama a `serie_demanda` y pasa la lectura a las dos
    demandas: si alguien vuelve a leer por su cuenta, se ve acá."""

    def _rop(self):
        src = (RAIZ / 'app/services/armador_service.py').read_text(encoding='utf-8')
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.FunctionDef) and n.name == 'calcular_demanda_rop':
                return n
        raise AssertionError('no encontré calcular_demanda_rop')

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


class TestNingunRequestCalculaLaDemanda:
    """La clase: *un request que calcula en caliente lo que tarda más que el
    corte del servidor*. Solo `compras_rop` llama a lo caro, y ninguna ruta
    llama a la puerta del worker."""

    CARO = {'calcular_demanda_rop'}
    PUERTAS_DEL_WORKER = {'calcular_y_guardar', 'recalcular_si_hace_falta'}

    @staticmethod
    def _llamadas(src):
        out = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call):
                f = n.func
                out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None))
        return out

    def _archivos(self):
        for base in ('app', 'flota'):
            yield from (RAIZ / base).rglob('*.py')

    def test_solo_compras_rop_llama_lo_caro(self):
        malos, vistos = [], 0
        for f in self._archivos():
            vistos += 1
            if f.name == 'compras_rop.py':
                continue
            if self._llamadas(f.read_text(encoding='utf-8')) & self.CARO:
                malos.append(str(f.relative_to(RAIZ)))
        assert vistos >= 200
        assert not malos, f'llaman a calcular_demanda_rop fuera de compras_rop: {malos}'

    def test_ninguna_ruta_llama_la_puerta_del_worker(self):
        malos = []
        for f in list((RAIZ / 'app' / 'routes').rglob('*.py')) + list((RAIZ / 'flota' / 'api').rglob('*.py')):
            if self._llamadas(f.read_text(encoding='utf-8')) & (self.CARO | self.PUERTAS_DEL_WORKER):
                malos.append(str(f.relative_to(RAIZ)))
        assert not malos, f'una ruta calcula la demanda en caliente: {malos}'

    def test_el_detector_ve_las_dos_formas(self):
        src = 'def f():\n    ArmadorService.calcular_demanda_rop(0.95)\n    recalcular_si_hace_falta()\n'
        assert {'calcular_demanda_rop', 'recalcular_si_hace_falta'} <= self._llamadas(src)
        sano = 'def f():\n    """calcular_demanda_rop()"""\n    # recalcular_si_hace_falta()\n    return 1\n'
        assert not self._llamadas(sano) & (self.CARO | self.PUERTAS_DEL_WORKER)
