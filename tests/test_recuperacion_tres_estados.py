"""
«No pude preguntar» no es «no existe» — las lecturas de recuperación.

## El caso (P0-6, auditoría 2026-09-25)

`get_sts_info_by_alterno` y `get_consec_entrada_transito_by_alterno` devolvían
`None` en el `except`. `POST /traslados/<id>/reintentar-despacho` y
`/reintentar-recepcion` preguntan justamente eso antes de reenviar —«¿el
documento ya existe?»— y leían ese `None` como «no existe». Con Siesa lento o
caído, el botón que el sistema recomienda para recuperar un traslado
**posteaba un segundo STS o ETS**: la mercancía se descargaba del origen, o se
cargaba al destino, dos veces.

## La clase

Es la de `tests/test_compromisos_vacios.py` (`CompromisosNoDisponibles`) y la de
`esta_saldada` (`True/False/None`): **una lectura que degrada «no sé» hacia una
respuesta que alguien usa para decidir**. Toda lectura de recuperación tiene
tres respuestas —existe / no existe / no sé— y ante «no sé» nadie hace POST ni
marca hecho.

## El trinquete

Por AST, sobre los módulos del gateway: todo método `get_*` que atrapa una
excepción amplia y, sin volver a levantar, devuelve vacío (`None`, `[]`, `{}`,
`False`, `0`) o cae al `return` vacío del final. El inventario de abajo es lo
que hoy sigue así, **con su motivo**; solo encoge.
"""
import ast
import pathlib
from unittest.mock import patch

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Lecturas del gateway que hoy convierten «no pude preguntar» en vacío.
#: Clave: `archivo::metodo`. Valor: por qué todavía se tolera, o de quién es.
#: **Solo encoge.** Las dos de recuperación de traslados salieron el
#: 2026-09-25 y no pueden volver.
LECTURAS_QUE_TRAGAN = {
    'connekta_consultas_gateway.py::get_estado_pedido': (
        'Devuelve None ante error de red y el caller lo trata como «no bloquear»: '
        'el POST posterior revela el error. No decide un reenvío ni marca hecho '
        'por sí solo. Frente de despacho de pedidos.'),
    'connekta_consultas_gateway.py::get_punto_envio_factura': (
        'Punto de envío para la FE; {} cae al fallback SIESA_PUNTO_ENVIO_DEFAULT '
        'y, sin él, la FE la rechaza Siesa (ruidoso). Frente fiscal.'),
    'connekta_consultas_gateway.py::get_detalle_factura': (
        'Detalle de líneas de una FE para mostrar/prorratear. [] se lee como '
        '«sin líneas» y bloquea el cálculo aguas abajo, no reenvía. Frente '
        'liquidación (NC).'),
    'connekta_consultas_gateway.py::get_pedido_cabecera': (
        'Cabecera (NIT, condición de pago) del pedido. None cae al camino '
        'conservador del cond_pago (SIESA_COND_PAGO_RUTA o ValueError), no a '
        'un POST de más. Frente fiscal.'),
    'connekta_consultas_gateway.py::get_pedido_rowid_map': (
        'Levanta CompromisosNoDisponibles (el caso que importa); el {} del '
        'except amplio queda para errores de formato. Frente de despacho.'),
    'connekta_consultas_gateway.py::get_terceros_contacto': (
        'Contacto del cliente para mostrar al conductor. [] = no se muestra el '
        'bloque; no mueve inventario ni dinero.'),
    'connekta_consultas_gateway.py::get_vendedor_contacto': (
        'Teléfono del asesor para mostrar al conductor. [] = no se muestra el '
        'bloque (Regla 0 ya aplicada: no inventa nombre).'),
    'connekta_consultas_gateway.py::get_cxc_general': (
        'Cartera abierta. [] ante error: los callers de cruce ya usan '
        'cxc_cruce.esta_saldada con tres estados; el uso restante es de '
        'lectura/pantalla. Frente liquidación/cartera.'),
    'connekta_liquidacion_gateway.py::get_max_rowid_nc': (
        'Marca de agua antes del POST de la NC. None deja el motivo DIAN en '
        'MANUAL (exige exactamente una coincidencia) — conservador. Frente '
        'liquidación.'),
    'connekta_traslados_gateway.py::get_consec_rit_by_referencia': (
        'Consecutivo de la RIT. Con TRASLADO_USA_RIT apagada (default) no decide '
        'nada; con ella encendida, None deja la RIT huérfana y el despacho sale '
        'por el 173076 — no duplica la RIT porque no la reenvía.'),
}

