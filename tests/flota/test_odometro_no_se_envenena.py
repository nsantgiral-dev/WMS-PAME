"""El odómetro no se envenena (T3 de la auditoría del 2026-09-27).

## La clase

*«Una lectura que no dice cuánto marca el camión decide igual.»* Tres formas:

1. **El salto.** 125.000 tecleado donde iba 12.500 subía el tope: todo lo que
   viniera después daba 409 y el camión quedaba trabado. Ahora un salto (×10 o
   km en cero tiempo) nace `serie='salto'`, en duda, y **no sube el tope ni
   mueve el odómetro** hasta que una persona lo verifique.
2. **La corrección.** Dejaba atrás TODA la historia anterior (el CPK del mes
   salía +61 %). Ahora ANULA una lectura puntual (`anula_lectura_id`, con
   autor y motivo): la anulada sigue en la tabla y deja de contar; lo demás
   sigue contando. Las correcciones viejas (sin `anula_lectura_id`) conservan
   su ventana.
3. **El tanqueo tardío.** El que se sincronizaba después de la entrega del
   turno (con más km) daba 409 y se perdía la plata. Ahora nace
   `serie='tardia'`: en duda, fuera del odómetro y del rendimiento, declarado.

La pregunta «¿cuenta esta lectura?» es UNA: `dominio.odometro.cuenta_en_la_serie`
(y su gemela SQL en los triggers, `_CUENTA_SQL`). La traducción de una fila a
dominio es UNA: `LecturaOdometro.a_dominio` — el trinquete del final impide
una segunda que pierda el id, la anulación o la serie.

## Lo que NO cubre

- Los saltos de antes de `m051flotakm` (serie NULL) siguen contando: no hay
  backfill. Se corrigen anulándolos.
- El umbral de plausibilidad del teléfono (3× el ritmo, mínimo 400 km/día, 800
  sin historia) es provisional y el servidor no lo usa para nada.
"""
import ast
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from flota.dominio import odometro as dom
from flota.dominio.errores import LecturaRechazada
from flota.dominio.valores import Confianza, Lectura, OrigenLectura

RAIZ = Path(__file__).resolve().parents[2]
_RECHAZO = (IntegrityError, OperationalError)
_T0 = datetime(2026, 9, 1, 8, 0)


def _auth(t):
    return {'Authorization': f'Bearer {t}'}


def _l(km, horas, id, origen='cierre_dia', serie=None,
       confianza=Confianza.DECLARADA, anula=None, motivo=None):
    return Lectura(valor_km=km, ts=_T0 + timedelta(hours=horas),
                   origen=OrigenLectura(origen), autor_usuario_id=7,
                   motivo_correccion=motivo, confianza=confianza, id=id,
                   anula_lectura_id=anula, serie=serie)


def _corr(km, horas, id, anula, motivo='dedazo contra la foto'):
    return _l(km, horas, id, origen='correccion', anula=anula, motivo=motivo)


# ══════════════════════════════════════════════════════════════════════════
# 1 · El dominio
# ══════════════════════════════════════════════════════════════════════════

class TestUnSaltoNoSubeElTope:

    SERIE = [_l(12500, 0, 1),
             _l(125000, 24, 2, serie=dom.SERIE_SALTO, confianza=Confianza.DUDOSA)]

    def test_la_siguiente_lectura_normal_entra(self):
        dom.validar_lectura(self.SERIE, _l(12600, 48, None))

    def test_el_odometro_vigente_no_es_el_salto(self):
        assert dom.odometro_actual(self.SERIE) == 12500

    def test_verificado_si_cuenta(self):
        verificado = [self.SERIE[0],
                      _l(125000, 24, 2, serie=dom.SERIE_SALTO,
                         confianza=Confianza.VERIFICADA)]
        assert dom.odometro_actual(verificado) == 125000
        with pytest.raises(LecturaRechazada):
            dom.validar_lectura(verificado, _l(12600, 48, None))


