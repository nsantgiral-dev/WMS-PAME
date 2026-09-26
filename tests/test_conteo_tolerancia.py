"""
Conteo: tolerancias, recuento propio, topes en pesos, aprobación por valor y
techo de recuentos por movimiento (2026-09-23).

## Lo que había

- **Toda** diferencia ≠ 0 mandaba a un segundo conteo de otra persona. Con
  tres operarios, una unidad suelta costaba dos conteos y la deriva chica no
  se corregía nunca.
- CC1 == CC2 ajustaba solo **sin tope de valor**: un faltante de millones
  entraba a Siesa sin firma.
- La aprobación manual era solo de supervisor/admin, y la regla vivía en la
  RUTA: el servicio no miraba a nadie.
- Un producto que se vendía sin parar pedía «recontar» para siempre.

## Las reglas (todas en `conteo_politica` + `ConteoService`)

1. Tolerancia por clase: `|dif| ≤ max(unidades, pct × teórico)` y
   `|dif| × costo ≤ tope de valor`; fuera de A/B/C → regla A.
2. Dentro → se acepta el PRIMER conteo y se ajusta, por la política de
   siempre y con el tope del punto 4.
3. Fuera → el mismo operario recuenta, a ciegas (`RECONTAR_TU`), una vez por
   cadena; si sigue fuera → segundo conteo de otra persona.
4. Ningún ajuste automático sale sin costo o por encima de
   `CONTEO_TOPE_AUTOAJUSTE`. La guarda vive donde se arma el job.
5. Supervisor/admin aprueban todo; jefe de almacén hasta
   `CONTEO_TOPE_APROBACION_JEFE` (0 = nada).
6. Tres movimientos seguidos → BLOQUEADO `MOVIMIENTO_CONTINUO`.
7. Métricas: exactitud con tolerancia (ASCM) al lado de la exacta.

Cada regla tiene su mutación verificada (ver el commit).
"""
import ast
import json
import pathlib
import shutil
import subprocess

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from tests.test_conteo_teorico_pos import SKU, _jobs, _un_job, siesa, tienda  # noqa: F401 (fixtures)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'


@pytest.fixture(autouse=True)
def _config_limpia(monkeypatch):
    """Los defaults decididos, sin lo que tenga el entorno del desarrollador."""
    from app.services.conteo_politica import VARIABLES_DE_ENTORNO
    for nombre in VARIABLES_DE_ENTORNO:
        monkeypatch.delenv(nombre, raising=False)


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _pol():
    from app.services import conteo_politica
    return conteo_politica


