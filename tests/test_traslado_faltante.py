"""
Lo que la tienda no recibió, y las rutas de traslados que contestaban con la
traza (validación e2e 2026-09-26, tareas a y b).

(a) **La clase:** *una diferencia de recepción que ningún registro nombra.*
STS 10, la tienda cuenta 9, el ETS entra 9: una unidad en la bodega puente,
la solicitud ENTREGADA sin `siesa_error`, ninguna alerta, ningún invariante.
Ahora: `traslado_service.faltante_de_recepcion` (una política) la declara con
quién la resuelve; **TRA-13** (BLOQUEA) la ve hasta que supervisión diga qué se
hizo (devuelto al origen / ajustado; «en investigación» la deja abierta); el
resumen diario la lista; la tienda y recepción leen del servidor lo que pasó
(no afirman «Siesa registró la entrada»). De paso: un ítem contado en CERO
entraba al ETS con la cantidad enviada (`recibida or enviada`).

(b) **La clase:** *una ruta que le devuelve al usuario el texto de una
excepción inesperada.* Todas las de traslados pasan por `_falla`: el error del
usuario con 4xx, el de Siesa dicho como de Siesa, el interno sin trazas y con
una referencia. Trinquete AST sobre `app/routes/` con inventario por archivo
que solo encoge (traslados en cero).
"""
import ast
import pathlib
import uuid
from datetime import datetime

import pytest

from tests.test_cartera_retencion import _jwt, _usuario

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _traslado(db, producto, *, enviada=10, estado='EN_TRANSITO', consec_entrada=None):
    from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
    u = _usuario(db, rol='tienda')
    st = SolicitudTraslado(codigo=f'ST-F-{uuid.uuid4().hex[:6]}', bodega_origen_siesa='NB1',
                           bodega_destino_siesa='NS1', solicitante_id=u.id, estado=estado,
                           modo_transferencia='EN_TRANSITO', bodega_transito_siesa='TRA1',
                           siesa_salida_consec=925, siesa_entrada_consec=consec_entrada,
                           fecha_despacho=datetime.utcnow())
    db.session.add(st)
    db.session.flush()
    it = ItemSolicitudTraslado(solicitud_id=st.id, producto_id=producto.id,
                               producto_codigo_siesa=producto.codigo_siesa or 'SKU-F',
                               cantidad_solicitada=enviada, cantidad_aprobada=enviada,
                               cantidad_enviada=enviada)
    db.session.add(it)
    db.session.commit()
    return st, it, u


def _recibir(st, it, u, cantidad):
    from app.services.traslado_service import TrasladoService
    return TrasladoService.confirmar_recepcion(
        st.id, usuario_id=u.id, items_recibidos=[{'id': it.id, 'cantidad_recibida': cantidad}])


def _tra13():
    from app.services.auditoria.traslados import lo_que_no_llego_tiene_quien_lo_resuelva
    return {h.referencia: h for h in lo_que_no_llego_tiene_quien_lo_resuelva()}


class TestTra13VeLoQueNoLlego:

    def test_ve_la_unidad_que_no_llego(self, db, producto):
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 9)
        st.siesa_entrada_consec, st.siesa_error = 926, None
        db.session.commit()
        h = _tra13()[st.codigo]
        assert h.datos['unidades'] == 1 and 'supervisión' in h.detalle

    def test_lo_sano_no_lo_marca(self, db, producto):
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 10)
        assert st.codigo not in _tra13()

    def test_resuelto_deja_de_verse_y_en_investigacion_sigue(self, db, producto):
        from app.services.traslado_service import TrasladoService
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 9)
        sup = _usuario(db, rol='supervisor')
        TrasladoService.resolver_faltante(st.id, sup.id, 'EN_INVESTIGACION', 'la busca Ana')
        assert 'investigación' in _tra13()[st.codigo].detalle
        TrasladoService.resolver_faltante(st.id, sup.id, 'AJUSTADO', 'ajuste AJ-12 en TRA1')
        assert st.codigo not in _tra13()

    def test_el_resumen_diario_lo_dice(self, db, producto):
        from app.services.alertas_service import avisos_sin_canal
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 9)
        ahora = datetime.utcnow()
        lineas = avisos_sin_canal(ahora, ahora)
        assert any(st.codigo in l and 'faltante' in l for l in lineas), lineas


