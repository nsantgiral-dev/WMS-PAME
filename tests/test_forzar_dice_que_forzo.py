"""Todo FORZAR de la bitácora dice QUÉ forzó — y los lectores lo respetan.

**La clase** (QA e2e 2026-09-24, P2): *un verbo de bitácora con dos
significados sobre la misma entidad*. `FORZAR` sobre `RutaDespacho` lo
escriben el cierre forzado de una ruta (`forzar_cierre_ruta`) y el despacho
con advertencias de flota (`_reconocer_advertencias_flota`). La jornada juntaba
todo FORZAR en «rutas forzadas» —«Ruta cerrada a la fuerza por la oficina»,
y la sacaba de la duración de ruta— y la bitácora legible decía «forzó el
cierre» de una ruta que cerró el conductor.

- `registrar_accion('FORZAR', …)` **exige** `despues['forzado']` ∈
  `TIPOS_FORZADO` (en tiempo de ejecución) y, por AST, toda llamada del repo
  lo pasa literal — con piso.
- `bitacora.tipo_de_forzado` es la única función que contesta «¿qué se
  forzó?»; las filas viejas (sin el campo) se leen por su forma.
- Los dos lectores (jornada, bitácora legible) distinguen, y el cierre forzado
  de verdad **se sigue viendo** (el detector no queda ciego).
"""
import ast
from pathlib import Path

import pytest

from tests.test_qa_e2e_flota_20260924 import _auth, _ruta_con_parada, mundo  # noqa: F401

RAIZ = Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════════════════
# La política
# ═════════════════════════════════════════════════════════════════════════

class TestTipoDeForzado:
    def test_lee_el_campo(self):
        from app.services import bitacora as b
        assert b.tipo_de_forzado('RutaDespacho', {'forzado': b.FORZADO_CIERRE_RUTA}) \
            == b.FORZADO_CIERRE_RUTA

    def test_fila_vieja_de_advertencias(self):
        from app.services import bitacora as b
        d = {'momento': 'despachar', 'advertencias_flota': ['soat_vencido']}
        assert b.tipo_de_forzado('RutaDespacho', d) == b.FORZADO_ADVERTENCIAS_FLOTA
        assert 'cierre' not in b.verbo_de_forzado('RutaDespacho', d)

    def test_fila_vieja_de_cierre(self):
        from app.services import bitacora as b
        d = {'estado': 'ENTREGADA', 'paradas_auto_rechazadas': [3]}
        assert b.tipo_de_forzado('RutaDespacho', d) == b.FORZADO_CIERRE_RUTA
        assert b.verbo_de_forzado('RutaDespacho', d) == 'forzó el cierre de'

    def test_fila_que_no_dice_nada_no_se_inventa(self):
        from app.services import bitacora as b
        assert b.tipo_de_forzado('RutaDespacho', {'estado': 'X'}) is None
        assert b.tipo_de_forzado('RutaDespacho', None) is None
        assert 'cierre' not in b.verbo_de_forzado('RutaDespacho', {})

    def test_los_forzar_de_cartera_y_devoluciones_no_son_cierres(self):
        # Integración 2026-09-24: cartera, devoluciones y la liquidación con
        # devoluciones sin contar escribían FORZAR sin tipo. La liquidación
        # cae sobre `RutaDespacho`, igual que el cierre forzado: con su tipo
        # la jornada no la cuenta como «ruta cerrada a la fuerza».
        from app.services import bitacora as b
        for t in (b.FORZADO_AUTORIZACION_CARTERA, b.FORZADO_LIQUIDACION_SIN_CONTAR,
                  b.FORZADO_NC_APROBADA_A_MANO):
            assert b.tipo_de_forzado('RutaDespacho', {'forzado': t}) == t
            assert 'cierre' not in b.verbo_de_forzado('RutaDespacho', {'forzado': t})

    def test_registrar_un_forzar_sin_decir_que_forzo_se_rechaza(self, db):
        from app.services.bitacora import registrar_accion
        with pytest.raises(ValueError, match='forzado'):
            registrar_accion('FORZAR', 'RutaDespacho', 1, despues={'estado': 'X'})
        with pytest.raises(ValueError, match='forzado'):
            registrar_accion('FORZAR', 'RutaDespacho', 1, despues={'forzado': 'otra_cosa'})
        db.session.rollback()


# ═════════════════════════════════════════════════════════════════════════
# Trinquete por AST: toda escritura de FORZAR del repo pasa `forzado`
# ═════════════════════════════════════════════════════════════════════════

