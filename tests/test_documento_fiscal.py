"""
Ninguna mercancía sale sin documento, y ningún documento se duplica.

## La clase

*«No pude preguntarle a Siesa» leído como «no existe» o como «ya está hecho».*

| Instancia | Lectura equivocada | Costo |
|---|---|---|
| `reconciliacion_service._ESTADOS_CUMPLIDO = {'9'}` | ANULADO leído «ya facturado» | DESPACHADO sin RM ni FE; bultos al camión |
| `get_remision_desde_pedido` devolvía `None` ante error | «no pude» leído «no hay RM» | el reintento reenviaba el 142945: RM #2 |
| rama de compromisos vacíos | «no queda nada» leído «hay documento» | `244328-AUTO`: DESPACHADO sin RM ni FE |
| `PackingService.cancelar` solo miraba jobs vivos | RM + job FALLIDO leído «sin documento» | otra caja del pedido: RM #2 |
| `except Timeout` atrapaba `ConnectTimeout` | «no salió» leído «no se sabe» | FALLIDO sin reintento de algo que nunca se envió |

Y, decisión del dueño (2026-09-25): **si Siesa está caído y no se puede
facturar, se para todo** — ninguna caja se cierra, ningún pedido queda
DESPACHADO y ningún bulto va al muelle sin RM y FE confirmadas.

Trinquetes (por AST, nunca por texto): la tabla de estados de pedido, la
política de documento en toda operación que deshace o rehace un empaque, la
cola del muelle sin la bandera cruda, y toda caja de pedido nace con su
condición de pago.
"""
import ast
import json
import pathlib
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'


# ═════════════════════════════════════════════════════════════════════════════
# Mundo
# ═════════════════════════════════════════════════════════════════════════════

def _tarea(db, almacen, **kw):
    from app.models.packing import TareaPacking
    s = uuid.uuid4().hex[:6]
    base = dict(codigo=f'PK-DF-{s}', almacen_id=almacen.id, estado='VERIFICADO',
                tipo_documento='PEDIDO', numero_pedido_siesa=f'PD{s}',
                tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='4321',
                pedido_clave=f'003-PD-{s}')
    base.update(kw)
    t = TareaPacking(**base)
    db.session.add(t)
    db.session.commit()
    return t


def _bulto(db, tarea, estado='PENDIENTE', ruta_id=None):
    from app.models.bulto import Bulto
    b = Bulto(tarea_id=tarea.id, tipo='Caja', numero=1, total=1,
              codigo_barras=f'B-{uuid.uuid4().hex[:8].upper()}', estado=estado,
              ruta_despacho_id=ruta_id)
    db.session.add(b)
    db.session.commit()
    return b


@pytest.fixture
def siesa_real(monkeypatch):
    """El gateway sale de simulación. Los métodos se parchean por CLASE
    (CLAUDE.md: un parche de instancia deja sombras)."""
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'modo_simulacion', False)
    monkeypatch.setattr(connekta, 'modo_ensayo', False)
    monkeypatch.setattr(connekta, '_cb_state', 'CLOSED')
    return connekta


@pytest.fixture
def siesa(siesa_real, monkeypatch):
    """Una Siesa falsa para el despacho: registra los POST y contesta lo que
    cada test le pide."""
    from app.services import cartera_service
    from app.services.connekta_gateway import ConnektaGateway

    class Fake:
        posts_142945 = 0
        posts_142943 = 0
        posts_244328 = 0
        compromisos = [{'f120_referencia': 'SKU1', 'f431_rowid': 11, 'f120_id': 5,
                        'f405_cant_por_remisionar_base': 10, 'f405_id_unidad_medida': 'UND'}]
        respuesta_142945 = {'codigo': 0, 'mensaje': 'Transacción Exitosa'}
        error_142945 = None
        remision = None                 # dict | None | Exception
        facturas = []
        flag_al_postear = []

    f = Fake()

    def trigger_despacho(self, tipo, consec, items, **k):
        from app.models.packing import TareaPacking
        from app.extensions import db
        f.posts_142945 += 1
        # ¿El pre-flag estaba COMITEADO cuando salió el POST?
        db.session.expire_all()
        f.flag_al_postear.append([t.rm_enviada_at is not None for t in TareaPacking.query.all()])
        if f.error_142945:
            raise f.error_142945
        return f.respuesta_142945

    def remision(self, tipo, consec):
        if isinstance(f.remision, Exception):
            raise f.remision
        return f.remision

    def factura_rm(self, tipo, consec, cab):
        f.posts_142943 += 1
        return {'codigo': 0, 'mensaje': 'Transacción Exitosa'}

    def comprometer(self, consec, payload):
        f.posts_244328 += 1
        return {'codigo': 0}

    monkeypatch.setattr(ConnektaGateway, 'trigger_despacho', trigger_despacho)
    monkeypatch.setattr(ConnektaGateway, 'get_remision_desde_pedido', remision)
    monkeypatch.setattr(ConnektaGateway, 'trigger_factura_desde_remision', factura_rm)
    monkeypatch.setattr(ConnektaGateway, 'trigger_comprometer_pedido', comprometer)
    monkeypatch.setattr(ConnektaGateway, 'get_pedido_cabecera',
                        lambda self, t, c: {'f430_rowid': 1, 'f430_id_cond_pago': 'C02'})
    monkeypatch.setattr(ConnektaGateway, 'get_compromisos_pedido',
                        lambda self, t, c, r=None: list(f.compromisos))
    monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido',
                        lambda self, t, c: list(f.facturas))
    monkeypatch.setattr(cartera_service, 'compuerta_emision', lambda t, cab: cab)
    monkeypatch.setattr(cartera_service, 'cabecera_para_factura', lambda t, cab: cab)
    return f


def _despachar(tarea):
    from app.services.despacho_parcial_service import DespachoParialService
    return DespachoParialService.despachar_parcial(tarea, {'SKU1': 10})


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La tabla única de estados de pedido
# ═════════════════════════════════════════════════════════════════════════════

class TestUnaTablaDeEstadosDePedido:

    def test_nueve_es_anulado_en_todos_lados(self):
        from app.services import estado_pedido_siesa as e
        assert e.es_anulado(9) and e.es_anulado('9') and e.es_anulado(5)
        assert not e.es_cumplido(9)
        assert e.impide_facturar(9) and e.impide_facturar(-1)
        assert not e.impide_facturar(None) and not e.impide_facturar(3)

    def test_no_se_no_es_no_existe(self):
        from app.services import estado_pedido_siesa as e
        assert e.clasificar(None) == e.SIN_DATO
        assert not e.dejo_de_estar_vivo(None)
        assert e.dejo_de_estar_vivo(9) and e.dejo_de_estar_vivo(-1) and e.dejo_de_estar_vivo(7)
        assert not e.dejo_de_estar_vivo(4) and not e.dejo_de_estar_vivo(3)

    def test_la_historia_lee_la_misma_tabla(self):
        from app.models.pedido_historia import MotivoSalidaPedido as M
        from app.services import estado_pedido_siesa as e
        assert e.motivo_salida(9) == M.ANULADO
        assert e.motivo_salida(4) == M.CUMPLIDO
        assert e.motivo_salida(3) is None and e.motivo_salida(None) is None
        assert e.motivo_salida(2) == M.OTRO_ESTADO


def _funciones(fuente):
    """(nombre_calificado, nodo) de toda función, con su clase."""
    tree = ast.parse(fuente)
    out = []

    def visitar(nodo, prefijo=''):
        for n in ast.iter_child_nodes(nodo):
            if isinstance(n, ast.ClassDef):
                visitar(n, f'{prefijo}{n.name}.')
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f'{prefijo}{n.name}', n))
    visitar(tree)
    return out


def _llamadas(fn):
    """Nombres llamados en el cuerpo de la función (sin docstrings: ahí no hay
    `Call`). Incluye funciones anidadas."""
    nombres = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                nombres.add(f.attr)
            elif isinstance(f, ast.Name):
                nombres.add(f.id)
    return nombres


def _archivos(raiz=APP):
    return [p for p in raiz.rglob('*.py') if '__pycache__' not in p.parts]


_TABLA = {'clasificar', 'es_anulado', 'es_cumplido', 'impide_facturar',
          'dejo_de_estar_vivo', 'motivo_salida', 'nombre'}

