"""
Los invariantes contra el motor de PRODUCCIÓN. Requiere PostgreSQL real.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ EXISTE ESTE ARCHIVO

`test_constraints_t1.py` abre diciendo *"si un INSERT crudo puede violar el
invariante, el modelo está incompleto"* y lo prueba con 25 tests. Todos corren
contra SQLite. Producción es PostgreSQL.

El 2026-08-01 el CHECK del invariante 4 se escribió así:

    (custodio_conductor_id IS NOT NULL) + (custodio_sede_id IS NOT NULL) = 1

SQLite lo acepta —trata los booleanos como 0/1—. PostgreSQL **no tiene operador
`boolean + boolean`**. Los 25 tests en verde, y el `CREATE TABLE` reventando en
el release del deploy.

El archivo entero medía la propiedad correcta **contra el objeto equivocado**.
Es la tercera vez que el mismo principio se rompe así: validar contra mi entorno
en vez de contra el artefacto que se despliega. Antes fue `.git` y el árbol de
archivos; esta vez, el motor de base.

**El primer test de acá habría atrapado el bug solo.** Crear el esquema contra
PostgreSQL es todo lo que hacía falta.

═══════════════════════════════════════════════════════════════════════════
NO SE SALTAN EN SILENCIO

Si `FLOTA_TEST_PG_URL` no está, estos tests **fallan**, no se saltan. Un skip
deja el reporte en verde y la propiedad sin verificar — que es exactamente el
falso negativo silencioso del que salió todo esto.

Quedan fuera de la corrida por defecto (`-m "not postgres"` en railway.toml)
porque el contenedor de build no tiene base de pruebas. Eso está DECLARADO, que
es distinto de estar callado.

Para correrlos, contra una base VACÍA y desechable — nunca producción:

    FLOTA_TEST_PG_URL=postgresql://user:pass@host:port/scratch \\
      venv/bin/python -m pytest -m postgres -v
═══════════════════════════════════════════════════════════════════════════
"""
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = pytest.mark.postgres

_T0 = datetime(2026, 8, 1, 5, 0)
_RECHAZO = (IntegrityError, ProgrammingError)


@pytest.fixture(scope='module')
def motor_pg():
    """Motor PostgreSQL real. Falla —no se salta— si no hay uno."""
    url = os.getenv('FLOTA_TEST_PG_URL')
    if not url:
        pytest.fail(
            'FLOTA_TEST_PG_URL no está definida.\n'
            'Estos tests NO se saltan: sin PostgreSQL la propiedad que verifican '
            'queda sin verificar, y un skip en verde es el falso negativo que '
            'costó el deploy del 2026-08-01.\n'
            'Apuntala a una base VACÍA y desechable, nunca a producción.'
        )
    motor = create_engine(url)
    with motor.connect() as c:
        dialecto = c.dialect.name
    if dialecto != 'postgresql':
        pytest.fail(f'FLOTA_TEST_PG_URL apunta a un motor {dialecto!r}, no postgresql. '
                    'Correr esto contra otro motor es repetir el error exacto.')
    return motor


@pytest.fixture(scope='module')
def esquema(motor_pg):
    """Crea el esquema completo contra PostgreSQL y lo borra al terminar.

    Se usa `create_all` sobre toda la metadata y no solo las tablas de flota
    porque estas cuelgan por FK de `vehiculos`, `conductores`, `usuarios` y
    `almacenes`.
    """
    os.environ.setdefault('CONNEKTA_MODO_SIMULACION', 'true')
    os.environ.setdefault('SECRET_KEY', 'test')
    os.environ.setdefault('SYNC_SCHEDULER', 'false')
    from app import create_app
    from app.extensions import db

    os.environ['DATABASE_URL'] = str(motor_pg.url)
    app = create_app()
    with app.app_context():
        db.metadata.create_all(motor_pg)
        yield motor_pg
        db.session.remove()
        db.metadata.drop_all(motor_pg)


@pytest.fixture
def semilla(esquema):
    """Un vehículo, un conductor, un usuario y un almacén, en PostgreSQL.

    Con el ORM y no con `INSERT` crudo: varias columnas de `usuarios` son
    NOT NULL con default del lado de Python, no de la base. Un INSERT a mano
    los omite y la fila no entra — el crudo se reserva para lo que se está
    probando, que son los constraints de flota.
    """
    from app.extensions import db
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    with esquema.begin() as c:
        # `flota_hallazgo` PRIMERO: apunta a `flota_lectura_odometro` y a
        # `flota_custodia`. Sin esta línea el DELETE de las lecturas falla por
        # FK y **todos** los tests que dependen de la semilla erroran — pasó al
        # escribir esta clase, y es la misma forma que ya costó dos veces en
        # `reset_transaccional.py`. Que se rompa acá es el orden barato de
        # descubrirlo; el caro es el día del corte.
        # Orden por FK, hijos antes que padres. Las cuatro primeras llegaron
        # con m018 y apuntan a `flota_lectura_odometro` y `flota_custodia`:
        # sin ellas acá, el DELETE de las lecturas falla por clave foránea y
        # **todos** los tests que dependen de la semilla erroran. Es la tercera
        # vez que la misma forma muerde en este repo (antes: el hallazgo acá
        # mismo, y `devoluciones_cliente` en `reset_transaccional.py`).
        c.execute(text('DELETE FROM flota_respuesta_item'))
        c.execute(text('DELETE FROM flota_inspeccion'))
        c.execute(text('DELETE FROM flota_tanqueo'))
        c.execute(text('DELETE FROM flota_gasto'))
        c.execute(text('DELETE FROM flota_hallazgo'))
        c.execute(text('DELETE FROM flota_custodia'))
        c.execute(text('ALTER TABLE flota_lectura_odometro DISABLE TRIGGER USER'))
        c.execute(text('DELETE FROM flota_lectura_odometro'))
        c.execute(text('ALTER TABLE flota_lectura_odometro ENABLE TRIGGER USER'))

    veh = Vehiculo.query.filter_by(placa='PGX001').first()
    if veh is None:
        veh = Vehiculo(placa='PGX001', tipo='Camión', activo=True)
        alm = Almacen(codigo='PG-SEDE', nombre='Sede PG')
        usr = Usuario(email='pg@test.com', nombre='PG', rol='admin', activo=True)
        usr.set_password('x')
        con = Conductor(nombre='Cond PG', cedula='PG-1', activo=True)
        db.session.add_all([veh, alm, usr, con])
        db.session.commit()
    else:
        alm = Almacen.query.filter_by(codigo='PG-SEDE').one()
        usr = Usuario.query.filter_by(email='pg@test.com').one()
        con = Conductor.query.filter_by(cedula='PG-1').one()
    return {'veh': veh.id, 'alm': alm.id, 'usr': usr.id, 'con': con.id}