def _sesion(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


def _nuevo(db, tienda, *, tipo='MANUAL', clase=None, sku=SKU):
    """Un CC1 del hueco. `crear_conteo_manual` es la puerta que existe; el tipo
    y la clase se ajustan para medir la regla de un conteo del plan."""
    from app.models.conteo import SesionConteo
    creado = _svc().crear_conteo_manual(tienda['almacen'].id, sku)
    s = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one()
    s.tipo = tipo
    s.clasificacion_abc = clase
    db.session.commit()
    return s.id


def _abrir(sid, actor):
    return _svc().obtener_tarea_operario(sid, actor.id)


def _contar(sid, actor, n):
    return _svc().registrar_conteo(sid, actor.id, n, cero_confirmado=True)


def _actor(db, tienda, email, rol):
    from app.models.usuario import Usuario
    u = Usuario(nombre=email, email=email, password_hash=generate_password_hash('x'),
                rol=rol, puede_picar=True, almacen_id=tienda['almacen'].id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _descuadre_cc1_cc2(db, siesa, tienda, *, costo, fisico=9, existencia=10):
    """CC1 (con su recuento) y CC2 coinciden en −1 sobre un conteo manual."""
    siesa.poner(existencia=existencia, costo=costo)
    sid = _nuevo(db, tienda)
    _abrir(sid, tienda['a'])
    assert _contar(sid, tienda['a'], fisico)['resultado'] == 'RECONTAR_TU'
    r1 = _contar(sid, tienda['a'], fisico)
    assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
    _abrir(r1['segundo_conteo_id'], tienda['b'])
    r2 = _contar(r1['segundo_conteo_id'], tienda['b'], fisico)
    assert r2['resultado'] == 'DESCUADRE', r2
    return sid, r2


# ─────────────────────────────────────────────────────────────────────────────
# 1 · La regla de tolerancia (conteo_politica.evaluar_tolerancia)
# ─────────────────────────────────────────────────────────────────────────────

class TestLaReglaDeTolerancia:

    @pytest.mark.parametrize('tipo, clase, teorico, dif, dentro', [
        ('DIARIO_ABC', 'A', 1000, 5, True),     # 0,5 % de 1000 = 5: justo en el límite
        ('DIARIO_ABC', 'A', 1000, 6, False),
        ('DIARIO_ABC', 'A', 100, 1, False),     # A: 0 und, y el 0,5 % de 100 no llega a 1
        ('DIARIO_ABC', 'B', 10, 1, True),       # una unidad suelta
        ('DIARIO_ABC', 'B', 10, 2, False),
        ('DIARIO_ABC', 'B', 100, 2, True),      # 2 %
        ('DIARIO_ABC', 'B', 100, 3, False),
        ('DIARIO_ABC', 'C', 100, 5, True),      # 5 %
        ('DIARIO_ABC', 'C', 100, 6, False),
        ('DIARIO_ABC', 'C', 10, -1, True),      # el signo no importa
        ('WATCHDOG_ABC', 'C', 10, 1, True),     # el watchdog es del plan
        ('MANUAL', 'C', 10, 1, False),          # manual → regla A aunque diga C
        ('EXCEPCION_PICKING', 'C', 10, 1, False),
        ('DIARIO_ABC', None, 10, 1, False),     # sin clase → regla A
        ('DIARIO_ABC', 'Z', 10, 1, False),
        ('DIARIO_ABC', 'C', -1000, 2, False),   # teórico negativo: sin holgura por %
    ])
    def test_unidades_y_porcentaje(self, tipo, clase, teorico, dif, dentro):
        t = _pol().evaluar_tolerancia(dif, teorico, 100, tipo=tipo, clase=clase)
        assert t['dentro'] is dentro, t

    def test_la_regla_por_defecto_se_declara(self):
        t = _pol().evaluar_tolerancia(1, 10, 100, tipo='MANUAL', clase='C')
        assert (t['regla_clase'], t['regla_por_defecto']) == ('A', True)
        t = _pol().evaluar_tolerancia(1, 10, 100, tipo='DIARIO_ABC', clase='C')
        assert (t['regla_clase'], t['regla_por_defecto']) == ('C', False)

    def test_el_tope_de_valor_justo_en_el_limite(self):
        pol = _pol()
        assert pol.evaluar_tolerancia(1, 10, 20000, tipo='DIARIO_ABC', clase='C')['dentro'] is True
        t = pol.evaluar_tolerancia(1, 10, 20001, tipo='DIARIO_ABC', clase='C')
        assert t['dentro'] is False and t['dentro_por_unidades'] is True
        assert '$20.001' in t['motivo'] and '$20.000' in t['motivo']

    @pytest.mark.parametrize('costo', [None, 0, -5, 'abc', float('nan')])
    def test_sin_costo_solo_cuentan_las_unidades_y_se_declara(self, costo):
        t = _pol().evaluar_tolerancia(1, 10, costo, tipo='DIARIO_ABC', clase='C')
        assert t['dentro'] is True and t['sin_costo'] is True and t['valor'] is None
        t = _pol().evaluar_tolerancia(2, 10, costo, tipo='DIARIO_ABC', clase='C')
        assert t['dentro'] is False

    def test_se_configura_por_entorno_y_lo_ilegible_se_declara(self, monkeypatch):
        pol = _pol()
        monkeypatch.setenv('CONTEO_TOLERANCIA_UNIDADES', '{"A": 2}')
        monkeypatch.setenv('CONTEO_TOLERANCIA_PCT', '{"C": 150, "X": 1}')
        monkeypatch.setenv('CONTEO_TOLERANCIA_TOPE_VALOR', 'mucho')
        assert pol.evaluar_tolerancia(2, 10, 100, tipo='MANUAL', clase=None)['dentro'] is True
        adv = ' '.join(pol.advertencias_de_configuracion())
        assert 'CONTEO_TOLERANCIA_PCT[C]' in adv and "clase 'X'" in adv
        assert 'CONTEO_TOLERANCIA_TOPE_VALOR' in adv
        # Lo ilegible usa el defecto, no se inventa.
        assert pol.descripcion_de_tolerancias()['porcentaje']['C'] == 5.0
        assert pol.descripcion_de_tolerancias()['tope_valor_tolerancia'] == 20000

    def test_tope_del_autoajuste(self, monkeypatch):
        pol = _pol()
        assert pol.motivo_tope_autoajuste(1, 100000) is None            # justo en el tope
        assert pol.motivo_tope_autoajuste(-1, 100001)['codigo'] == pol.SUPERA_TOPE
        assert pol.motivo_tope_autoajuste(1, None)['codigo'] == pol.SIN_COSTO
        assert pol.motivo_tope_autoajuste(1, 0)['codigo'] == pol.SIN_COSTO
        monkeypatch.setenv('CONTEO_TOPE_AUTOAJUSTE', '500')
        assert pol.motivo_tope_autoajuste(1, 600)['codigo'] == pol.SUPERA_TOPE


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Dentro de tolerancia: se acepta el primer conteo y se ajusta
# ─────────────────────────────────────────────────────────────────────────────

class TestDentroDeToleranciaSeAjustaSinSegundoConteo:

    def test_una_unidad_en_un_c_del_plan(self, db, siesa, tienda):
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        r = _contar(sid, tienda['a'], 9)
        assert r['resultado'] == 'DENTRO_TOLERANCIA' and r['auto_encolado'] is True, r
        # Al operario no le llega nada del teórico.
        assert not ({'teorico_siesa', 'diferencia', 'existencia_siesa'} & set(r))
        assert not any(ch.isdigit() for ch in r['mensaje'])
        s = _sesion(db, sid)
        assert s.hijo_conteo is None, 'dentro de tolerancia no hay segundo conteo'
        assert (s.estado, s.ajuste_por_tolerancia, s.tolerancia_primer_conteo) == (
            'AJUSTANDO', True, 'DENTRO')
        assert s.aprobador_id is None
        p = _un_job(sid)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 1)

    def test_diferencia_cero_sigue_siendo_match(self, db, siesa, tienda):
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        assert _contar(sid, tienda['a'], 10)['resultado'] == 'MATCH'
        s = _sesion(db, sid)
        assert (s.tolerancia_primer_conteo, s.ajuste_por_tolerancia) == ('EXACTO', None)
        assert _jobs(sid) == []

    def test_la_politica_de_siempre_sigue_mandando(self, db, siesa, tienda):
        """Dentro de tolerancia pero con salidas que no son POS: se acepta sin
        segundo conteo, y el ajuste NO sale (motivo_bloqueo_ajuste)."""
        siesa.poner(existencia=10, pos=2, salida_sin_conf=5)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        r = _contar(sid, tienda['a'], 7)
        assert r['resultado'] == 'DENTRO_TOLERANCIA' and r['auto_encolado'] is False, r
        assert 'salidas sin confirmar' in r['ajuste_bloqueado']
        s = _sesion(db, sid)
        assert s.estado == 'DESCUADRE' and s.hijo_conteo is None
        assert _jobs(sid) == []

    def test_sin_costo_dentro_por_unidades_pero_no_sale_solo(self, db, siesa, tienda):
        siesa.poner(existencia=10, costo=None)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        r = _contar(sid, tienda['a'], 9)
        assert r['resultado'] == 'DENTRO_TOLERANCIA', r
        assert r['auto_encolado'] is False and r['no_sale_solo']['codigo'] == 'SIN_COSTO'
        d = _sesion(db, sid).to_dict()
        assert d['estado'] == 'DESCUADRE' and d['no_sale_solo']['codigo'] == 'SIN_COSTO'
        assert d['valor_ajuste'] is None and d['bloqueo_ajuste'] is None
        assert _jobs(sid) == []
        _svc().confirmar_ajuste(sid, tienda['supervisor'].id)     # un líder sí
        assert _un_job(sid)['cantidad'] == 1

    def test_el_tope_del_autoajuste_tambien_corta_la_tolerancia(self, db, siesa, tienda,
                                                               monkeypatch):
        monkeypatch.setenv('CONTEO_TOPE_AUTOAJUSTE', '500')
        siesa.poner(existencia=10, costo=1000)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        r = _contar(sid, tienda['a'], 9)
        assert r['resultado'] == 'DENTRO_TOLERANCIA' and r['auto_encolado'] is False, r
        assert r['no_sale_solo']['codigo'] == 'SUPERA_TOPE'
        assert _sesion(db, sid).estado == 'DESCUADRE' and _jobs(sid) == []

    def test_una_unidad_cara_no_es_deriva(self, db, siesa, tienda):
        """Una unidad de un C que vale más que el tope de tolerancia: fuera."""
        siesa.poner(existencia=10, costo=30000)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        assert _contar(sid, tienda['a'], 9)['resultado'] == 'RECONTAR_TU'


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Fuera de tolerancia: el mismo operario recuenta, a ciegas, una vez
# ─────────────────────────────────────────────────────────────────────────────

class TestFueraDeToleranciaRecuentoPropio:

    def test_recontar_a_ciegas(self, db, siesa, tienda):
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        r = _contar(sid, tienda['a'], 7)
        assert r['resultado'] == 'RECONTAR_TU'
        assert set(r) == {'resultado', 'mensaje', 'sesion_id'}, 'a ciegas: nada más'
        assert 'Recuente este producto con cuidado' in r['mensaje']
        assert not any(ch.isdigit() for ch in r['mensaje'])
        s = _sesion(db, sid)
        assert (s.estado, s.operario_id) == ('EN_PROCESO', tienda['a'].id)
        assert s.cantidad_fisica is None and s.diferencia is None
        assert s.foto_siesa_at is None, 'una sesión recontándose no tiene cierre'
        assert s.hijo_conteo is None and _jobs(sid) == []
        assert s.tolerancia_primer_conteo == 'FUERA'
        (d,) = s.lista_conteos_descartados()
        assert (d['motivo'], d['cantidad_fisica'], d['diferencia']) == ('FUERA_DE_TOLERANCIA', 7, -3)
        assert d['tolerancia']['dentro'] is False
        # El HUD retoma desde cero: el recuento no hereda lo contado.
        assert _abrir(sid, tienda['a'])['cantidad_contada'] == 0

    def test_por_el_camino_movil(self, db, siesa, tienda):
        from app.services.mobile_service import MobileService
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        r = MobileService.confirmar_tarea(tienda['a'].id, sid, 'CONTEO', total_contado=7)
        assert r['resultado'] == 'RECONTAR_TU'
        assert _sesion(db, sid).cantidad_fisica is None

    def test_el_recuento_dentro_se_acepta_y_reemplaza_al_primero(self, db, siesa, tienda):
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        assert _contar(sid, tienda['a'], 5)['resultado'] == 'RECONTAR_TU'
        r = _contar(sid, tienda['a'], 9)
        assert r['resultado'] == 'DENTRO_TOLERANCIA' and r['auto_encolado'] is True, r
        s = _sesion(db, sid)
        assert s.cantidad_fisica == 9 and s.hijo_conteo is None
        assert s.tolerancia_primer_conteo == 'FUERA', 'el recuento no pisa lo que dijo el primero'
        assert s.ajuste_por_tolerancia is True
        assert _un_job(sid)['cantidad'] == 1

    def test_el_recuento_fuera_va_a_otra_persona(self, db, siesa, tienda):
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        assert _contar(sid, tienda['a'], 7)['resultado'] == 'RECONTAR_TU'
        r = _contar(sid, tienda['a'], 7)
        assert r['resultado'] == 'SEGUNDO_CONTEO', r
        cc2 = _sesion(db, r['segundo_conteo_id'])
        assert cc2.operario_id != tienda['a'].id
        assert _sesion(db, sid).ajuste_por_tolerancia is None

    def test_un_solo_recuento_propio_por_cadena(self, db, siesa, tienda):
        """Ni bloquear y reabrir le da otro."""
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        assert _contar(sid, tienda['a'], 7)['resultado'] == 'RECONTAR_TU'
        _svc().bloquear_conteo(sid, tienda['a'].id, 'NO_ENCONTRADO')
        _svc().reabrir_bloqueado(sid, tienda['supervisor'].id, operario_id=tienda['a'].id)
        _abrir(sid, tienda['a'])
        assert _contar(sid, tienda['a'], 7)['resultado'] == 'SEGUNDO_CONTEO'

    def test_el_cc2_no_tiene_recuento_propio(self, db, siesa, tienda):
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        _contar(sid, tienda['a'], 7)
        r1 = _contar(sid, tienda['a'], 7)
        _abrir(r1['segundo_conteo_id'], tienda['b'])
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 4)
        assert r2['resultado'] == 'TERCER_CONTEO', r2

    def test_una_venta_durante_el_recuento_lo_descarta_igual(self, db, siesa, tienda):
        """El recuento arranca de la foto del cierre descartado: una venta
        entre esa lectura y el nuevo cierre se ve."""
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        assert _contar(sid, tienda['a'], 7)['resultado'] == 'RECONTAR_TU'
        siesa.poner(existencia=10, pos=1)
        assert _contar(sid, tienda['a'], 7)['resultado'] == 'RECONTAR'


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Tope en pesos de TODO ajuste automático
# ─────────────────────────────────────────────────────────────────────────────

