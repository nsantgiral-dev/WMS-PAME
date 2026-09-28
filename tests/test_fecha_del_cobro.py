"""
La fecha del cobro es la del cobro (validación de la plata, 2026-09-26, P2).

El recibo de caja llevaba el día de `fecha_confirmacion`, que **cada
re-confirmación reescribe**: un dedazo corregido al día siguiente movía el
cobro de día (y de mes). Ahora `recaudos_entrega.fecha_cobro` (m050plata) se
fija en la primera confirmación —hora del teléfono corregida por el desfase
medido, o la del servidor—, no la toca una re-confirmación, y la oficina la
declara en su formulario (por defecto hoy). `politica_cobro.momento_del_cobro`
es lo que lee el recibo.
"""
import ast
import pathlib
from datetime import datetime, timedelta

import pytest

from tests.test_cartera_retencion import _jwt
from tests.test_parada_tardia import mundo  # noqa: F401 — fixture
from tests.test_parada_de_oficina import _cerrar, _url

RAIZ = pathlib.Path(__file__).resolve().parents[1]


class TestLaPolitica:

    def test_telefono_corregido_por_el_desfase_y_nunca_futuro(self):
        from app.services import politica_cobro as pc
        ahora = datetime(2026, 9, 30, 23, 0)
        ts = datetime(2026, 9, 30, 21, 0)
        assert pc.fecha_cobro_de_la_confirmacion(ts, 600, ahora) == ts - timedelta(seconds=600)
        assert pc.fecha_cobro_de_la_confirmacion(None, None, ahora) == ahora
        assert pc.fecha_cobro_de_la_confirmacion(ahora + timedelta(hours=2), 0, ahora) == ahora

    def test_la_oficina_declara_un_dia_de_bogota(self):
        from app.services import politica_cobro as pc
        ahora = datetime(2026, 10, 2, 15, 0)
        f = pc.fecha_cobro_declarada('2026-09-30', ahora)
        from app.utils.fecha import fecha_bogota_de
        assert fecha_bogota_de(f) == '20260930'
        with pytest.raises(ValueError, match='futura'):
            pc.fecha_cobro_declarada('2026-10-05', ahora)
        with pytest.raises(ValueError, match='ilegible'):
            pc.fecha_cobro_declarada('30/09/2026', ahora)

    def test_momento_del_cobro(self):
        from types import SimpleNamespace
        from app.services import politica_cobro as pc
        a, b = datetime(2026, 9, 30), datetime(2026, 10, 1)
        assert pc.momento_del_cobro(SimpleNamespace(fecha_cobro=a, fecha_confirmacion=b)) == a
        assert pc.momento_del_cobro(SimpleNamespace(fecha_cobro=None, fecha_confirmacion=b)) == b


class TestPorLaPuerta:

    def test_una_reconfirmacion_no_mueve_el_cobro(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.packing import TareaPacking
        f, uc, _ = mundo
        v = float(db.session.get(TareaPacking, f.packing_id).valor_factura or 0) or 1000.0
        cuerpo = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': v,
                  'version_formulario': 4, 'observaciones': 'x'}
        assert client.post(_url(f), headers=_jwt(app, uc), json=cuerpo).status_code == 200
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        primera = rec.fecha_cobro
        assert primera is not None
        r = client.post(_url(f), headers=_jwt(app, uc), json={**cuerpo, 'observaciones': 'y'})
        assert r.status_code == 200
        db.session.refresh(rec)
        assert rec.fecha_cobro == primera

    def test_la_oficina_la_declara(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.utils.fecha import dia_operativo, fecha_bogota_de
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        ayer = (dia_operativo() - timedelta(days=1)).isoformat()
        r = client.post(_url(f), headers=_jwt(app, ad), json={
            'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO', 'observaciones': 'x',
            'motivo_tardia': 'y', 'version_formulario': 4, 'fecha_cobro': ayer})
        assert r.status_code == 200, r.get_json()
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert fecha_bogota_de(rec.fecha_cobro) == ayer.replace('-', '')


class TestElReciboLeeElMomentoDelCobro:

    def test_ast(self):
        """El ejecutor del RC arma `fecha_recaudo` con `momento_del_cobro`, no
        con `fecha_confirmacion`; y solo `confirmar_parada` escribe
        `fecha_cobro`."""
        fuente = (RAIZ / 'app/services/siesa_job_service.py').read_text(encoding='utf-8')
        kws = [k for n in ast.walk(ast.parse(fuente)) if isinstance(n, ast.Call)
               for k in n.keywords if k.arg == 'fecha_recaudo']
        assert kws
        for k in kws:
            nombres = {getattr(x, 'attr', None) or getattr(x, 'id', None) for x in ast.walk(k.value)}
            assert 'momento_del_cobro' in nombres and 'fecha_confirmacion' not in nombres
        escritores = set()
        for p in (RAIZ / 'app').rglob('*.py'):
            for n in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for x in ast.walk(n):
                        if isinstance(x, ast.Assign) and any(
                                isinstance(t, ast.Attribute) and t.attr == 'fecha_cobro'
                                for t in x.targets):
                            escritores.add(n.name)
        assert escritores == {'confirmar_parada'}, escritores