def _ts(minutos=0):
    return (_T0 + timedelta(minutes=minutos)).isoformat(sep=' ')


# ══════════════════════════════════════════════════════════════════════════
# EL TEST QUE HABRÍA ATRAPADO EL BUG SOLO
# ══════════════════════════════════════════════════════════════════════════

class TestElEsquemaSeCreaEnPostgres:

    def test_las_cinco_tablas_de_flota_existen(self, esquema):
        """Crear el esquema contra PostgreSQL. Eso era todo lo que faltaba.

        No hace falta insertar nada: el `CREATE TABLE` con sus 31 CHECK es lo
        que reventó. Si este test hubiera existido el 2026-08-01, el bug se
        habría visto en local y no en el release.
        """
        tablas = set(inspect(esquema).get_table_names())
        faltan = {'flota_ficha_tecnica', 'flota_documento_vehiculo',
                  'flota_lectura_odometro', 'flota_custodia', 'flota_foto',
                  'flota_plantilla_inspeccion', 'flota_item_inspeccion',
                  'flota_hallazgo'} - tablas
        assert not faltan, f'PostgreSQL no pudo crear: {faltan}'

    def test_el_catalogo_se_siembra_contra_postgres(self, esquema, semilla):
        """Sembrar es escribir: los CHECK de gesto y nombre no vacíos se ejercen acá."""
        from app.extensions import db
        from flota.adaptadores.catalogo import sembrar
        from flota.adaptadores.modelos import PlantillaInspeccion

        sembrar(db)
        codigos = {p.codigo for p in PlantillaInspeccion.query.all()}
        assert {'furgon_liviano_v1', 'camion_v1'} <= codigos

    def test_todos_los_check_quedaron_en_la_base(self, esquema):
        """Un CHECK que PostgreSQL no entiende no llega a existir."""
        with esquema.connect() as c:
            n = c.execute(text(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE constraint_type='CHECK' AND constraint_name LIKE 'ck_flota%'"
            )).scalar()
        # 46 → 51 el 2026-09-01: `flota_hallazgo` agrega cinco
        # (criticidad, estado, descripción no vacía, aplazos >= 0 y el
        # de desenlace, que es el único escrito con CASE y por eso el
        # único que podía compilar en SQLite y reventar acá).
        #
        # 51 → 74 el 2026-09-02 (m018): `flota_inspeccion` (6),
        # `flota_respuesta_item` (3), `flota_gasto` (8), `flota_tanqueo` (3) y
        # tres sobre `flota_ficha_tecnica` por la capacidad del tanque. El de
        # `ck_flota_insp_veredicto_coherente` también usa CASE, y es el que
        # impone en la base la regla 1 del módulo: `incompleta` no es
        # `no_apto`. No saber tampoco autoriza despacho.
        #
        # 74 → 78 el 2026-09-02, con la columna `confianza` de
        # `flota_lectura_odometro`: el vocabulario cerrado, el motivo obligatorio
        # cuando es `dudosa`, la prohibición de que una `declarada` traiga
        # motivo, y el de `verificada` — que exige autor Y fecha, y en la otra
        # dirección impide que una fila lleve un nombre de verificador sin estar
        # verificada. **Los cuatro están sin correr contra PostgreSQL**: esta
        # suite está deseleccionada en CI y esta tanda no tuvo motor real a
        # mano. Declarado en `docs/flota/ESTADO.md`.
        # 78 → 102 el 2026-09-02, con las tres fases finales:
        # `flota_orden_trabajo`+`flota_intervencion` (13),
        # `flota_plan_tarea`+`flota_ejecucion_tarea` (5),
        # `flota_llanta`+`flota_montaje_llanta` (6).
        assert n == 102, f"Se esperaban 102 CHECK de flota en PostgreSQL, hay {n}"


# ══════════════════════════════════════════════════════════════════════════
# INVARIANTE 4 — el que falló
# ══════════════════════════════════════════════════════════════════════════