class TestLaRecepcionDiceLoQuePaso:

    def test_un_cero_contado_no_entra_al_ets(self, db, producto, monkeypatch):
        from app.models.traslado import ItemSolicitudTraslado
        from app.services import traslado_service as ts
        st, it, u = _traslado(db, producto)
        it2 = ItemSolicitudTraslado(solicitud_id=st.id, producto_id=producto.id,
                                    producto_codigo_siesa='OTRO', cantidad_solicitada=5,
                                    cantidad_aprobada=5, cantidad_enviada=5)
        db.session.add(it2)
        db.session.commit()
        enviados = []
        monkeypatch.setattr(ts.siesa_traslado, 'registrar_entrada',
                            lambda **k: enviados.append(k['items']) or {'codigo': 0})
        ts.TrasladoService.confirmar_recepcion(st.id, u.id, items_recibidos=[
            {'id': it.id, 'cantidad_recibida': 10}, {'id': it2.id, 'cantidad_recibida': 0}])
        assert [i['cantidad'] for i in enviados[0]] == [10], (
            'el ítem contado en cero entró al ETS con la cantidad enviada')
        assert ts.faltante_de_recepcion(st)['unidades'] == 5

    def test_sin_consecutivo_no_afirma_que_siesa_registro(self, db, producto, monkeypatch):
        from app.services import traslado_service as ts
        st, it, u = _traslado(db, producto)
        monkeypatch.setattr(ts.siesa_traslado, 'registrar_entrada', lambda **k: {'codigo': 0})
        monkeypatch.setattr(ts.TrasladoService, 'resolver_consecutivo_entrada',
                            staticmethod(lambda codigo, res: (None, 'sin consecutivo')))
        _recibir(st, it, u, 9)
        texto, tipo = ts.mensaje_de_recepcion(st)
        assert 'NO confirmó' in texto and 'Faltaron 1' in texto and tipo == 'advertencia'
        st.siesa_entrada_consec = 88
        texto, tipo = ts.mensaje_de_recepcion(st)
        assert 'registró la entrada (consecutivo 88)' in texto

    def test_la_ruta_devuelve_el_mensaje(self, app, client, db, producto):
        st, it, u = _traslado(db, producto)
        admin = _usuario(db, rol='admin')
        r = client.post(f'/api/traslados/{st.id}/recibir', headers=_jwt(app, admin),
                        json={'items_recibidos': [{'id': it.id, 'cantidad_recibida': 9}]})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert 'Faltaron 1' in d['mensaje_recepcion'] and d['faltante_recepcion']['abierto']

    def test_las_pantallas_no_afirman_la_entrada(self):
        for f in ('tienda.js', 'recepcion.js'):
            src = (RAIZ / 'app/static/pwa' / f).read_text(encoding='utf-8')
            assert 'Siesa registró la entrada en tránsito' not in src
            assert 'ETS generado en Siesa' not in src
            assert 'mensaje_recepcion' in src, f


