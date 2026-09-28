"""
La carga física de las 7:00: «completo» es completo, nada se pone en cero sin
rastro, y una carga que no corrió se ve (validación 2026-09-26, P1-3, P2-6,
P2-9, P3).

**Las clases:**

1. *«Completo» sin medir la completitud.* La consulta de existencias se leía
   en tres pasadas de `tamPag=1000` y con dos perdidas `fuente_para_escribir`
   la daba por buena. Desde el 2026-09-27 es UNA lectura de a 100 con su
   prueba (`LineaRegistro` 1…N contra el total declarado): sin ella no se
   escribe (el detalle vive en `test_existencias_verdaderas.py`).
2. *Inventario puesto en cero sin kardex.* El bulk zero era un UPDATE en
   bloque. Ahora cada cero deja su `MovimientoInventario`. Trinquete AST: toda
   función que pone `cantidad = 0` escribe su movimiento; nadie hace un
   `update({'cantidad': 0})` en bloque.
3. *Una carga que no corrió no deja rastro.* La omitida por operaciones
   activas o fuera de ventana solo se logueaba, y 🩺 Salud leía «OK» con la
   última carga de hace cinco días. Ahora queda en `registros_sync`, se avisa
   en la misma corrida y Salud mira la antigüedad de la última ESCRITA.
4. *Sello fresco sin dato fresco* (`stock_siesa`): una lectura que no es
   completa no escribe nada (2026-09-27; ver `test_existencias_verdaderas.py`).
"""
import ast
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import inventario_siesa_service as iss

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def sembrar_cargas_escritas(db, hace_horas=1):
    """Una carga física escrita (ok) en cada bodega calibrada."""
    from app.models.registro_sync import RegistroSync
    cuando = datetime.utcnow() - timedelta(hours=hace_horas)
    for bod in iss._BODEGAS_CALIBRACION_FISICA:
        db.session.add(RegistroSync(tipo=iss._tipo_registro_stock(bod or iss.connekta.bodega),
                                    inicio=cuando, fin=cuando, ok=True))
    db.session.commit()


def _fila(existencia=5.0):
    return {'existencia': existencia, 'comprometido': 0.0, 'salida_sin_conf': 0.0,
            'descripcion': '', 'unidad': 'UND'}


@pytest.fixture
def cache_limpio():
    orig = dict(iss._cache_inventario_multibodega)
    iss._cache_inventario_multibodega.update(data=None, ts=None, degradado=False,
                                             bodegas_frescas=frozenset(),
                                             completa=False, motivo='')
    yield iss._cache_inventario_multibodega
    iss._cache_inventario_multibodega.clear()
    iss._cache_inventario_multibodega.update(orig)


def completa(datos):
    """Una lectura de Siesa que pasó la prueba de completitud."""
    return iss.ResultadoDescarga(datos, completa=True)


class TestSoloLaLecturaCompletaSirveParaEscribir:
    """El reemplazo de «las tres pasadas» (2026-09-27): la licencia es la
    prueba de completitud de UNA lectura, no la cantidad de pasadas."""

    def test_incompleta_no_sirve(self, db, monkeypatch, cache_limpio):
        base = {'NB1': {f'S{i}': _fila() for i in range(80)}}
        monkeypatch.setattr(iss, '_descargar_una_pasada_custom', lambda: iss.ResultadoDescarga(
            base, completa=False, motivo='la página 2 no se pudo leer (tres intentos)'))
        iss._descargar_inventario_siesa_raw(forzar=True)
        assert 'página 2' in iss.fuente_para_escribir('NB1')

    def test_completa_si_sirve(self, db, monkeypatch, cache_limpio):
        base = {'NB1': {f'S{i}': _fila() for i in range(80)}}
        monkeypatch.setattr(iss, '_descargar_una_pasada_custom', lambda: completa(base))
        monkeypatch.setattr('time.sleep', lambda s: None)
        iss._descargar_inventario_siesa_raw(forzar=True)
        assert iss.fuente_para_escribir('NB1') == ''

    def test_un_dict_sin_la_medida_se_lee_incompleto(self, db, cache_limpio):
        with patch.object(iss, '_descargar_una_pasada_custom',
                          return_value={'NB1': {f'S{i}': _fila() for i in range(80)}}):
            iss._descargar_inventario_siesa_raw(forzar=True)
        assert iss.fuente_para_escribir('NB1') != ''