#: Las que se cerraron con este archivo y no pueden volver al inventario.
CERRADAS = (
    'connekta_traslados_gateway.py::get_sts_info_by_alterno',
    'connekta_traslados_gateway.py::get_consec_entrada_transito_by_alterno',
    # Cerrada por el frente fiscal (RemisionNoDisponible, P0-2): el «no sé»
    # de la remisión decidía un segundo 142945.
    'connekta_consultas_gateway.py::get_remision_desde_pedido',
)


def _es_vacio(nodo) -> bool:
    if nodo is None:
        return True
    if isinstance(nodo, ast.Constant) and nodo.value in (None, False, 0, ''):
        return True
    if isinstance(nodo, (ast.List, ast.Tuple)) and not nodo.elts:
        return True
    if isinstance(nodo, ast.Dict) and not nodo.keys:
        return True
    return False


def _handler_amplio(h: ast.ExceptHandler) -> bool:
    t = h.type
    return t is None or (isinstance(t, ast.Name) and t.id in ('Exception', 'BaseException'))


def _traga(fn: ast.FunctionDef) -> bool:
    """¿El método convierte una excepción amplia en una respuesta vacía?

    · un `except Exception` que no levanta y devuelve vacío adentro, o
    · uno que no levanta ni devuelve, y el método termina en `return` vacío
      (o sin `return`: None implícito).
    """
    cuerpo_final = fn.body[-1] if fn.body else None
    termina_vacio = (not isinstance(cuerpo_final, ast.Return)) or _es_vacio(cuerpo_final.value)
    for n in ast.walk(fn):
        if not isinstance(n, ast.Try):
            continue
        for h in n.handlers:
            if not _handler_amplio(h):
                continue
            if any(isinstance(x, ast.Raise) for x in ast.walk(h)):
                continue
            rets = [x for x in ast.walk(h) if isinstance(x, ast.Return)]
            if rets and all(_es_vacio(r.value) for r in rets):
                return True
            if not rets and termina_vacio:
                return True
    return False


def _lecturas_que_tragan(base: pathlib.Path = None) -> tuple:
    """(set de `archivo::metodo` que tragan, cantidad de `get_*` revisados)."""
    base = base or RAIZ
    hallados, revisados = set(), 0
    for f in sorted((base / 'app' / 'services').glob('connekta_*.py')):
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        for clase in ast.walk(arbol):
            if not isinstance(clase, ast.ClassDef):
                continue
            for fn in clase.body:
                if isinstance(fn, ast.FunctionDef) and fn.name.startswith('get_'):
                    revisados += 1
                    if _traga(fn):
                        hallados.add(f'{f.name}::{fn.name}')
    return hallados, revisados


# ─────────────────────────────────────────────────────────────────────────────
# El caso: las dos lecturas de recuperación de traslados
# ─────────────────────────────────────────────────────────────────────────────

def _gw():
    from app.services.connekta_gateway import ConnektaGateway
    g = ConnektaGateway()
    g.modo_simulacion = False
    g._cb_state = 'CLOSED'
    return g


class TestLasTresRespuestas:

    @pytest.mark.parametrize('metodo', ['get_sts_info_by_alterno',
                                        'get_consec_entrada_transito_by_alterno'])
    def test_un_fallo_de_red_es_no_se(self, metodo):
        from app.services.connekta_gateway import RecuperacionNoDisponible
        gw = _gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('timeout')):
            with pytest.raises(RecuperacionNoDisponible):
                getattr(gw, metodo)('ST-20260925-0001')

    @pytest.mark.parametrize('metodo', ['get_sts_info_by_alterno',
                                        'get_consec_entrada_transito_by_alterno'])
    def test_el_circuito_abierto_es_no_se(self, metodo):
        """`_get` devuelve None con el circuito abierto: no preguntó nada."""
        from app.services.connekta_gateway import RecuperacionNoDisponible
        gw = _gw()
        with patch.object(gw, '_get', return_value=None):
            with pytest.raises(RecuperacionNoDisponible):
                getattr(gw, metodo)('ST-20260925-0001')

    @pytest.mark.parametrize('metodo', ['get_sts_info_by_alterno',
                                        'get_consec_entrada_transito_by_alterno'])
    def test_una_fila_sin_consecutivo_legible_es_no_se(self, metodo):
        """El documento EXISTE: devolver None invitaría a crearlo otra vez."""
        from app.services.connekta_gateway import RecuperacionNoDisponible
        gw = _gw()
        with patch.object(gw, '_get', return_value={'detalle': {'Table': [
                {'f350_consec_docto': None}]}}):
            with pytest.raises(RecuperacionNoDisponible):
                getattr(gw, metodo)('ST-20260925-0001')

    @pytest.mark.parametrize('metodo', ['get_sts_info_by_alterno',
                                        'get_consec_entrada_transito_by_alterno'])
    def test_siesa_contesto_vacio_es_no_existe(self, metodo):
        """El único caso que autoriza reenviar."""
        gw = _gw()
        with patch.object(gw, '_get', return_value={'detalle': {'Table': []}}):
            assert getattr(gw, metodo)('ST-20260925-0001') is None

    def test_encontrado_devuelve_el_consecutivo(self):
        gw = _gw()
        with patch.object(gw, '_get', return_value={'detalle': {'Table': [
                {'f350_consec_docto': '53', 'f150_id_bodega_entrada': 'TRA1'}]}}):
            assert gw.get_sts_info_by_alterno('ST-1') == {'consec': 53, 'bodega_transito': 'TRA1'}
            assert gw.get_consec_entrada_transito_by_alterno('ST-1') == 53