class TestTopeDelAutoajuste:

    def test_cc1_igual_cc2_por_encima_del_tope_espera_firma(self, db, siesa, tienda):
        sid, r2 = _descuadre_cc1_cc2(db, siesa, tienda, costo=200000)
        assert r2['auto_encolado'] is False and r2['ajuste_bloqueado'] is None, r2
        assert r2['no_sale_solo']['codigo'] == 'SUPERA_TOPE'
        assert '$200.000' in r2['mensaje'] and '$100.000' in r2['mensaje']
        assert _jobs(sid) == []
        d = _sesion(db, sid).to_dict()
        assert (d['estado'], d['valor_ajuste']) == ('DESCUADRE', 200000.0)
        assert d['no_sale_solo']['codigo'] == 'SUPERA_TOPE'
        _svc().confirmar_ajuste(sid, tienda['supervisor'].id)
        assert _un_job(sid)['cantidad'] == 1

    def test_justo_en_el_tope_sale_solo(self, db, siesa, tienda):
        sid, r2 = _descuadre_cc1_cc2(db, siesa, tienda, costo=100000)
        assert r2['auto_encolado'] is True, r2

    def test_sin_costo_no_sale_solo(self, db, siesa, tienda):
        sid, r2 = _descuadre_cc1_cc2(db, siesa, tienda, costo=None)
        assert r2['auto_encolado'] is False and r2['no_sale_solo']['codigo'] == 'SIN_COSTO'
        assert _jobs(sid) == []

    def test_la_guarda_vive_donde_se_arma_el_job(self, db, siesa, tienda):
        """Cualquier puerta automática —la de hoy y la que venga— pasa por
        `_encolar_ajuste_fisico` sin aprobador, y ahí se corta."""
        sid, _ = _descuadre_cc1_cc2(db, siesa, tienda, costo=200000)
        raiz = _sesion(db, sid)
        with pytest.raises(ValueError, match='NO enviado'):
            _svc()._encolar_ajuste_fisico(raiz, aprobador_id=None)
        db.session.rollback()
        assert _jobs(sid) == []
        raiz = _sesion(db, sid)
        _svc()._encolar_ajuste_fisico(raiz, aprobador_id=tienda['supervisor'].id)
        db.session.commit()
        assert len(_jobs(sid)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5 · Quién aprueba cuánto
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def jefe(db, tienda):
    return _actor(db, tienda, 'jefe@test.com', 'jefe_almacen')


def _descuadre_para_aprobar(db, siesa, tienda, monkeypatch, costo):
    """Un DESCUADRE aprobable de −1 × `costo`. El tope del autoajuste en 0 para
    que nada salga solo y quede para firmar."""
    monkeypatch.setenv('CONTEO_TOPE_AUTOAJUSTE', '0')
    sid, r2 = _descuadre_cc1_cc2(db, siesa, tienda, costo=costo)
    assert r2['auto_encolado'] is False, r2
    return sid


class TestAprobacionPorValor:

    def test_el_jefe_no_aprueba_con_el_tope_por_defecto(self, db, siesa, tienda, jefe,
                                                        monkeypatch):
        sid = _descuadre_para_aprobar(db, siesa, tienda, monkeypatch, 1000)
        with pytest.raises(PermissionError, match=r'tope de aprobación en \$0'):
            _svc().confirmar_ajuste(sid, jefe.id)
        db.session.rollback()
        assert _jobs(sid) == []

    @pytest.mark.parametrize('costo, puede', [(4000, True), (5000, True), (5001, False)])
    def test_el_jefe_aprueba_hasta_su_tope(self, db, siesa, tienda, jefe, monkeypatch,
                                          costo, puede):
        monkeypatch.setenv('CONTEO_TOPE_APROBACION_JEFE', '5000')
        sid = _descuadre_para_aprobar(db, siesa, tienda, monkeypatch, costo)
        if puede:
            _svc().confirmar_ajuste(sid, jefe.id)
            assert _un_job(sid)['cantidad'] == 1
            assert _sesion(db, sid).aprobador_id == jefe.id
        else:
            with pytest.raises(PermissionError, match='supera su tope'):
                _svc().confirmar_ajuste(sid, jefe.id)
            db.session.rollback()
            assert _jobs(sid) == []

    def test_sin_costo_el_jefe_no_aprueba(self, db, siesa, tienda, jefe, monkeypatch):
        monkeypatch.setenv('CONTEO_TOPE_APROBACION_JEFE', '5000')
        sid = _descuadre_para_aprobar(db, siesa, tienda, monkeypatch, None)
        with pytest.raises(PermissionError, match='no tiene costo'):
            _svc().confirmar_ajuste(sid, jefe.id)

    def test_un_operario_no_aprueba_nada(self, db, siesa, tienda, monkeypatch):
        sid = _descuadre_para_aprobar(db, siesa, tienda, monkeypatch, 1)
        with pytest.raises(PermissionError):
            _svc().confirmar_ajuste(sid, tienda['a'].id)

    @pytest.mark.parametrize('rol', ['supervisor', 'admin'])
    def test_lead_aprueba_cualquier_monto(self, db, siesa, tienda, monkeypatch, rol):
        lider = _actor(db, tienda, f'{rol}-x@test.com', rol)
        sid = _descuadre_para_aprobar(db, siesa, tienda, monkeypatch, 5_000_000)
        _svc().confirmar_ajuste(sid, lider.id)
        assert _un_job(sid)['cantidad'] == 1

    def test_la_ruta_lo_traduce_a_403_con_su_motivo(self, app, client, db, siesa, tienda,
                                                    jefe, monkeypatch):
        sid = _descuadre_para_aprobar(db, siesa, tienda, monkeypatch, 1000)

        def _tok(u):
            with app.app_context():
                return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}
        r = client.put(f'/api/conteo/{sid}/ajustar', json={}, headers=_tok(jefe))
        assert r.status_code == 403 and 'tope de aprobación' in r.get_json()['error']
        r = client.put(f'/api/conteo/{sid}/ajustar', json={}, headers=_tok(tienda['a']))
        assert r.status_code == 403
        assert _jobs(sid) == []
        r = client.put(f'/api/conteo/{sid}/ajustar', json={}, headers=_tok(tienda['supervisor']))
        assert r.status_code in (200, 202), r.get_json()


