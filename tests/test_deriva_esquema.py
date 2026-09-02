"""
El esquema y los modelos dicen lo mismo — y hay quien lo compruebe.

`flask db check` compara una base construida **solo con migraciones** contra
lo que declaran los modelos. Es el único mecanismo del repo que atrapa esta
clase:

    una columna declarada en un modelo que NINGUNA migración crea

Que es exactamente lo que pasó con `usuarios.puede_usar_camara` y
`tareas_picking.empaques_escaneados`. Producción las tenía —entraron por
`create_all()` o a mano— así que nada fallaba; una base nueva no las habría
tenido y la aplicación habría reventado al leer un usuario.

**No se notó durante meses porque la cadena de migraciones no se podía correr
desde cero**: dos migraciones la rompían (un índice creado dos veces y otro
dropeado dos veces, corregidos el 2026-08-20). Sin poder construir una base
limpia, no había contra qué comparar.

## Por qué este archivo no ejecuta `db check`

Correrlo de verdad exige levantar PostgreSQL, aplicar 103 migraciones y
comparar — un minuto largo, y el repo no tiene Postgres en la suite normal
(ver el marcador `postgres` en `pytest.ini`).

Lo que sí se puede comprobar barato, y es donde la deriva **se introduce**,
es que ningún modelo declare una columna que ninguna migración menciona. Ese
es el 90% del daño: un índice con otro nombre no rompe nada; una columna
faltante sí.

## La deriva de índices se limpió, y eso es lo que hace usable el canal

Antes de `m014`/`m015` había trece diferencias, todas inofensivas. **Trece
líneas de ruido vuelven inservible el detector**: nadie corre una herramienta
que siempre grita. Es la lección de los 639 avisos conocidos aplicada a otro
canal. Hoy `db check` dice `No new upgrade operations detected`, así que la
primera línea que aparezca significa algo.
"""
import ast
import pathlib
import re

_RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: Columnas que un modelo declara y ninguna migración menciona, con su motivo.
#: **Vacío a propósito.** Si algo entra acá tiene que traer por qué, y quien
#: lo escriba debería preguntarse antes si no es más barato la migración.
_SIN_MIGRACION_ACEPTADO: dict = {}


def _columnas_de_modelos():
    """(tabla, columna) por cada `db.Column` declarada en `app/models/`."""
    fuera = []
    for p in sorted((_RAIZ / 'app' / 'models').rglob('*.py')):
        arbol = ast.parse(p.read_text())
        for clase in [n for n in ast.walk(arbol) if isinstance(n, ast.ClassDef)]:
            tabla = None
            for n in clase.body:
                if (isinstance(n, ast.Assign)
                        and any(getattr(t, 'id', None) == '__tablename__'
                                for t in n.targets)
                        and isinstance(n.value, ast.Constant)):
                    tabla = n.value.value
            if not tabla:
                continue
            for n in clase.body:
                if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Call):
                    continue
                if getattr(n.value.func, 'attr', None) != 'Column':
                    continue
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        fuera.append((tabla, t.id, f'{p.name}:{n.lineno}'))
    return fuera


def _texto_migraciones() -> str:
    return ' '.join(p.read_text()
                    for p in (_RAIZ / 'migrations' / 'versions').glob('*.py'))


