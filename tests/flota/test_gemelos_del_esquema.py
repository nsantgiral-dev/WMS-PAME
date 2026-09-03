"""
`create_all()` contra `flask db upgrade`, los dos sobre PostgreSQL de verdad.

El esquema de flota se construye por **dos caminos distintos** y nada obliga a
que lleguen al mismo sitio:

    tests            →  db.create_all()      desde flota/adaptadores/modelos.py
    producción       →  flask db upgrade     desde migrations/versions/

`tests/test_deriva_esquema.py` cubre una parte —que ninguna columna declarada
falte en las migraciones— y su propio encabezado declara lo que **no** cubre:
*«un índice con otro nombre no rompe nada»*, y nada sobre triggers ni sobre el
cuerpo de las funciones que los implementan. Ayer se encontró que dos triggers
vivían en los modelos y no en las migraciones; el que los encontró fue una
lectura a mano, no un detector.

Esto es el detector. Levanta las dos bases, lee el catálogo de PostgreSQL —no el
texto de los archivos— y compara **columnas, CHECK, FK, únicos, índices,
triggers y el cuerpo de las funciones**. Un trigger que exista en un lado y no
en el otro, o que diga algo distinto, aparece solo.

## Por qué el conteo de invariantes de PostgreSQL no lo veía

`tests/flota/test_constraints_postgres.py` —los 45 que corren contra el motor
real— construye su base con `db.metadata.create_all(motor_pg)`. Ejerce el DDL de
**los modelos**, que es el gemelo que producción no usa. Es la forma de
«arreglar el gemelo muerto»: con dos implementaciones y los tests apuntando a
una, la otra puede divergir sin que nada se ponga rojo.

## Cómo se corre

    createdb wms_gemelo_base
    FLOTA_TEST_PG_URL=postgresql://localhost/wms_gemelo_base \\
        venv/bin/python -m pytest tests/flota/test_gemelos_del_esquema.py \\
        -q -m postgres -p no:randomly

Crea y destruye dos bases hermanas al lado de la que se le indica. **Solo
local**: con `PGHOST` remoto aborta, igual que `scripts/verificar_flota_postgres.sh`
y por el mismo motivo — esto ejecuta DDL.
"""
import os
import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.postgres

_HOSTS_LOCALES = ('localhost', '127.0.0.1', '', None)

# ── Las siete preguntas que se le hacen al catálogo ──────────────────────────
#
# Al catálogo de PostgreSQL y no al texto de los archivos: lo que importa es lo
# que quedó creado. Un `op.execute` con el DDL adentro no se puede comparar
# leyendo Python, y es justo la forma en que se escriben los triggers.

CONSULTAS = {
    'columnas': """
        SELECT table_name||'.'||column_name||' :: '||data_type
               ||' null='||is_nullable
               ||' default='||COALESCE(column_default,'-')
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name LIKE 'flota%'
    """,
    'checks': """
        SELECT rel.relname||' :: '||con.conname||' :: '
               ||pg_get_constraintdef(con.oid)
        FROM pg_constraint con JOIN pg_class rel ON rel.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=rel.relnamespace
        WHERE n.nspname='public' AND rel.relname LIKE 'flota%'
          AND con.contype='c'
    """,
    'claves_foraneas': """
        SELECT rel.relname||' :: '||pg_get_constraintdef(con.oid)
        FROM pg_constraint con JOIN pg_class rel ON rel.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=rel.relnamespace
        WHERE n.nspname='public' AND rel.relname LIKE 'flota%'
          AND con.contype='f'
    """,
    'unicos_y_primarias': """
        SELECT rel.relname||' :: '||pg_get_constraintdef(con.oid)
        FROM pg_constraint con JOIN pg_class rel ON rel.oid=con.conrelid
        JOIN pg_namespace n ON n.oid=rel.relnamespace
        WHERE n.nspname='public' AND rel.relname LIKE 'flota%'
          AND con.contype IN ('u','p')
    """,
    'indices': """
        SELECT tablename||' :: '||indexdef
        FROM pg_indexes
        WHERE schemaname='public' AND tablename LIKE 'flota%'
    """,
    'triggers': """
        SELECT c.relname||' :: '||t.tgname||' :: '||pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname LIKE 'flota%'
          AND NOT t.tgisinternal
    """,
    'cuerpo_de_las_funciones': """
        SELECT p.proname||' :: '||pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname LIKE 'flota%'
    """,
}