#: Llamadores de `get_estado_pedido` que NO interpretan el número. Solo encoge.
LECTORES_DE_ESTADO_SIN_TABLA = {
    'app/services/connekta_gateway.py::ConnektaGateway.get_estado_pedido':
        'el delegado del gateway: devuelve el número tal cual',
}


def _lectores_de_estado_sin_tabla(fuente, ruta='x.py'):
    out = set()
    for nombre, fn in _funciones(fuente):
        ll = _llamadas(fn)
        if 'get_estado_pedido' in ll and not (ll & _TABLA):
            out.add(f'{ruta}::{nombre}')
    return out


class TestTodoLectorDeEstadoUsaLaTabla:

    def test_nadie_interpreta_el_numero_a_mano(self):
        hallados = set()
        for p in _archivos():
            rel = str(p.relative_to(RAIZ))
            hallados |= _lectores_de_estado_sin_tabla(p.read_text(encoding='utf-8'), rel)
        nuevos = hallados - set(LECTORES_DE_ESTADO_SIN_TABLA)
        assert not nuevos, (
            f'{sorted(nuevos)} llaman a get_estado_pedido y leen el número sin '
            '`estado_pedido_siesa`: así el 9 fue «cumplido» en un sitio y «anulado» '
            'en los otros tres.')

    def test_el_inventario_solo_encoge(self):
        hallados = set()
        for p in _archivos():
            rel = str(p.relative_to(RAIZ))
            hallados |= _lectores_de_estado_sin_tabla(p.read_text(encoding='utf-8'), rel)
        assert set(LECTORES_DE_ESTADO_SIN_TABLA) <= hallados, (
            'una entrada del inventario ya no existe: sacala')
        assert all(len(v) > 20 for v in LECTORES_DE_ESTADO_SIN_TABLA.values())

    def test_piso_de_llamadores(self):
        n = 0
        for p in _archivos():
            for _nombre, fn in _funciones(p.read_text(encoding='utf-8')):
                if 'get_estado_pedido' in _llamadas(fn):
                    n += 1
        assert n >= 6, f'solo {n} llamadores: el escáner se rompió'

    def test_meta_ve_la_forma_rota_y_no_la_sana(self):
        roto = ('def f(c):\n    e = c.get_estado_pedido("PD", 1)\n'
                '    return str(e) in ("9",)\n')
        sano = ('def f(c):\n    """get_estado_pedido(x) en un docstring"""\n'
                '    e = c.get_estado_pedido("PD", 1)\n    return _eps.es_anulado(e)\n')
        solo_doc = 'def f():\n    """se llama get_estado_pedido(1)"""\n    return 1\n'
        assert _lectores_de_estado_sin_tabla(roto) == {'x.py::f'}
        assert _lectores_de_estado_sin_tabla(sano) == set()
        assert _lectores_de_estado_sin_tabla(solo_doc) == set()


# ═════════════════════════════════════════════════════════════════════════════
# 2 · La política: una función y su gemela SQL
# ═════════════════════════════════════════════════════════════════════════════

_COMBINACIONES = [
    dict(),
    dict(siesa_triggered=True),
    dict(rm_tipo='RM', rm_consec=5),
    dict(fe_tipo='FE', fe_consec='9'),
    dict(fe_confirmada_at=datetime(2026, 9, 1)),
    dict(rm_enviada_at=datetime(2026, 9, 1)),
    dict(siesa_triggered=True, rm_tipo='RM', rm_consec=5),
    dict(siesa_triggered=True, rm_tipo='RM', rm_consec=5, fe_confirmada_at=datetime(2026, 9, 1)),
    dict(siesa_triggered=True, rm_tipo='RM', rm_consec=5, fe_tipo='FE', fe_consec='7'),
    dict(siesa_triggered=True, fe_tipo='FE', fe_consec='7'),
    dict(tipo_documento='TRASLADO', siesa_triggered=True),
    dict(tipo_documento='TRASLADO'),
    dict(tipo_documento=None, siesa_triggered=True, rm_tipo='RM', rm_consec=5,
         fe_confirmada_at=datetime(2026, 9, 1)),
]


class TestUnaPoliticaDosFormas:

    def test_python_y_sql_dicen_lo_mismo(self, db, almacen):
        from app.models.packing import TareaPacking
        from app.services import documento_fiscal as df
        tareas = [_tarea(db, almacen, **c) for c in _COMBINACIONES]
        ids = [t.id for t in tareas]
        con_doc = {t.id for t in TareaPacking.query.filter(
            TareaPacking.id.in_(ids), df.filtro_tiene_documento())}
        despach = {t.id for t in TareaPacking.query.filter(
            TareaPacking.id.in_(ids), df.filtro_despachable())}
        for t in tareas:
            assert (t.id in con_doc) == df.tiene_documento_en_siesa(t), t.id
            assert (t.id in despach) == df.despachable(t), t.id
        # Y distinguen: ni todo ni nada.
        assert 0 < len(con_doc) < len(ids) and 0 < len(despach) < len(ids)

    def test_solo_rm_y_fe_confirmadas_salen(self, db, almacen):
        from app.services import documento_fiscal as df
        assert not df.despachable(_tarea(db, almacen, siesa_triggered=True))
        assert not df.despachable(_tarea(db, almacen, siesa_triggered=True, rm_tipo='RM',
                                         rm_consec=1))
        assert df.despachable(_tarea(db, almacen, siesa_triggered=True, rm_tipo='RM',
                                     rm_consec=1, fe_confirmada_at=datetime.utcnow()))

    def test_el_pre_flag_cuenta_como_documento(self, db, almacen):
        from app.services import documento_fiscal as df
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow())
        assert df.tiene_documento_en_siesa(t) and df.rm_resultado_desconocido(t)


# ═════════════════════════════════════════════════════════════════════════════
# 3 · La consulta de la remisión: tres estados, tamPag = 100
# ═════════════════════════════════════════════════════════════════════════════

def _gw():
    from app.services.connekta_gateway import ConnektaGateway
    g = ConnektaGateway()
    g.modo_simulacion = False
    g._cb_state = 'CLOSED'
    return g


def _pagina(filas):
    return {'codigo': 0, 'detalle': {'Datos': filas}}


def _filas(desde, n, pedido_objetivo=None, pagina_obj=None):
    return [{'consec_pd': 100000 + desde + i, 'tipo_rm': 'RM', 'consec_rm': 500 + desde + i}
            for i in range(n)]


class TestLaRemisionTieneTresEstados:

    def test_un_error_no_es_no_hay(self):
        from app.services.connekta_gateway import RemisionNoDisponible
        gw = _gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('timeout')):
            with pytest.raises(RemisionNoDisponible):
                gw.get_remision_desde_pedido('PD', 1234)

    def test_el_circuito_abierto_no_es_no_hay(self):
        from app.services.connekta_gateway import RemisionNoDisponible
        gw = _gw()
        with patch.object(gw, '_get', return_value=None):
            with pytest.raises(RemisionNoDisponible):
                gw.get_remision_desde_pedido('PD', 1234)

    def test_un_rechazo_alerta_no_es_no_hay(self):
        from app.services.connekta_gateway import RemisionNoDisponible
        gw = _gw()
        with patch.object(gw, '_get', return_value=_pagina([{'alerta': 'verifique'}])):
            with pytest.raises(RemisionNoDisponible):
                gw.get_remision_desde_pedido('PD', 1234)

    def test_pagina_de_a_cien_y_encuentra_en_la_segunda(self):
        gw = _gw()
        p1 = _filas(0, 100)
        p2 = _filas(100, 30) + [{'consec_pd': 1234, 'tipo_rm': 'RM', 'consec_rm': 77}]
        with patch.object(gw, '_get', side_effect=[_pagina(p1), _pagina(p2)]) as m:
            assert gw.get_remision_desde_pedido('PD', 1234) == {'tipo': 'RM', 'consec': 77}
        pags = [c.kwargs['params_extra']['paginacion'] for c in m.call_args_list]
        assert pags == ['numPag=1|tamPag=100', 'numPag=2|tamPag=100']

    def test_barrido_completo_sin_la_rm_es_no_hay(self):
        gw = _gw()
        with patch.object(gw, '_get', side_effect=[_pagina(_filas(0, 100)),
                                                   _pagina(_filas(100, 5))]):
            assert gw.get_remision_desde_pedido('PD', 1234) is None

    def test_una_fila_repetida_entre_paginas_es_no_se(self):
        from app.services.connekta_gateway import RemisionNoDisponible
        gw = _gw()
        p1 = _filas(0, 100)
        p2 = [p1[3]] + _filas(100, 10)
        with patch.object(gw, '_get', side_effect=[_pagina(p1), _pagina(p2)]):
            with pytest.raises(RemisionNoDisponible, match='repitió'):
                gw.get_remision_desde_pedido('PD', 1234)

    def test_el_tope_con_la_ultima_llena_es_no_se(self, monkeypatch):
        from app.services.connekta_consultas_gateway import ConnektaConsultasGateway
        from app.services.connekta_gateway import RemisionNoDisponible
        monkeypatch.setattr(ConnektaConsultasGateway, 'REMISION_MAX_PAGINAS', 2)
        gw = _gw()
        with patch.object(gw, '_get', side_effect=[_pagina(_filas(0, 100)),
                                                   _pagina(_filas(100, 100))]):
            with pytest.raises(RemisionNoDisponible, match='tope'):
                gw.get_remision_desde_pedido('PD', 1234)


