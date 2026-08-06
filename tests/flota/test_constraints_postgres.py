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
                  'flota_plantilla_inspeccion', 'flota_item_inspeccion'} - tablas
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
        assert n == 46, f'Se esperaban 46 CHECK de flota en PostgreSQL, hay {n}'


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
            f"autor_usuario_id, motivo_correccion) VALUES ({s['veh']}, {km}, "
            f"'{_ts(minutos)}', '{origen}', {s['usr']}, {mot})"))


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
