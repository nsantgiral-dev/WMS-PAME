"""Una corrección declara «de acá en adelante, esto es lo cierto» — y hasta el
2026-09-01 no lo hacía.

## El caso que lo destapó

THP696 tiene en producción una lectura de **16.697.948 km** registrada el
2026-08-18 con `origen=entrega`: un salto de +16.354.514 km en 312 horas. Son
43 idas y vueltas a la Luna. Pasó el trigger, pasó el dominio, y quedó como
`odometro_actual` del camión.

`validar_lectura` calculaba `tope = max(TODAS las previas)`. Consecuencia,
medida ejecutando el dominio:

    lectura real 55.400                → RECHAZADA
    se corrige con un registro nuevo   → aceptada
    lectura normal 55.450 después      → RECHAZADA otra vez

**La vía de escape que el propio mensaje de error recomienda no funcionaba.**
El máximo seguía incluyendo los 16,7 millones para siempre.

Lo que eso obliga en la calle: toda lectura futura de ese camión tiene que
marcarse `correccion` con motivo escrito —o sea, el operario tiene que
mentir— y `correccion` **salta la validación entera**. El vehículo pierde la
protección justo por usar el mecanismo diseñado para protegerlo.

## Por qué la corrección no puede ser un UPDATE

`flota_lectura_odometro` es append-only por trigger: `UPDATE` y `DELETE` están
bloqueados (`flota_odometro_no_update`, `flota_odometro_no_delete`), y el
mensaje del trigger lo dice: *«se corrige con un registro nuevo»*. Entonces la
única forma de neutralizar una lectura envenenada es que el registro nuevo
**supersede** a los anteriores. Este archivo fija esa semántica.

## Las dos mitades, contra los mismos casos

El módulo tiene la regla escrita: dominio y base dicen lo mismo, y *«lo que no
se vale es que digan cosas distintas — por eso las dos mitades se prueban
contra los mismos casos»*. Acá se prueba el dominio; el trigger de PostgreSQL
lo prueba `test_constraints_postgres.py` (que corre con
`./scripts/verificar_flota_postgres.sh`), y el de SQLite entra por los tests
de integración de este mismo directorio.

## El empate de `ts`

En producción hay **diez lecturas con el mismo timestamp al segundo**
(2026-08-03 20:04:50, el bug de las nueve custodias). Por eso la ventana es
`ts >= ts_de_la_correccion` y no `>`: ante el empate se cuenta hacia adentro,
o sea hacia el lado estricto. Rechazar de más es recuperable; aceptar de menos
envenena la serie.
"""
from datetime import datetime, timedelta

import pytest

from flota.dominio.errores import LecturaRechazada
from flota.dominio.odometro import odometro_actual, validar_lectura
from flota.dominio.valores import Lectura, OrigenLectura

_T0 = datetime(2026, 8, 3, 20, 4, 50)


def _lec(km, origen='entrega', minutos=0, motivo=None, autor=7):
    return Lectura(
        valor_km=km,
        ts=_T0 + timedelta(minutes=minutos),
        origen=OrigenLectura(origen),
        autor_usuario_id=autor,
        motivo_correccion=motivo,
    )


def _correccion(km, minutos, motivo='dedazo verificado contra la foto'):
    return _lec(km, 'correccion', minutos, motivo=motivo)


class TestElCasoTHP696:
    """La serie real que quedó trabada, reproducida."""

    @pytest.fixture
    def envenenada(self):
        """55.349 real, y después el dedazo de 16,7 millones."""
        return [_lec(55349, minutos=0), _lec(16697948, minutos=312 * 60)]

    def test_sin_corregir_el_camion_esta_trabado(self, envenenada):
        """El estado de hoy, y por qué hay que arreglarlo: la lectura real del
        odómetro rebota."""
        with pytest.raises(LecturaRechazada) as e:
            validar_lectura(envenenada, _lec(55400, minutos=400 * 60))
        assert '16697948' in str(e.value)

    def test_la_correccion_se_acepta(self, envenenada):
        """Ya funcionaba: `correccion` salta la monotonía. No es el arreglo."""
        validar_lectura(envenenada, _correccion(55400, minutos=400 * 60))

    def test_y_DESPUES_de_la_correccion_el_camion_vuelve_a_medirse(self, envenenada):
        """**El arreglo.** Antes del 2026-09-01 esto levantaba `LecturaRechazada`
        y el camión quedaba condenado a que toda lectura fuera una 'corrección'."""
        serie = envenenada + [_correccion(55400, minutos=400 * 60)]
        validar_lectura(serie, _lec(55450, minutos=500 * 60))

    def test_la_correccion_sigue_siendo_el_piso_nuevo(self, envenenada):
        """Superseder no es desactivar: por debajo de la corrección se rechaza
        igual. Si no, 'corregir' sería 'apagar el invariante'."""
        serie = envenenada + [_correccion(55400, minutos=400 * 60)]
        with pytest.raises(LecturaRechazada) as e:
            validar_lectura(serie, _lec(55350, minutos=500 * 60))
        assert '55400' in str(e.value)

    def test_el_mensaje_dice_desde_cuando_mide(self, envenenada):
        """Un rechazo que nombra un tope sin decir desde cuándo manda a buscar
        una lectura que puede estar meses atrás."""
        serie = envenenada + [_correccion(55400, minutos=400 * 60)]
        with pytest.raises(LecturaRechazada) as e:
            validar_lectura(serie, _lec(55350, minutos=500 * 60))
        assert 'desde la corrección' in str(e.value)