# ═════════════════════════════════════════════════════════════════════════════
# 4 · El POST: «no salió» no es «no se sabe»; un «no» explícito no es un 5xx
# ═════════════════════════════════════════════════════════════════════════════

def _post_con(gw, efecto):
    with patch('app.services.connekta_gateway.requests.post', side_effect=efecto):
        return gw._post('142945', 'X', {})


def _resp(status, cuerpo):
    r = MagicMock()
    r.status_code, r.ok = status, status < 400
    r.json.return_value = cuerpo
    r.text = json.dumps(cuerpo)
    r.headers = {}
    return r


class TestElTimeoutDeConexionNoEsDesconocido:

    def test_connect_timeout_no_salio(self):
        from app.services.connekta_gateway import (ConnektaNoEnviado,
                                                   ConnektaResultadoDesconocido)
        gw = _gw()
        with pytest.raises(ConnektaNoEnviado) as e:
            _post_con(gw, requests.exceptions.ConnectTimeout('conectando'))
        assert not isinstance(e.value, ConnektaResultadoDesconocido)

    def test_read_timeout_sigue_siendo_desconocido(self):
        from app.services.connekta_gateway import ConnektaResultadoDesconocido
        with pytest.raises(ConnektaResultadoDesconocido):
            _post_con(_gw(), requests.exceptions.ReadTimeout('leyendo'))

    def test_un_rechazo_explicito_se_tipa(self):
        from app.services.connekta_gateway import ConnektaRechazado
        gw = _gw()
        with patch('app.services.connekta_gateway.requests.post',
                   return_value=_resp(200, {'codigo': 1, 'mensaje': 'no'})):
            with pytest.raises(ConnektaRechazado):
                gw._post('142945', 'X', {})
        with patch('app.services.connekta_gateway.requests.post',
                   return_value=_resp(400, {'detalle': 'mal'})):
            with pytest.raises(ConnektaRechazado):
                gw._post('142945', 'X', {})

    def test_un_5xx_no_es_un_no(self):
        from app.services.connekta_gateway import ConnektaRechazado
        gw = _gw()
        with patch('app.services.connekta_gateway.requests.post',
                   return_value=_resp(502, {'detalle': 'gateway'})):
            with pytest.raises(Exception) as e:
                gw._post('142945', 'X', {})
        assert not isinstance(e.value, ConnektaRechazado)


class TestUnaJerarquiaDeExcepcionesDelPost:
    """Integración 2026-09-25. Los frentes fiscal y dinero inventaron la misma
    idea con los nombres cruzados: para fiscal `ConnektaNoEnviado` era solo el
    `ConnectTimeout`; para dinero, toda prueba de que no entró. Las dos
    `class ConnektaNoEnviado` convivieron un momento en el mismo módulo, y en
    Python **la segunda gana en silencio**: los `except` de liquidación habrían
    dejado de atrapar el 4xx sin que nada fallara al importar."""

    def test_toda_prueba_de_que_no_entro_es_un_no_enviado(self):
        from app.services import connekta_gateway as g
        for cls in (g.ConnektaRechazado, g.ConnektaPayloadInvalido,
                    g.ConnektaCircuitOpenError):
            assert issubclass(cls, g.ConnektaNoEnviado), cls
        assert issubclass(g.ConnektaPayloadInvalido, ValueError)

    def test_no_se_no_es_un_no_enviado(self):
        from app.services import connekta_gateway as g
        from app.services.despacho_parcial_service import RemisionNoIdentificada
        for cls in (g.ConnektaResultadoDesconocido, RemisionNoIdentificada):
            assert not issubclass(cls, g.ConnektaNoEnviado), cls

    @staticmethod
    def _clases_repetidas(fuente: str) -> set:
        import ast
        vistas, repetidas = set(), set()
        for n in ast.parse(fuente).body:
            nombre = (n.name if isinstance(n, ast.ClassDef) else
                      n.targets[0].id if isinstance(n, ast.Assign) and len(n.targets) == 1
                      and isinstance(n.targets[0], ast.Name) else None)
            if nombre is None:
                continue
            if nombre in vistas:
                repetidas.add(nombre)
            vistas.add(nombre)
        return repetidas

    def test_ningun_nombre_se_define_dos_veces_en_el_gateway(self):
        import pathlib
        raiz = pathlib.Path(__file__).resolve().parents[1]
        fuente = (raiz / 'app/services/connekta_gateway.py').read_text(encoding='utf-8')
        assert not self._clases_repetidas(fuente)

    def test_el_detector_ve_la_redefinicion(self):
        assert self._clases_repetidas(
            'class A(Exception): pass\nclass B: pass\nclass A(Exception): pass\n'
            '"""class B: pass"""\n') == {'A'}


# ═════════════════════════════════════════════════════════════════════════════
# 5 · El 142945: pre-flag, identificar, nunca reenviar
# ═════════════════════════════════════════════════════════════════════════════

class TestLaRemisionNoSeReenvia:

    def test_el_pre_flag_esta_comiteado_cuando_sale_el_post(self, db, almacen, siesa):
        siesa.respuesta_142945 = {'codigo': 0, 'detalle': 'Se generó RM-321'}
        t = _tarea(db, almacen)
        _despachar(t)
        assert siesa.flag_al_postear == [[True]]
        assert t.rm_consec == 321 and t.estado == 'DESPACHADO' and t.fe_confirmada_at

    def test_un_no_explicito_revierte_el_pre_flag(self, db, almacen, siesa):
        from app.services.connekta_gateway import ConnektaRechazado
        siesa.error_142945 = ConnektaRechazado('codigo=1')
        t = _tarea(db, almacen)
        with pytest.raises(ConnektaRechazado):
            _despachar(t)
        db.session.refresh(t)
        assert t.rm_enviada_at is None

    def test_no_salio_revierte_el_pre_flag(self, db, almacen, siesa):
        from app.services.connekta_gateway import ConnektaNoEnviado
        siesa.error_142945 = ConnektaNoEnviado('connect')
        t = _tarea(db, almacen)
        with pytest.raises(ConnektaNoEnviado):
            _despachar(t)
        db.session.refresh(t)
        assert t.rm_enviada_at is None

    def test_un_timeout_deja_el_pre_flag_y_el_reintento_no_reenvia(self, db, almacen, siesa):
        from app.services.connekta_gateway import ConnektaResultadoDesconocido
        siesa.error_142945 = ConnektaResultadoDesconocido('timeout')
        t = _tarea(db, almacen)
        with pytest.raises(ConnektaResultadoDesconocido):
            _despachar(t)
        db.session.refresh(t)
        assert t.rm_enviada_at is not None
        # El reintento: la RM aparece en la consulta → se factura, sin 142945.
        siesa.error_142945 = None
        siesa.remision = {'tipo': 'RM', 'consec': 88}
        _despachar(t)
        assert siesa.posts_142945 == 1 and siesa.posts_142943 == 1
        assert t.rm_consec == 88 and t.estado == 'DESPACHADO'

    def test_recien_enviada_y_sin_consecutivo_espera(self, db, almacen, siesa):
        from app.services.despacho_parcial_service import EsperandoRemision
        from app.services.siesa_job_service import DependenciaPendiente
        siesa.remision = None
        t = _tarea(db, almacen)
        with pytest.raises(EsperandoRemision) as e:
            _despachar(t)
        assert isinstance(e.value, DependenciaPendiente)
        db.session.refresh(t)
        assert t.rm_enviada_at is not None and t.rm_consec is None
        assert t.estado == 'VERIFICADO' and not t.siesa_triggered

    def test_pasada_la_gracia_es_resultado_desconocido_y_no_se_reenvia(self, db, almacen, siesa):
        from app.services.connekta_gateway import ConnektaResultadoDesconocido
        from app.services.despacho_parcial_service import RemisionNoIdentificada
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=1))
        siesa.remision = None
        with pytest.raises(RemisionNoIdentificada) as e:
            _despachar(t)
        assert isinstance(e.value, ConnektaResultadoDesconocido)
        assert 'Facturar RM manual' in str(e.value)
        assert siesa.posts_142945 == 0 and siesa.posts_244328 == 0

    def test_no_poder_preguntar_tampoco_reenvia(self, db, almacen, siesa):
        from app.services.connekta_gateway import RemisionNoDisponible
        from app.services.despacho_parcial_service import RemisionNoIdentificada
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=1))
        siesa.remision = RemisionNoDisponible('red')
        with pytest.raises(RemisionNoIdentificada):
            _despachar(t)
        assert siesa.posts_142945 == 0