class TestLaCorreccionAnulaUnaSola:

    SERIE = [_l(100, 0, 1), _l(200, 24, 2), _l(9999, 48, 3)]

    def test_anula_la_mala_y_la_historia_sigue(self):
        c = _corr(250, 72, 4, anula=3)
        dom.validar_lectura(self.SERIE, c)
        cuentan = {l.id for l in dom.lecturas_que_cuentan(self.SERIE + [c])}
        assert cuentan == {1, 2, 4}
        assert dom.odometro_actual(self.SERIE + [c]) == 250

    def test_la_correccion_vieja_conserva_su_ventana(self):
        vieja = _l(250, 72, 4, origen='correccion', motivo='de antes')
        cuentan = {l.id for l in dom.lecturas_que_cuentan(self.SERIE + [vieja])}
        assert cuentan == {4}

    def test_sin_decir_cual_se_rechaza(self):
        with pytest.raises(LecturaRechazada, match='anula UNA lectura'):
            dom.validar_lectura(self.SERIE, _corr(250, 72, None, anula=None))

    def test_una_lectura_de_otro_vehiculo_se_rechaza(self):
        with pytest.raises(LecturaRechazada, match='no es de este vehículo'):
            dom.validar_lectura(self.SERIE, _corr(250, 72, None, anula=99))

    def test_una_lectura_se_anula_una_sola_vez(self):
        serie = self.SERIE + [_corr(250, 72, 4, anula=3)]
        with pytest.raises(LecturaRechazada, match='una sola vez'):
            dom.validar_lectura(serie, _corr(260, 96, None, anula=3))

    def test_la_correccion_tiene_que_cuadrar_con_el_resto(self):
        with pytest.raises(LecturaRechazada, match='no puede decrecer'):
            dom.validar_lectura(self.SERIE, _corr(150, 72, None, anula=3))

    def test_sin_motivo_se_rechaza(self):
        with pytest.raises(LecturaRechazada):
            dom.validar_lectura(self.SERIE, _corr(250, 72, None, anula=3, motivo=''))


class TestLaPreguntaEsUna:

    def test_una_anulada_no_cuenta_aunque_se_pregunte_suelta(self):
        """`cuenta_en_la_serie` la llaman también quienes no pasan por
        `lecturas_que_cuentan` (el preventivo, el rendimiento)."""
        l = _l(1000, 0, 7)
        assert dom.cuenta_en_la_serie(l, set())
        assert not dom.cuenta_en_la_serie(l, {7})


class TestLaTardiaNoMueveElOdometro:

    def test_ni_el_odometro_ni_el_ritmo(self):
        serie = [_l(1000, 0, 1), _l(1500, 48, 2),
                 _l(1400, 72, 3, serie=dom.SERIE_TARDIA, confianza=Confianza.DUDOSA)]
        assert dom.odometro_actual(serie) == 1500
        assert 3 not in {l.id for l in dom.lecturas_que_cuentan(serie)}

    def test_fuera_de_la_serie_cuenta_cada_razon(self):
        serie = [_l(1000, 0, 1), _l(99999, 24, 2), _corr(1100, 30, 3, anula=2),
                 _l(20000, 48, 4, serie=dom.SERIE_SALTO, confianza=Confianza.DUDOSA),
                 _l(1050, 50, 5, serie=dom.SERIE_TARDIA, confianza=Confianza.DUDOSA)]
        assert dom.fuera_de_la_serie(serie) == {
            'anuladas': 1, 'saltos_sin_verificar': 1, 'tardias': 1}


class TestDondeNaceUnaLectura:

    def test_cuenta(self):
        assert dom.serie_al_nacer(valor_km=1100, ts=_T0, previa_valor_km=1000,
                                  previa_ts=_T0 - timedelta(days=1),
                                  tardia=False) == dom.SERIE_CUENTA

    def test_por_diez_es_salto(self):
        assert dom.serie_al_nacer(valor_km=10000, ts=_T0, previa_valor_km=1000,
                                  previa_ts=_T0 - timedelta(days=1),
                                  tardia=False) == dom.SERIE_SALTO

    def test_en_cero_tiempo_es_salto(self):
        assert dom.serie_al_nacer(valor_km=1001, ts=_T0, previa_valor_km=1000,
                                  previa_ts=_T0, tardia=False) == dom.SERIE_SALTO

    def test_la_tardia_la_pide_quien_escribe(self):
        assert dom.serie_al_nacer(valor_km=900, ts=_T0, previa_valor_km=1000,
                                  previa_ts=_T0 - timedelta(days=1),
                                  tardia=True) == dom.SERIE_TARDIA