@pytest.fixture
def traslado(db, usuario_admin):
    from app.models.traslado import SolicitudTraslado
    s = SolicitudTraslado(
        codigo='ST-20260925-RC01', bodega_origen_siesa='NB1',
        bodega_destino_siesa='NC1', nombre_punto_venta='NC', estado='EN_TRANSITO',
        modo_transferencia='EN_TRANSITO', bodega_transito_siesa='TRA1',
        solicitante_id=usuario_admin.id)
    db.session.add(s)
    db.session.commit()
    return s


class TestLosBotonesNoReenvianSinConfirmacion:
    """La ruta es la que postea: se mide que ante «no sé» el POST NO sale."""

    def _post(self, client, token, s, que):
        return client.post(f'/api/traslados/{s.id}/{que}', json={},
                           headers={'Authorization': f'Bearer {token}'})

    def test_reintentar_despacho_con_no_se_no_postea(self, client, jwt_token_admin, traslado):
        from app.services.connekta_gateway import ConnektaGateway, RecuperacionNoDisponible
        from app.services.siesa_traslado_adapter import SiesaTrasladoAdapter
        with patch.object(SiesaTrasladoAdapter, 'recuperar_consec_salida',
                          side_effect=RecuperacionNoDisponible('caído')), \
                patch.object(ConnektaGateway, 'transferencia_transito_salida') as post:
            r = self._post(client, jwt_token_admin, traslado, 'reintentar-despacho')
        assert r.status_code == 409
        assert r.get_json()['resultado_desconocido'] is True
        post.assert_not_called()

    def test_reintentar_despacho_con_no_existe_si_postea(self, client, jwt_token_admin, traslado):
        """La otra dirección: un guard que bloquea todo prueba la mitad."""
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.siesa_traslado_adapter import SiesaTrasladoAdapter
        with patch.object(SiesaTrasladoAdapter, 'recuperar_consec_salida', return_value=None), \
                patch.object(ConnektaGateway, 'transferencia_transito_salida',
                             return_value={'simulado': True}) as post:
            r = self._post(client, jwt_token_admin, traslado, 'reintentar-despacho')
        assert r.status_code == 200
        post.assert_called_once()

    def test_reintentar_despacho_directo_no_postea(self, db, client, jwt_token_admin, traslado):
        """Un 173066 no aparece en la consulta del STS: su «no está» no
        confirma nada."""
        from app.services.connekta_gateway import ConnektaGateway
        traslado.modo_transferencia = 'DIRECTA'
        db.session.commit()
        with patch.object(ConnektaGateway, 'transferencia_directa') as post:
            r = self._post(client, jwt_token_admin, traslado, 'reintentar-despacho')
        assert r.status_code == 409
        post.assert_not_called()

    def test_reintentar_recepcion_con_no_se_no_postea(self, db, client, jwt_token_admin, traslado):
        from app.services.connekta_gateway import ConnektaGateway, RecuperacionNoDisponible
        from app.services.siesa_traslado_adapter import SiesaTrasladoAdapter
        traslado.estado = 'ENTREGADA'
        traslado.siesa_salida_consec = 1001
        db.session.commit()
        with patch.object(SiesaTrasladoAdapter, 'recuperar_consec_entrada',
                          side_effect=RecuperacionNoDisponible('caído')), \
                patch.object(ConnektaGateway, 'transferencia_transito_entrada') as post:
            r = self._post(client, jwt_token_admin, traslado, 'reintentar-recepcion')
        assert r.status_code == 409
        post.assert_not_called()

    def test_reintentar_recepcion_con_no_existe_si_postea(self, db, client, jwt_token_admin, traslado):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.siesa_traslado_adapter import SiesaTrasladoAdapter
        traslado.estado = 'ENTREGADA'
        traslado.siesa_salida_consec = 1001
        db.session.commit()
        with patch.object(SiesaTrasladoAdapter, 'recuperar_consec_entrada', return_value=None), \
                patch.object(ConnektaGateway, 'transferencia_transito_entrada',
                             return_value={'simulado': True}) as post:
            r = self._post(client, jwt_token_admin, traslado, 'reintentar-recepcion')
        assert r.status_code == 200
        post.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# La clase: ninguna lectura nueva del gateway traga «no sé»
