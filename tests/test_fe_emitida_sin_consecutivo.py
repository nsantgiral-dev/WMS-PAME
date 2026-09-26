"""
La FE emitida sin consecutivo anotado (validación 2026-09-26, P1-1).

**La clase:** «la factura existe» se decidía por `fe_consec`. En el camino
normal del cierre el 142943 no devuelve el consecutivo: la tarea queda con
`fe_confirmada_at` y sin `fe_consec`, y la cartera la leía «sin factura» —
consumía cupo para siempre (una FE pagada) y se contaba dos veces (una FE
abierta, que ya está en el saldo de Siesa).

Ahora:
· `consumo_wms` usa `documento_fiscal.fe_confirmada` (consecutivo O
  `fe_confirmada_at`) con la regla de 48 h; sin consecutivo, la fila de
  cartera se busca por el pedido (`cxc_cruce`);
· la consulta se acota a lo que puede consumir (no recorre la historia);
· `fe_resolver.anotar_fe_emitidas` anota el consecutivo (GET por pedido, en su
  CO; solo con una FE candidata).

Trinquete: ninguna condición nueva decide sobre `fe_consec` sin mirar
`fe_confirmada_at` (AST, inventario que solo encoge).
"""
import ast
import pathlib
from datetime import datetime, timedelta

import pytest

from app.services import cartera_service as cs
from tests.test_cartera_retencion import (NIT, _codigos, _historia, _inicio,  # noqa: F401
                                          _tarea, fake)

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _emitida_sin_consec(db, almacen, clave, *, hace_h, valor=900_000):
    t = _tarea(db, almacen, clave, estado='DESPACHADO', valor=valor, cond='C04')
    t.siesa_triggered = True
    t.rm_tipo, t.rm_consec = 'RM', 1500
    t.fe_confirmada_at = datetime.utcnow() - timedelta(hours=hace_h)
    t.fecha_despachado = t.fe_confirmada_at
    db.session.commit()
    return t