class TestElTechoDeKmPorDia:

    def test_sin_ritmo_es_el_provisional(self):
        t = dom.techo_km_por_dia(dom.RitmoDeUso(dom.SIN_DATO, dom.SIN_DATO, 0, dom.SIN_DATO))
        assert t['km_dia'] == dom.KM_DIA_PLAUSIBLE_SIN_HISTORIA
        assert 'provisional' in t['base']

    def test_con_ritmo_tres_veces_y_un_minimo(self):
        lento = dom.RitmoDeUso(Decimal('50'), Confianza.DECLARADA, 5, Decimal('30'))
        rapido = dom.RitmoDeUso(Decimal('200'), Confianza.DECLARADA, 5, Decimal('30'))
        assert dom.techo_km_por_dia(lento)['km_dia'] == dom.KM_DIA_PLAUSIBLE_MINIMO
        assert dom.techo_km_por_dia(rapido)['km_dia'] == 600


# ══════════════════════════════════════════════════════════════════════════
# 2 · La base: el gancho y los triggers dicen lo mismo que el dominio
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mundo(db, almacen):
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='ENV100', tipo='NHR', activo=True)
    otro = Vehiculo(placa='ENV200', tipo='NHR', activo=True)
    admin = Usuario(nombre='Admin ENV', email='env_admin@test.com',
                    password_hash=generate_password_hash('x'), rol='admin',
                    almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, otro, admin])
    db.session.commit()
    return {'veh': veh.id, 'otro': otro.id, 'placa': veh.placa, 'u': admin.id,
            't': create_access_token(identity=str(admin.id)), 'db': db}


def _lectura(mundo, km, horas, origen='cierre_dia', anula=None, motivo=None,
             vehiculo=None):
    from flota.adaptadores.modelos import LecturaOdometro

    db = mundo['db']
    fila = LecturaOdometro(vehiculo_id=vehiculo or mundo['veh'], valor_km=km,
                           ts=_T0 + timedelta(hours=horas), origen=origen,
                           autor_usuario_id=mundo['u'], motivo_correccion=motivo,
                           anula_lectura_id=anula)
    db.session.add(fila)
    db.session.commit()
    return fila


def _actual(mundo):
    from flota.adaptadores.modelos import LecturaOdometro

    return dom.odometro_actual([l.a_dominio() for l in LecturaOdometro.query
                                .filter_by(vehiculo_id=mundo['veh']).all()])


