"""
La pantalla del que cuenta dice qué contar y qué NO (P1-3 / P2-1, 2026-09-27).

## El defecto

«Búsquelo en toda la bodega» no decía qué NO contar. En NB1 eso incluía los
bultos y canastos del muelle esperando ruta y los traslados empacados esperando
camión: su remisión (142945) o STS ya descontó Siesa. CC1 y CC2 los contaban
igual —misma instrucción— y CC1 == CC2 disparaba un **AJ-ENT automático** que
inflaba Siesa; al día siguiente el pedido se entregaba y el sobrante quedaba.
En temporada es el caso normal. En tienda, «toda la bodega» invitaba a no mirar
la exhibición. Y el aviso de cajas POS salía también en NB1, pidiéndole a un
picker verificar cajas que no existen.

## La clase

*Una instrucción al que cuenta que no depende del almacén donde cuenta.* Una
política: `conteo_politica.perimetro_de_conteo` (y `tipo_de_almacen`, la única
que decide CD / tienda); el HUD la pinta y el aviso POS la obedece. Y
`ConteoService.empacado_por_salir` dice, sin cifras, si hay unidades de ese
producto empacadas esperando salir.
"""
import ast
import pathlib

import pytest

from tests.test_conteo_teorico_pos import tienda  # noqa: F401

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


# ─────────────────────────────────────────────────────────────────────────────
# 1 · El perímetro por tipo de almacén
# ─────────────────────────────────────────────────────────────────────────────

class TestElPerimetro:

    @pytest.mark.parametrize('bodega,tipo,donde', [
        ('NB1', 'CD', 'toda la bodega'),
        ('NS1', 'TIENDA', 'toda la tienda'),
        ('PC1', 'TIENDA', 'toda la tienda'),
        (None, 'DESCONOCIDO', 'todo el almacén'),
        ('', 'DESCONOCIDO', 'todo el almacén'),
    ])
    def test_cada_almacen_dice_lo_suyo(self, bodega, tipo, donde, monkeypatch):
        from types import SimpleNamespace
        from app.services.connekta_gateway import connekta
        from app.services.conteo_politica import perimetro_de_conteo
        monkeypatch.setattr(connekta, 'bodega', 'NB1')
        p = perimetro_de_conteo(SimpleNamespace(bodega_siesa_id=bodega))
        assert (p['tipo'], p['donde']) == (tipo, donde)
        assert p['cuente'] and p['no_cuente'].startswith('NO cuente lo empacado')

    def test_el_cd_es_la_bodega_del_gateway(self, monkeypatch):
        """Si el CD cambia de bodega (PT1 en proceso de volverse CDI), cambia
        con `CONNEKTA_BODEGA`, sin tocar esta política."""
        from types import SimpleNamespace
        from app.services.connekta_gateway import connekta
        from app.services.conteo_politica import tipo_de_almacen
        monkeypatch.setattr(connekta, 'bodega', 'PT1')
        assert tipo_de_almacen(SimpleNamespace(bodega_siesa_id='PT1')) == 'CD'
        assert tipo_de_almacen(SimpleNamespace(bodega_siesa_id='NB1')) == 'TIENDA'

    def test_el_hud_lo_trae(self, db, tienda):
        from app.models.conteo import SesionConteo
        r = _svc().crear_conteo_manual(tienda['almacen'].id, 'POSITEM')
        s = SesionConteo.query.filter_by(codigo=r['codigos'][0]).one()
        v = _svc().vista_hud(s)
        assert v['perimetro']['tipo'] == 'TIENDA' and v['empacado_por_salir'] is False


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Empacado esperando salir
# ─────────────────────────────────────────────────────────────────────────────