class TestCompromisosVaciosNoEsDespachado:

    def test_sin_rm_identificada_no_hay_despachado(self, db, almacen, siesa):
        from app.services.despacho_parcial_service import RemisionNoIdentificada
        siesa.compromisos = []
        siesa.remision = None
        t = _tarea(db, almacen)
        with pytest.raises(RemisionNoIdentificada):
            _despachar(t)
        db.session.refresh(t)
        assert not t.siesa_triggered and t.estado == 'VERIFICADO'
        assert siesa.posts_142945 == 0

    def test_con_la_rm_que_los_consumio_se_factura(self, db, almacen, siesa):
        siesa.compromisos = []
        siesa.remision = {'tipo': 'RM', 'consec': 64}
        t = _tarea(db, almacen)
        _despachar(t)
        assert t.rm_consec == 64 and t.estado == 'DESPACHADO' and t.siesa_triggered
        assert siesa.posts_142945 == 0 and siesa.posts_142943 == 1

    def test_con_la_fe_ya_emitida_no_se_factura_otra(self, db, almacen, siesa):
        siesa.compromisos = []
        siesa.remision = {'tipo': 'RM', 'consec': 64}
        siesa.facturas = [{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 1500}]
        t = _tarea(db, almacen)
        _despachar(t)
        assert siesa.posts_142943 == 0 and t.fe_consec == '1500'

    def test_persistir_sin_rm_se_niega(self, db, almacen):
        from app.services.despacho_parcial_service import DespachoParialService
        t = _tarea(db, almacen)
        with pytest.raises(ValueError, match='sin remisión'):
            DespachoParialService._persistir_resultado(t, '244328-AUTO', {'codigo': 0})


class TestLaSalidaHumana:

    def test_declarar_rm_inexistente_exige_motivo_y_verifica(self, db, almacen, siesa):
        from app.models.bitacora import BitacoraAccion
        from app.services.bitacora import MotivoRequerido
        from app.services.despacho_parcial_service import DespachoParialService
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=2))
        with pytest.raises(MotivoRequerido):
            DespachoParialService.declarar_rm_inexistente(t, 1, '  ')
        siesa.remision = None
        r = DespachoParialService.declarar_rm_inexistente(t, 1, 'Revisé Auditoría: no está')
        assert r['pre_flag_quitado'] and t.rm_enviada_at is None
        f = BitacoraAccion.query.filter_by(entidad='TareaPacking', entidad_id=t.id).one()
        assert f.accion == 'EDITAR' and f.despues['declarado'] == 'RM_INEXISTENTE'

    def test_si_siesa_la_tiene_se_guarda_y_no_se_quita_nada(self, db, almacen, siesa):
        from app.services.despacho_parcial_service import DespachoParialService
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=2))
        siesa.remision = {'tipo': 'RM', 'consec': 91}
        r = DespachoParialService.declarar_rm_inexistente(t, 1, 'creí que no')
        assert r['rm_encontrada'] == 'RM-91' and t.rm_consec == 91

    def test_sin_poder_preguntar_no_se_declara(self, db, almacen, siesa):
        from app.services.connekta_gateway import RemisionNoDisponible
        from app.services.despacho_parcial_service import DespachoParialService
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=2))
        siesa.remision = RemisionNoDisponible('red')
        with pytest.raises(ValueError, match='No se pudo verificar'):
            DespachoParialService.declarar_rm_inexistente(t, 1, 'no está')
        assert t.rm_enviada_at is not None

    def test_facturar_rm_manual_no_reemplaza_otra_rm(self, db, almacen, siesa):
        from app.services.despacho_parcial_service import DespachoParialService
        t = _tarea(db, almacen, rm_tipo='RM', rm_consec=10)
        with pytest.raises(ValueError, match='no se reemplaza'):
            DespachoParialService.facturar_rm_con_consec(t, 'RM', 11)

    def test_el_endpoint_de_facturar_rm_manual_valida(self, app, client, db, almacen,
                                                     usuario_admin, jwt_token_admin):
        t = _tarea(db, almacen)
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.post(f'/api/despacho_parcial/{t.id}/facturar-rm-manual', headers=h,
                        json={'tipo_rm': 'R M', 'consec_rm': 5})
        assert r.status_code == 400
        r = client.post(f'/api/despacho_parcial/{t.id}/facturar-rm-manual', headers=h,
                        json={'rm_inexistente': True, 'motivo': 'x'})
        assert r.status_code == 409   # no hay remisión enviada sin identificar


# ═════════════════════════════════════════════════════════════════════════════
# 6 · El DLQ: ventana, «no sé», resultado desconocido
# ═════════════════════════════════════════════════════════════════════════════

def _job_despacho(db, tarea):
    from app.models.siesa_job import SiesaJob
    job = SiesaJob.encolar(tipo='DESPACHO_F470', referencia_tipo='TareaPacking',
                           referencia_id=tarea.id, payload={
                               'tarea_id': tarea.id, 'tipo_docto_pedido': 'PD',
                               'consec_docto_pedido': '4321',
                               'numero_pedido_siesa': tarea.numero_pedido_siesa,
                               'items': [{'producto_codigo': 'SKU1', 'cantidad_empacada': 10}]})
    db.session.commit()
    return job


class TestElDLQ:

    def test_fuera_de_ventana_espera_sin_postear(self, db, almacen, siesa, monkeypatch, ventana_qa):
        from app.services import documento_fiscal
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        monkeypatch.setattr(documento_fiscal, '_ahora_bogota',
                            lambda: datetime(2026, 9, 25, 21, 30))
        t = _tarea(db, almacen)
        with pytest.raises(DependenciaPendiente, match='no está disponible'):
            _ejecutar_job(_job_despacho(db, t))
        assert siesa.posts_142945 == 0 and siesa.posts_244328 == 0

    def test_sin_poder_preguntar_por_la_factura_no_se_manda_nada(self, db, almacen, siesa,
                                                                  monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job

        def falla(self, t, c):
            raise RuntimeError('Siesa no respondió')
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido', falla)
        t = _tarea(db, almacen)
        with pytest.raises(DependenciaPendiente, match='No se pudo verificar'):
            _ejecutar_job(_job_despacho(db, t))
        assert siesa.posts_142945 == 0

    def test_resultado_desconocido_va_a_fallido_sin_reintento(self, db, almacen, siesa,
                                                              monkeypatch):
        from app.models.siesa_job import EstadoSiesaJob
        from app.services import siesa_job_service as sjs
        monkeypatch.setattr(sjs, '_crear_alerta_admin', lambda job: None)
        from app.services.connekta_gateway import ConnektaGateway
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: 3)
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=1))
        job = _job_despacho(db, t)
        siesa.remision = None
        sjs._run_dlq_jobs()
        db.session.refresh(job)
        assert job.estado == EstadoSiesaJob.FALLIDO and job.proximo_intento is None
        assert siesa.posts_142945 == 0

    def test_anulado_va_a_fallido_y_no_marca_nada(self, db, almacen, siesa, monkeypatch):
        from app.models.siesa_job import EstadoSiesaJob
        from app.services import siesa_job_service as sjs
        from app.services.connekta_gateway import ConnektaGateway
        monkeypatch.setattr(sjs, '_crear_alerta_admin', lambda job: None)
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: 9)
        t = _tarea(db, almacen)
        job = _job_despacho(db, t)
        sjs._run_dlq_jobs()
        db.session.refresh(job)
        db.session.refresh(t)
        assert job.estado == EstadoSiesaJob.FALLIDO and 'ANULADO' in job.error_ultimo
        assert t.pedido_anulado_siesa and not t.siesa_triggered
        assert siesa.posts_142945 == 0


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Reconciliación: solo con la factura
# ═════════════════════════════════════════════════════════════════════════════