class TestLaBaseNoSeTraba:

    def test_el_salto_nace_salto_y_la_siguiente_entra(self, mundo):
        _lectura(mundo, 12500, 0)
        salto = _lectura(mundo, 125000, 24)
        assert (salto.serie, salto.confianza) == ('salto', 'dudosa')
        buena = _lectura(mundo, 12600, 48)          # el trigger la deja pasar
        assert buena.serie == 'cuenta'
        assert _actual(mundo) == 12600

    def test_menor_que_lo_que_cuenta_sigue_rechazada(self, mundo):
        _lectura(mundo, 12500, 0)
        _lectura(mundo, 125000, 24)
        with pytest.raises(_RECHAZO):
            _lectura(mundo, 12400, 48)
        mundo['db'].session.rollback()

    def test_el_salto_verificado_si_sube_el_tope(self, mundo):
        from flota.adaptadores import verificacion

        _lectura(mundo, 12500, 0)
        salto = _lectura(mundo, 125000, 24)
        verificacion.verificar(lectura_id=salto.id, usuario_id=mundo['u'])
        assert _actual(mundo) == 125000
        with pytest.raises(_RECHAZO):
            _lectura(mundo, 12700, 48)
        mundo['db'].session.rollback()

    def test_la_anulacion_deja_la_fila_y_la_saca_de_la_cuenta(self, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        _lectura(mundo, 1000, 0)
        mala = _lectura(mundo, 5000, 24)            # ×5: no es salto, envenena
        _lectura(mundo, 1200, 48, origen='correccion', anula=mala.id,
                 motivo='era 1.200')
        assert LecturaOdometro.query.filter_by(vehiculo_id=mundo['veh']).count() == 3
        assert _actual(mundo) == 1200
        _lectura(mundo, 1300, 72)                   # ya no la traba la mala

    def test_una_lectura_no_se_anula_dos_veces(self, mundo):
        _lectura(mundo, 1000, 0)
        mala = _lectura(mundo, 5000, 24)
        _lectura(mundo, 1200, 48, origen='correccion', anula=mala.id, motivo='x')
        with pytest.raises(IntegrityError):
            _lectura(mundo, 1250, 72, origen='correccion', anula=mala.id, motivo='y')
        mundo['db'].session.rollback()

    def test_solo_una_correccion_anula(self, mundo):
        uno = _lectura(mundo, 1000, 0)
        with pytest.raises(IntegrityError):
            _lectura(mundo, 1100, 24, anula=uno.id)
        mundo['db'].session.rollback()

    def test_un_salto_no_nace_declarado(self, mundo, db):
        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
                "origen, autor_usuario_id, confianza, serie) VALUES "
                "(:v, 90000, :ts, 'cierre_dia', :u, 'declarada', 'salto')"),
                {'v': mundo['veh'], 'ts': _T0, 'u': mundo['u']})
        db.session.rollback()

    def test_la_serie_no_se_edita(self, mundo, db):
        f = _lectura(mundo, 1000, 0)
        with pytest.raises(_RECHAZO):
            db.session.execute(text(
                "UPDATE flota_lectura_odometro SET serie = 'tardia' WHERE id = :i"),
                {'i': f.id})
        db.session.rollback()

    def test_la_tardia_la_deja_pasar_el_trigger(self, mundo, db):
        _lectura(mundo, 1500, 24)
        db.session.execute(text(
            "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, origen, "
            "autor_usuario_id, confianza, motivo_dudosa, serie) VALUES "
            "(:v, 1400, :ts, 'tanqueo', :u, 'dudosa', 'tarde', 'tardia')"),
            {'v': mundo['veh'], 'ts': _T0 + timedelta(hours=30), 'u': mundo['u']})
        db.session.commit()
        assert _actual(mundo) == 1500

    def test_pedir_tardia_sin_serlo_nace_normal(self, mundo, db):
        """`before_insert` comprueba: una «tardía» que no queda por debajo de
        lo que cuenta es una lectura normal."""
        from flota.adaptadores.modelos import LecturaOdometro

        _lectura(mundo, 1000, 0)
        f = LecturaOdometro(vehiculo_id=mundo['veh'], valor_km=1100,
                            ts=_T0 + timedelta(hours=24), origen='tanqueo',
                            autor_usuario_id=mundo['u'], serie='tardia')
        db.session.add(f)
        db.session.commit()
        assert f.serie == 'cuenta'


# ══════════════════════════════════════════════════════════════════════════
# 3 · Los adaptadores: gasto tardío, daño, rendimiento, preventivo
# ══════════════════════════════════════════════════════════════════════════

def _tanquear(mundo, km, dia, tanque='lleno', horas=10):
    from flota.adaptadores import gastos

    return gastos.registrar_tanqueo(
        vehiculo_id=mundo['veh'], fecha=dia, valor='168000', galones='12',
        tanque=tanque, estacion='Terpel', km=km, proveedor='Terpel',
        origen_costo='tarjeta_convenio', registrado_por_usuario_id=mundo['u'],
        ts=datetime.combine(dia, datetime.min.time()) + timedelta(hours=horas))