def _mundo(db, almacen, producto):
    from app.models.inventario import UbicacionProducto
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    pik = Ubicacion(codigo='PIK-Z1', almacen_id=almacen.id, tipo_zona='PICKING', activo=True)
    db.session.add(pik)
    db.session.flush()
    db.session.add(UbicacionProducto(ubicacion_id=pik.id, producto_id=producto.id, cantidad=12))
    filas = {}
    for i in range(60):
        cod = f'RELL-{i:03d}'
        db.session.add(Producto(codigo=cod, nombre=cod, codigo_siesa=cod, activo=True))
        filas[cod] = _fila()
    db.session.commit()
    return pik, {almacen.bodega_siesa_id: filas}


def _cargar(app, almacen, datos):
    with patch.object(iss, '_descargar_una_pasada_custom', return_value=completa(datos)), \
            patch.object(iss, 'connekta') as ck:
        ck.bodega = almacen.bodega_siesa_id
        iss._run_carga_inicial(app, almacen.bodega_siesa_id)


class TestNadaSePoneEnCeroSinRastro:

    def test_el_cero_deja_su_movimiento(self, app, db, almacen, producto, cache_limpio):
        from app.models.inventario import MovimientoInventario, UbicacionProducto
        pik, datos = _mundo(db, almacen, producto)
        _cargar(app, almacen, datos)
        db.session.expire_all()
        assert UbicacionProducto.query.filter_by(ubicacion_id=pik.id).one().cantidad == 0
        m = MovimientoInventario.query.filter_by(ubicacion_id=pik.id, producto_id=producto.id).one()
        assert m.saldo_antes == 12 and m.saldo_despues == 0
        assert 'lectura completa verificada' in m.motivo

    def test_con_una_lectura_incompleta_no_se_escribe_ni_se_cerea(self, app, db, almacen, producto,
                                                                  cache_limpio, monkeypatch):
        from app.models.inventario import UbicacionProducto
        from app.services import registro_sync_service as reg
        pik, datos = _mundo(db, almacen, producto)
        rota = iss.ResultadoDescarga(datos, completa=False,
                                     motivo='la página 2 no se pudo leer (tres intentos)')
        with patch.object(iss, '_descargar_una_pasada_custom', return_value=rota), \
                patch.object(iss, 'connekta') as ck:
            ck.bodega = almacen.bodega_siesa_id
            iss._run_carga_inicial(app, almacen.bodega_siesa_id)
        db.session.expire_all()
        assert UbicacionProducto.query.filter_by(ubicacion_id=pik.id).one().cantidad == 12
        u = reg.ultimo(iss._tipo_registro_stock(almacen.bodega_siesa_id))
        assert u['ok'] is False and 'quedó incompleta' in u['error']


class TestUnaCargaQueNoCorrioSeVe:

    def test_omitida_por_operaciones_activas_queda_registrada_y_se_avisa(
            self, app, db, almacen, monkeypatch):
        from app.services import alertas_service, registro_sync_service as reg
        enviados = []
        monkeypatch.setattr(alertas_service, '_config_resend', lambda: True)
        monkeypatch.setattr(alertas_service, 'enviar_email',
                            lambda **k: enviados.append(k) or True)
        monkeypatch.setattr(iss, '_BODEGAS_CALIBRACION_FISICA', (almacen.bodega_siesa_id,))
        monkeypatch.setattr(iss, '_operaciones_activas_en_almacen', lambda _id: (2, 1))
        monkeypatch.setenv('CARGA_FISICA_AUTOMATICA', 'true')
        iss._ejecutar_carga_fisica_diaria(app)
        u = reg.ultimo(iss._tipo_registro_stock(almacen.bodega_siesa_id))
        assert u['ok'] is False and '2 picking(s)' in u['error']
        assert iss.estado_carga_fisica()[0]['no_escribio']
        assert enviados and 'NO se escribió' in enviados[0]['asunto']

    def test_fuera_de_la_ventana_tambien(self, app, db, almacen, monkeypatch):
        from app.services import registro_sync_service as reg, ventana_siesa
        monkeypatch.setattr(iss, '_BODEGAS_CALIBRACION_FISICA', (almacen.bodega_siesa_id,))
        monkeypatch.setattr(ventana_siesa, 'ventana_abierta', lambda momento=None: False)
        llamado = []
        monkeypatch.setattr(iss, '_run_carga_inicial', lambda *a, **k: llamado.append(1))
        iss._ejecutar_carga_fisica_diaria(app)
        assert not llamado
        assert 'fuera de la ventana' in reg.ultimo(
            iss._tipo_registro_stock(almacen.bodega_siesa_id))['error']

    def test_nunca_escrita_no_es_ok_y_hoy_si(self, db):
        from app.services.analitica_salud import OK, carga_fisica
        assert carga_fisica()['nivel'] != OK
        sembrar_cargas_escritas(db)
        assert carga_fisica()['nivel'] == OK

    def test_el_lock_ocupado_cierra_su_registro(self, app, db, almacen, monkeypatch):
        from app.services import registro_sync_service as reg
        from app.utils import lock
        monkeypatch.setattr(lock, 'tomar_lock_de_sesion',
                            lambda *a, **k: lock.LockDeSesion(lock.LOCK_CARGA_INVENTARIO_SIESA))
        with patch.object(iss, 'connekta') as ck:
            ck.bodega = almacen.bodega_siesa_id
            iss._run_carga_inicial(app, almacen.bodega_siesa_id)
        u = reg.ultimo(iss._tipo_registro_stock(almacen.bodega_siesa_id))
        assert u['fin'] is not None and u['ok'] is False and 'otro proceso' in u['error']