class TestLaReconciliacionExigeLaFactura:

    def test_estado_nueve_no_es_cumplido(self, db, almacen, siesa, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.reconciliacion_service import ReconciliacionService
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: 9)
        t = _tarea(db, almacen)
        r = ReconciliacionService.reconciliar_despacho(t, 'PD', '4321')
        assert r == {'reconciliado': False, 'no_se': False, 'anulado': True}
        assert not t.siesa_triggered and t.estado == 'VERIFICADO' and t.pedido_anulado_siesa

    def test_factura_con_consecutivo_y_rm(self, db, almacen, siesa):
        from app.services.reconciliacion_service import ReconciliacionService
        siesa.facturas = [{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 1500}]
        siesa.remision = {'tipo': 'RM', 'consec': 40}
        t = _tarea(db, almacen)
        r = ReconciliacionService.reconciliar_despacho(t, 'PD', '4321')
        assert r['reconciliado'] and r['rm_identificada']
        assert (t.fe_tipo, t.fe_consec, t.rm_consec, t.estado) == ('FE', '1500', 40, 'DESPACHADO')

    def test_factura_sin_rm_identificada_no_despacha(self, db, almacen, siesa):
        from app.services.connekta_gateway import RemisionNoDisponible
        from app.services.documento_fiscal import despachable
        from app.services.reconciliacion_service import ReconciliacionService
        siesa.facturas = [{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 1500}]
        siesa.remision = RemisionNoDisponible('red')
        t = _tarea(db, almacen)
        r = ReconciliacionService.reconciliar_despacho(t, 'PD', '4321')
        assert r['reconciliado'] and not r['rm_identificada']
        assert t.siesa_triggered and t.estado == 'VERIFICADO' and not despachable(t)

    def test_una_fila_sin_consecutivo_no_es_evidencia(self, db, almacen, siesa):
        from app.services.reconciliacion_service import ReconciliacionService
        siesa.facturas = [{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': ''}]
        t = _tarea(db, almacen)
        r = ReconciliacionService.reconciliar_despacho(t, 'PD', '4321')
        assert not r['reconciliado'] and not t.siesa_triggered

    def test_no_poder_preguntar_es_no_se(self, db, almacen, siesa, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.reconciliacion_service import ReconciliacionService

        def falla(self, t, c):
            raise RuntimeError('401')
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido', falla)
        t = _tarea(db, almacen)
        r = ReconciliacionService.reconciliar_despacho(t, 'PD', '4321')
        assert r['no_se'] and not r['reconciliado']


class TestElBarridoRota:

    def test_diez_imposibles_no_tapan_a_la_once(self, db, almacen, monkeypatch):
        from app.models.packing import TareaPacking
        from app.services.reconciliacion_service import LOTE_SWEEP, ReconciliacionService
        vistas = []
        monkeypatch.setattr(ReconciliacionService, 'reconciliar_despacho',
                            staticmethod(lambda t, **k: vistas.append(t.id) or
                                         {'reconciliado': False, 'no_se': True}))
        ts = [_tarea(db, almacen) for _ in range(LOTE_SWEEP + 3)]
        ReconciliacionService._sweep_con_lock(TareaPacking)
        primera = set(vistas)
        vistas.clear()
        ReconciliacionService._sweep_con_lock(TareaPacking)
        segunda = set(vistas)
        assert len(primera) == LOTE_SWEEP
        faltaban = {t.id for t in ts} - primera
        assert faltaban <= segunda, 'las que no entraron la primera vez van primero'

    def test_el_anulado_no_entra_al_barrido(self, db, almacen, monkeypatch):
        from app.models.packing import TareaPacking
        from app.services.reconciliacion_service import ReconciliacionService
        vistas = []
        monkeypatch.setattr(ReconciliacionService, 'reconciliar_despacho',
                            staticmethod(lambda t, **k: vistas.append(t.id) or {}))
        t = _tarea(db, almacen, pedido_anulado_siesa=True)
        ReconciliacionService._sweep_con_lock(TareaPacking)
        assert t.id not in vistas


# ═════════════════════════════════════════════════════════════════════════════
# 8 · El cierre de caja: sin Siesa no se cierra, y no queda DESPACHADO
# ═════════════════════════════════════════════════════════════════════════════

def _caja_para_cerrar(db, almacen, producto):
    from app.models.packing import ItemPacking
    t = _tarea(db, almacen)
    db.session.add(ItemPacking(tarea_id=t.id, producto_id=producto.id,
                               cantidad_esperada=5, cantidad_real=5, verificado=True))
    db.session.commit()
    return t


class TestElCierreSinSiesaSeNiegaLimpio:

    def _cerrar(self, t):
        from app.services.closing.pedido_closer import PedidoPackingCloser
        return PedidoPackingCloser().ejecutar_cierre(t.id, [{'tipo': 'Caja', 'cantidad': 1}], 1)

    def _nada_cambio(self, db, t):
        from app.models.bulto import Bulto
        from app.models.siesa_job import SiesaJob
        db.session.refresh(t)
        assert t.estado == 'VERIFICADO'
        assert Bulto.query.filter_by(tarea_id=t.id).count() == 0
        assert SiesaJob.query.filter_by(referencia_id=t.id).count() == 0

    @pytest.fixture
    def preguntas(self, monkeypatch):
        """Sin Siesa disponible el cierre se niega ANTES de preguntarle nada
        (una consulta a un Siesa caído son 30 s del request)."""
        from app.services.connekta_gateway import ConnektaGateway
        hechas = []
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido',
                            lambda self, t, c: hechas.append('estado') or 3)
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido',
                            lambda self, t, c: hechas.append('fe') or [])
        return hechas

    def test_circuito_abierto(self, db, almacen, producto, siesa_real, monkeypatch, preguntas):
        from app.services.documento_fiscal import MENSAJE_SIESA_NO_DISPONIBLE
        monkeypatch.setattr(siesa_real, '_cb_state', 'OPEN')
        t = _caja_para_cerrar(db, almacen, producto)
        r = self._cerrar(t)
        assert not r.exitoso and r.mensaje.startswith(MENSAJE_SIESA_NO_DISPONIBLE)
        assert 'no responde' in r.mensaje and preguntas == []
        self._nada_cambio(db, t)

    def test_fuera_de_ventana(self, db, almacen, producto, siesa_real, monkeypatch, preguntas,
                              ventana_qa):
        from app.services import documento_fiscal
        monkeypatch.setattr(documento_fiscal, '_ahora_bogota',
                            lambda: datetime(2026, 9, 25, 20, 30))
        t = _caja_para_cerrar(db, almacen, producto)
        r = self._cerrar(t)
        assert not r.exitoso and 'la caja queda esperando' in r.mensaje
        assert 'factura de 06:00 a 19:30' in r.mensaje and preguntas == []  # SIESA_VENTANA de QA
        self._nada_cambio(db, t)

    def test_sin_ventana_de_noche_cierra(self, db, almacen, producto, siesa_real, monkeypatch):
        """Tanda 2 · H: sin `SIESA_VENTANA` (producción) no hay horario que
        frene la facturación: a las 23:30 el cierre pregunta y encola."""
        from app.services import documento_fiscal
        from app.services.connekta_gateway import ConnektaGateway
        monkeypatch.setattr(documento_fiscal, '_ahora_bogota',
                            lambda: datetime(2026, 9, 25, 23, 30))
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: 3)
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido', lambda self, t, c: [])
        t = _caja_para_cerrar(db, almacen, producto)
        assert self._cerrar(t).exitoso

    def test_sin_ventana_con_siesa_caido_se_niega_igual(self, db, almacen, producto, siesa_real,
                                                        monkeypatch, preguntas):
        """Lo que protege cuando Siesa no responde no es el reloj: es el
        circuito. Sin ventana y de noche, con el circuito abierto, se niega."""
        from app.services import documento_fiscal
        from app.services.documento_fiscal import MENSAJE_SIESA_NO_DISPONIBLE
        monkeypatch.setattr(documento_fiscal, '_ahora_bogota',
                            lambda: datetime(2026, 9, 25, 23, 30))
        monkeypatch.setattr(siesa_real, '_cb_state', 'OPEN')
        t = _caja_para_cerrar(db, almacen, producto)
        r = self._cerrar(t)
        assert not r.exitoso and r.mensaje.startswith(MENSAJE_SIESA_NO_DISPONIBLE)
        assert preguntas == []
        self._nada_cambio(db, t)

    def test_sin_ventana_con_el_precheck_caido_se_niega_igual(self, db, almacen, producto,
                                                              siesa_real, monkeypatch):
        from app.services import documento_fiscal
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.documento_fiscal import MENSAJE_SIESA_NO_DISPONIBLE

        def falla(self, t, c):
            raise RuntimeError('timeout')
        monkeypatch.setattr(documento_fiscal, '_ahora_bogota',
                            lambda: datetime(2026, 9, 25, 23, 30))
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: 3)
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido', falla)
        t = _caja_para_cerrar(db, almacen, producto)
        r = self._cerrar(t)
        assert not r.exitoso and r.mensaje.startswith(MENSAJE_SIESA_NO_DISPONIBLE)
        self._nada_cambio(db, t)

    def test_siesa_no_contesta_el_precheck(self, db, almacen, producto, siesa_real,
                                           monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.documento_fiscal import MENSAJE_SIESA_NO_DISPONIBLE

        def falla(self, t, c):
            raise RuntimeError('timeout')
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: 3)
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido', falla)
        t = _caja_para_cerrar(db, almacen, producto)
        r = self._cerrar(t)
        assert not r.exitoso and r.mensaje.startswith(MENSAJE_SIESA_NO_DISPONIBLE)
        self._nada_cambio(db, t)

    def test_con_siesa_se_encola_y_no_queda_despachado(self, db, almacen, producto):
        from app.models.siesa_job import SiesaJob
        t = _caja_para_cerrar(db, almacen, producto)
        r = self._cerrar(t)
        assert r.exitoso
        db.session.refresh(t)
        assert t.estado == 'VERIFICADO' and not t.siesa_triggered
        assert SiesaJob.query.filter_by(tipo='DESPACHO_F470', referencia_id=t.id).count() == 1

    def test_con_rm_sin_confirmar_no_se_re_encola(self, db, almacen, producto):
        from app.models.siesa_job import SiesaJob
        t = _caja_para_cerrar(db, almacen, producto)
        t.rm_enviada_at = datetime.utcnow()
        db.session.commit()
        r = self._cerrar(t)
        assert not r.exitoso and 'Facturar RM manual' in r.mensaje
        assert SiesaJob.query.filter_by(referencia_id=t.id).count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# 9 · Muelle y ruta: solo lo despachable
