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

    def test_los_37_check_quedaron_en_la_base(self, esquema):
        """Un CHECK que PostgreSQL no entiende no llega a existir."""
        with esquema.connect() as c:
            n = c.execute(text(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE constraint_type='CHECK' AND constraint_name LIKE 'ck_flota%'"
            )).scalar()
        assert n == 37, f'Se esperaban 37 CHECK de flota en PostgreSQL, hay {n}'


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