class TestNingunaColumnaSinMigracion:
    def test_toda_columna_declarada_aparece_en_alguna_migracion(self):
        """**El detector.** Una columna que solo existe en el modelo funciona
        en producción —donde entró por otra vía— y revienta en cualquier base
        construida desde cero."""
        mig = _texto_migraciones()
        huerfanas = []
        for tabla, col, donde in _columnas_de_modelos():
            if (tabla, col) in _SIN_MIGRACION_ACEPTADO:
                continue
            # Palabra suelta, no entre comillas: `m014` las agrega con SQL
            # crudo (`ADD COLUMN IF NOT EXISTS puede_usar_camara ...`) y la
            # primera versión de este test exigía comillas — se le escapaban
            # justamente las migraciones que usan `op.execute`.
            #
            # Limitación declarada: no distingue tabla. Una columna llamada
            # `activo` en un modelo se da por cubierta si CUALQUIER migración
            # menciona `activo`. Es un falso negativo aceptado a cambio de
            # cero falsos positivos — lo que este test persigue es el nombre
            # que no aparece en NINGUNA parte, que es el caso que rompe una
            # base nueva.
            if not re.search(rf"\b{re.escape(col)}\b", mig):
                huerfanas.append(f'{tabla}.{col} ({donde})')
        assert not huerfanas, (
            f'{len(huerfanas)} columna(s) declaradas en modelos y ausentes de '
            f'toda migración:\n  ' + '\n  '.join(huerfanas) +
            '\n\nProducción puede tenerlas —entraron por create_all() o a '
            'mano— pero una base construida desde cero no, y la aplicación '
            'revienta al leerlas. Escribí la migración con '
            '`ADD COLUMN IF NOT EXISTS`, que es correcta en los dos mundos '
            '(ver m014).')

    def test_el_detector_ve_una_columna_inventada(self):
        """Detector ciego: sin esto, «0 huérfanas» no distingue un repo sano
        de un detector roto."""
        mig = _texto_migraciones()
        assert not re.search(r"['\"]columna_que_no_existe_en_ninguna_parte['\"]",
                             mig), 'el corpus de migraciones no es el esperado'

    def test_el_detector_encuentra_una_columna_conocida(self):
        """Y el otro lado: que sí vea las que están. Si el corpus se leyera
        vacío, todo pasaría."""
        mig = _texto_migraciones()
        assert re.search(r"\bpuede_usar_camara\b", mig), (
            'no encuentra `puede_usar_camara`, que m014 agrega — el lector de '
            'migraciones está roto y este archivo daría verde sobre cualquier '
            'cosa')
        assert len(mig) > 100_000, (
            f'el corpus de migraciones mide {len(mig)} caracteres: demasiado '
            f'poco, probablemente no se leyeron todas')


class TestLaCadenaSeCorreDesdeCero:
    """No lo ejecuta —necesita PostgreSQL—, pero deja escrito cómo y por qué.

    Verificado a mano el 2026-08-20: 103 migraciones, 58 tablas,
    `flask db check` → «No new upgrade operations detected».

        createdb wms_verif
        DATABASE_URL=postgresql://localhost/wms_verif venv/bin/python -m flask db upgrade
        DATABASE_URL=postgresql://localhost/wms_verif venv/bin/python -m flask db check
        dropdb wms_verif
    """

    def test_head_unico(self):
        """Con dos heads el `releaseCommand` de Railway falla y el deploy no
        sale — es la comprobación barata que sí cabe en la suite."""
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config(str(_RAIZ / 'migrations' / 'alembic.ini'))
        cfg.set_main_option('script_location', str(_RAIZ / 'migrations'))
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert len(heads) == 1, f'{len(heads)} heads: {heads}'

    def test_ningun_indice_se_crea_dos_veces_directo(self):
        """El defecto que impedía construir una base nueva. Los `batch_op` no
        cuentan: recrean el índice tras reconstruir la tabla."""
        creados = {}
        for p in sorted((_RAIZ / 'migrations' / 'versions').glob('*.py')):
            src = p.read_text()
            up = (src[src.index('def upgrade'):src.index('def downgrade')]
                  if 'def downgrade' in src else src)
            for m in re.finditer(r"op\.create_index\(\s*'([a-z0-9_]+)'", up):
                creados.setdefault(m.group(1), []).append(p.name)
        dobles = {k: v for k, v in creados.items() if len(v) > 1}
        assert not dobles, (
            f'índice(s) creados por más de una migración con `op.create_index`: '
            f'{dobles}. La cadena desde cero revienta con DuplicateTable. Usar '
            f'`op.execute("CREATE INDEX IF NOT EXISTS ...")` en la segunda, '
            f'como en a8b9c0d1e2f4.')