# ─────────────────────────────────────────────────────────────────────────────

class TestNingunaLecturaNuevaTragaElNoSe:

    def test_la_lista_no_crece_sin_decision(self):
        hallados, _ = _lecturas_que_tragan()
        nuevos = sorted(hallados - set(LECTURAS_QUE_TRAGAN))
        assert not nuevos, (
            '\nLecturas del gateway que convierten «no pude preguntar» en vacío y '
            'NO están declaradas:\n  · ' + '\n  · '.join(nuevos)
            + '\n\nSi el resultado decide un reenvío o marca algo hecho, levantá '
              'una excepción (ver RecuperacionNoDisponible). Si no, declarala '
              'con su motivo en LECTURAS_QUE_TRAGAN.')

    def test_la_lista_solo_encoge(self):
        hallados, _ = _lecturas_que_tragan()
        sobran = sorted(set(LECTURAS_QUE_TRAGAN) - hallados)
        assert not sobran, f'Ya no tragan — sacalas del inventario: {sobran}'

    def test_toda_entrada_dice_por_que(self):
        flojas = [k for k, v in LECTURAS_QUE_TRAGAN.items() if len(v) < 60]
        assert not flojas, f'Motivos demasiado cortos: {flojas}'

    def test_las_cerradas_no_vuelven(self):
        hallados, _ = _lecturas_que_tragan()
        for k in CERRADAS:
            assert k not in LECTURAS_QUE_TRAGAN
            assert k not in hallados, f'{k} volvió a tragar el «no sé»'


class TestElDetectorMuerde:
    """Meta-tests sobre un árbol de mentira con el escáner REAL."""

    def _en(self, tmp_path, fuente):
        d = tmp_path / 'app' / 'services'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'connekta_x_gateway.py').write_text(fuente, encoding='utf-8')
        return _lecturas_que_tragan(base=tmp_path)[0]

    def test_ve_el_return_none_en_el_except(self, tmp_path):
        src = ('class G:\n    def get_a(self):\n        try:\n            return 1\n'
               '        except Exception as e:\n            return None\n')
        assert self._en(tmp_path, src) == {'connekta_x_gateway.py::get_a'}

    def test_ve_el_except_que_cae_al_return_vacio(self, tmp_path):
        """La forma que tenían las dos de traslados: log en el except y
        `return None` al final del método."""
        src = ('class G:\n    def get_b(self):\n        try:\n            x = 1\n'
               '        except Exception as e:\n            log(e)\n        return None\n')
        assert self._en(tmp_path, src) == {'connekta_x_gateway.py::get_b'}

    def test_ve_la_lista_y_el_dict_vacios(self, tmp_path):
        src = ('class G:\n    def get_c(self):\n        try:\n            return [1]\n'
               '        except Exception:\n            return []\n'
               '    def get_d(self):\n        try:\n            return {1: 1}\n'
               '        except:\n            return {}\n')
        assert self._en(tmp_path, src) == {'connekta_x_gateway.py::get_c',
                                           'connekta_x_gateway.py::get_d'}

    def test_NO_marca_el_except_que_levanta(self, tmp_path):
        src = ('class G:\n    def get_e(self):\n        try:\n            return 1\n'
               '        except Exception as e:\n            raise X() from e\n')
        assert self._en(tmp_path, src) == set()

    def test_NO_marca_un_except_estrecho(self, tmp_path):
        src = ('class G:\n    def get_f(self):\n        try:\n            return int(x)\n'
               '        except ValueError:\n            return None\n')
        assert self._en(tmp_path, src) == set()

    def test_un_docstring_no_cuenta(self, tmp_path):
        src = ('class G:\n    def get_g(self):\n        """except Exception: return None"""\n'
               '        return 1\n')
        assert self._en(tmp_path, src) == set()

    def test_piso_minimo(self):
        """Si el escáner se desincroniza devuelve cero, y cero se lee igual
        que «no hay nada que hacer»."""
        hallados, revisados = _lecturas_que_tragan()
        assert revisados >= 40, revisados
        assert len(hallados) >= 8, hallados