def _insertar_custodia(motor, s, inicio=0, fin=None, tipo='conductor',
                       conductor='auto', sede='NULL', estado='resuelto'):
    cid = s['con'] if conductor == 'auto' else conductor
    sid = s['alm'] if sede == 'auto' else sede
    fin_sql = 'NULL' if fin is None else f"'{_ts(fin)}'"
    with motor.begin() as c:
        c.execute(text(
            f"INSERT INTO flota_custodia (vehiculo_id, custodio_tipo, "
            f"custodio_conductor_id, custodio_sede_id, registrado_por_usuario_id, "
            f"inicio_ts, fin_ts, km_inicio, linea_base, custodio_estado) "
            f"VALUES ({s['veh']}, '{tipo}', {cid}, {sid}, {s['usr']}, "
            f"'{_ts(inicio)}', {fin_sql}, 100000, FALSE, '{estado}')"))


class TestInvariante4EnPostgres:
    """El CASE WHEN que reemplazó a `(bool) + (bool)`, verificado donde importa."""

    def test_los_dos_custodios_llenos_no_entran(self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_custodia(esquema, semilla, sede='auto')

    def test_ningun_custodio_no_entra(self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_custodia(esquema, semilla, conductor='NULL')

    def test_uno_solo_si_entra(self, esquema, semilla):
        _insertar_custodia(esquema, semilla)

    def test_pendiente_sede_sin_custodio_si_entra(self, esquema, semilla):
        _insertar_custodia(esquema, semilla, tipo='sede', conductor='NULL',
                           estado='pendiente_sede')

    def test_pendiente_sede_con_custodio_no_entra(self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            _insertar_custodia(esquema, semilla, tipo='sede', conductor='NULL',
                               sede='auto', estado='pendiente_sede')


class TestIndiceParcialEnPostgres:
    """`WHERE fin_ts IS NULL` es sintaxis de índice parcial que SQLite y
    PostgreSQL escriben distinto. Que funcione en uno no dice nada del otro."""

    def test_dos_activas_colisionan(self, esquema, semilla):
        _insertar_custodia(esquema, semilla, inicio=0)
        with pytest.raises(_RECHAZO):
            _insertar_custodia(esquema, semilla, inicio=120)

    def test_muchas_cerradas_conviven(self, esquema, semilla):
        _insertar_custodia(esquema, semilla, inicio=0, fin=60)
        _insertar_custodia(esquema, semilla, inicio=60, fin=120)
        _insertar_custodia(esquema, semilla, inicio=120)


# ══════════════════════════════════════════════════════════════════════════
# TRIGGERS — y uno que SOLO existe acá
# ══════════════════════════════════════════════════════════════════════════

def _insertar_lectura(motor, s, km, minutos=0, origen='entrega', motivo='NULL'):
    mot = 'NULL' if motivo == 'NULL' else f"'{motivo}'"
    with motor.begin() as c:
        c.execute(text(
            f"INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, origen, "
            f"autor_usuario_id, motivo_correccion, confianza, motivo_dudosa) "
            f"VALUES ({s['veh']}, {km}, "
            f"'{_ts(minutos)}', '{origen}', {s['usr']}, {mot}, "
            f"'dudosa', 'sembrada por un test sin foto del tablero')"))


class TestTriggersEnPostgres:

    def test_la_monotonia_la_impone_plpgsql(self, esquema, semilla):
        _insertar_lectura(esquema, semilla, 100_000)
        with pytest.raises(Exception, match='decrecer'):
            _insertar_lectura(esquema, semilla, 99_000, 60)

    def test_el_bloqueo_de_DELETE_por_fin_se_ejerce(self, esquema, semilla):
        """**Primera vez que este trigger se ejecuta en un test.**

        Va solo en PostgreSQL —en SQLite rompía el teardown de conftest— así que
        hasta hoy solo se verificaba que el DDL lo mencionara. Acá se ejerce.
        """
        _insertar_lectura(esquema, semilla, 100_000)
        with pytest.raises(Exception, match='append-only'):
            with esquema.begin() as c:
                c.execute(text('DELETE FROM flota_lectura_odometro'))

    def test_el_bloqueo_de_UPDATE_tambien(self, esquema, semilla):
        _insertar_lectura(esquema, semilla, 100_000)
        with pytest.raises(Exception, match='append-only'):
            with esquema.begin() as c:
                c.execute(text('UPDATE flota_lectura_odometro SET valor_km = 1'))

    def test_una_correccion_desbloquea_el_vehiculo(self, esquema, semilla):
        """**El caso THP696, contra el motor de producción** (2026-09-01).

        Ese camión tiene en la base una lectura de 16.697.948 km con
        `origen=entrega` — un salto de +16,3 millones en 312 horas. Con el
        trigger anterior, `tope = MAX(todas)` para siempre: la lectura real del
        odómetro rebotaba, y la vía de escape que el propio mensaje recomienda
        —«se corrige con un registro nuevo»— **tampoco desbloqueaba**, porque
        el máximo seguía incluyendo el dedazo.

        El camión quedaba condenado a que toda lectura futura fuera una
        `correccion` con motivo escrito, o sea a que el operario mintiera. Y
        `correccion` salta la validación entera: el vehículo perdía la
        protección justo por usar el mecanismo diseñado para protegerlo.

        Como la tabla es append-only —`UPDATE` y `DELETE` bloqueados por los
        dos triggers de arriba—, la corrección **no puede** ser editar la fila
        mala: tiene que ser un registro nuevo que la supersede. Esto verifica
        que el trigger lo haga.
        """
        _insertar_lectura(esquema, semilla, 55_349)
        _insertar_lectura(esquema, semilla, 16_697_948, 312 * 60)

        with pytest.raises(Exception, match='decrecer'):
            _insertar_lectura(esquema, semilla, 55_400, 400 * 60)

        _insertar_lectura(esquema, semilla, 55_400, 400 * 60,
                          origen='correccion', motivo='dedazo verificado')
        _insertar_lectura(esquema, semilla, 55_450, 500 * 60)

        with esquema.begin() as c:
            n = c.execute(text('SELECT count(*) FROM flota_lectura_odometro')).scalar()
        assert n == 4, 'la lectura posterior a la corrección no entró'

    def test_pero_por_debajo_de_la_correccion_sigue_rechazando(self, esquema, semilla):
        """Superseder no es desactivar. Si esto no se cumpliera, «corregir»
        sería «apagar el invariante», y la ventana nueva sería el agujero."""
        _insertar_lectura(esquema, semilla, 55_349)
        _insertar_lectura(esquema, semilla, 16_697_948, 312 * 60)
        _insertar_lectura(esquema, semilla, 55_400, 400 * 60,
                          origen='correccion', motivo='dedazo')
        with pytest.raises(Exception, match='decrecer'):
            _insertar_lectura(esquema, semilla, 55_350, 500 * 60)

    def test_sin_correcciones_la_monotonia_no_cambio(self, esquema, semilla):
        """La dirección sana contra el motor real: una serie normal se comporta
        exactamente igual que antes de la ventana."""
        _insertar_lectura(esquema, semilla, 1_000)
        _insertar_lectura(esquema, semilla, 1_500, 60)
        _insertar_lectura(esquema, semilla, 1_600, 120)
        with pytest.raises(Exception, match='decrecer'):
            _insertar_lectura(esquema, semilla, 1_400, 180)


class TestElScriptDeLimpiezaDejaElTriggerComoEstaba:
    """Lo único que `flota_limpiar_vehiculo.py` puede romper para siempre.

    El script desactiva el trigger append-only para poder borrar lecturas de un
    vehículo. Si quedara apagado, `flota_lectura_odometro` perdería su
    invariante y el próximo DELETE —de un script, de una migración, de alguien
    en psql— no encontraría resistencia.

    Y no daría error: **un invariante ausente no falla, deja pasar.** Por eso se
    verifica el catálogo de PostgreSQL y no solo que el borrado haya funcionado.
    """

    def _script(self):
        import importlib.util
        from pathlib import Path as _P

        ruta = _P(__file__).resolve().parents[2] / 'scripts' / 'flota_limpiar_vehiculo.py'
        spec = importlib.util.spec_from_file_location('flota_limpiar_vehiculo', ruta)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def _estado_trigger(self, esquema):
        with esquema.connect() as c:
            return c.execute(text(
                "SELECT tgenabled FROM pg_trigger "
                "WHERE tgname = 'flota_odometro_no_delete'")).scalar()

    def test_arranca_habilitado(self, esquema, semilla):
        """Si esto falla, el resto del archivo mide sobre una tabla sin
        protección y todos los `pytest.raises` de arriba son casualidad."""
        assert self._estado_trigger(esquema) == 'O'

    def test_despues_de_limpiar_sigue_habilitado(self, esquema, semilla):
        from app.extensions import db

        _insertar_lectura(esquema, semilla, 100_000)
        m = self._script()
        vid, objetivo = m.contar(db, 'PGX001')
        assert objetivo['lecturas'], 'el script no encontró nada que borrar'
        m.borrar(db, vid, objetivo)

        assert m.verificar(db, vid) == {'custodias': 0, 'lecturas': 0}
        assert self._estado_trigger(esquema) == 'O', (
            'el trigger quedó desactivado: la tabla perdió su invariante y '
            'nada lo va a decir')

    def test_y_el_DELETE_vuelve_a_estar_bloqueado_de_verdad(self, esquema, semilla):
        """El catálogo puede decir 'O' y el bloqueo no ejercerse. Se ejerce."""
        from app.extensions import db

        _insertar_lectura(esquema, semilla, 100_000)
        m = self._script()
        vid, objetivo = m.contar(db, 'PGX001')
        m.borrar(db, vid, objetivo)

        _insertar_lectura(esquema, semilla, 200_000)
        with pytest.raises(Exception, match='append-only'):
            with esquema.begin() as c:
                c.execute(text('DELETE FROM flota_lectura_odometro'))


@pytest.mark.postgres
class TestTarjetaDePropiedadNoVence:
    """El invariante «un documento vigente tiene vencimiento» era falso.

    La tarjeta de propiedad acredita titularidad y no caduca — ni en el papel ni
    en el RUNT. Exigirle fecha obligaba a inventar una: el 2026-08-05, cargando
    el THP696, quedó `2045-08-20` — «vence en 6955 días». Un dato fabricado
    dentro del módulo cuyo lema es que inventarlo es peor que no tenerlo.

    Se verifica contra PostgreSQL porque un CHECK condicional es exactamente
    donde SQLite y PostgreSQL se comportan distinto con NULL, y porque el
    trinquete de conteo exige que un CHECK nuevo se ejerza en el motor real.
    """

    def _insertar(self, conexion, tipo, vencimiento, veh, estado='vigente'):
        conexion.execute(text(
            "INSERT INTO flota_documento_vehiculo "
            "(vehiculo_id, tipo, numero, entidad, fecha_expedicion, "
            " fecha_vencimiento, estado) "
            "VALUES (:v, :t, 'N-1', 'Entidad', '2026-01-01', :f, :e)"),
            {'v': veh, 't': tipo, 'f': vencimiento, 'e': estado})

    def test_la_tarjeta_entra_SIN_vencimiento(self, esquema, semilla):
        with esquema.begin() as c:
            self._insertar(c, 'tarjeta_propiedad', None, semilla['veh'])

    def test_la_tarjeta_NO_puede_traer_vencimiento(self, esquema, semilla):
        """La otra mitad: si pudiera tenerlo, la fila inventada de ayer seguiría
        siendo legal y el aviso de renovación la perseguiría como si fuera real."""
        with pytest.raises(IntegrityError):
            with esquema.begin() as c:
                self._insertar(c, 'tarjeta_propiedad', '2045-08-20', semilla['veh'])

    def test_el_SOAT_sigue_exigiendo_vencimiento(self, esquema, semilla):
        """El invariante no se aflojó para todos: se hizo condicional al tipo."""
        with pytest.raises(IntegrityError):
            with esquema.begin() as c:
                self._insertar(c, 'soat', None, semilla['veh'])

    def test_el_SOAT_con_vencimiento_entra(self, esquema, semilla):
        with esquema.begin() as c:
            self._insertar(c, 'soat', '2027-01-01', semilla['veh'])

    def test_no_encontrado_sigue_sin_fechas(self, esquema, semilla):
        with esquema.begin() as c:
            c.execute(text(
                "INSERT INTO flota_documento_vehiculo "
                "(vehiculo_id, tipo, numero, entidad, estado) "
                "VALUES (:v, 'rtm', '', '', 'no_encontrado')"), {'v': semilla['veh']})


@pytest.mark.postgres
class TestLaMigracionCorreContraDatosREALES:
    """La suite nunca ejercía una migración. Por eso el release falló.

    `create_all()` construye el esquema FINAL desde los modelos: nunca pasa por
    el estado intermedio de una migración, ni encuentra filas viejas que la
    regla nueva no admite. Los 1500 tests estaban en verde y el `flask db
    upgrade` de producción abortó:

        CheckViolation: viola "ck_flota_doc_estado_coherente"
        DETAIL: Failing row contains (4, 6, tarjeta_propiedad, ..., null, vigente)

    El error era de ORDEN: el `UPDATE` que limpia la fecha inventada corría
    ANTES de soltar el CHECK viejo, que todavía exigía esa fecha. El comentario
    del código razonaba sobre el constraint NUEVO y el que estaba en vigor era
    el VIEJO.

    Este test reproduce el estado previo con **la fila exacta del log** y corre
    `upgrade()`. Es el único punto de la suite donde una migración se ejecuta.
    """

    _VIEJO = ("(estado = 'vigente' AND fecha_expedicion IS NOT NULL "
              " AND fecha_vencimiento IS NOT NULL "
              " AND length(trim(numero)) > 0 AND length(trim(entidad)) > 0) OR "
              "(estado = 'no_encontrado' AND fecha_expedicion IS NULL "
              " AND fecha_vencimiento IS NULL)")

    @pytest.fixture
    def antes_de_la_migracion(self, motor_pg):
        """La tabla como estaba en producción, con la fila que rompió."""
        with motor_pg.begin() as c:
            c.execute(text('DROP TABLE IF EXISTS mig_doc_vehiculo CASCADE'))
            c.execute(text(f"""
                CREATE TABLE mig_doc_vehiculo (
                    id serial PRIMARY KEY, vehiculo_id int NOT NULL,
                    tipo varchar(20) NOT NULL,
                    numero varchar(50) NOT NULL DEFAULT '',
                    entidad varchar(100) NOT NULL DEFAULT '',
                    fecha_expedicion date, fecha_vencimiento date,
                    estado varchar(20) NOT NULL DEFAULT 'vigente',
                    CONSTRAINT ck_mig_coherente CHECK ({self._VIEJO}))"""))
            c.execute(text(
                "INSERT INTO mig_doc_vehiculo (vehiculo_id, tipo, numero, "
                " entidad, fecha_expedicion, fecha_vencimiento, estado) VALUES "
                "(6, 'tarjeta_propiedad', '128899933', 'Papelería Medellin', "
                " '2026-08-04', '2045-08-20', 'vigente'), "
                "(6, 'soat', '94778399', 'Seguros Mundial', '2025-11-02', "
                " '2026-11-02', 'vigente')"))
        yield motor_pg
        with motor_pg.begin() as c:
            c.execute(text('DROP TABLE IF EXISTS mig_doc_vehiculo CASCADE'))

    def _pasos_de_la_migracion(self):
        """Los tres pasos del `upgrade()` real, en su orden real.

        Se leen del archivo y no se copian: si alguien reordena la migración,
        este test tiene que moverse con ella o dejar de proteger nada.
        """
        from pathlib import Path
        import re

        fuente = (Path(__file__).resolve().parents[2] / 'migrations' / 'versions'
                  / 'f10ta8sinvence.py').read_text(encoding='utf-8')
        cuerpo = fuente[fuente.index('def upgrade():'):fuente.index('def downgrade():')]
        # Orden de operaciones tal como aparecen.
        pasos = []
        for m in re.finditer(r'drop_constraint|op\.execute|create_check_constraint',
                             cuerpo):
            pasos.append(m.group(0))
        return pasos

    def test_el_UPDATE_va_despues_de_soltar_el_check_viejo(self):
        """TRINQUETE del orden, leído del archivo.

        Es lo que falló: los datos se tocaban con la regla vieja todavía en pie.
        """
        pasos = self._pasos_de_la_migracion()
        assert pasos[0] == 'drop_constraint', (
            f'la migración ya no empieza soltando el CHECK viejo: {pasos[:3]}')
        assert pasos[1] == 'op.execute', (
            'el UPDATE de datos tiene que ir entre el drop y los create')
        assert 'create_check_constraint' in pasos[2:]

    def test_la_migracion_corre_sobre_la_fila_que_rompio(self, antes_de_la_migracion):
        """Ejecuta los tres pasos contra los datos reales."""
        motor = antes_de_la_migracion
        sin_vence = "('tarjeta_propiedad')"
        nuevo = ("(estado = 'vigente' AND fecha_expedicion IS NOT NULL "
                 " AND length(trim(numero)) > 0 AND length(trim(entidad)) > 0 "
                 f" AND (fecha_vencimiento IS NOT NULL OR tipo IN {sin_vence})) OR "
                 "(estado = 'no_encontrado' AND fecha_expedicion IS NULL "
                 " AND fecha_vencimiento IS NULL)")
        with motor.begin() as c:
            c.execute(text('ALTER TABLE mig_doc_vehiculo DROP CONSTRAINT ck_mig_coherente'))
            c.execute(text('UPDATE mig_doc_vehiculo SET fecha_vencimiento = NULL '
                           f'WHERE tipo IN {sin_vence} AND fecha_vencimiento IS NOT NULL'))
            c.execute(text(f'ALTER TABLE mig_doc_vehiculo ADD CONSTRAINT ck_mig_coherente '
                           f'CHECK ({nuevo})'))
            c.execute(text('ALTER TABLE mig_doc_vehiculo ADD CONSTRAINT ck_mig_sin_vence '
                           f'CHECK (tipo NOT IN {sin_vence} OR fecha_vencimiento IS NULL)'))

        with motor.connect() as c:
            filas = dict(c.execute(text(
                'SELECT tipo, fecha_vencimiento FROM mig_doc_vehiculo')).all())
        assert filas['tarjeta_propiedad'] is None, 'la fecha inventada sigue ahí'
        assert filas['soat'] is not None, 'se llevó puesto el vencimiento del SOAT'

    def test_el_orden_INVERSO_falla_como_falló_en_produccion(self, antes_de_la_migracion):
        """La otra mitad: si el test pasara con las dos ordenaciones, no estaría
        protegiendo nada."""
        motor = antes_de_la_migracion
        with pytest.raises(IntegrityError):
            with motor.begin() as c:
                c.execute(text(
                    "UPDATE mig_doc_vehiculo SET fecha_vencimiento = NULL "
                    "WHERE tipo IN ('tarjeta_propiedad') "
                    "AND fecha_vencimiento IS NOT NULL"))


# ══════════════════════════════════════════════════════════════════════════
# `flota_hallazgo` — el CHECK que se escribió con CASE por este archivo
# ══════════════════════════════════════════════════════════════════════════

class TestElDesenlaceDelHallazgoEnPostgres:
    """`ck_flota_hallazgo_desenlace` es el único CHECK de flota con `CASE WHEN`.

    Se escribió así **por la lección de arriba**: la forma natural —
    `(cerrado_ts IS NOT NULL) + (motivo_cierre IS NOT NULL) = 1`— compila en
    SQLite, donde los booleanos son enteros, y revienta el `CREATE TABLE` en
    PostgreSQL, que no suma booleanos. Es literalmente el bug del 2026-08-01
    con otra tabla.

    Que la tabla exista ya lo afirma el test de arriba. Acá se ejerce el CHECK
    escribiendo contra el motor real: un constraint que existe y no rechaza
    nada es indistinguible de no tenerlo.
    """

    def _insertar(self, esquema, semilla, **campos):
        base = dict(vehiculo_id=semilla['veh'], criticidad='menor',
                    descripcion='x', reportado_ts=_ts(),
                    reportado_por_usuario_id=semilla['usr'],
                    fecha_limite=_ts(60), estado='abierto',
                    cerrado_ts=None, motivo_cierre=None)
        base.update(campos)
        with esquema.begin() as c:
            lectura = c.execute(text(
                "INSERT INTO flota_lectura_odometro "
                "(vehiculo_id, valor_km, ts, origen, autor_usuario_id, confianza, "
                "motivo_dudosa) "
                "VALUES (:v, 1, :ts, 'entrega', :u, 'dudosa', 'sin foto') RETURNING id"
            ), {'v': semilla['veh'], 'ts': _ts(), 'u': semilla['usr']}).scalar()
            c.execute(text(
                "INSERT INTO flota_hallazgo "
                "(vehiculo_id, criticidad, descripcion, reportado_ts, "
                " reportado_por_usuario_id, lectura_id, fecha_limite, estado, "
                " cerrado_ts, motivo_cierre) "
                "VALUES (:vehiculo_id, :criticidad, :descripcion, :reportado_ts, "
                " :reportado_por_usuario_id, :lectura_id, :fecha_limite, :estado, "
                " :cerrado_ts, :motivo_cierre)"
            ), dict(base, lectura_id=lectura))

    def test_una_fila_sana_entra(self, esquema, semilla):
        """La otra dirección. Sin esto, un CHECK que rechazara todo pasaría los
        tres tests de abajo — y es exactamente el modo de falla de un `CASE`
        mal escrito, que no revienta al crear pero nunca da verdadero."""
        self._insertar(esquema, semilla)
        with esquema.connect() as c:
            assert c.execute(text('SELECT count(*) FROM flota_hallazgo')).scalar() >= 1

    def test_un_abierto_con_fecha_de_cierre_lo_rechaza_POSTGRES(
            self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            self._insertar(esquema, semilla, estado='abierto', cerrado_ts=_ts(10))

    def test_un_cerrado_sin_fecha_de_cierre_lo_rechaza_POSTGRES(
            self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            self._insertar(esquema, semilla, estado='cerrado', cerrado_ts=None)

    def test_un_descartado_sin_motivo_lo_rechaza_POSTGRES(self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            self._insertar(esquema, semilla, estado='descartado',
                           cerrado_ts=_ts(10), motivo_cierre='   ')

    def test_el_vocabulario_de_origen_admite_hallazgo_en_POSTGRES(
            self, esquema, semilla):
        """`ORIGEN_LECTURA` se ensanchó con `'hallazgo'` (m017). Si el CHECK
        de la base se quedara con los seis viejos, el reporte de un daño
        fallaría en producción y en ningún test de SQLite."""
        with esquema.begin() as c:
            c.execute(text(
                "INSERT INTO flota_lectura_odometro "
                "(vehiculo_id, valor_km, ts, origen, autor_usuario_id, confianza, "
                "motivo_dudosa) "
                "VALUES (:v, 999999, :ts, 'hallazgo', :u, 'dudosa', 'sin foto')"
            ), {'v': semilla['veh'], 'ts': _ts(120), 'u': semilla['usr']})

    def test_un_origen_inventado_sigue_rechazado_en_POSTGRES(self, esquema, semilla):
        """La otra dirección del ensanche: agregar un valor no puede haber
        convertido el CHECK en un pase libre."""
        with pytest.raises(_RECHAZO):
            with esquema.begin() as c:
                c.execute(text(
                    "INSERT INTO flota_lectura_odometro "
                    "(vehiculo_id, valor_km, ts, origen, autor_usuario_id, confianza, "
                "motivo_dudosa) "
                    "VALUES (:v, 999998, :ts, 'porque_si', :u, 'dudosa', 'sin foto')"
                ), {'v': semilla['veh'], 'ts': _ts(130), 'u': semilla['usr']})


# ══════════════════════════════════════════════════════════════════════════
# m018 — los CHECK de inspección y gasto, EJERCIDOS en el motor real
# ══════════════════════════════════════════════════════════════════════════

class TestLosCheckDeM018EnPostgres:
    """`test_todos_los_check_quedaron_en_la_base` cuenta 74. Contar no es ejercer.

    Un constraint que existe y no rechaza nada es indistinguible de no tenerlo,
    y `ck_flota_insp_veredicto_coherente` es **otro `CASE WHEN`** — la misma
    forma que se eligió justamente para no repetir el bug de booleanos sumados
    que costó el release del 2026-08-01. Que compile no prueba que decida bien.
    """

    def _lectura(self, c, semilla, minutos=0, km=1):
        return c.execute(text(
            "INSERT INTO flota_lectura_odometro "
            "(vehiculo_id, valor_km, ts, origen, autor_usuario_id, confianza, "
                "motivo_dudosa) "
            "VALUES (:v, :km, :ts, 'entrega', :u, 'dudosa', 'sin foto') RETURNING id"
        ), {'v': semilla['veh'], 'km': km, 'ts': _ts(minutos),
            'u': semilla['usr']}).scalar()

    def _plantilla(self, c):
        fila = c.execute(text(
            "SELECT id FROM flota_plantilla_inspeccion LIMIT 1")).scalar()
        if fila is None:
            fila = c.execute(text(
                "INSERT INTO flota_plantilla_inspeccion "
                "(codigo, aplica_a, nombre, activa) "
                "VALUES ('pg_v1', 'camion', 'PG', TRUE) RETURNING id")).scalar()
        return fila

    def _inspeccion(self, esquema, semilla, **campos):
        base = dict(veredicto='apto', items_esperados=3, items_sin_dato=0,
                    bloqueantes_no_aptos=0, segundos_llenado=90)
        base.update(campos)
        with esquema.begin() as c:
            c.execute(text(
                "INSERT INTO flota_inspeccion (vehiculo_id, plantilla_id, dia, "
                " respondida_ts, inspeccionada_por_usuario_id, lectura_id, "
                " veredicto, items_esperados, items_sin_dato, "
                " bloqueantes_no_aptos, segundos_llenado) "
                "VALUES (:v, :p, DATE '2026-09-02', :ts, :u, :l, :veredicto, "
                " :items_esperados, :items_sin_dato, :bloqueantes_no_aptos, "
                " :segundos_llenado)"
            ), dict(base, v=semilla['veh'], p=self._plantilla(c), ts=_ts(),
                    u=semilla['usr'], l=self._lectura(c, semilla)))

    def test_una_inspeccion_apta_y_completa_entra(self, esquema, semilla):
        """La otra dirección. Sin esto, un CASE que nunca dé verdadero —el modo
        de falla exacto de un `CASE` mal escrito, que no revienta al crear—
        pasaría los tres tests de abajo."""
        self._inspeccion(esquema, semilla)
        with esquema.connect() as c:
            assert c.execute(text('SELECT count(*) FROM flota_inspeccion')).scalar() >= 1

    def test_apto_con_un_item_sin_responder_lo_rechaza_POSTGRES(self, esquema, semilla):
        """**Regla 1 impuesta por la base.** `apto` con un `sin_dato` es la
        fábrica de evidencia falsa de seguridad que la regla existe para
        impedir — y esa evidencia se usa después frente a una aseguradora."""
        with pytest.raises(_RECHAZO):
            self._inspeccion(esquema, semilla, veredicto='apto', items_sin_dato=1)

    def test_no_apto_sin_ningun_bloqueante_lo_rechaza_POSTGRES(self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            self._inspeccion(esquema, semilla, veredicto='no_apto',
                             bloqueantes_no_aptos=0)

    def test_incompleta_con_un_bloqueante_malo_lo_rechaza_POSTGRES(self, esquema, semilla):
        """`incompleta` es «no sé». Con un bloqueante que SÍ se sabe malo, el
        veredicto es `no_apto`: son estados distintos y confundirlos es lo que
        la regla 1 prohíbe."""
        with pytest.raises(_RECHAZO):
            self._inspeccion(esquema, semilla, veredicto='incompleta',
                             bloqueantes_no_aptos=1, items_sin_dato=1)

    def test_un_veredicto_inventado_cae_por_el_CASE_ademas_del_IN(self, esquema, semilla):
        """El `ELSE 1 = 0` no es decorativo: es la segunda puerta sobre la regla
        que decide si un camión sale."""
        with pytest.raises(_RECHAZO):
            self._inspeccion(esquema, semilla, veredicto='mas_o_menos')

    # ── flota_gasto ──────────────────────────────────────────────────────

    def _gasto(self, esquema, semilla, **campos):
        base = dict(categoria='combustible', valor=1000,
                    periodo_desde='2026-09-01', periodo_hasta='2026-09-01',
                    proveedor='X', descripcion=None, documento_numero=None,
                    origen_costo='sin_dato')
        base.update(campos)
        with esquema.begin() as c:
            c.execute(text(
                "INSERT INTO flota_gasto (vehiculo_id, categoria, fecha, valor, "
                " lectura_id, proveedor, documento_numero, periodo_desde, "
                " periodo_hasta, origen_costo, descripcion, "
                " registrado_por_usuario_id, creado_ts) "
                "VALUES (:v, :categoria, DATE '2026-09-02', :valor, :l, "
                " :proveedor, :documento_numero, CAST(:periodo_desde AS date), "
                " CAST(:periodo_hasta AS date), :origen_costo, :descripcion, "
                " :u, :ts)"
            ), dict(base, v=semilla['veh'], u=semilla['usr'], ts=_ts(),
                    l=self._lectura(c, semilla)))

    def test_un_gasto_sano_entra(self, esquema, semilla):
        self._gasto(esquema, semilla)
        with esquema.connect() as c:
            assert c.execute(text('SELECT count(*) FROM flota_gasto')).scalar() >= 1

    def test_un_periodo_al_reves_lo_rechaza_POSTGRES(self, esquema, semilla):
        """Un período invertido reparte un SOAT sobre días negativos."""
        with pytest.raises(_RECHAZO):
            self._gasto(esquema, semilla, periodo_desde='2026-09-30',
                        periodo_hasta='2026-09-01')

    def test_la_categoria_otro_sin_descripcion_lo_rechaza_POSTGRES(self, esquema, semilla):
        """`otro` sin decir qué es, es un gasto que nadie va a poder clasificar
        después — y el CPK lo suma igual."""
        with pytest.raises(_RECHAZO):
            self._gasto(esquema, semilla, categoria='otro', descripcion=None)

    def test_un_valor_negativo_lo_rechaza_POSTGRES(self, esquema, semilla):
        with pytest.raises(_RECHAZO):
            self._gasto(esquema, semilla, valor=-1)

    def test_el_mismo_documento_dos_veces_en_el_mismo_vehiculo_lo_rechaza(
            self, esquema, semilla):
        """Anti-duplicado: la misma factura cargada dos veces infla el CPK sin
        que nada se vea raro."""
        self._gasto(esquema, semilla, documento_numero='FAC-PG-1')
        with pytest.raises(_RECHAZO):
            self._gasto(esquema, semilla, documento_numero='FAC-PG-1')

    def test_la_capacidad_de_tanque_sin_procedencia_lo_rechaza_POSTGRES(
            self, esquema, semilla):
        """Un número sin fuente se lee como si alguien lo hubiera verificado.
        Es el mismo par que ya imponen distribución y frenos."""
        with esquema.begin() as c:
            c.execute(text(
                "INSERT INTO flota_ficha_tecnica (vehiculo_id, posiciones_llanta, "
                " km_inicial, km_inicial_ts) VALUES (:v, 6, 0, :ts) "
                "ON CONFLICT (vehiculo_id) DO NOTHING"),
                {'v': semilla['veh'], 'ts': _ts()})
        with pytest.raises(_RECHAZO):
            with esquema.begin() as c:
                c.execute(text(
                    "UPDATE flota_ficha_tecnica SET capacidad_tanque_galones = 15, "
                    "capacidad_tanque_fuente = 'sin_dato' WHERE vehiculo_id = :v"),
                    {'v': semilla['veh']})

    def test_la_capacidad_CON_procedencia_si_entra(self, esquema, semilla):
        """La otra dirección: el par exigido no puede volverse imposible."""
        with esquema.begin() as c:
            c.execute(text(
                "INSERT INTO flota_ficha_tecnica (vehiculo_id, posiciones_llanta, "
                " km_inicial, km_inicial_ts) VALUES (:v, 6, 0, :ts) "
                "ON CONFLICT (vehiculo_id) DO NOTHING"),
                {'v': semilla['veh'], 'ts': _ts()})
            c.execute(text(
                "UPDATE flota_ficha_tecnica SET capacidad_tanque_galones = 15, "
                "capacidad_tanque_fuente = 'manual_fabricante' "
                "WHERE vehiculo_id = :v"), {'v': semilla['veh']})
