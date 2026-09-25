"""Una foto que la base no acepta es 400, nunca 500 (QA e2e 2026-09-24, P3).

**La clase:** *un valor que un CHECK de la base rechaza llega al commit sin
validarse antes*. `angulo: 'lateral_izquierda'` explotaba en `ck_flota_angulo`
→ 500, y la cola del conductor (`flotaColaEnviarUna`) reintenta todo 5xx: el
ítem no sale nunca y traba los que vienen detrás. Lo mismo una clase de foto
desconocida (`ClaseFoto(...)` → ValueError) o un data URL ilegible
(`ErrorAlmacen`, que ninguna de las tres puertas atrapaba).

- `almacen_fotos.validar_fotos` valida contra el MISMO vocabulario del CHECK,
  antes de escribir (y `colgar_fotos` la llama: ningún adaptador la esquiva).
- Trinquete por AST: toda ruta de `flota/api/` que lee un campo `fotos*` del
  cuerpo traduce `FotoInvalida` y `ErrorAlmacen` a 400, ANTES de cualquier
  `except ErrorFlota` (que sería 409) — con piso y meta-tests.
- Por HTTP: las tres puertas con fotos (traspaso, daño, tanqueo).
"""
import ast
from pathlib import Path

import pytest

from tests.test_qa_e2e_flota_20260924 import _DATA_URL, _auth, _foto, mundo  # noqa: F401

API = Path(__file__).resolve().parents[2] / 'flota' / 'api'


# ═════════════════════════════════════════════════════════════════════════
# Trinquete: toda puerta con fotos traduce la foto inválida a 400
# ═════════════════════════════════════════════════════════════════════════

def _nombres_except(h):
    t = h.type
    if t is None:
        return {'<todo>'}
    elts = t.elts if isinstance(t, ast.Tuple) else [t]
    return {getattr(e, 'id', getattr(e, 'attr', None)) for e in elts}