class TestResolverElFaltante:

    def test_supervision_lo_resuelve_con_motivo_y_bitacora(self, app, client, db, producto):
        from app.models.bitacora import BitacoraAccion
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 9)
        jefe = _usuario(db, rol='jefe_almacen')
        r = client.post(f'/api/traslados/{st.id}/resolver-faltante', headers=_jwt(app, jefe),
                        json={'resolucion': 'DEVUELTO_AL_ORIGEN', 'motivo': 'ETS inverso 77'})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['faltante_recepcion']['resuelto']
        assert BitacoraAccion.query.filter_by(entidad_id=st.id, accion='EDITAR').count() == 1

    def test_la_tienda_no_lo_resuelve(self, app, client, db, producto):
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 9)
        r = client.post(f'/api/traslados/{st.id}/resolver-faltante', headers=_jwt(app, u),
                        json={'resolucion': 'AJUSTADO', 'motivo': 'x'})
        assert r.status_code == 403

    @pytest.mark.parametrize('cuerpo', [{'resolucion': 'AJUSTADO'},
                                        {'resolucion': 'BORRADO', 'motivo': 'x'}])
    def test_sin_motivo_o_resolucion_invalida_es_400(self, app, client, db, producto, cuerpo):
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 9)
        r = client.post(f'/api/traslados/{st.id}/resolver-faltante',
                        headers=_jwt(app, _usuario(db, rol='admin')), json=cuerpo)
        assert r.status_code == 400, r.get_json()

    def test_sin_faltante_es_400_y_sin_traslado_404(self, app, client, db, producto):
        st, it, u = _traslado(db, producto)
        _recibir(st, it, u, 10)
        h = _jwt(app, _usuario(db, rol='admin'))
        cuerpo = {'resolucion': 'AJUSTADO', 'motivo': 'x'}
        assert client.post(f'/api/traslados/{st.id}/resolver-faltante', headers=h,
                           json=cuerpo).status_code == 400
        assert client.post('/api/traslados/999999/resolver-faltante', headers=h,
                           json=cuerpo).status_code == 404

    def test_los_roles_de_la_pantalla_son_los_del_servidor(self):
        import re
        from app.routes._auth_helpers import Roles
        src = (RAIZ / 'app/static/pwa/traslados.js').read_text(encoding='utf-8')
        m = re.search(r"TRAS_ROLES_RESUELVEN_FALTANTE = \[([^\]]*)\]", src)
        assert set(re.findall(r"'(\w+)'", m.group(1))) == set(Roles.SUPERVISION)


# ═════════════════════════════════════════════════════════════════════════════
# (b) Ninguna ruta le devuelve al usuario el texto de una excepción inesperada
# ═════════════════════════════════════════════════════════════════════════════

class TestLasRutasDeTrasladosNoMuestranTrazas:

    def _despachar_con(self, app, client, db, producto, monkeypatch, exc):
        from app.services.traslado_service import TrasladoService

        def _revienta(*a, **k):
            raise exc
        monkeypatch.setattr(TrasladoService, 'despachar', staticmethod(_revienta))
        st, it, u = _traslado(db, producto, estado='PREPARADO')
        return client.post(f'/api/traslados/{st.id}/despachar',
                           headers=_jwt(app, _usuario(db, rol='admin')))

    def test_un_error_interno_no_muestra_su_texto(self, app, client, db, producto, monkeypatch):
        r = self._despachar_con(app, client, db, producto, monkeypatch,
                                AttributeError("'NoneType' object has no attribute 'id'"))
        assert r.status_code == 500
        d = r.get_json()
        assert 'NoneType' not in d['error'] and d['referencia'] in d['error']
        assert 'despachar el traslado' in d['error']

    def test_un_rechazo_de_siesa_se_dice_como_de_siesa(self, app, client, db, producto, monkeypatch):
        from app.services.connekta_gateway import ConnektaRechazado
        r = self._despachar_con(app, client, db, producto, monkeypatch,
                                ConnektaRechazado('Item sin cantidad disponible'))
        assert r.status_code == 502 and 'Siesa rechazó' in r.get_json()['error']

    def test_lo_del_usuario_es_4xx(self, app, client, db, producto, monkeypatch):
        r = self._despachar_con(app, client, db, producto, monkeypatch,
                                ValueError('No se puede despachar en estado BORRADOR'))
        assert r.status_code == 400 and 'BORRADOR' in r.get_json()['error']