class TestUnGastoNoSePierdePorElKm:

    def test_el_tanqueo_tardio_entra_y_queda_fuera_del_rendimiento(self, mundo):
        from flota.adaptadores import gastos

        d0 = date(2026, 9, 1)
        _tanquear(mundo, 1000, d0)
        _lectura(mundo, 1500, 24 * 3)               # la entrega del turno
        tq = _tanquear(mundo, 1400, d0 + timedelta(days=4))
        assert tq.gasto.lectura.serie == 'tardia'
        assert tq.gasto.lectura.confianza == 'dudosa'
        assert _actual(mundo) == 1500
        assert len(gastos.tanqueos_de(mundo['veh'])) == 1
        assert gastos.rendimiento_publicable_de(mundo['veh'])['tanqueos_fuera_por_km'] == 1

    def test_no_se_cuelga_de_una_lectura_anulada(self, mundo):
        """Con el mismo km se reutiliza la última lectura, pero solo una que
        CUENTA: colgado de una anulada, el gasto quedaría fuera del
        rendimiento por una lectura que no es la suya."""
        _lectura(mundo, 1000, 0)
        mala = _lectura(mundo, 5000, 24)
        _lectura(mundo, 1200, 48, origen='correccion', anula=mala.id, motivo='x')
        tq = _tanquear(mundo, 5000, date(2026, 9, 4))
        assert tq.gasto.lectura_id != mala.id
        assert tq.gasto.lectura.serie == 'cuenta'

    def test_no_se_cuelga_de_un_salto_con_el_mismo_numero(self, mundo):
        """La última lectura es un salto con ese mismo km: el gasto no se
        cuelga de ELLA (sería de otra hora y otro autor); nace la suya, y el
        dominio la juzga."""
        _lectura(mundo, 1000, 0)
        salto = _lectura(mundo, 12000, 24)
        assert salto.serie == 'salto'
        tq = _tanquear(mundo, 12000, date(2026, 9, 4))
        assert tq.gasto.lectura_id != salto.id

    def test_un_dano_con_km_menor_sigue_rechazado(self, mundo):
        """La tardía es solo para el gasto (hay plata detrás): el km de un daño
        que retrocede sigue siendo un error de digitación."""
        from flota.adaptadores.hallazgos import anclar_odometro

        _lectura(mundo, 1500, 0)
        with pytest.raises(LecturaRechazada):
            anclar_odometro(mundo['veh'], 1400, mundo['u'], _T0 + timedelta(hours=24))


class TestElPreventivoNoCuentaLoQueNoCuenta:

    def test_una_ejecucion_sobre_un_salto_no_es_linea_base(self, mundo):
        from flota.adaptadores import preventivo
        from flota.adaptadores.modelos import EjecucionTarea, FichaTecnica, PlanTarea
        from flota.adaptadores.preventivo import _ultimas_ejecuciones

        db = mundo['db']
        db.session.add(FichaTecnica(
            vehiculo_id=mundo['veh'], posiciones_llanta=6, km_inicial=1000,
            km_inicial_ts=datetime(2026, 1, 1), distribucion='correa',
            distribucion_fuente='manual_fabricante', distribucion_km_cambio=60000))
        db.session.commit()
        preventivo.sembrar_desde_ficha(mundo['veh'])
        buena = _lectura(mundo, 12500, 0)
        salto = _lectura(mundo, 125000, 24)
        plan = PlanTarea.query.filter_by(vehiculo_id=mundo['veh']).first()
        assert plan is not None, 'el escenario no sembró el plan'
        db.session.add_all([
            EjecucionTarea(plan_id=plan.id, lectura_id=buena.id,
                           ejecutado_ts=_T0, registrado_por_usuario_id=mundo['u']),
            EjecucionTarea(plan_id=plan.id, lectura_id=salto.id,
                           ejecutado_ts=_T0 + timedelta(hours=24),
                           registrado_por_usuario_id=mundo['u'])])
        db.session.commit()
        assert _ultimas_ejecuciones([plan.id]) == {plan.id: 12500}


# ══════════════════════════════════════════════════════════════════════════
# 4 · La bandeja no calla
# ══════════════════════════════════════════════════════════════════════════