class TestEmpacadoPorSalir:

    @pytest.fixture
    def caja(self, db, tienda):
        """Un empaque del SKU con 3 unidades, sin bultos todavía."""
        from app.models.packing import ItemPacking, TareaPacking
        t = TareaPacking(codigo='PK-EMP-1', almacen_id=tienda['almacen'].id,
                         numero_pedido_siesa='PD9001', estado='EN_PROCESO')
        db.session.add(t)
        db.session.flush()
        db.session.add(ItemPacking(tarea_id=t.id, producto_id=tienda['producto'].id,
                                   cantidad_esperada=3, cantidad_real=3))
        db.session.commit()
        return t

    def _si(self, tienda):
        return _svc().empacado_por_salir(tienda['producto'].id, tienda['almacen'].id)

    def _bulto(self, db, caja, estado, ruta_estado=None):
        from app.models.bulto import Bulto
        from app.models.ruta_despacho import RutaDespacho
        ruta_id = None
        if ruta_estado:
            ruta = RutaDespacho(conductor_id=1, tipo_ruta='Urbana', estado=ruta_estado)
            db.session.add(ruta)
            db.session.flush()
            ruta_id = ruta.id
        db.session.add(Bulto(tarea_id=caja.id, codigo_barras=f'PD9001-{estado}',
                             tipo='Caja', numero=1, total=1, estado=estado,
                             ruta_despacho_id=ruta_id))
        caja.estado = 'DESPACHADO'
        db.session.commit()

    def test_una_caja_armandose_o_armada(self, db, tienda, caja):
        assert self._si(tienda)
        caja.estado = 'VERIFICADO'
        db.session.commit()
        assert self._si(tienda)

    def test_despachada_con_el_bulto_en_el_muelle(self, db, tienda, caja):
        self._bulto(db, caja, 'PENDIENTE')
        assert self._si(tienda)

    def test_cargada_en_un_camion_que_no_salio(self, db, tienda, caja):
        self._bulto(db, caja, 'CARGADO', 'EN_CARGUE')
        assert self._si(tienda)

    def test_el_camion_ya_salio(self, db, tienda, caja):
        self._bulto(db, caja, 'CARGADO', 'EN_TRANSITO')
        assert not self._si(tienda)

    def test_entregada_o_cancelada_no(self, db, tienda, caja):
        self._bulto(db, caja, 'ENTREGADO')
        assert not self._si(tienda)
        caja.estado = 'CANCELADO'
        db.session.commit()
        assert not self._si(tienda)

    def test_otro_almacen_no(self, db, tienda, caja):
        from app.models.almacen import Almacen
        otro = Almacen(codigo='ALM-EMP', nombre='Otro', bodega_siesa_id='NC1', activo=True)
        db.session.add(otro)
        db.session.commit()
        assert not _svc().empacado_por_salir(tienda['producto'].id, otro.id)

    def test_un_traslado_empacado_sin_salir(self, db, tienda, caja):
        from app.models.traslado import SolicitudTraslado
        sol = SolicitudTraslado(codigo='ST-EMP-1', bodega_origen_siesa='NS1',
                                bodega_destino_siesa='NC1', estado='PREPARADO',
                                solicitante_id=tienda['a'].id)
        db.session.add(sol)
        db.session.flush()
        caja.tipo_documento = 'TRASLADO'
        caja.solicitud_id = sol.id
        caja.estado = 'DESPACHADO'
        db.session.commit()
        assert self._si(tienda)
        sol.estado = 'EN_TRANSITO'
        db.session.commit()
        assert not self._si(tienda)

    def test_sin_unidades_no(self, db, tienda, caja):
        from app.models.packing import ItemPacking
        ItemPacking.query.filter_by(tarea_id=caja.id).update({'cantidad_real': 0})
        db.session.commit()
        assert not self._si(tienda)


# ─────────────────────────────────────────────────────────────────────────────
# 3 · La pantalla (Node, util.js real)
# ─────────────────────────────────────────────────────────────────────────────