# ═════════════════════════════════════════════════════════════════════════════

def _ruta(db, estado='EN_CARGUE'):
    from app.models.ruta_despacho import RutaDespacho
    r = RutaDespacho(conductor_id=1, tipo_ruta='Urbana', estado=estado)
    db.session.add(r)
    db.session.commit()
    return r


_LISTA = dict(siesa_triggered=True, rm_tipo='RM', rm_consec=5, estado='DESPACHADO')


class TestElMuelleSoloVeLoDespachable:

    def test_la_lista_no_trae_lo_que_no_tiene_factura(self, db, almacen):
        from app.services.muelle_service import MuelleService
        ok = _tarea(db, almacen, fe_confirmada_at=datetime.utcnow(), **_LISTA)
        sin_fe = _tarea(db, almacen, **_LISTA)
        solo_bandera = _tarea(db, almacen, siesa_triggered=True, estado='DESPACHADO')
        for t in (ok, sin_fe, solo_bandera):
            _bulto(db, t)
        pedidos = {b['numero_pedido'] for g in MuelleService.listar_bultos_listos()['grupos']
                   for b in g['bultos']}
        assert pedidos == {ok.numero_pedido_siesa}

    def test_asignar_a_ruta_niega_lo_que_no_puede_salir(self, db, almacen):
        from app.models.bulto import Bulto
        from app.services.muelle_service import MuelleService
        t = _tarea(db, almacen, **_LISTA)
        b = _bulto(db, t)
        ruta = _ruta(db)
        with pytest.raises(ValueError, match='no la factura confirmada'):
            MuelleService.asignar_a_ruta(ruta.id, bultos_ids=[b.id])
        assert db.session.get(Bulto, b.id).ruta_despacho_id is None

    def test_asignar_por_pedido_tambien(self, db, almacen):
        from app.services.muelle_service import MuelleService
        t = _tarea(db, almacen, siesa_triggered=True, estado='DESPACHADO')
        _bulto(db, t)
        ruta = _ruta(db)
        with pytest.raises(ValueError, match='no tiene remisión confirmada'):
            MuelleService.asignar_a_ruta(ruta.id, pedido_siesa=t.numero_pedido_siesa)

    def test_cargar_niega_lo_que_no_puede_salir(self, db, almacen):
        from app.services.muelle_service import MuelleService
        t = _tarea(db, almacen, **_LISTA)
        ruta = _ruta(db)
        b = _bulto(db, t, ruta_id=ruta.id)
        with pytest.raises(ValueError, match='no la factura confirmada'):
            MuelleService.cargar_bulto(b.codigo_barras, ruta.id)

    def test_la_ruta_no_sale_con_un_bulto_sin_documento(self, db, almacen, monkeypatch):
        from app.services.ruta_service import RutaService
        monkeypatch.setattr(RutaService, '_reconocer_advertencias_flota',
                            staticmethod(lambda *a, **k: []))
        t = _tarea(db, almacen, **_LISTA)
        ruta = _ruta(db)
        _bulto(db, t, estado='CARGADO', ruta_id=ruta.id)
        with pytest.raises(ValueError, match='La ruta no puede salir'):
            RutaService.cerrar_ruta(ruta.id)

    def test_la_ruta_sale_con_todo_en_orden(self, db, almacen, monkeypatch):
        from app.services.ruta_service import RutaService
        monkeypatch.setattr(RutaService, '_reconocer_advertencias_flota',
                            staticmethod(lambda *a, **k: []))
        t = _tarea(db, almacen, fe_confirmada_at=datetime.utcnow(), **_LISTA)
        ruta = _ruta(db)
        _bulto(db, t, estado='CARGADO', ruta_id=ruta.id)
        assert RutaService.cerrar_ruta(ruta.id).estado == 'EN_TRANSITO'


# ═════════════════════════════════════════════════════════════════════════════
# 10 · Cancelar, resetear, otra caja, devuelto al estante
# ═════════════════════════════════════════════════════════════════════════════

class TestConDocumentoNadieDeshaceNiRehace:

    @pytest.mark.parametrize('rastro', [
        dict(rm_tipo='RM', rm_consec=3), dict(rm_enviada_at=datetime(2026, 9, 1)),
        dict(fe_tipo='FE', fe_consec='8'), dict(siesa_triggered=True)])
    def test_cancelar_se_niega(self, db, almacen, rastro):
        from app.services.documento_fiscal import DocumentoEnSiesa
        from app.services.packing_service import PackingService
        t = _tarea(db, almacen, **rastro)
        with pytest.raises(DocumentoEnSiesa):
            PackingService.cancelar(t.id, motivo='ya no va', usuario_id=1)
        assert t.estado == 'VERIFICADO'

    def test_resetear_se_niega_con_rm(self, db, almacen):
        from app.services.documento_fiscal import DocumentoEnSiesa
        from app.services.packing_service import PackingService
        t = _tarea(db, almacen, rm_tipo='RM', rm_consec=3)
        _bulto(db, t)
        with pytest.raises(DocumentoEnSiesa):
            PackingService.resetear_siesa(t.id, usuario_id=1)

    def test_otra_caja_del_pedido_se_niega(self, db, almacen, producto):
        from app.services.documento_fiscal import DocumentoEnSiesa
        from app.services.packing_service import PackingService
        t = _tarea(db, almacen, rm_tipo='RM', rm_consec=3, estado='CANCELADO')
        with pytest.raises(DocumentoEnSiesa, match='segundo documento'):
            PackingService.crear_manual(t.numero_pedido_siesa, almacen.id,
                                        [{'producto_id': producto.id, 'cantidad': 1}])

    def test_iniciar_despacho_se_niega_antes_del_picking(self, client, db, almacen, producto,
                                                        jwt_token_admin):
        from app.models.picking import TareaPicking
        t = _tarea(db, almacen, rm_tipo='RM', rm_consec=3, estado='CANCELADO')
        r = client.post('/api/siesa/iniciar-despacho',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'},
                        json={'numero_pedido': t.numero_pedido_siesa, 'tipo_docto': 'PD',
                              'consec_docto': '4321', 'almacen_id': almacen.id,
                              'items': [{'producto_id': producto.id,
                                         'cantidad_pendiente': 1}]})
        assert r.status_code == 409 and 'segundo documento' in r.get_json()['error']
        assert TareaPicking.query.count() == 0

    def test_reconfirmar_una_caja_con_rm_se_niega(self, db, almacen):
        from app.services.documento_fiscal import DocumentoEnSiesa
        from app.services.packing_service import PackingService
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow())
        with pytest.raises(DocumentoEnSiesa):
            PackingService.confirmar_packing(t.id)