def _lee_fotos(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                and isinstance(n.slice.value, str) and n.slice.value.startswith('fotos'):
            return True
    return False


def puertas_con_fotos(fuentes):
    """{(archivo, funcion): ok} para toda función que lee `datos['fotos…']`.
    ok = hay un `try` con un handler que nombra FotoInvalida Y ErrorAlmacen,
    antes de cualquier handler de ErrorFlota / Exception / except desnudo."""
    out = {}
    for archivo, texto in fuentes.items():
        for fn in ast.walk(ast.parse(texto)):
            if not isinstance(fn, ast.FunctionDef) or not _lee_fotos(fn):
                continue
            ok = False
            for t in ast.walk(fn):
                if not isinstance(t, ast.Try):
                    continue
                vistos = set()
                for h in t.handlers:
                    nombres = _nombres_except(h)
                    if {'FotoInvalida', 'ErrorAlmacen'} <= nombres | vistos \
                            and not ({'ErrorFlota', 'Exception', '<todo>'} & vistos):
                        ok = True
                    vistos |= nombres
            out[(archivo, fn.name)] = ok
    return out


def _fuentes_api():
    return {p.name: p.read_text(encoding='utf-8') for p in sorted(API.glob('*.py'))}


class TestTodaPuertaConFotosDa400:
    def test_ninguna_puerta_sin_traducir(self):
        malas = [k for k, ok in puertas_con_fotos(_fuentes_api()).items() if not ok]
        assert not malas, ('Puertas que reciben fotos sin traducir FotoInvalida/ErrorAlmacen '
                           f'a 400 antes de ErrorFlota: {malas}')

    def test_piso(self):
        assert len(puertas_con_fotos(_fuentes_api())) >= 3

    def test_ve_la_puerta_sin_traduccion(self):
        src = ("def f():\n    x = datos['fotos']\n    try:\n        g()\n"
               "    except ErrorFlota as e:\n        return 409\n")
        assert puertas_con_fotos({'x.py': src}) == {('x.py', 'f'): False}

    def test_ve_el_orden_equivocado(self):
        src = ("def f():\n    x = datos['fotos_fin']\n    try:\n        g()\n"
               "    except ErrorFlota:\n        return 409\n"
               "    except (FotoInvalida, ErrorAlmacen):\n        return 400\n")
        assert puertas_con_fotos({'x.py': src}) == {('x.py', 'f'): False}

    def test_acepta_la_forma_correcta_y_no_mira_lo_que_no_tiene_fotos(self):
        src = ("def f():\n    x = datos['fotos']\n    try:\n        g()\n"
               "    except (FotoInvalida, ErrorAlmacen):\n        return 400\n"
               "    except ErrorFlota:\n        return 409\n"
               "def h():\n    try:\n        g()\n    except ErrorFlota:\n        return 409\n")
        assert puertas_con_fotos({'x.py': src}) == {('x.py', 'f'): True}


# ═════════════════════════════════════════════════════════════════════════
# La validación
# ═════════════════════════════════════════════════════════════════════════

class TestValidarFotos:
    def test_angulo_desconocido(self):
        from flota.adaptadores.almacen_fotos import validar_fotos
        from flota.dominio.errores import FotoInvalida
        with pytest.raises(FotoInvalida, match='lateral_izquierda'):
            validar_fotos([_foto('evidencia_estado', 'lateral_izquierda')])

    def test_clase_desconocida(self):
        from flota.adaptadores.almacen_fotos import validar_fotos
        from flota.dominio.errores import FotoInvalida
        with pytest.raises(FotoInvalida, match='clase'):
            validar_fotos([_foto('selfie', 'frontal')])

    def test_todos_los_angulos_del_check_pasan(self):
        from flota.adaptadores.almacen_fotos import validar_fotos
        from flota.adaptadores.modelos import ANGULO_FOTO
        validar_fotos([_foto('evidencia_estado', a) for a in ANGULO_FOTO])
        validar_fotos([{'clase': 'evidencia_estado'}])   # sin ángulo: filas viejas
        validar_fotos(None)


# ═════════════════════════════════════════════════════════════════════════
# Por HTTP, las tres puertas
# ═════════════════════════════════════════════════════════════════════════

class TestLasTresPuertas:
    def test_traspaso_clase_desconocida_es_400(self, client, db, mundo):  # noqa: F811
        from tests.flota._turno import dar_turno
        dar_turno(db, mundo.u_cond.id, mundo.placa, km=1000)
        r = client.post('/flota/custodia/traspaso', headers=_auth(mundo.t_cond), json={
            'placa': mundo.placa, 'km': 1010, 'ubicacion': 'sede', 'custodio_tipo': 'sede',
            'custodio_sede_id': mundo.almacen.id, 'fotos_fin': [_foto('selfie', 'frontal')]})
        assert r.status_code == 400, (r.status_code, r.get_json())

    def test_traspaso_data_url_ilegible_es_400(self, client, db, mundo):  # noqa: F811
        from tests.flota._turno import dar_turno
        dar_turno(db, mundo.u_cond.id, mundo.placa, km=1000)
        f = _foto('evidencia_estado', 'frontal')
        f['data_url'] = 'esto no es una imagen'
        r = client.post('/flota/custodia/traspaso', headers=_auth(mundo.t_cond), json={
            'placa': mundo.placa, 'km': 1010, 'ubicacion': 'sede', 'custodio_tipo': 'sede',
            'custodio_sede_id': mundo.almacen.id, 'fotos_fin': [f]})
        assert r.status_code == 400, (r.status_code, r.get_json())

    def test_el_traspaso_rechazado_no_deja_nada_escrito(self, client, db, mundo):  # noqa: F811
        from flota.adaptadores.modelos import Custodia, Foto
        from tests.flota._turno import dar_turno
        dar_turno(db, mundo.u_cond.id, mundo.placa, km=1000)
        antes = (Custodia.query.count(), Foto.query.count())
        client.post('/flota/custodia/traspaso', headers=_auth(mundo.t_cond), json={
            'placa': mundo.placa, 'km': 1010, 'ubicacion': 'sede', 'custodio_tipo': 'sede',
            'custodio_sede_id': mundo.almacen.id,
            'fotos_fin': [_foto('evidencia_estado', 'frontal'),
                          _foto('evidencia_estado', 'lateral_izquierda')]})
        assert (Custodia.query.count(), Foto.query.count()) == antes

    def test_dano_con_angulo_desconocido_es_400(self, client, db, mundo):  # noqa: F811
        r = client.post('/flota/hallazgos', headers=_auth(mundo.t_flota), json={
            'placa': mundo.placa, 'criticidad': 'menor', 'descripcion': 'rayón', 'km': 1000,
            'fotos': [_foto('evidencia_estado', 'lateral_izquierda')]})
        assert r.status_code == 400, (r.status_code, r.get_json())

    def test_tanqueo_con_clase_desconocida_es_400(self, client, db, mundo):  # noqa: F811
        from app.utils.fecha import dia_operativo
        r = client.post('/flota/tanqueos', headers=_auth(mundo.t_flota), json={
            'placa': mundo.placa, 'fecha': dia_operativo().isoformat(), 'valor': '120000',
            'galones': '10', 'tanque': 'lleno', 'estacion': 'Terpel', 'km': 1000,
            'proveedor': 'Terpel', 'origen_costo': 'efectivo_conductor',
            'fotos': [_foto('recibo_raro', None)]})
        assert r.status_code == 400, (r.status_code, r.get_json())