class TestLaBandejaNoCalla:

    def _bandeja(self):
        from flota.adaptadores.bandeja import armar_bandeja

        return armar_bandeja()

    @staticmethod
    def _ne(b, placa, clase):
        return [n for n in b['senales_no_evaluables']
                if n['placa'] == placa and n['clase'] == clase]

    def test_sin_lecturas_se_declara(self, mundo):
        b = self._bandeja()
        motivos = [n['motivo'] for n in self._ne(b, 'ENV100', 'km_sin_ruta')]
        assert any('sin ninguna lectura' in m for m in motivos)

    def test_sin_tanqueos_recientes_se_declara(self, mundo):
        b = self._bandeja()
        assert self._ne(b, 'ENV100', 'galones')

    def test_casi_todo_parcial_es_senal(self, mundo):
        from app.utils.fecha import dia_operativo

        hoy = dia_operativo()
        for i, t in enumerate(['lleno', 'parcial', 'parcial', 'parcial', 'lleno']):
            _tanquear(mundo, 1000 + 100 * i, hoy - timedelta(days=10 - i), tanque=t)
        b = self._bandeja()
        s = [x for x in b['senales']
             if x['placa'] == 'ENV100' and x['clase'] == 'tanqueos_no_llenos']
        assert len(s) == 1 and s[0]['evidencia']['no_llenos'] == 3

    def test_con_pocos_tanqueos_no_se_juzga_y_se_dice(self, mundo):
        from app.utils.fecha import dia_operativo

        hoy = dia_operativo()
        for i in range(3):
            _tanquear(mundo, 1000 + 100 * i, hoy - timedelta(days=5 - i), tanque='parcial')
        b = self._bandeja()
        assert not [x for x in b['senales'] if x['clase'] == 'tanqueos_no_llenos']
        assert self._ne(b, 'ENV100', 'tanqueos_no_llenos')


# ══════════════════════════════════════════════════════════════════════════
# 5 · La frontera
# ══════════════════════════════════════════════════════════════════════════

class TestLaFrontera:

    def test_custodia_activa_dice_la_ultima_y_con_que_comparar(self, client, mundo):
        _lectura(mundo, 12500, 0)
        salto = _lectura(mundo, 125000, 24)
        d = client.get('/flota/custodia/activa/ENV100', headers=_auth(mundo['t'])).get_json()
        assert d['ultima_lectura']['id'] == salto.id        # la que se anula
        assert d['km_plausible']['km'] == 12500             # la que cuenta
        assert d['km_plausible']['km_dia'] == dom.KM_DIA_PLAUSIBLE_SIN_HISTORIA

    def test_anular_una_lectura_de_otro_camion_es_409(self, client, mundo):
        _lectura(mundo, 1000, 0)
        ajena = _lectura(mundo, 3000, 0, vehiculo=mundo['otro'])
        r = client.post('/flota/odometro', headers=_auth(mundo['t']), json={
            'placa': 'ENV100', 'valor_km': 900, 'origen': 'correccion',
            'motivo_correccion': 'x', 'anula_lectura_id': ajena.id})
        assert r.status_code == 409

    def test_la_anulada_no_se_verifica_ni_esta_en_la_cola(self, client, mundo):
        from flota.adaptadores import verificacion

        _lectura(mundo, 1000, 0)
        mala = _lectura(mundo, 5000, 24)
        _lectura(mundo, 1200, 48, origen='correccion', anula=mala.id, motivo='x')
        assert mala.id not in {l.id for l, _ in verificacion.pendientes()}
        r = client.post(f'/flota/odometro/{mala.id}/verificar',
                        headers=_auth(mundo['t']), json={})
        assert r.status_code == 409 and 'anuló' in r.get_json()['error']

    def test_el_ritmo_declara_lo_que_quedo_fuera(self, mundo):
        from flota.adaptadores.medicion import MedidorSQL

        _lectura(mundo, 1000, 0)
        _lectura(mundo, 20000, 24)
        fila = next(f for f in MedidorSQL().km_dia_por_vehiculo()
                    if f['placa'] == 'ENV100')
        assert fila['fuera_de_la_serie']['saltos_sin_verificar'] == 1