def _hud(tmp_path, perimetro, **extra):
    from tests.test_conteo_hud_operario import _correr_hud
    from app.services.conteo_politica import PERIMETRO_DE_CONTEO
    tarea = {'id': 7, 'tipo': 'CONTEO', 'producto_nombre': 'x',
             'lugares': [{'codigo': 'CROSS-DOCK', 'fisica': True},
                         {'codigo': None, 'fisica': False}]}
    if perimetro:
        tarea['perimetro'] = {'tipo': perimetro, **PERIMETRO_DE_CONTEO[perimetro]}
    tarea.update(extra)
    return _correr_hud(tmp_path, {'tarea': tarea})['html']


class TestLaPantallaDelQueCuenta:

    def test_en_el_cd(self, tmp_path):
        html = _hud(tmp_path, 'CD')
        assert 'CUENTE EN TODA LA BODEGA' in html
        assert 'el resto de la bodega' in html
        assert 'NO cuente lo empacado (bultos, canastos)' in html
        assert 'CAJAS POS' not in html, 'el CD no tiene cajas: el aviso no sale'

    def test_en_tienda(self, tmp_path):
        html = _hud(tmp_path, 'TIENDA')
        assert 'CUENTE EN TODA LA TIENDA' in html and 'exhibición' in html
        assert 'CAJAS POS' in html and 'antes de abrir o después de cerrar caja' in html

    def test_sin_saber_el_almacen_avisa_todo(self, tmp_path):
        html = _hud(tmp_path, None)
        assert 'CAJAS POS' in html, 'sin dato del almacén se avisa (Regla 0)'

    def test_empacado_por_salir(self, tmp_path):
        assert 'empacadas esperando despacho' in _hud(tmp_path, 'CD', empacado_por_salir=True)
        assert 'empacadas esperando despacho' not in _hud(tmp_path, 'CD', empacado_por_salir=False)


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Trinquete: una definición, y el HUD siempre la lleva
# ─────────────────────────────────────────────────────────────────────────────

def _llamadas(fn, nombre):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
            and (getattr(n.func, 'attr', None) == nombre or getattr(n.func, 'id', None) == nombre)]


class TestUnaDefinicion:

    def test_el_hud_de_los_dos_caminos_sale_de_vista_hud(self):
        """El HUD del operario (`_conteo_a_dict`) y el del definitivo
        (`obtener_tarea_operario`) arman su dict con `vista_hud`: una pantalla
        que no la use no trae ni el perímetro ni el aviso de empacado."""
        for archivo, funcion in (('app/services/mobile_service.py', '_conteo_a_dict'),
                                 ('app/services/conteo_service.py', 'obtener_tarea_operario')):
            arbol = ast.parse((RAIZ / archivo).read_text(encoding='utf-8'))
            fn = next(n for n in ast.walk(arbol)
                      if isinstance(n, ast.FunctionDef) and n.name == funcion)
            assert _llamadas(fn, 'vista_hud'), f'{archivo}::{funcion} no usa vista_hud'

    def test_vista_hud_trae_perimetro_y_empacado(self):
        arbol = ast.parse((RAIZ / 'app/services/conteo_service.py').read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == 'vista_hud')
        claves = {k.value for d in ast.walk(fn) if isinstance(d, ast.Dict)
                  for k in d.keys if isinstance(k, ast.Constant)}
        assert {'perimetro', 'empacado_por_salir', 'lugares'} <= claves
        assert _llamadas(fn, 'perimetro_de_conteo') and _llamadas(fn, 'empacado_por_salir')

    def test_el_hud_le_pasa_el_tipo_al_aviso_pos(self):
        """En `conteoHudHtml` el aviso POS recibe el tipo de almacén: una
        llamada sin argumento lo mostraría también en el CD."""
        src = (RAIZ / 'app/static/pwa/conteo.js').read_text(encoding='utf-8')
        cuerpo = src[src.index('function conteoHudHtml('):]
        cuerpo = cuerpo[:cuerpo.index('\n}\n')]
        assert 'avisoCajasPosHtml(t.perimetro' in cuerpo
        assert 'avisoCajasPosHtml()' not in cuerpo
