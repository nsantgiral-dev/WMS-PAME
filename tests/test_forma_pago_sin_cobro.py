"""Sin cobro no hay forma de pago (QA e2e 2026-09-24, P3).

**La clase:** *una forma de pago guardada en una parada donde no hubo cobro*.
«No pagó y se quedó» con el select trayendo CREDITO quedaba
`forma_pago='CREDITO'` sobre un ENTREGADO_SIN_PAGO: la liquidación lo pintaba
«$0 · CREDITO», el desglose lo contaba en la matriz como «CREDITO |
ENTREGADO_SIN_PAGO» y lo listaba en `paradas_credito` — sobre una factura de
contado contraentrega. Lo mismo un RECHAZADO con la forma del select.

- `recaudo_entrega.forma_pago_de(estado, forma)` es la única respuesta:
  `None` en `EstadoEntrega.SIN_COBRO`. La usa quien escribe
  (`confirmar_parada`) y quien lee (`to_dict`, desglose): las filas viejas
  también se leen bien.
- Trinquete por AST: toda escritura de `forma_pago` sobre un recaudo en
  `app/` (asignación a `.forma_pago` o `RecaudoEntrega(forma_pago=...)`) pasa
  por `forma_pago_de` o es `None` literal. Meta-tests y piso.
"""
import ast
from pathlib import Path

from tests.test_qa_e2e_flota_20260924 import _DATA_URL, _auth, _ruta_con_parada, mundo  # noqa: F401

RAIZ = Path(__file__).resolve().parents[1]


class TestLaPolitica:
    def test_sin_cobro_no_hay_forma(self):
        from app.models.recaudo_entrega import EstadoEntrega, forma_pago_de
        for e in EstadoEntrega.SIN_COBRO:
            assert forma_pago_de(e, 'CREDITO') is None
        assert forma_pago_de('ENTREGADO', 'EFECTIVO') == 'EFECTIVO'
        assert forma_pago_de('PARCIAL', 'CREDITO') == 'CREDITO'

    def test_sin_cobro_es_lo_que_no_cobra(self):
        from app.models.recaudo_entrega import EstadoEntrega
        assert set(EstadoEntrega.SIN_COBRO) == {'RECHAZADO', 'ENTREGADO_SIN_PAGO'}
        assert set(EstadoEntrega.SIN_COBRO) <= set(EstadoEntrega.TODOS)


# ═════════════════════════════════════════════════════════════════════════
# Trinquete: toda escritura de forma_pago pasa por la política
# ═════════════════════════════════════════════════════════════════════════

def _pasa_por_la_politica(v):
    if isinstance(v, ast.Constant) and v.value is None:
        return True
    return isinstance(v, ast.Call) and getattr(v.func, 'id', getattr(v.func, 'attr', None)) \
        in ('forma_pago_de', '_fp_de')


def escrituras_de_forma_pago(fuentes):
    """[(archivo, linea, ok)] de `x.forma_pago = v` y `RecaudoEntrega(forma_pago=v)`."""
    out = []
    for nombre, texto in fuentes.items():
        for n in ast.walk(ast.parse(texto)):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and t.attr == 'forma_pago':
                        out.append((nombre, n.lineno, _pasa_por_la_politica(n.value)))
            elif isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'RecaudoEntrega':
                for kw in n.keywords:
                    if kw.arg == 'forma_pago':
                        out.append((nombre, n.lineno, _pasa_por_la_politica(kw.value)))
    return out


def _fuentes_app():
    return {str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8')
            for p in (RAIZ / 'app').rglob('*.py')}


class TestTodaEscrituraPasaPorLaPolitica:
    def test_ninguna_escritura_cruda(self):
        malas = [f'{a}:{ln}' for a, ln, ok in escrituras_de_forma_pago(_fuentes_app()) if not ok]
        assert not malas, ('forma_pago escrita sin `forma_pago_de` — sobre un rechazo o un '
                           f'«no pagó y se quedó» quedaría la forma del select: {malas}')

    def test_piso(self):
        assert len(escrituras_de_forma_pago(_fuentes_app())) >= 2

    def test_el_detector_ve_la_escritura_cruda_y_no_la_sana(self):
        src = ("recaudo.forma_pago = forma_pago\n"
               "recaudo.forma_pago = forma_pago_de(e, f)\n"
               "RecaudoEntrega(ruta_id=1, forma_pago='CREDITO')\n"
               "RecaudoEntrega(ruta_id=1, forma_pago=None)\n"
               "x = recaudo.forma_pago\n")
        assert [ok for _a, _l, ok in escrituras_de_forma_pago({'x.py': src})] == \
            [False, True, False, True]


# ═════════════════════════════════════════════════════════════════════════
# Por el servicio y por el desglose
# ═════════════════════════════════════════════════════════════════════════

class TestLaParadaSinCobroNoEsCredito:
    def test_rechazo_con_el_select_en_credito_no_guarda_forma(self, db, mundo):  # noqa: F811
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        ruta, t = _ruta_con_parada(db, mundo, estado='EN_TRANSITO')
        RutaService.confirmar_parada(ruta.id, t.id, mundo.u_cond.id, {
            'version_formulario': 3, 'estado_entrega': 'RECHAZADO',
            'motivo_rechazo': 'CLIENTE_CERRADO', 'forma_pago': 'CREDITO',
            'observaciones': 'cerrado', 'monto_cobrado': 0,
            'foto_entrega': _DATA_URL, 'geo': {'fuente': 'sin_dato', 'motivo': 'sin_senal'}})
        rec = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=t.id).one()
        assert rec.estado_entrega == 'RECHAZADO' and rec.forma_pago is None

    def test_la_fila_vieja_no_sale_como_credito(self, client, db, mundo):  # noqa: F811
        """Una fila escrita antes del arreglo (ENTREGADO_SIN_PAGO + CREDITO)."""
        from app.models.recaudo_entrega import RecaudoEntrega
        ruta, t = _ruta_con_parada(db, mundo, estado='ENTREGADA')
        rec = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='ENTREGADO_SIN_PAGO',
                             motivo_rechazo='NO_PAGO_SE_QUEDO', monto_cobrado=0,
                             observaciones='paga el viernes', confirmado_por=mundo.u_cond.id)
        db.session.add(rec)
        db.session.flush()
        rec.forma_pago = 'CREDITO'   # noqa — la escritura vieja, a propósito
        db.session.commit()
        assert rec.to_dict()['forma_pago'] == ''
        r = client.get('/api/rutas/liquidacion/desglose', headers=_auth(mundo.t_admin))
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert 'CREDITO | ENTREGADO_SIN_PAGO' not in d['recaudos']['matriz'], d['recaudos']['matriz']
        assert not any(x['recaudo_id'] == rec.id for x in d['paradas_credito']['detalle'])