class TestCancelarLaCajaRetenidaEsSuSalida:

    def _retenida(self, db, almacen):
        from app.models.cartera import RetencionCartera
        t = _tarea(db, almacen)
        job = _job_despacho(db, t)
        r = RetencionCartera(pedido_clave=t.pedido_clave, numero_pedido=t.numero_pedido_siesa,
                             nit='900', compuerta='EMISION', estado='RETENIDO',
                             tarea_packing_id=t.id, motivos=[])
        db.session.add(r)
        db.session.commit()
        return t, job, r

    def test_cancelar_descarta_el_job_y_cancela_la_retencion(self, db, almacen):
        from app.models.bitacora import BitacoraAccion
        from app.services.packing_service import PackingService
        t, job, r = self._retenida(db, almacen)
        PackingService.cancelar(t.id, motivo='El cliente no paga', usuario_id=1)
        db.session.refresh(job)
        db.session.refresh(r)
        assert (t.estado, job.estado, r.estado) == ('CANCELADO', 'DESCARTADO', 'CANCELADO')
        acciones = {(f.entidad, f.accion) for f in BitacoraAccion.query.all()}
        assert {('SiesaJob', 'DESCARTAR'), ('RetencionCartera', 'CANCELAR'),
                ('TareaPacking', 'CANCELAR')} <= acciones

    def test_un_job_ejecutandose_no_se_descarta(self, db, almacen):
        from app.services.packing_service import PackingService
        t, job, _r = self._retenida(db, almacen)
        job.estado = 'PROCESANDO'
        db.session.commit()
        with pytest.raises(ValueError, match='PROCESANDO'):
            PackingService.cancelar(t.id, motivo='x', usuario_id=1)

    def test_sin_retencion_el_job_vivo_sigue_bloqueando(self, db, almacen):
        from app.services.packing_service import PackingService
        t = _tarea(db, almacen)
        _job_despacho(db, t)
        with pytest.raises(ValueError, match='job Siesa PENDIENTE'):
            PackingService.cancelar(t.id, motivo='x', usuario_id=1)


class TestDescartarUnaRemisionSinFactura:

    def test_se_niega_sin_reconocerlo(self, db, almacen):
        from app.services.siesa_job_service import descartar_job
        t = _tarea(db, almacen, rm_tipo='RM', rm_consec=3)
        job = _job_despacho(db, t)
        job.estado = 'FALLIDO'
        db.session.commit()
        with pytest.raises(ValueError, match='remisionada sin factura'):
            descartar_job(job.id, usuario_id=1, motivo='limpieza')
        j = descartar_job(job.id, usuario_id=1, motivo='anulada en Siesa a mano',
                          reconoce_remision_sin_factura=True)
        assert j.estado == 'DESCARTADO'

    def test_con_la_factura_se_descarta_normal(self, db, almacen):
        from app.services.siesa_job_service import descartar_job
        t = _tarea(db, almacen, rm_tipo='RM', rm_consec=3, fe_confirmada_at=datetime.utcnow())
        job = _job_despacho(db, t)
        job.estado = 'FALLIDO'
        db.session.commit()
        assert descartar_job(job.id, usuario_id=1, motivo='ya está').estado == 'DESCARTADO'


# ═════════════════════════════════════════════════════════════════════════════
# 11 · La auditoría lo ve (detectores ciegos de VTA-31 y VTA-32)
# ═════════════════════════════════════════════════════════════════════════════

def _res(r, codigo):
    return next(x for x in r['resultados'] if x['codigo'] == codigo)


class TestLaAuditoriaLoVe:

    def test_vta31_ve_un_despacho_sin_documento(self, db, almacen, monkeypatch):
        from app.services import corte
        from app.services.auditoria import base as auditoria
        monkeypatch.setattr(corte, 'inicio_auditoria', lambda: None)
        _tarea(db, almacen, siesa_triggered=True, estado='DESPACHADO',
               siesa_triggered_at=datetime.utcnow())
        _tarea(db, almacen, estado='DESPACHADO', fecha_creacion=datetime.utcnow())
        _tarea(db, almacen, fe_confirmada_at=datetime.utcnow(), **_LISTA)       # sana
        _tarea(db, almacen, tipo_documento='TRASLADO', estado='DESPACHADO')     # otro circuito
        assert _res(auditoria.auditar('venta'), 'VTA-31')['total'] == 2

    def test_vta31_lo_de_antes_de_la_correccion_es_artefacto(self, db, almacen, monkeypatch):
        from app.services import corte
        from app.services.auditoria import base as auditoria
        monkeypatch.setattr(corte, 'inicio_auditoria', lambda: None)
        _tarea(db, almacen, siesa_triggered=True, estado='DESPACHADO',
               siesa_triggered_at=datetime(2026, 9, 1))
        r = _res(auditoria.auditar('venta'), 'VTA-31')
        assert r['total'] == 0 and r['artefacto']['casos'] == 1

    def test_vta32_ve_una_remision_sin_factura(self, db, almacen, monkeypatch):
        from app.services import corte
        from app.services.auditoria import base as auditoria
        monkeypatch.setattr(corte, 'inicio_auditoria', lambda: None)
        vieja = datetime.utcnow() - timedelta(hours=30)
        _tarea(db, almacen, rm_tipo='RM', rm_consec=4, rm_enviada_at=vieja)
        _tarea(db, almacen, rm_enviada_at=vieja)                                # sin confirmar
        _tarea(db, almacen, rm_tipo='RM', rm_consec=4,
               rm_enviada_at=datetime.utcnow() - timedelta(hours=2))            # dentro del día
        _tarea(db, almacen, rm_tipo='RM', rm_consec=4, rm_enviada_at=vieja,
               fe_confirmada_at=datetime.utcnow())                              # facturada
        assert _res(auditoria.auditar('venta'), 'VTA-32')['total'] == 2


# ═════════════════════════════════════════════════════════════════════════════
# 12 · Trinquete: toda operación que deshace o rehace una caja pregunta
# ═════════════════════════════════════════════════════════════════════════════

#: Solo las funciones de `documento_fiscal`: un ayudante que las llama por
#: dentro (`_retencion_que_frena`) no protege la operación que lo usa.
_POLITICA = {'exigir_sin_documento', 'exigir_pedido_sin_documento',
             'tiene_documento_en_siesa', 'filtro_tiene_documento'}

#: Funciones que, por su nombre, la política debe cubrir. Solo crece con una
#: decisión escrita; un sitio que pierde la llamada se pone rojo.
DEBEN_PREGUNTAR = {
    'app/services/packing_service.py::PackingService.cancelar',
    'app/services/packing_service.py::PackingService.resetear_siesa',
    'app/services/packing_service.py::PackingService.crear_manual',
    'app/services/packing_service.py::PackingService.crear_desde_picking',
    'app/services/packing_service.py::PackingService.confirmar_packing',
    # Inventario extrajo la condición «un empaque del pedido la explica» a un
    # ayudante que comparten la lista de «devuelto al estante» y
    # `reabrir_picking` (P0-7). La pregunta vive ahí: es SQL, no una llamada
    # que el caller pueda saltarse.
    'app/services/picking_service.py::PickingService._empaque_que_la_explica',
    'app/routes/siesa.py::iniciar_despacho',
}

#: Sitios que tocan una caja SIN preguntar, con su porqué. Solo encoge.
EXENTOS = {
    'app/services/traslado_service.py::TrasladoService.confirmar_picking_traslado':
        'traslado: su documento es el STS (otro circuito, `tipo_documento=TRASLADO`)',
}