class TestLoQueQuedaFueraSeDice:

    def test_la_analitica_nombra_lo_que_no_entro_al_ritmo(self, tmp_path):
        from tests.flota.test_render_analitica_js import _correr as _correr_an

        h = {'km_dia_por_vehiculo': [
            {'placa': 'THP696', 'km_dia': '84.30', 'marca': 'declarada', 'n': 4,
             'dias': '13.0', 'motivo': None,
             'fuera_de_la_serie': {'anuladas': 1, 'saltos_sin_verificar': 2,
                                   'tardias': 0}},
            {'placa': 'TGZ653', 'km_dia': '10.00', 'marca': 'declarada', 'n': 3,
             'dias': '5.0', 'motivo': None,
             'fuera_de_la_serie': {'anuladas': 0, 'saltos_sin_verificar': 0,
                                   'tardias': 0}}]}
        html = _correr_an(tmp_path, h, fn='flotaAnRitmo', args=[h])['devuelto']
        assert 'Fuera del ritmo: 1 lectura anulada por una corrección · 2 saltos sin verificar' in html
        assert html.count('Fuera del ritmo') == 1, 'la placa sin nada fuera no lleva el renglón'

    def test_la_jornada_no_juzga_un_tramo_con_una_lectura_anulada(self, mundo):
        from app.services.jornada_conductor import _alguna_anulada

        a = _lectura(mundo, 1000, 0)
        mala = _lectura(mundo, 5000, 24)
        assert not _alguna_anulada(a, mala)
        _lectura(mundo, 1200, 48, origen='correccion', anula=mala.id, motivo='x')
        assert _alguna_anulada(a, mala)
        assert not _alguna_anulada(a)


# ══════════════════════════════════════════════════════════════════════════
# 6 · El teléfono pregunta antes de mandar un km increíble (Node, util.js real)
# ══════════════════════════════════════════════════════════════════════════

class TestElTelefonoPregunta:

    @staticmethod
    def _plausible(tmp_path, km, tanqueo=False, ref=12500, horas=24, km_dia=400):
        from tests.flota.test_mi_camion_hoy_js import _correr

        ts = (datetime.utcnow() - timedelta(hours=horas)).isoformat() + 'Z'
        semilla = ('FLOTA_COND = ' + json.dumps({
            'placa': 'THP696',
            'km_plausible': None if ref is None else
            {'km': ref, 'ts': ts, 'km_dia': km_dia}}) + ';')
        return _correr(tmp_path, f"flotaKmPlausible('THP696', {km}, "
                                 f"{{tanqueo: {str(tanqueo).lower()}}})",
                       semilla=semilla)

    def test_normal_no_dice_nada(self, tmp_path):
        assert self._plausible(tmp_path, 12700) is None

    def test_menor_bloquea(self, tmp_path):
        v = self._plausible(tmp_path, 12400)
        assert v['bloquea'] and 'no puede ser menor' in v['texto']

    def test_menor_en_un_tanqueo_pregunta(self, tmp_path):
        v = self._plausible(tmp_path, 12400, tanqueo=True)
        assert v['pregunta'] and not v.get('bloquea')

    def test_un_dedo_de_mas_pregunta_con_la_cuenta(self, tmp_path):
        v = self._plausible(tmp_path, 125000)
        assert v['pregunta'] and 'un día' in v['texto']

    def test_sin_referencia_no_pregunta(self, tmp_path):
        assert self._plausible(tmp_path, 999999, ref=None) is None

    def test_la_pregunta_es_en_pantalla_y_corregir_no_manda(self, tmp_path):
        from tests.flota.test_mi_camion_hoy_js import _correr

        ts = (datetime.utcnow() - timedelta(hours=24)).isoformat() + 'Z'
        semilla = ('FLOTA_COND = ' + json.dumps({'placa': 'THP696', 'km_plausible':
                   {'km': 12500, 'ts': ts, 'km_dia': 400}}) + ';'
                   'confirm = () => false;')
        assert _correr(tmp_path, "flotaKmCreible('THP696', 125000, 'x')",
                       semilla=semilla) is False


