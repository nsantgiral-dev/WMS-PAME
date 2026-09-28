"""
El monto del recibo de caja no lo cambia quien liquida (validación de la
plata, 2026-09-26, P0).

## La clase

*Una escritura del monto del recibo de caja que no pasa por la política de
corrección.* En una PARCIAL, `monto_rc` tomaba el `monto_override` de quien
liquida tal cual: la pantalla ofrecía el campo en toda PARCIAL y el
liquidador —sin `puede_corregir_cobro`— mandaba el RC por menos de lo que el
conductor declaró y entregó; el faltante quedaba de saldo del cliente en
cartera. Y `corregir_monto_declarado` no tenía tope.

## Ahora

- `politica_cobro.exigir_override_permitido` (la llama `monto_rc`, la única que
  arma el monto del RC): en una PARCIAL el override tiene que ser lo declarado;
  en un ENTREGADO sin nada declarado no hay override.
- `politica_cobro.exigir_correccion_de_monto` (la llama
  `corregir_monto_declarado`, que la ruta abre solo a admin + líder de
  cartera): razón obligatoria y tope (la factura; sin ella, una PARCIAL no
  sube).

## El trinquete (AST)

Toda función de `app/` que recibe `monto_override` se lo pasa a `monto_rc`, y
`monto_rc` llama a la política antes de devolver nada.
"""
import ast
import pathlib
from types import SimpleNamespace

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _rec(estado='PARCIAL', monto=600000, valor=None, descuento=0):
    return SimpleNamespace(estado_entrega=estado, monto_cobrado=monto,
                           monto_descuento=descuento,
                           tarea=SimpleNamespace(valor_factura=valor))


class TestElOverride:

    def test_una_parcial_sale_por_lo_declarado(self):
        from app.services import politica_cobro as pc
        r = _rec()
        assert pc.monto_rc(r) == 600000
        assert pc.monto_rc(r, monto_override=600000.3) == 600000
        for otro in (400000, 700000):
            with pytest.raises(pc.MontoNoPermitido, match='Corregir monto'):
                pc.monto_rc(r, monto_override=otro)

    def test_un_entregado_sin_nada_declarado_no_admite_override(self):
        from app.services import politica_cobro as pc
        with pytest.raises(pc.MontoNoPermitido, match='no declaró'):
            pc.monto_rc(_rec('ENTREGADO', 0), total_neto_siesa=1000, monto_override=1000)
        # Con lo declarado, el override (el neto de Siesa) lo acota el guard de
        # diferencia al registrar, como siempre.
        assert pc.monto_rc(_rec('ENTREGADO', 990), monto_override=1000) == 1000


class TestLaCorreccion:

    def test_razon_obligatoria(self):
        from app.services import politica_cobro as pc
        with pytest.raises(pc.MontoNoPermitido, match='razón'):
            pc.exigir_correccion_de_monto(_rec(), 500000, '  ')

    def test_tope_la_factura(self):
        from app.services import politica_cobro as pc
        r = _rec(valor=1000000)
        assert pc.exigir_correccion_de_monto(r, 1000000, 'pagó el resto') == 1000000
        with pytest.raises(pc.MontoNoPermitido, match='supera'):
            pc.exigir_correccion_de_monto(r, 1000200, 'pagó de más')

    def test_sin_factura_una_parcial_solo_baja(self):
        from app.services import politica_cobro as pc
        r = _rec()
        assert pc.exigir_correccion_de_monto(r, 550000, 'error de digitación') == 550000
        with pytest.raises(pc.MontoNoPermitido, match='supera'):
            pc.exigir_correccion_de_monto(r, 650000, 'pagó de más')

    def test_sin_factura_un_entregado_queda_para_el_guard_de_siesa(self):
        from app.services import politica_cobro as pc
        assert pc.exigir_correccion_de_monto(_rec('ENTREGADO', 58000), 60101.3, 'x') == 60101.3

    def test_por_http_el_liquidador_no_corrige(self, app, client, db):
        """La ruta de la corrección exige `puede_corregir_cobro`."""
        from tests.test_cartera_retencion import _jwt, _usuario
        liq = _usuario(db, 'liquidador')
        r = client.post('/api/rutas/1/recaudos/1/corregir-monto', headers=_jwt(app, liq),
                        json={'monto': 1, 'razon': 'x'})
        assert r.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete
# ═════════════════════════════════════════════════════════════════════════════

def _funciones_con_override(fuente: str):
    """{nombre: nodo} de las funciones que reciben un parámetro `monto_override`."""
    out = {}
    for n in ast.walk(ast.parse(fuente)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nombres = [a.arg for a in n.args.args + n.args.kwonlyargs]
            if 'monto_override' in nombres:
                out[n.name] = n
    return out


def _pasa_override_a(fn, destino: str) -> bool:
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and (getattr(c.func, 'id', None)
                                        or getattr(c.func, 'attr', None)) == destino:
            if any(k.arg == 'monto_override' and isinstance(k.value, ast.Name)
                   and k.value.id == 'monto_override' for k in c.keywords):
                return True
    return False


def _llama(fn, nombre: str) -> bool:
    return any(isinstance(c, ast.Call) and (getattr(c.func, 'id', None)
                                            or getattr(c.func, 'attr', None)) == nombre
               for c in ast.walk(fn))


class TestTodoMontoDelRcPasaPorLaPolitica:

    def test_quien_recibe_el_override_se_lo_pasa_a_monto_rc(self):
        vistas = {}
        for p in sorted((RAIZ / 'app').rglob('*.py')):
            for nombre, fn in _funciones_con_override(p.read_text(encoding='utf-8')).items():
                vistas[(str(p.relative_to(RAIZ)), nombre)] = fn
        assert len(vistas) >= 2, sorted(vistas)       # piso: monto_rc + registrar
        for (rel, nombre), fn in vistas.items():
            if nombre == 'exigir_override_permitido':
                continue                              # la política misma
            if nombre == 'monto_rc':
                assert _llama(fn, 'exigir_override_permitido'), rel
            else:
                assert _pasa_override_a(fn, 'monto_rc'), (rel, nombre)

    def test_el_detector_ve_y_no_inventa(self):
        src = '''
def a(x, monto_override=None):
    return monto_rc(x, monto_override=monto_override)
def b(x, monto_override=None):
    return x + monto_override
def c(x):
    """monto_override"""
    return monto_rc(x, monto_override=1)
'''
        fns = _funciones_con_override(src)
        assert set(fns) == {'a', 'b'}
        assert _pasa_override_a(fns['a'], 'monto_rc')
        assert not _pasa_override_a(fns['b'], 'monto_rc')