def _toca_una_caja(fn):
    """¿La función crea una caja, la cancela o la reabre, o borra sus bultos?
    Por AST: `TareaPacking(...)`, `x.estado = EstadoPacking.CANCELADO|VERIFICADO`,
    o `Bulto.query...delete()`."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'TareaPacking':
            return True
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Attribute) and t.attr == 'estado'
                        and isinstance(n.value, ast.Attribute)
                        and isinstance(n.value.value, ast.Name)
                        and n.value.value.id == 'EstadoPacking'
                        and n.value.attr in ('CANCELADO', 'VERIFICADO')):
                    return True
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'delete'):
            if any(isinstance(x, ast.Name) and x.id == 'Bulto' for x in ast.walk(n.func.value)):
                return True
    return False


def _sitios(raiz=APP):
    out = {}
    for p in _archivos(raiz):
        rel = str(p.relative_to(RAIZ))
        for nombre, fn in _funciones(p.read_text(encoding='utf-8')):
            out[f'{rel}::{nombre}'] = fn
    return out


class TestTodaCajaPreguntaPorSuDocumento:

    def test_los_sitios_declarados_preguntan(self):
        sitios = _sitios()
        for s in sorted(DEBEN_PREGUNTAR):
            assert s in sitios, f'{s} ya no existe: actualizá DEBEN_PREGUNTAR'
            assert _llamadas(sitios[s]) & _POLITICA, (
                f'{s} ya no pregunta por el documento en Siesa: una caja con '
                'remisión se deshace o se rehace, y sale la segunda.')

    def test_todo_sitio_que_toca_una_caja_pregunta_o_esta_exento(self):
        sitios = _sitios()
        tocan = {s for s, fn in sitios.items() if _toca_una_caja(fn)}
        sin_preguntar = {s for s in tocan if not (_llamadas(sitios[s]) & _POLITICA)}
        nuevos = sin_preguntar - set(EXENTOS)
        assert not nuevos, (
            f'{sorted(nuevos)} crean, cancelan o reabren una caja sin '
            '`documento_fiscal`. Llamá la política o declará por qué no en EXENTOS.')

    def test_exentos_solo_encoge_y_dice_por_que(self):
        sitios = _sitios()
        for s, motivo in EXENTOS.items():
            assert s in sitios and _toca_una_caja(sitios[s]), (
                f'{s} ya no toca una caja: sacalo de EXENTOS')
            assert not (_llamadas(sitios[s]) & _POLITICA), f'{s} ya pregunta: sacalo'
            assert len(motivo) > 30

    def test_piso(self):
        tocan = [s for s, fn in _sitios().items() if _toca_una_caja(fn)]
        assert len(tocan) >= 6, f'solo {len(tocan)}: el detector se rompió'

    def test_meta(self):
        def una(src):
            return _funciones(src)[0][1]
        assert _toca_una_caja(una('def f(t):\n    t.estado = EstadoPacking.CANCELADO\n'))
        assert _toca_una_caja(una('def f():\n    return TareaPacking(codigo="x")\n'))
        assert _toca_una_caja(una('def f(i):\n    Bulto.query.filter_by(tarea_id=i).delete()\n'))
        assert not _toca_una_caja(una(
            'def f(t):\n    """t.estado = EstadoPacking.CANCELADO"""\n'
            '    # TareaPacking(codigo=1)\n    return t.estado == EstadoPacking.CANCELADO\n'))
        assert not _toca_una_caja(una('def f(t):\n    t.estado = EstadoPicking.CANCELADO\n'))


#: Módulos que arman la cola del muelle o sacan una ruta: ninguno lee la
#: bandera `siesa_triggered` de una caja — deciden con `despachable`.
MODULOS_DEL_MUELLE = ('app/services/muelle_service.py', 'app/services/ruta_service.py',
                      'app/routes/muelle.py')


def _lecturas_de_la_bandera(fuente):
    return [n.lineno for n in ast.walk(ast.parse(fuente))
            if isinstance(n, ast.Attribute) and n.attr == 'siesa_triggered']


class TestLaColaDelMuelleNoLeeLaBandera:

    def test_ninguno_lee_siesa_triggered(self):
        for m in MODULOS_DEL_MUELLE:
            lineas = _lecturas_de_la_bandera((RAIZ / m).read_text(encoding='utf-8'))
            assert not lineas, (
                f'{m} lee `siesa_triggered` (líneas {lineas}): la bandera se encendía sin '
                'remisión ni factura. Decidí con `documento_fiscal.despachable`.')

    def test_meta(self):
        assert _lecturas_de_la_bandera('x = t.siesa_triggered\n') == [1]
        assert _lecturas_de_la_bandera(
            'def f():\n    """siesa_triggered"""\n    # t.siesa_triggered\n    return 1\n') == []
        assert _lecturas_de_la_bandera('x = r.siesa_rc_triggered\n') == []


# ═════════════════════════════════════════════════════════════════════════════
# 13 · Toda caja de pedido nace con su condición de pago
# ═════════════════════════════════════════════════════════════════════════════

_ANOTA = {'anotar_desde_historia', 'anotar_en_tarea'}

#: Constructores de `TareaPacking` que no son de un pedido. Solo encoge.
CAJAS_SIN_CONDICION = {
    'app/services/traslado_service.py::TrasladoService.confirmar_picking_traslado':
        'traslado entre sedes: no es una venta, no tiene condición de pago',
}


def _crea_caja(fn):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == 'TareaPacking' for n in ast.walk(fn))


class TestTodaCajaDePedidoNaceConSuCondicion:
    """`crear_manual` —la caja de «Aprobar pedido»— no anotaba la condición:
    la compuerta de cartera G2 daba por contado supuesto (NO_APLICA) un C04."""

    def test_todo_constructor_anota(self):
        sitios = _sitios()
        crean = {s for s, fn in sitios.items() if _crea_caja(fn)}
        sin = {s for s in crean if not (_llamadas(sitios[s]) & _ANOTA)}
        assert sin <= set(CAJAS_SIN_CONDICION), (
            f'{sorted(sin - set(CAJAS_SIN_CONDICION))} crean una caja sin anotar la '
            'condición de pago del pedido (`cond_pago.anotar_desde_historia`).')
        assert set(CAJAS_SIN_CONDICION) <= crean
        assert len(crean) >= 3

    def test_la_caja_de_aprobar_pedido_nace_con_la_condicion(self, db, almacen, producto,
                                                            monkeypatch):
        from app.services import cond_pago
        from app.services.packing_service import PackingService
        monkeypatch.setattr(cond_pago, 'cond_pago_de_historia', lambda clave: 'C04')
        t = PackingService.crear_manual('PD7001', almacen.id,
                                        [{'producto_id': producto.id, 'cantidad': 1}],
                                        tipo_docto_pedido_siesa='PD',
                                        consec_docto_pedido_siesa='7001')
        assert t.cond_pago == 'C04'

    def test_meta(self):
        def una(src):
            return _funciones(src)[0][1]
        assert _crea_caja(una('def f():\n    return TareaPacking(codigo="x")\n'))
        assert not _crea_caja(una('def f():\n    """TareaPacking(x)"""\n    return 1\n'))


# ═════════════════════════════════════════════════════════════════════════════
# 14 · Cartera: acuerdo vigente → solo contado (decisión del dueño)
# ═════════════════════════════════════════════════════════════════════════════

class TestAcuerdoVigenteSoloContado:

    def _retencion(self, db, motivos):
        from app.models.cartera import RetencionCartera
        r = RetencionCartera(pedido_clave=f'003-PD-{uuid.uuid4().hex[:5]}', numero_pedido='PD1',
                             nit='900', compuerta='G1', estado='RETENIDO', motivos=motivos)
        db.session.add(r)
        db.session.commit()
        return r

    def test_no_se_autoriza_a_credito(self, db):
        from app.services import cartera_service as cs
        r = self._retencion(db, [{'codigo': 'ACUERDO_VIGENTE', 'texto': 'acuerdo',
                                  'retiene': True}])
        with pytest.raises(cs.AccionRechazada, match='solo sale de contado'):
            cs.autorizar(r.id, {'nombre': 'Coordinadora Cartera'}, 'pagará', 100_000)
        assert r.estado == 'RETENIDO'
        assert 'autorizar' not in cs.retencion_publica(r)['acciones']
        assert 'convertir_contado' in cs.retencion_publica(r)['acciones']

    def test_por_mora_si_se_ofrece(self, db):
        from app.services import cartera_service as cs
        r = self._retencion(db, [{'codigo': 'MORA', 'texto': 'mora', 'retiene': True}])
        assert 'autorizar' in cs.retencion_publica(r)['acciones']