# ─────────────────────────────────────────────────────────────────────────────
# 6 · Máximo 2 recuentos por movimiento
# ─────────────────────────────────────────────────────────────────────────────

def _mover_y_contar(siesa, sid, actor, pos, fisico=7):
    siesa.poner(existencia=10, pos=pos)
    return _contar(sid, actor, fisico)


class TestMovimientoContinuo:

    def test_al_tercer_movimiento_queda_para_el_lider(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        assert _mover_y_contar(siesa, sid, tienda['a'], 1)['resultado'] == 'RECONTAR'
        assert _mover_y_contar(siesa, sid, tienda['a'], 2)['resultado'] == 'RECONTAR'
        r = _mover_y_contar(siesa, sid, tienda['a'], 3)
        assert r['resultado'] == 'BLOQUEADO', r
        assert 'se está vendiendo mientras usted cuenta' in r['mensaje']
        s = _sesion(db, sid)
        assert (s.estado, s.motivo_bloqueo) == ('BLOQUEADO', 'MOVIMIENTO_CONTINUO')
        assert [d['motivo'] for d in s.lista_conteos_descartados()] == ['MOVIMIENTO_SIESA'] * 3
        assert s.hijo_conteo is None and _jobs(sid) == []
        (fila,) = [b for b in _svc().listar_bloqueados() if b['id'] == sid]
        assert fila['motivo_bloqueo'] == 'MOVIMIENTO_CONTINUO'

    def test_dos_movimientos_y_uno_quieto_sigue_normal(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        _mover_y_contar(siesa, sid, tienda['a'], 1)
        _mover_y_contar(siesa, sid, tienda['a'], 2)
        assert _contar(sid, tienda['a'], 8)['resultado'] == 'MATCH'

    def test_reabrir_le_devuelve_sus_recuentos(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        for pos in (1, 2, 3):
            _mover_y_contar(siesa, sid, tienda['a'], pos)
        _svc().reabrir_bloqueado(sid, tienda['supervisor'].id, nota='a la tarde')
        s = _sesion(db, sid)
        assert s.lista_conteos_descartados()[-1]['evento'] == 'REABIERTO'
        _abrir(sid, tienda['a'])
        assert _mover_y_contar(siesa, sid, tienda['a'], 4)['resultado'] == 'RECONTAR'

    def test_el_lider_lo_puede_cancelar(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        sid = _nuevo(db, tienda)
        _abrir(sid, tienda['a'])
        for pos in (1, 2, 3):
            _mover_y_contar(siesa, sid, tienda['a'], pos)
        _svc().cancelar_bloqueado(sid, tienda['supervisor'].id, 'se vende sin parar')
        assert _sesion(db, sid).estado == 'CANCELADO'


# ─────────────────────────────────────────────────────────────────────────────
# 7 · Métricas
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricasConTolerancia:

    def test_mundo_con_tolerancia(self, db, siesa, tienda):
        from datetime import datetime, timedelta

        from app.services.metricas import conteo as m
        from app.utils.fecha import dia_operativo_de
        from tests.test_estadisticas_conteo import _hueco

        a = tienda['a']
        huecos = iter(_hueco(db, tienda, n) for n in range(1, 6))

        def plan():
            return _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C', sku=next(huecos))

        siesa.poner(existencia=10)
        dentro = plan()
        _abrir(dentro, a)
        assert _contar(dentro, a, 9)['resultado'] == 'DENTRO_TOLERANCIA'

        exacto = plan()
        _abrir(exacto, a)
        assert _contar(exacto, a, 10)['resultado'] == 'MATCH'

        recontado = plan()
        _abrir(recontado, a)
        assert _contar(recontado, a, 5)['resultado'] == 'RECONTAR_TU'
        assert _contar(recontado, a, 9)['resultado'] == 'DENTRO_TOLERANCIA'

        siesa.poner(existencia=10, costo=None)
        sin_costo = plan()
        _abrir(sin_costo, a)
        assert _contar(sin_costo, a, 9)['no_sale_solo']['codigo'] == 'SIN_COSTO'

        siesa.poner(existencia=10, pos=0)
        vendiendo = plan()
        _abrir(vendiendo, a)
        for pos in (1, 2, 3):
            _mover_y_contar(siesa, vendiendo, a, pos)

        for sid, v in ((dentro, m.AJUSTE_EN_TOLERANCIA), (exacto, m.OK_CC1),
                       (recontado, m.AJUSTE_EN_TOLERANCIA), (sin_costo, m.AJUSTE_EN_TOLERANCIA),
                       (vendiendo, m.SIN_VEREDICTO)):
            assert m.veredicto_cadena(_sesion(db, sid)) == v, sid
        assert m.motivo_sin_veredicto(_sesion(db, vendiendo)) == 'movimiento_continuo'

        hoy = dia_operativo_de(datetime.utcnow())
        rep = m.calcular_estadisticas_conteo(hoy - timedelta(days=6), hoy)
        e = rep['exactitud']
        exacta = e['por_clase']['C']['DIARIO_ABC']
        assert (exacta['numerador'], exacta['denominador']) == (1, 4)
        tol = e['con_tolerancia']['por_clase']['C']['DIARIO_ABC']
        # Acierto = primer conteo dentro: dentro, exacto y sin_costo; el
        # recontado empezó fuera.
        assert (tol['numerador'], tol['denominador']) == (3, 4)
        assert e['con_tolerancia']['primer_conteo_fuera'] == {
            'resuelto_por_recuento_propio': 1, 'a_segundo_conteo': 0}
        v = rep['volumen']
        assert v['recuentos_propios'] == 1
        assert v['recuentos']['numerador'] == 3
        assert v['sin_veredicto'].get('movimiento_continuo') == 1
        aj = rep['ajustes']
        assert aj['por_tolerancia'] == 2 and aj['automaticos'] == 2
        assert aj['bloqueados_hoy']['aprobables_por_motivo'] == {'SIN_COSTO': 1}

    def test_una_cadena_vieja_no_se_adivina(self, db, siesa, tienda):
        """Sin `tolerancia_primer_conteo` (anterior a la regla) va a excluidos."""
        from datetime import datetime, timedelta

        from app.services.metricas import conteo as m
        from app.utils.fecha import dia_operativo_de
        siesa.poner(existencia=10)
        sid = _nuevo(db, tienda, tipo='DIARIO_ABC', clase='C')
        _abrir(sid, tienda['a'])
        _contar(sid, tienda['a'], 10)
        s = _sesion(db, sid)
        s.tolerancia_primer_conteo = None
        db.session.commit()
        hoy = dia_operativo_de(datetime.utcnow())
        t = m.calcular_estadisticas_conteo(hoy - timedelta(days=6), hoy)['exactitud']['con_tolerancia']
        assert t['por_clase'] == {}
        assert t['excluidos'] == {'sin_evaluacion_de_tolerancia': 1}


# ─────────────────────────────────────────────────────────────────────────────
# 8 · Trinquete: ningún ajuste automático nace fuera de la guarda
# ─────────────────────────────────────────────────────────────────────────────
#
# El tope en pesos vive en `_encolar_ajuste_fisico` (sin aprobador = automático).
# Eso protege toda puerta… mientras el job AJUSTE_CONTEO se arme ahí. Una puerta
# nueva que encole el job por su cuenta se saltaría el tope sin que nada lo
# dijera. Inventario declarado: dónde se encola, y por qué no es una puerta
# nueva.

SITIOS_QUE_ENCOLAN_AJUSTE = {
    'app/services/conteo_service.py::ConteoService._encolar_ajuste_fisico': (
        'EL sitio: arma el ajuste con la foto del conteo y corta todo automático '
        'por encima del tope o sin costo.'),
    'app/services/conteo_service.py::ConteoService.confirmar_ajuste': (
        'Recuperación: re-encola un ajuste que YA se aprobó (AJUSTANDO) y quedó sin '
        'job. No decide un ajuste nuevo.'),
    'app/services/siesa_job_service.py::_run_dlq_jobs': (
        'Barrido del DLQ: re-encola una sesión AJUSTANDO >15 min sin job. Misma '
        'recuperación, sobre un ajuste ya decidido.'),
}


def _encola_ajuste(nodo) -> bool:
    """`X.encolar('AJUSTE_CONTEO', …)` o `X.encolar(tipo='AJUSTE_CONTEO', …)`."""
    if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == 'encolar'):
        return False
    candidatos = list(nodo.args[:1]) + [k.value for k in nodo.keywords if k.arg == 'tipo']
    return any(isinstance(c, ast.Constant) and c.value == 'AJUSTE_CONTEO' for c in candidatos)


def _sitios(fuente: str, ruta: str) -> set:
    arbol = ast.parse(fuente)
    out = set()

    def visitar(nodo, pila):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visitar(hijo, pila + [hijo.name])
            else:
                if _encola_ajuste(hijo):
                    out.add(f'{ruta}::{".".join(pila) or "<modulo>"}')
                visitar(hijo, pila)
    visitar(arbol, [])
    return out


def _barrer() -> set:
    encontrados = set()
    for ruta in sorted((RAIZ / 'app').rglob('*.py')):
        rel = ruta.relative_to(RAIZ).as_posix()
        encontrados |= _sitios(ruta.read_text(encoding='utf-8'), rel)
    return encontrados


class TestNingunAjusteAutomaticoFueraDeLaGuarda:

    def test_no_crece(self):
        nuevos = _barrer() - set(SITIOS_QUE_ENCOLAN_AJUSTE)
        assert not nuevos, (
            f'Encolan AJUSTE_CONTEO fuera de _encolar_ajuste_fisico: {sorted(nuevos)}. '
            'Un ajuste armado ahí se salta el tope en pesos. Usar _encolar_ajuste_fisico, '
            'o declararlo con su motivo.')

    def test_solo_encoge(self):
        viejos = set(SITIOS_QUE_ENCOLAN_AJUSTE) - _barrer()
        assert not viejos, f'Ya no encolan: sacarlos del inventario: {sorted(viejos)}'

    def test_cada_entrada_dice_por_que(self):
        assert all(len(v) > 40 for v in SITIOS_QUE_ENCOLAN_AJUSTE.values())

    def test_piso(self):
        assert len(_barrer()) >= 3

    @pytest.mark.parametrize('fuente', [
        "def f():\n    SiesaJob.encolar('AJUSTE_CONTEO', {})\n",
        "class C:\n    def g(self):\n        J.encolar(tipo='AJUSTE_CONTEO', payload={})\n",
    ])
    def test_el_detector_marca(self, fuente):
        assert _sitios(fuente, 'x.py')

    @pytest.mark.parametrize('fuente', [
        "def f():\n    SiesaJob.encolar('AJUSTE_INVENTARIO', {})\n",
        "def f():\n    \"\"\"encolar('AJUSTE_CONTEO')\"\"\"\n",
        "def f():\n    q.filter(SiesaJob.tipo == 'AJUSTE_CONTEO')\n",
    ])
    def test_el_detector_no_marca(self, fuente):
        assert _sitios(fuente, 'x.py') == set()


# ─────────────────────────────────────────────────────────────────────────────
# 9 · El HUD: «Recuente este producto con cuidado», a ciegas, desde cero
# ─────────────────────────────────────────────────────────────────────────────

def _harness():
    from tests.test_conteo_hud_operario import HARNESS
    h = HARNESS.replace("const envios = []; const alertas = [];",
                        "const envios = []; const alertas = []; const creados = []; const pedidas = [];")
    h = h.replace("createElement: () => el('x'),",
                  "createElement: () => { const e = el('x'); creados.push(e); return e; },")
    h = h.replace("pedirTarea() {},", "pedirTarea() { pedidas.push(1); },")
    h = h.replace("process.stdout.write(JSON.stringify({ html, envios, alertas,",
                  "process.stdout.write(JSON.stringify({ html, final: els['contenido-tarea'].innerHTML, "
                  "creados: creados.map(e => e.innerHTML), pedidas: pedidas.length, envios, alertas,")
    assert h.count('creados') >= 3 and 'pedidas.push' in h and "final:" in h, 'el arnés cambió'
    return h


def _correr(tmp_path, respuesta, pasos=({'teclear': '7'}, {'confirmar': True})):
    if not shutil.which('node'):
        pytest.skip('sin node')
    guion = {'tarea': {'id': 7, 'tipo': 'CONTEO', 'producto_codigo': SKU,
                       'producto_nombre': 'Cuaderno', 'ubicacion': 'SIESA-GENERAL',
                       'ubicacion_fisica': False, 'cantidad_escaneada': 0},
             'scan': {'tipo': 'NO_ENCONTRADO'}, 'pasos': list(pasos), 'confirmar': True,
             'texto': None, 'respuesta': respuesta}
    (tmp_path / 'h.js').write_text(_harness(), encoding='utf-8')
    (tmp_path / 'g.json').write_text(json.dumps(guion), encoding='utf-8')
    out = subprocess.run(['node', str(tmp_path / 'h.js'), str(PWA / 'util.js'),
                          str(PWA / 'conteo.js'), str(PWA / 'app.js'), str(tmp_path / 'g.json')],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


class TestElHudPideRecontar:

    def test_recontar_tu_reinicia_el_contador_con_el_aviso(self, tmp_path):
        r = _correr(tmp_path, {'resultado': 'RECONTAR_TU', 'sesion_id': 7,
                               'mensaje': 'Revisá <b>todo</b> otra vez.'})
        assert r['total'] == 0, 'el contador vuelve a cero'
        assert 'Recuente este producto con cuidado' in r['final']
        assert 'Revisá &lt;b&gt;todo&lt;/b&gt; otra vez.' in r['final'], 'esc() en el aviso'
        assert r['pedidas'] == 0, 'la misma tarea sigue en pantalla'
        assert 'Recuente' not in r['html'], 'el aviso no estaba antes de confirmar'

    def test_el_arnes_ve_la_diferencia_sin_aviso(self, tmp_path):
        """Contraprueba: un MATCH no deja el aviso ni reinicia en el HUD."""
        r = _correr(tmp_path, {'resultado': 'MATCH', 'mensaje': 'ok'})
        assert 'Recuente este producto con cuidado' not in (r['final'] or '')
        assert r['total'] is None, 'el HUD se cerró'

    def test_movimiento_continuo_queda_para_el_lider(self, tmp_path):
        r = _correr(tmp_path, {'resultado': 'BLOQUEADO', 'motivo_bloqueo': 'MOVIMIENTO_CONTINUO',
                               'mensaje': 'Este producto se está vendiendo mientras contás: queda para el líder.'})
        assert any('se está vendiendo mientras contás' in c for c in r['creados'])
        assert r['pedidas'] == 1

    def test_dentro_de_tolerancia_es_neutro(self, tmp_path):
        r = _correr(tmp_path, {'resultado': 'DENTRO_TOLERANCIA', 'mensaje': 'Conteo registrado — gracias.'})
        pintado = ' '.join(r['creados'])
        assert 'Conteo registrado' in pintado
        assert 'Diferencia' not in pintado and 'diferencia' not in pintado