#: Rutas que todavía devuelven el texto de una excepción genérica, por archivo
#: (conteo de `jsonify` que la nombran). **Solo encoge**; traslados en cero.
PENDIENTES_POR_ARCHIVO = {
    'app/routes/almacenes.py': 1,
    'app/routes/armador.py': 2,
    'app/routes/bloqueo_recompra.py': 1,
    'app/routes/cartera.py': 3,
    'app/routes/compras_bandeja.py': 1,
    'app/routes/conteo.py': 7,
    'app/routes/dashboard.py': 10,
    'app/routes/despacho_parcial.py': 3,
    'app/routes/devoluciones.py': 2,
    'app/routes/empaques.py': 2,
    'app/routes/factura_admin.py': 2,
    'app/routes/health.py': 2,
    'app/routes/kardex.py': 8,
    'app/routes/mobile.py': 3,
    'app/routes/packing.py': 2,
    'app/routes/recepcion.py': 1,
    'app/routes/reposicion.py': 7,
    'app/routes/siesa.py': 3,
    'app/routes/vigia.py': 1,
}


def exponen(src: str) -> int:
    """Cuántos `jsonify(...)` dentro de un `except Exception as e` (o sin
    tipo) nombran la excepción. Un `except ValueError` no cuenta: es del
    usuario. Docstrings y comentarios no cuentan (AST)."""
    n = 0
    for h in ast.walk(ast.parse(src)):
        if not (isinstance(h, ast.ExceptHandler) and h.name):
            continue
        if h.type is not None and not (isinstance(h.type, ast.Name)
                                       and h.type.id in ('Exception', 'BaseException')):
            continue
        for c in ast.walk(h):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == 'jsonify'
                    and any(isinstance(m, ast.Name) and m.id == h.name for m in ast.walk(c))):
                n += 1
    return n


def _conteo():
    out = {}
    for p in sorted((RAIZ / 'app/routes').rglob('*.py')):
        k = exponen(p.read_text(encoding='utf-8'))
        if k:
            out[p.relative_to(RAIZ).as_posix()] = k
    return out


class TestNingunaRutaMuestraLaExcepcion:

    def test_no_crece(self):
        c = _conteo()
        crecio = {k: v for k, v in c.items() if v > PENDIENTES_POR_ARCHIVO.get(k, 0)}
        assert not crecio, (f'{crecio}: una ruta devuelve el texto de una excepción genérica. '
                            f'Use una respuesta en palabras (ver routes/traslados._falla).')

    def test_solo_encoge(self):
        c = _conteo()
        bajo = {k: (v, c.get(k, 0)) for k, v in PENDIENTES_POR_ARCHIVO.items() if c.get(k, 0) < v}
        assert not bajo, f'{bajo}: bajó; actualice PENDIENTES_POR_ARCHIVO'

    def test_traslados_en_cero(self):
        assert 'app/routes/traslados.py' not in _conteo()
        assert 'app/routes/traslados.py' not in PENDIENTES_POR_ARCHIVO

    def test_piso(self):
        assert sum(_conteo().values()) >= 30

    def test_ve_lo_que_debe_y_no_lo_sano(self):
        malo = ('def f():\n    try:\n        x()\n    except Exception as e:\n'
                '        return jsonify({"error": str(e)}), 500\n'
                'def g():\n    try:\n        y()\n    except BaseException as err:\n'
                '        return jsonify({"error": f"falló {err}"}), 500\n')
        sano = ('def f():\n    """except Exception as e: jsonify(str(e))"""\n'
                '    try:\n        x()\n    except ValueError as e:\n'
                '        return jsonify({"error": str(e)}), 400\n'
                '    except Exception as e:  # jsonify(str(e))\n'
                '        return _falla(e, "x")\n')
        assert exponen(malo) == 2 and exponen(sano) == 0