def llamadas_forzar(fuentes):
    """[(archivo, linea, forzado_literal | None)] de toda llamada
    `registrar_accion('FORZAR', ...)`. `forzado_literal` es el nombre (o
    atributo) que va en la clave `'forzado'` del dict literal de `despues`."""
    out = []
    for nombre, texto in fuentes.items():
        for n in ast.walk(ast.parse(texto)):
            if not (isinstance(n, ast.Call) and getattr(n.func, 'id', getattr(n.func, 'attr', None))
                    == 'registrar_accion'):
                continue
            if not (n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == 'FORZAR'):
                continue
            valor = None
            for kw in n.keywords:
                if kw.arg == 'despues' and isinstance(kw.value, ast.Dict):
                    for k, v in zip(kw.value.keys, kw.value.values):
                        if isinstance(k, ast.Constant) and k.value == 'forzado':
                            valor = getattr(v, 'id', None) or getattr(v, 'attr', None) \
                                or (v.value if isinstance(v, ast.Constant) else None)
            out.append((nombre, n.lineno, valor))
    return out


def _fuentes_repo():
    return {str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8')
            for base in ('app', 'flota', 'scripts') for p in (RAIZ / base).rglob('*.py')}


class TestTodaEscrituraDeForzarDiceQueForzo:
    def test_toda_llamada_pasa_un_tipo_declarado(self):
        from app.services import bitacora as b
        validos = {k for k, v in vars(b).items() if k.startswith('FORZADO_')} | set(b.TIPOS_FORZADO)
        malas = [f'{a}:{ln} → {v!r}' for a, ln, v in llamadas_forzar(_fuentes_repo())
                 if v not in validos]
        assert not malas, 'FORZAR sin `despues={"forzado": FORZADO_…}` literal:\n' + '\n'.join(malas)

    def test_piso(self):
        assert len(llamadas_forzar(_fuentes_repo())) >= 2

    def test_el_detector_ve_la_llamada_sin_tipo(self):
        src = ("registrar_accion('FORZAR', r, despues={'estado': 1})\n"
               "bitacora.registrar_accion('FORZAR', r, despues={'forzado': FORZADO_CIERRE_RUTA})\n"
               "registrar_accion('EDITAR', r, despues={})\n")
        assert [v for _a, _l, v in llamadas_forzar({'x.py': src})] == [None, 'FORZADO_CIERRE_RUTA']


# ═════════════════════════════════════════════════════════════════════════
# El cierre forzado DE VERDAD se sigue viendo (el detector no queda ciego)
# ═════════════════════════════════════════════════════════════════════════

class TestElCierreForzadoSeSigueViendo:
    def _forzar(self, db, m):
        from app.services.ruta_service import RutaService
        from app.services.liquidacion_service import LiquidacionService
        ruta, _t = _ruta_con_parada(db, m, estado='EN_TRANSITO')
        orig = LiquidacionService.crear_devoluciones_pendientes_ruta
        LiquidacionService.crear_devoluciones_pendientes_ruta = staticmethod(lambda _id: {'creadas': 0})
        try:
            RutaService.forzar_cierre_ruta(ruta.id, m.u_admin.id, motivo='conductor sin señal')
        finally:
            LiquidacionService.crear_devoluciones_pendientes_ruta = staticmethod(orig)
        return ruta

    def test_la_jornada_dice_cierre_forzado(self, client, db, mundo):  # noqa: F811
        from app.utils.fecha import dia_operativo
        self._forzar(db, mundo)
        r = client.get(f'/api/jornada?dia={dia_operativo().isoformat()}'
                       f'&conductor_id={mundo.conductor_id}', headers=_auth(mundo.t_flota))
        assert r.status_code == 200, r.get_json()
        tipos = [e.get('tipo') for e in r.get_json()['eventos']]
        assert 'cierre_ruta_forzado' in tipos, tipos

    def test_la_bitacora_dice_que_forzo_el_cierre(self, client, db, mundo):  # noqa: F811
        self._forzar(db, mundo)
        r = client.get('/api/analitica/bitacora?accion=FORZAR&entidad=RutaDespacho',
                       headers=_auth(mundo.t_admin))
        assert r.status_code == 200, r.get_json()
        frases = [a['frase'] for a in r.get_json()['acciones']]
        assert any('forzó el cierre' in f for f in frases), frases

    def test_el_despacho_con_advertencias_dice_lo_que_fue(self, client, db, mundo):  # noqa: F811
        from app.services.ruta_service import RutaService
        ruta, _t = _ruta_con_parada(db, mundo, estado='EN_CARGUE')
        RutaService.cerrar_ruta(ruta.id, motivo_advertencias='sale igual',
                                usuario_id=mundo.u_admin.id)
        r = client.get('/api/analitica/bitacora?accion=FORZAR&entidad=RutaDespacho',
                       headers=_auth(mundo.t_admin))
        frases = [a['frase'] for a in r.get_json()['acciones']]
        assert any('advertencias de flota' in f for f in frases), frases