class TestStockSiesaSoloSellaLoLeido:

    def test_un_barrido_parcial_no_se_tapa_con_la_bd(self, db, cache_limpio):
        """El guard anti-parcial mira lo que Siesa trajo, no la mezcla con la BD.
        Una lectura completa de 10 SKU contra 100 guardados no cerea ni sella."""
        from app.models.stock_siesa import StockSiesa
        vieja = datetime.utcnow() - timedelta(days=4)
        for i in range(100):
            db.session.add(StockSiesa(bodega='NB1', codigo_siesa=f'B{i}', existencia=3,
                                      comprometido=0, salida_sin_conf=0, updated_at=vieja))
        db.session.commit()
        pocas = completa({'NB1': {f'B{i}': _fila(9) for i in range(10)}})
        with patch.object(iss, '_descargar_una_pasada_custom', return_value=pocas), \
                patch('time.sleep'):
            iss._descargar_inventario_siesa_raw(forzar=True)
        db.session.expire_all()
        r = StockSiesa.query.filter_by(bodega='NB1', codigo_siesa='B1').one()
        assert r.existencia == 3 and r.updated_at < datetime.utcnow() - timedelta(days=3)


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete: poner inventario en cero deja kardex
# ═════════════════════════════════════════════════════════════════════════════

#: (archivo, función) que ponen `cantidad` en 0 sin escribir su movimiento. Vacío.
CEROS_SIN_KARDEX = {}


def ceros(src: str, archivo: str) -> dict:
    """(archivo, función) → {'ceros': n, 'bloque': n, 'movimiento': bool}."""
    out = {}
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        n = bloque = 0
        mov = False
        for x in ast.walk(fn):
            if (isinstance(x, ast.Assign) and isinstance(x.value, ast.Constant)
                    and x.value.value == 0
                    and any(isinstance(t, ast.Attribute) and t.attr == 'cantidad'
                            for t in x.targets)):
                n += 1
            if (isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                    and x.func.attr == 'update' and x.args and isinstance(x.args[0], ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == 'cantidad'
                            and isinstance(v, ast.Constant) and v.value == 0
                            for k, v in zip(x.args[0].keys, x.args[0].values))):
                bloque += 1
            if isinstance(x, ast.Call) and getattr(x.func, 'id', None) == 'MovimientoInventario':
                mov = True
        if n or bloque:
            out[(archivo, fn.name)] = {'ceros': n, 'bloque': bloque, 'movimiento': mov}
    return out


def _todos():
    out = {}
    for p in sorted((RAIZ / 'app').rglob('*.py')):
        out.update(ceros(p.read_text(encoding='utf-8'), p.relative_to(RAIZ).as_posix()))
    return out