def _url_base():
    url = os.getenv('FLOTA_TEST_PG_URL')
    if not url:
        pytest.fail(
            'FLOTA_TEST_PG_URL no está definida.\n'
            'Estos tests NO se saltan: un skip silencioso deja el gemelo de '
            'producción sin comparar con nadie, que es exactamente el agujero '
            'que este archivo existe para cerrar.\n\n'
            '    createdb wms_gemelo_base\n'
            '    FLOTA_TEST_PG_URL=postgresql://localhost/wms_gemelo_base \\\n'
            '        venv/bin/python -m pytest '
            'tests/flota/test_gemelos_del_esquema.py -q -m postgres')
    u = sa.engine.make_url(url)
    if u.get_backend_name() != 'postgresql':
        pytest.fail(f'FLOTA_TEST_PG_URL apunta a {u.get_backend_name()!r}')
    if u.host not in _HOSTS_LOCALES:
        pytest.fail(
            f'ABORTA: FLOTA_TEST_PG_URL apunta a {u.host!r}, que no es local. '
            f'Esto CREA Y BORRA bases de datos. Mismo criterio que '
            f'scripts/verificar_flota_postgres.sh y que reset_transaccional.py: '
            f'lo destructivo no se hace por inercia.')
    return u


def _mantenimiento(url):
    """Motor contra `postgres`, en AUTOCOMMIT: `CREATE DATABASE` no va en una
    transacción."""
    return sa.create_engine(
        url.set(database='postgres'), isolation_level='AUTOCOMMIT')