class TestLaOperacionSanaNoCambia:
    """Detector en las dos direcciones. Sin esto, «la corrección desbloquea»
    no distingue el arreglo de haber apagado la monotonía."""

    def test_una_serie_creciente_sin_correcciones_se_acepta_igual(self):
        serie = [_lec(1000, minutos=0), _lec(1200, minutos=60),
                 _lec(1500, minutos=120)]
        validar_lectura(serie, _lec(1600, minutos=180))

    def test_una_baja_sin_correcciones_se_sigue_rechazando(self):
        """El invariante original, intacto: es el caso que más veces va a
        correr y el que no puede haberse aflojado."""
        serie = [_lec(1000, minutos=0), _lec(1500, minutos=60)]
        with pytest.raises(LecturaRechazada):
            validar_lectura(serie, _lec(1400, minutos=120))

    def test_el_tope_sigue_siendo_el_maximo_y_no_la_ultima(self):
        """Si ya se registró 1.500, una lectura de 1.400 decrece aunque la
        anterior fuera 1.450. Es la razón por la que se usa `max` y no la
        última — no se perdió al meter la ventana."""
        serie = [_lec(1500, minutos=0), _lec(1450, minutos=60,
                                             origen='correccion',
                                             motivo='x')]
        # tras la corrección a 1.450, el piso es 1.450
        validar_lectura(serie, _lec(1460, minutos=120))
        with pytest.raises(LecturaRechazada):
            validar_lectura(serie, _lec(1440, minutos=120))

    def test_un_vehiculo_sin_lecturas_acepta_cualquier_primera(self):
        validar_lectura([], _lec(98765))

    def test_la_correccion_sigue_exigiendo_motivo_y_autor(self):
        """Lo que hace auditable la vía de escape. Si esto se afloja, la
        ventana nueva se vuelve un agujero: cualquiera baja el piso sin dejar
        rastro."""
        with pytest.raises(LecturaRechazada, match='motivo'):
            validar_lectura([_lec(1000)], _lec(500, 'correccion', motivo='  '))
        with pytest.raises(LecturaRechazada, match='autor'):
            validar_lectura([_lec(1000)],
                            _lec(500, 'correccion', motivo='ok', autor=None))


class TestVariasCorrecciones:
    def test_manda_la_ultima_correccion_no_la_primera(self):
        serie = [_lec(9000, minutos=0),
                 _correccion(100, minutos=10),
                 _lec(150, minutos=20),
                 _correccion(80, minutos=30)]
        validar_lectura(serie, _lec(90, minutos=40))      # ≥ 80 → entra
        with pytest.raises(LecturaRechazada):
            validar_lectura(serie, _lec(70, minutos=40))  # < 80 → no

    def test_una_lectura_alta_DESPUES_de_la_correccion_si_cuenta(self):
        """La ventana no es «solo la corrección»: es «la corrección y todo lo
        posterior». Una lectura legítima de 900 después de corregir a 100 sube
        el piso a 900."""
        serie = [_lec(9000, minutos=0),
                 _correccion(100, minutos=10),
                 _lec(900, minutos=20)]
        with pytest.raises(LecturaRechazada) as e:
            validar_lectura(serie, _lec(500, minutos=30))
        assert '900' in str(e.value)


class TestElEmpateDeTimestamp:
    """Diez lecturas comparten segundo en producción. El empate tiene que caer
    del lado estricto."""

    def test_una_lectura_con_el_MISMO_ts_que_la_correccion_cuenta(self):
        serie = [_lec(9000, minutos=0),
                 _correccion(100, minutos=10),
                 _lec(700, minutos=10)]      # mismo ts que la corrección
        with pytest.raises(LecturaRechazada) as e:
            validar_lectura(serie, _lec(300, minutos=20))
        assert '700' in str(e.value), (
            'el empate de ts cayó del lado permisivo: la lectura de 700 quedó '
            'fuera de la ventana y el piso bajó a 100')