# ══════════════════════════════════════════════════════════════════════════
# 7 · TRINQUETE — una fila se vuelve dominio por UNA puerta
# ══════════════════════════════════════════════════════════════════════════

#: `Lectura(valor_km=<atributo>, …)` fuera de `a_dominio`: una segunda
#: traducción de fila a dominio que pierde el id, la anulación o la serie, y
#: con eso cuenta una lectura anulada o un salto. Solo encoge.
TRADUCCIONES_DECLARADAS: dict = {}


def _traducciones(fuente: str, nombre: str):
    """`(archivo, función)` de cada `Lectura(valor_km=<Attribute>)` fuera de
    `LecturaOdometro.a_dominio`."""
    arbol = ast.parse(fuente)
    hallados = []

    def visitar(nodo, clase, funcion):
        for hijo in ast.iter_child_nodes(nodo):
            c, f = clase, funcion
            if isinstance(hijo, ast.ClassDef):
                c = hijo.name
            elif isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f = hijo.name
            elif (isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Name)
                  and hijo.func.id == 'Lectura'
                  and any(k.arg == 'valor_km' and isinstance(k.value, ast.Attribute)
                          for k in hijo.keywords)
                  and not (c == 'LecturaOdometro' and f == 'a_dominio')):
                hallados.append((nombre, f))
            visitar(hijo, c, f)

    visitar(arbol, None, None)
    return hallados


def _escanear():
    total, hallados, a_dominio = 0, [], 0
    for base in ('app', 'flota'):
        for p in (RAIZ / base).rglob('*.py'):
            total += 1
            src = p.read_text(encoding='utf-8')
            nombre = str(p.relative_to(RAIZ))
            hallados += _traducciones(src, nombre)
            if nombre.endswith('adaptadores/modelos.py'):
                a_dominio = sum(1 for n in ast.walk(ast.parse(src))
                                if isinstance(n, ast.Call)
                                and isinstance(n.func, ast.Name) and n.func.id == 'Lectura')
    return total, hallados, a_dominio


class TestUnaSolaTraduccionDeFilaADominio:

    def test_nadie_fuera_de_a_dominio(self):
        _total, hallados, _ = _escanear()
        nuevos = [h for h in hallados if h not in TRADUCCIONES_DECLARADAS]
        assert not nuevos, (
            f'{nuevos}: una fila de odómetro se vuelve dominio sin '
            f'`LecturaOdometro.a_dominio` — sin id, anulación ni serie, cuenta '
            f'lo que no cuenta.')

    def test_el_inventario_solo_encoge(self):
        _total, hallados, _ = _escanear()
        muertos = [k for k in TRADUCCIONES_DECLARADAS if k not in hallados]
        assert not muertos, f'sacar del inventario: {muertos}'

    def test_piso(self):
        total, _h, a_dominio = _escanear()
        assert total >= 250
        assert a_dominio >= 1, 'el escáner no ve ni la puerta legítima'

    def test_ve_la_forma_prohibida(self):
        src = ('def f(fila):\n'
               '    return Lectura(valor_km=fila.valor_km, ts=fila.ts)\n')
        assert _traducciones(src, 'x.py') == [('x.py', 'f')]

    def test_ve_la_anidada(self):
        src = ('def f(filas):\n'
               '    def g(l):\n'
               '        return Lectura(valor_km=l.valor_km)\n'
               '    return [g(l) for l in filas]\n')
        assert _traducciones(src, 'x.py') == [('x.py', 'g')]

    def test_no_marca_lo_sano(self):
        src = ('class LecturaOdometro:\n'
               '    def a_dominio(self):\n'
               '        return Lectura(valor_km=self.valor_km)\n'
               'def propuesta(km):\n'
               '    """Lectura(valor_km=fila.valor_km) en un docstring."""\n'
               '    # Lectura(valor_km=fila.valor_km)\n'
               '    return Lectura(valor_km=km)\n')
        assert _traducciones(src, 'x.py') == []