class TestConsumoConFeSinConsecutivo:

    def test_reciente_y_fuera_de_la_cartera_consume(self, db, fake, almacen):
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-601', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-602', lineas=(('SKU1', 10, 900_000),))
        _emitida_sin_consec(db, almacen, '003-PD-601', hace_h=2)
        paso = _inicio('PD602', 'PD', '602', co='003', almacen_id=almacen.id)
        assert not paso.pasa
        assert paso.evaluacion['consumo_wms'] == 900_000
        assert paso.evaluacion['pedidos_wms'][0]['fe'] == 'emitida, sin consecutivo anotado'
        assert paso.evaluacion['pedidos_wms'][0]['fe_emitida_segun'] == 'fe_confirmada_at'

    def test_reciente_ya_en_la_cartera_por_el_pedido_no_se_cuenta_dos_veces(
            self, db, fake, almacen):
        fake.cliente(cupo=2_000_000)
        fake.factura(saldo=900_000, vence_en=10, tipo='PD', consec='611')
        _historia(db, '003-PD-611', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-612', lineas=(('SKU1', 10, 900_000),))
        _emitida_sin_consec(db, almacen, '003-PD-611', hace_h=2)
        paso = _inicio('PD612', 'PD', '612', co='003', almacen_id=almacen.id)
        assert paso.pasa, paso.evaluacion.get('motivos')
        assert paso.evaluacion['consumo_wms'] == 0

    def test_la_hora_de_emision_es_fe_confirmada_at(self, db, almacen):
        """`fecha_despachado` reciente no rejuvenece una FE emitida hace días."""
        t = _emitida_sin_consec(db, almacen, '003-PD-621', hace_h=24 * 5)
        t.fecha_despachado = datetime.utcnow()
        db.session.commit()
        assert cs._momento_fe(t) == (t.fe_confirmada_at, 'fe_confirmada_at')


class TestLaConsultaSeAcota:

    def test_solo_carga_lo_que_puede_consumir(self, db, almacen):
        from app.models.packing import TareaPacking
        vieja = _emitida_sin_consec(db, almacen, '003-PD-631', hace_h=24 * 20)
        reciente = _emitida_sin_consec(db, almacen, '003-PD-632', hace_h=3)
        sin_fe = _tarea(db, almacen, '003-PD-633', estado='EN_PROCESO')
        con_consec_viejo = _tarea(db, almacen, '003-PD-634', estado='DESPACHADO',
                                  fe=('FE', '634'))
        con_consec_viejo.fecha_despachado = datetime.utcnow() - timedelta(days=9)
        sin_fecha = _tarea(db, almacen, '003-PD-635', estado='DESPACHADO', fe=('FE', '635'))
        db.session.commit()
        ids = {t.id for t in TareaPacking.query.filter(
            cs._filtro_puede_consumir(TareaPacking, datetime.utcnow())).all()}
        assert ids == {reciente.id, sin_fe.id, sin_fecha.id}
        assert vieja.id not in ids and con_consec_viejo.id not in ids

    def test_las_claves_del_nit_son_una_subconsulta(self, db):
        """No se trae la historia del NIT a memoria en cada evaluación."""
        from sqlalchemy.orm import Query
        assert isinstance(cs._claves_del_nit(NIT), Query)

    def test_el_indice_por_cliente_existe(self):
        from app.models.pedido_historia import PedidoHistoria
        idx = {i.name: [c.name for c in i.columns] for i in PedidoHistoria.__table__.indexes}
        assert idx.get('ix_pedidos_historia_cliente') == ['cliente_id']
        mig = (RAIZ / 'migrations' / 'versions').glob('m050inv2*.py')
        texto = next(mig).read_text(encoding='utf-8')
        assert 'ix_pedidos_historia_cliente' in texto


class _Gw:
    def __init__(self, filas=None, falla=None):
        self.filas, self.falla, self.llamadas = filas or [], falla, []

    def get_facturas_de_pedido(self, co, consec):
        self.llamadas.append((co, consec))
        if self.falla:
            raise self.falla
        return self.filas


class TestAnotarElConsecutivo:

    def test_una_fe_candidata_se_anota_consultando_en_el_co_del_pedido(self, db, almacen):
        from app.services.fe_resolver import anotar_fe_emitidas
        t = _emitida_sin_consec(db, almacen, '005-PD-641', hace_h=1)
        gw = _Gw([{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 7788,
                   'f460_consec_docto': '1500'}])
        r = anotar_fe_emitidas(gateway=gw)
        assert r['anotadas'] == 1 and gw.llamadas == [('005', 641)]
        db.session.refresh(t)
        assert (t.fe_tipo, t.fe_consec) == ('FE', '7788')

    def test_dos_facturas_distintas_no_se_anota_ninguna(self, db, almacen):
        from app.services.fe_resolver import anotar_fe_emitidas
        t = _emitida_sin_consec(db, almacen, '003-PD-642', hace_h=1)
        gw = _Gw([{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 1},
                  {'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 2}])
        assert anotar_fe_emitidas(gateway=gw)['sin_factura_unica'] == 1
        db.session.refresh(t)
        assert t.fe_consec is None

    def test_la_fila_de_otro_tipo_de_pedido_o_de_otra_remision_se_descarta(self, db, almacen):
        from app.services.fe_resolver import fe_de_pedido
        t = _emitida_sin_consec(db, almacen, '003-PD-643', hace_h=1)
        gw = _Gw([{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 1, 'f430_id_tipo_docto': 'PV'},
                  {'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 2, 'f460_consec_docto': '999'},
                  {'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 3, 'f430_id_tipo_docto': 'PD'}])
        assert fe_de_pedido(t, gateway=gw) == ('FE', '3')

    def test_no_poder_preguntar_no_anota_y_se_cuenta(self, db, almacen):
        from app.services.fe_resolver import anotar_fe_emitidas
        t = _emitida_sin_consec(db, almacen, '003-PD-644', hace_h=1)
        r = anotar_fe_emitidas(gateway=_Gw(falla=RuntimeError('timeout')))
        assert r['no_se'] == 1 and r['anotadas'] == 0
        db.session.refresh(t)
        assert t.fe_consec is None and t.reconciliacion_intento_at is not None

    def test_no_recorre_el_historico(self, db, almacen):
        from app.services.fe_resolver import DIAS_ANOTAR_FE, anotar_fe_emitidas
        _emitida_sin_consec(db, almacen, '003-PD-645', hace_h=24 * (DIAS_ANOTAR_FE + 1))
        gw = _Gw([{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 1}])
        assert anotar_fe_emitidas(gateway=gw)['revisadas'] == 0 and gw.llamadas == []

    def test_corre_con_el_barrido_de_cartera(self):
        """Sin cron propio: va en el job del barrido (misma ventana, mismo latido)."""
        src = (RAIZ / 'app/services/cartera_service.py').read_text(encoding='utf-8')
        arbol = ast.parse(src)
        init = next(n for n in arbol.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'init_scheduler')
        llamadas = {n.func.id for n in ast.walk(init)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert 'anotar_fe_emitidas' in llamadas


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete: «¿hay factura?» no se decide por el consecutivo solo
# ═════════════════════════════════════════════════════════════════════════════

#: Condiciones que miran `fe_consec` sin `fe_confirmada_at`, con su porqué.
#: (archivo, función) → motivo. Solo encoge.
DECLARADOS = {
    ('app/services/analitica_recorrido.py', 'linea_de_tiempo'):
        'pinta el número del documento en la línea de tiempo; no decide emisión',
    ('app/services/auditoria/venta.py', 'ningun_despacho_sin_remision_y_factura'):
        'arma el texto «fe» del hallazgo; la decisión es motivo_no_despachable',
    ('app/services/cartera_service.py', '_fila_en_cartera'):
        'elige la clave de búsqueda (FE si hay número, si no el pedido)',
    ('app/services/cartera_service.py', 'consumo_wms'):
        'arma el texto «fe» de la fila; la emisión la decide fe_confirmada',
    ('app/services/despacho_parcial_service.py', '_anotar_fe_encontrada'):
        'no pisa un consecutivo ya anotado',
    ('app/services/documento_fiscal.py', 'que_documento'):
        'texto: con número lo nombra; el elif siguiente mira fe_confirmada_at',
    ('app/services/fotos_siesa_service.py', 'completar_valor_factura'):
        'empareja la foto por el número de FE; sin él empareja por el pedido',
    ('app/services/reconciliacion_service.py', 'reconciliar_despacho'):
        'no pisa un consecutivo ya anotado',
    ('app/services/ruta_service.py', '_informe_de_cobro'):
        'aviso «FE saldada» al conductor: sin número no consulta la cartera (el '
        'número lo anota fe_resolver.anotar_fe_emitidas)',
}


def _condiciones(nodo):
    if isinstance(nodo, (ast.If, ast.IfExp, ast.While)):
        return [nodo.test]
    if isinstance(nodo, ast.comprehension):
        return list(nodo.ifs)
    return []


def _mira_solo_el_consecutivo(test) -> bool:
    attrs = {n.attr for n in ast.walk(test) if isinstance(n, ast.Attribute)}
    return 'fe_consec' in attrs and 'fe_confirmada_at' not in attrs


def sitios(src: str, archivo: str) -> set:
    out = set()
    arbol = ast.parse(src)

    def visitar(nodo, funcion):
        for hijo in ast.iter_child_nodes(nodo):
            f = hijo.name if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)) else funcion
            if any(_mira_solo_el_consecutivo(t) for t in _condiciones(hijo)):
                out.add((archivo, f))
            visitar(hijo, f)
    visitar(arbol, '<modulo>')
    return out


def _todos():
    out = set()
    for p in sorted((RAIZ / 'app').rglob('*.py')):
        rel = p.relative_to(RAIZ).as_posix()
        out |= sitios(p.read_text(encoding='utf-8'), rel)
    return out


class TestLaEmisionNoSeDecidePorElConsecutivo:

    def test_ningun_sitio_nuevo(self):
        nuevos = _todos() - set(DECLARADOS)
        assert not nuevos, (
            f'{sorted(nuevos)}: decide sobre `fe_consec` sin mirar `fe_confirmada_at`. '
            'La FE emitida es documento_fiscal.fe_confirmada (el 142943 no devuelve '
            'el consecutivo). Úsela, o declare el sitio con su porqué.')

    def test_el_inventario_solo_encoge(self):
        muertos = set(DECLARADOS) - _todos()
        assert not muertos, f'{sorted(muertos)} ya no existe: quítelo de DECLARADOS'

    def test_cada_entrada_dice_por_que(self):
        assert all(len(m.strip()) > 20 for m in DECLARADOS.values())

    def test_piso(self):
        assert len(_todos()) >= 8

    # meta-tests
    def test_ve_las_formas(self):
        src = ('def f(t):\n    if t.fe_consec:\n        pass\n'
               'def g(t):\n    return 1 if not t.fe_consec else 2\n'
               'def h(ts):\n    return [t for t in ts if t.fe_consec]\n')
        assert sitios(src, 'x.py') == {('x.py', 'f'), ('x.py', 'g'), ('x.py', 'h')}

    def test_no_marca_lo_sano_ni_los_textos(self):
        src = ('def f(t):\n    """if t.fe_consec: nada"""\n'
               '    # if t.fe_consec:\n'
               '    if t.fe_consec or t.fe_confirmada_at:\n        pass\n'
               '    return t.fe_consec\n')
        assert sitios(src, 'x.py') == set()