@pytest.fixture(scope='module')
def gemelos():
    """Las dos bases hermanas: una por `create_all`, otra por `upgrade`.

    Se destruyen al terminar **pase lo que pase**. Un `finally` que no corre
    deja bases sueltas en la máquina de quien corrió los tests, y la próxima vez
    se topa con nombres que no reconoce.
    """
    import subprocess
    import sys

    url = _url_base()
    sufijo = uuid.uuid4().hex[:8]
    a = f'wms_gemelo_modelos_{sufijo}'
    b = f'wms_gemelo_migraciones_{sufijo}'
    mant = _mantenimiento(url)

    with mant.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{a}"'))
        c.execute(sa.text(f'CREATE DATABASE "{b}"'))
    try:
        # ── A: el esquema que ve la suite ────────────────────────────────
        from app.extensions import db as _db
        motor_a = sa.create_engine(url.set(database=a))
        _db.metadata.create_all(motor_a)
        motor_a.dispose()

        # ── B: el esquema que ve producción ──────────────────────────────
        cfg = Config('migrations/alembic.ini')
        cfg.set_main_option('script_location', 'migrations')
        cfg.set_main_option('sqlalchemy.url',
                            str(url.set(database=b)).replace('%', '%%'))
        command.upgrade(cfg, 'head')

        yield (url.set(database=a), url.set(database=b))
    finally:
        with mant.connect() as c:
            for nombre in (a, b):
                c.execute(sa.text(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = :d'), {'d': nombre})
                c.execute(sa.text(f'DROP DATABASE IF EXISTS "{nombre}"'))
        mant.dispose()


def _leer(url, sql):
    motor = sa.create_engine(url)
    try:
        with motor.connect() as c:
            return {fila[0] for fila in c.execute(sa.text(sql))}
    finally:
        motor.dispose()


@pytest.fixture(scope='module')
def catalogo(gemelos):
    """`{pregunta: (lo_de_los_modelos, lo_de_las_migraciones)}`, leído una vez."""
    url_a, url_b = gemelos
    return {n: (_leer(url_a, sql), _leer(url_b, sql))
            for n, sql in CONSULTAS.items()}


def _informe(nombre, modelos, migraciones):
    solo_a = sorted(modelos - migraciones)
    solo_b = sorted(migraciones - modelos)
    return (
        f'\n{nombre}: el esquema de los modelos y el de las migraciones no '
        f'coinciden ({len(modelos)} vs {len(migraciones)}).\n'
        + ''.join(f'\n  SOLO create_all (los tests) : {x}' for x in solo_a)
        + ''.join(f'\n  SOLO upgrade (producción)   : {x}' for x in solo_b))


class TestElCatalogoSeLeyoDeVerdad:
    """Un detector que mide cero se lee igual que uno que no encontró nada."""

    def test_las_dos_bases_tienen_esquema(self, catalogo):
        modelos, migraciones = catalogo['columnas']
        assert len(modelos) >= 200, f'create_all creó {len(modelos)} columnas'
        assert len(migraciones) >= 200, f'upgrade creó {len(migraciones)}'

    def test_hay_triggers_que_comparar(self, catalogo):
        """Si el `after_create` dejara de colgar el DDL, o si la migración
        dejara de crearlos, esto lo dice antes que la comparación — que en ese
        caso saldría «igual: cero contra cero»."""
        modelos, migraciones = catalogo['triggers']
        assert len(modelos) >= 7, f'los modelos crearon {len(modelos)} triggers'
        assert len(migraciones) >= 7, f'las migraciones {len(migraciones)}'


class TestLosDosCaminosLleganAlMismoEsquema:

    @pytest.mark.parametrize('pregunta', [
        'columnas', 'checks', 'claves_foraneas', 'unicos_y_primarias',
        'triggers'])
    def test_coinciden(self, catalogo, pregunta):
        modelos, migraciones = catalogo[pregunta]
        assert modelos == migraciones, _informe(pregunta, modelos, migraciones)

    @pytest.mark.xfail(
        strict=True,
        reason='El índice único de `flota_aviso.clave` se llama '
               '`flota_aviso_clave_key` cuando lo crea `create_all()` (nombre '
               'automático de PostgreSQL para `unique=True`) y '
               '`uq_flota_aviso_clave` cuando lo crea la migración. La '
               'restricción es la misma; el nombre no. Una migración futura que '
               'haga DROP por nombre funciona contra una base y falla contra la '
               'otra, y el mensaje de UNIQUE violation que ve el usuario '
               'tampoco es el mismo. Renombrar exige tocar migrations/.')
    def test_los_indices_se_llaman_igual(self, catalogo):
        modelos, migraciones = catalogo['indices']
        assert modelos == migraciones, _informe(
            'indices', modelos, migraciones)

    @pytest.mark.xfail(
        strict=True,
        reason='Dos funciones de trigger tienen CUERPO DISTINTO entre los '
               'modelos y las migraciones: `flota_odometro_append_only` y '
               '`flota_montaje_posicion_de_la_ficha` levantan mensajes de '
               'excepción diferentes. El trigger existe en los dos lados y '
               'bloquea lo mismo, así que no es un invariante ausente — es que '
               'el usuario y los logs de producción leen un texto que ningún '
               'test ejerce, porque los 45 tests de PostgreSQL construyen su '
               'base con `create_all()`. Corregirlo es elegir cuál de los dos '
               'textos es el canónico y tocar migrations/ o el DDL de los '
               'modelos; el DDL propuesto va en el scratchpad.')
    def test_las_funciones_de_trigger_dicen_lo_mismo(self, catalogo):
        modelos, migraciones = catalogo['cuerpo_de_las_funciones']
        # Se comparan normalizando espacios: una migración escrita en varias
        # líneas y un DDL de una sola no son una diferencia — lo que importa es
        # lo que la función HACE y lo que DICE, no cómo está sangrada.
        def _normalizar(conjunto):
            return {' '.join(x.split()) for x in conjunto}

        assert _normalizar(modelos) == _normalizar(migraciones), _informe(
            'cuerpo_de_las_funciones',
            _normalizar(modelos), _normalizar(migraciones))


class TestElPuntoCiegoQueLoDejoPasar:
    """El motivo por el que esto no se había visto, afirmado como hecho.

    No es una nota: mientras la suite de PostgreSQL construya su base con
    `create_all()`, **ninguno de sus 45 tests puede fallar por una divergencia
    de las migraciones**. Es la forma de «arreglar el gemelo muerto».
    """

    def test_la_suite_de_postgres_construye_con_create_all(self):
        from pathlib import Path

        fuente = (Path(__file__).parent / 'test_constraints_postgres.py'
                  ).read_text(encoding='utf-8')
        assert 'create_all' in fuente, (
            'si esa suite pasó a construirse con `upgrade`, este archivo ya no '
            'está mirando dos gemelos distintos y hay que replantearlo')
        assert 'command.upgrade' not in fuente, (
            'la suite de PostgreSQL ahora corre migraciones: revisar si esta '
            'comparación sigue haciendo falta o si se puede plegar allá')