class TestLaBaseDiceLoMismo:
    """La otra mitad. `flota/CLAUDE.md` lo pide: dominio y base dicen lo mismo,
    *«y lo que no se vale es que digan cosas distintas — por eso las dos mitades
    se prueban contra los mismos casos»*.

    Con `INSERT` crudo, no por el ORM: `test_constraints_t1.py` abre diciendo
    que *«si un INSERT crudo puede violar el invariante, el modelo está
    incompleto»*. Un arreglo que solo vive en el dominio deja el camino de la
    base abierto.

    Corre contra SQLite (la suite normal). El trigger de PostgreSQL —el que
    protege producción— lo ejerce `test_constraints_postgres.py`, vía
    `./scripts/verificar_flota_postgres.sh`.
    """

    @pytest.fixture
    def veh(self, db):
        from app.models.usuario import Usuario
        from app.models.vehiculo import Vehiculo
        v = Vehiculo(placa='THP696-CT', tipo='camion', activo=True)
        u = Usuario(email='odo_ct@test.com', nombre='CT', rol='admin', activo=True)
        u.set_password('x')
        db.session.add_all([v, u])
        db.session.commit()
        return {'v': v.id, 'u': u.id}

    @staticmethod
    def _ins(db, veh, km, minutos, origen='entrega', motivo=None):
        from sqlalchemy import text
        ts = (_T0 + timedelta(minutes=minutos)).isoformat(sep=' ')
        mot = 'NULL' if motivo is None else f"'{motivo}'"
        db.session.execute(text(
            # `confianza` va explícita: la columna es NOT NULL y no tiene
            # `server_default` a propósito, así que un INSERT crudo que no la
            # declare falla. Acá se prueba el TRIGGER de monotonía, no la marca:
            # `declarada` mantiene estas filas fuera de la cola y deja el
            # invariante bajo prueba solo.
            f"INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
            f"origen, autor_usuario_id, motivo_correccion, confianza) VALUES "
            f"({veh['v']}, {km}, '{ts}', '{origen}', {veh['u']}, {mot}, "
            f"'declarada')"))
        db.session.commit()

    def test_el_trigger_desbloquea_despues_de_una_correccion(self, app, db, veh):
        """El caso THP696 completo, contra la base."""
        from sqlalchemy.exc import IntegrityError, OperationalError

        self._ins(db, veh, 55349, 0)
        self._ins(db, veh, 16697948, 312 * 60)

        # 1 · la lectura real rebota
        with pytest.raises((IntegrityError, OperationalError)):
            self._ins(db, veh, 55400, 400 * 60)
        db.session.rollback()

        # 2 · se corrige con un registro nuevo
        self._ins(db, veh, 55400, 400 * 60, 'correccion', 'dedazo verificado')

        # 3 · y el camión vuelve a medirse — esto es lo que no pasaba
        self._ins(db, veh, 55450, 500 * 60)

        from sqlalchemy import text
        n = db.session.execute(text(
            "SELECT count(*) FROM flota_lectura_odometro "
            f"WHERE vehiculo_id = {veh['v']}")).scalar()
        assert n == 4, 'la lectura posterior a la corrección no se persistió'

    def test_el_trigger_sigue_rechazando_por_debajo_de_la_correccion(self, app, db, veh):
        """Superseder no es desactivar."""
        from sqlalchemy.exc import IntegrityError, OperationalError

        self._ins(db, veh, 55349, 0)
        self._ins(db, veh, 16697948, 312 * 60)
        self._ins(db, veh, 55400, 400 * 60, 'correccion', 'dedazo')
        with pytest.raises((IntegrityError, OperationalError)):
            self._ins(db, veh, 55350, 500 * 60)
        db.session.rollback()

    def test_sin_correcciones_el_trigger_no_cambio(self, app, db, veh):
        """La dirección sana, contra la base: una serie normal se comporta
        exactamente igual que antes."""
        from sqlalchemy.exc import IntegrityError, OperationalError

        self._ins(db, veh, 1000, 0)
        self._ins(db, veh, 1500, 60)
        self._ins(db, veh, 1600, 120)          # sube: entra
        with pytest.raises((IntegrityError, OperationalError)):
            self._ins(db, veh, 1400, 180)      # baja: rebota
        db.session.rollback()


class TestOdometroActualNoCambio:
    """`odometro_actual` usa el ts más reciente, no el máximo — para que una
    corrección le gane a la lectura que corrige. Sigue igual."""

    def test_la_correccion_manda_sobre_la_lectura_envenenada(self):
        serie = [_lec(55349, minutos=0),
                 _lec(16697948, minutos=100),
                 _correccion(55400, minutos=200)]
        assert odometro_actual(serie) == 55400