class TestPonerEnCeroDejaKardex:

    def test_ningun_cero_sin_movimiento(self):
        malos = {k for k, v in _todos().items() if not v['movimiento']} - set(CEROS_SIN_KARDEX)
        assert not malos, f'{sorted(malos)}: pone inventario en 0 sin MovimientoInventario'

    def test_ningun_cero_en_bloque(self):
        """Un UPDATE en bloque no puede escribir un movimiento por fila."""
        assert not {k for k, v in _todos().items() if v['bloque']}

    def test_piso(self):
        t = _todos()
        assert len(t) >= 3
        assert ('app/services/inventario_siesa_service.py', '_run_carga_inicial') in t

    def test_ve_las_dos_formas_y_no_lo_sano(self):
        src = ('def a(r):\n    r.cantidad = 0\n'
               'def b(q):\n    q.update({"cantidad": 0}, synchronize_session=False)\n'
               'def c(r):\n    r.cantidad = 0\n    MovimientoInventario(x=1)\n'
               'def d(r):\n    """r.cantidad = 0"""\n    r.cantidad_recogida = 0\n    r.cantidad = 5\n')
        assert ceros(src, 'x') == {
            ('x', 'a'): {'ceros': 1, 'bloque': 0, 'movimiento': False},
            ('x', 'b'): {'ceros': 0, 'bloque': 1, 'movimiento': False},
            ('x', 'c'): {'ceros': 1, 'bloque': 0, 'movimiento': True}}


# ═════════════════════════════════════════════════════════════════════════════
# P2-7: el reintento que dicen Salud y el correo existe, por bodega, con la
# misma guarda; el setup inicial pasa por ella.
# ═════════════════════════════════════════════════════════════════════════════

class TestCargarInventarioPorBodega:

    def test_el_panel_existe_y_llama_la_ruta_por_bodega(self):
        js = (RAIZ / 'app/static/pwa/app.js').read_text(encoding='utf-8')
        assert 'Cargar inventario (por bodega)' in js
        cuerpo = js[js.index('async function siesaCargarInventario'):]
        cuerpo = cuerpo[:cuerpo.index('\n}\n')]
        assert "/api/siesa/cargar-inventario?bodega=" in cuerpo
        assert 'siesaCargaFisicaPintar();' in js

    def test_nb1_dicha_explicitamente_se_acepta(self, app, client, db, almacen, monkeypatch):
        from tests.test_cartera_retencion import _jwt, _usuario
        monkeypatch.setattr(iss, 'iniciar_carga_inventario',
                            lambda app, forzar=False, bodega=None: {'iniciado': True, 'bodega': bodega})
        r = client.post(f'/api/siesa/cargar-inventario?bodega={iss.connekta.bodega}',
                        headers=_jwt(app, _usuario(db, rol='admin')))
        assert r.status_code == 202, r.get_json()

    def test_operaciones_activas_es_409_con_su_motivo(self, app, client, db, almacen, monkeypatch):
        from tests.test_cartera_retencion import _jwt, _usuario
        monkeypatch.setattr(iss, '_get_almacen', lambda bod=None: almacen)
        monkeypatch.setattr(iss, '_operaciones_activas_en_almacen', lambda _id: (1, 0))
        monkeypatch.setattr(iss.connekta, 'modo_simulacion', False)
        r = client.post('/api/siesa/cargar-inventario?bodega=NS1',
                        headers=_jwt(app, _usuario(db, rol='admin')))
        assert r.status_code == 409 and '1 picking' in r.get_json()['error']

    def test_el_estado_por_bodega(self, app, client, db):
        from tests.test_cartera_retencion import _jwt, _usuario
        r = client.get('/api/siesa/carga-fisica', headers=_jwt(app, _usuario(db, rol='admin')))
        assert r.status_code == 200
        assert {b['bodega'] for b in r.get_json()['bodegas']} >= {'NS1', 'NC1'}

    def test_el_setup_inicial_pasa_por_la_misma_guarda(self, app, db, monkeypatch):
        from app.services import registro_sync_service as reg
        monkeypatch.setattr('app.services.siesa_sync_service._run_sync', lambda app: None)
        monkeypatch.setattr(iss, 'motivo_carga_bloqueada', lambda bod: '2 picking(s) activos')
        corrio = []
        monkeypatch.setattr(iss, '_run_carga_inicial', lambda *a, **k: corrio.append(1))
        iss._estado_setup['en_curso'] = True
        iss._run_setup_inicial(app)
        assert not corrio and iss._estado_setup['fase'] == 'error'
        assert '2 picking' in reg.ultimo(iss._tipo_registro_stock(iss.connekta.bodega))['error']

    def test_una_lista_de_bodegas(self):
        src = (RAIZ / 'app/routes/siesa.py').read_text(encoding='utf-8')
        assert '_BODEGAS_CALIBRACION_HABILITADAS' not in src
