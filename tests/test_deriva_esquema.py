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

## El barrido se amplió el 2026-09-01, y qué sigue sin cubrir

Hasta esa fecha recorría solo `app/models/` y **no veía doce tablas**: las ocho
de `flota/adaptadores/modelos.py` —el paquete vive fuera de `app/`— más
`kardex_movimientos`, `stock_diario`, `serie_vigia` y `alarma_vigia`,
declaradas dentro de sus servicios. Las dos del kardex alimentan el punto de
reorden y el armado del contenedor.

Hoy el barrido **descubre** por la presencia de `__tablename__` en vez de
seguir una ruta escrita a mano: 37 archivos, 57 tablas, 762 columnas.

**Lo que el detector sigue sin distinguir es la tabla.** Busca el nombre de la
columna como palabra suelta en el texto de todas las migraciones, así que una
columna nueva llamada `activo` se da por cubierta porque alguna migración
menciona `activo` en otra tabla. Medido al ampliarlo: **88 de 449 nombres de
columna únicos aparecen en más de una tabla**, o sea que ~20% del universo
tiene un homónimo en algún lado.

Se acepta a cambio de cero falsos positivos, y el riesgo real es más chico de
lo que ese 20% sugiere: si alguien olvida la migración **entera** de una tabla
nueva, las columnas de nombre único de esa tabla igual disparan. El hueco
verdadero es solo «escribí la migración pero me faltó UNA columna, y justo esa
tiene homónimo». Hacerlo consciente de la tabla exigiría entender
`op.add_column`, `sa.Column` dentro de `create_table` y el SQL crudo de
`op.execute` — tres formas, y la tercera no se puede parsear sin un
analizador de SQL.

## La deriva de índices se limpió, y eso es lo que hace usable el canal

Antes de `m014`/`m015` había trece diferencias, todas inofensivas. **Trece
líneas de ruido vuelven inservible el detector**: nadie corre una herramienta
que siempre grita. Es la lección de los 639 avisos conocidos aplicada a otro
canal. Hoy `db check` dice `No new upgrade operations detected`, así que la
primera línea que aparezca significa algo.
"""
import ast
import functools
import pathlib
import re

_RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: Columnas que un modelo declara y ninguna migración menciona, con su motivo.
#: **Estuvo vacío hasta el 2026-09-02.** Si algo entra acá tiene que traer por
#: qué, y quien lo escriba debería preguntarse antes si no es más barato la
#: migración.
#:
#: ── Las dos tablas de geo (2026-09-02) ──────────────────────────────────────
#:
#: `entregas_geo` y `clientes_geo` se construyeron con la migración **pendiente
#: a propósito**: había otro agente trabajando en paralelo sobre la misma
#: cadena, y dos migraciones simultáneas rompen el head único — con el head
#: roto el `releaseCommand` falla y el deploy entero se cae, que es peor que
#: esta lista. El DDL exacto (tablas, CHECK, índices, FKs) está escrito y
#: verificado; el encadenamiento lo hace el CTO.
#:
#: **La suite SÍ ejercita el esquema completo**: `tests/conftest.py` construye
#: la base con `db.create_all()` desde los modelos, así que los CHECK de estas
#: dos tablas se prueban de verdad (ver `tests/test_geo_cliente.py`, que inserta
#: 0,0 y exige que la base lo rechace). Lo que falta es el archivo de Alembic,
#: no la definición.
#:
#: **Esta lista tiene que volver a quedar vacía el día que esa migración
#: entre.** Mientras esté acá, una base construida desde cero no tiene estas
#: tablas y la pantalla del conductor revienta al leer `p.geo`.
_GEO_SIN_MIGRACION = (
    'la migración de entregas_geo/clientes_geo la encadena el CTO — DDL exacto '
    'listo, head único protegido de una escritura simultánea (2026-09-02). '
    'BORRAR esta entrada al encadenarla.')

#: ── Las cuatro columnas de `confianza` (2026-09-02, fase 0 del odómetro) ─────
#:
#: Mismo motivo y mismo día que las de geo, y por eso comparten forma: la
#: cadena de migraciones tenía **dos agentes escribiendo a la vez** y dos
#: archivos simultáneos rompen el head único — con el head roto el
#: `releaseCommand` falla y no despliega nada, que es peor que esta lista.
#:
#: El DDL exacto —columnas, los cuatro CHECK, el ALTER con su backfill y los
#: tres triggers— está escrito, con su orden y con la advertencia de que el
#: `UPDATE` del backfill choca con el append-only. La suite **sí** ejercita el
#: esquema completo: `tests/conftest.py` construye la base con `db.create_all()`
#: desde los modelos, así que los CHECK y los triggers se prueban de verdad (ver
#: `tests/flota/test_confianza_odometro.py`, que inserta una fila `verificada`
#: por SQL crudo y exige que la base la rechace). Lo que falta es el archivo de
#: Alembic, no la definición.
#:
#: **Esta lista tiene que volver a quedar vacía el día que esa migración entre.**
#: Mientras esté acá, una base construida desde cero por Alembic no tiene la
#: columna y `flota_lectura_odometro` revienta en el primer INSERT: `confianza`
#: es NOT NULL.
_CONFIANZA_SIN_MIGRACION = (
    'la migración de flota_lectura_odometro.confianza la encadena el CTO — DDL '
    'exacto listo (columnas + 4 CHECK + 3 triggers + backfill), head único '
    'protegido de una escritura simultánea (2026-09-02). '
    'BORRAR esta entrada al encadenarla.')

_SIN_MIGRACION_ACEPTADO: dict = {}


#: Directorios que no contienen modelos de producción.
_EXCLUIDOS = ('venv', 'tests', 'migrations', '__pycache__', 'scratchpad',
              '.git', 'node_modules')

#: Sitios donde vivían modelos fuera de `app/models/` cuando este detector se
#: amplió (2026-09-01). Son el canario de que el barrido no se volvió a
#: angostar: si alguien "simplifica" `_archivos_de_modelos` a `app/models/`,
#: estos tres desaparecen y `test_el_barrido_alcanza_los_tres_sitios_ciegos`
#: se pone rojo en vez de dar verde sobre doce tablas invisibles.
_ANTES_CIEGOS = (
    'flota/adaptadores/modelos.py',   # 8 tablas — el paquete vive FUERA de app/
    'app/services/kardex_service.py',  # kardex_movimientos, stock_diario
    'app/services/vigia_service.py',   # serie_vigia, alarma_vigia
)


@functools.lru_cache(maxsize=1)
def _archivos_de_modelos():
    """Todo `.py` de producción que declare al menos un `__tablename__`.

    **Se descubre, no se lista.** La versión anterior recorría solo
    `app/models/` y por eso no veía **doce tablas**: las ocho de
    `flota/adaptadores/modelos.py` —el paquete vive fuera de `app/`, y un
    barrido acotado a `app/` no lo ve— más `kardex_movimientos`, `stock_diario`,
    `serie_vigia` y `alarma_vigia`, declaradas dentro de sus servicios.

    Las dos primeras alimentan el punto de reorden y el armado del contenedor,
    que es una decisión irreversible de 120 días.

    Un detector que lleva escrita a mano la lista de sitios que revisa no ve el
    sitio nuevo. Es la misma forma que ya costó con los tres `_BODEGA_CO_MAP`
    (el guard leía una de las tres copias y afirmaba ser el único sitio) y con
    el guard de reconciliación que inspeccionaba una función de la que el
    cuerpo se había mudado. Acá el barrido parte del hecho —hay un
    `__tablename__`— y no de una ruta.
    """
    fuera = []
    for p in sorted(_RAIZ.rglob('*.py')):
        rel = p.relative_to(_RAIZ)
        if any(parte in _EXCLUIDOS for parte in rel.parts):
            continue
        try:
            texto = p.read_text()
        except (UnicodeDecodeError, OSError):       # pragma: no cover
            continue
        if '__tablename__' not in texto:
            continue
        fuera.append(p)
    return tuple(fuera)


@functools.lru_cache(maxsize=1)
def _columnas_de_modelos():
    """(tabla, columna, dónde) por cada `db.Column` de un modelo de producción."""
    fuera = []
    for p in _archivos_de_modelos():
        try:
            arbol = ast.parse(p.read_text())
        except SyntaxError:                          # pragma: no cover
            continue
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
                        # Ruta relativa, no `p.name`: ahora hay modelos en tres
                        # directorios y `modelos.py:262` no dice cuál.
                        donde = f'{p.relative_to(_RAIZ)}:{n.lineno}'
                        fuera.append((tabla, t.id, donde))
    return tuple(fuera)


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

    def test_el_barrido_alcanza_los_tres_sitios_ciegos(self):
        """**El canario del alcance.** Hasta el 2026-09-01 este detector
        recorría solo `app/models/` y daba verde sobre **doce tablas que no
        veía**. Si alguien vuelve a angostar `_archivos_de_modelos`, esto se
        pone rojo en vez de dejar el punto ciego a oscuras otra vez."""
        hallados = {str(p.relative_to(_RAIZ)) for p in _archivos_de_modelos()}
        faltan = [s for s in _ANTES_CIEGOS if s not in hallados]
        assert not faltan, (
            f'el barrido de modelos ya no alcanza: {faltan}. Eran invisibles '
            f'antes del 2026-09-01 y sus tablas (las 8 de flota, '
            f'kardex_movimientos, stock_diario, serie_vigia, alarma_vigia) '
            f'volverían a quedar sin detector de deriva.')

    def test_todo_modelo_declara_su_tabla_con_un_literal(self):
        """**El supuesto del barrido.** `_archivos_de_modelos` encuentra
        archivos por la cadena `__tablename__`, y `_columnas_de_modelos` solo
        acepta una asignación literal (`ast.Constant`).

        Un modelo que calcule su nombre de tabla —`__tablename__ = f'...'`, o
        heredado de una base declarativa— **sería invisible para el detector, y
        el detector no tendría cómo saberlo**: devolvería sus columnas como
        cero y todo pasaría.

        Al 2026-09-01 los 57 modelos usan literal. Esto no lo impone: lo
        vigila. Si alguien necesita un nombre calculado, este test le avisa que
        además tiene que enseñarle al detector a verlo."""
        sin_literal = []
        for p in _archivos_de_modelos():
            arbol = ast.parse(p.read_text())
            for clase in [n for n in ast.walk(arbol) if isinstance(n, ast.ClassDef)]:
                bases = [ast.unparse(b) for b in clase.bases]
                if not any('Model' in b for b in bases):
                    continue
                literal = any(
                    isinstance(n, ast.Assign)
                    and any(getattr(t, 'id', None) == '__tablename__'
                            for t in n.targets)
                    and isinstance(n.value, ast.Constant)
                    for n in clase.body
                )
                if not literal:
                    sin_literal.append(
                        f'{p.relative_to(_RAIZ)}:{clase.lineno} {clase.name}')
        assert not sin_literal, (
            f'modelo(s) sin `__tablename__` literal:\n  '
            + '\n  '.join(sin_literal) +
            '\n\nEl detector de deriva no los ve: sus columnas no se comparan '
            'contra ninguna migración. O se les pone un literal, o hay que '
            'enseñarle a `_columnas_de_modelos` a resolver el nombre.')

    def test_el_barrido_no_se_vacia_en_silencio(self):
        """**Piso mínimo.** Un descubrimiento roto —un `rglob` que no matchea,
        una exclusión de más, un `read_text` que revienta— devuelve la lista
        vacía, y entonces «0 columnas huérfanas» es verdad sobre nada.

        Es la falla que este repo persigue: el guard que da verde sin mirar.
        Los pisos van holgados a propósito: atrapan el barrido roto, no el
        cambio legítimo."""
        archivos = _archivos_de_modelos()
        columnas = _columnas_de_modelos()
        tablas = {t for t, _c, _d in columnas}

        assert len(archivos) >= 30, (
            f'solo {len(archivos)} archivos con `__tablename__`: el barrido '
            f'está roto (al 2026-09-01 eran 37)')
        assert len(tablas) >= 45, (
            f'solo {len(tablas)} tablas descubiertas (al 2026-09-01 eran 57)')
        assert len(columnas) >= 600, (
            f'solo {len(columnas)} columnas inspeccionadas: el parser AST no '
            f'está leyendo los modelos (al 2026-09-01 eran 762)')

    def test_el_detector_dispara_sobre_una_columna_ausente_de_verdad(self):
        """Detector ciego, ejercido sobre el mecanismo real y no sobre una
        constante: se inyecta una columna que ninguna migración menciona y se
        exige que la lógica de `huerfanas` la vea.

        El test hermano de arriba comprueba que el corpus no contiene el
        nombre; éste comprueba que **la comparación funciona**. Son cosas
        distintas: la primera puede pasar con la comparación rota."""
        mig = _texto_migraciones()
        inventada = [('tabla_ficticia', 'columna_que_no_existe_en_ninguna_parte',
                      'inyectada_por_el_test:1')]
        huerfanas = [f'{t}.{c}' for t, c, _d in inventada
                     if not re.search(rf"\b{re.escape(c)}\b", mig)]
        assert huerfanas == ['tabla_ficticia.columna_que_no_existe_en_ninguna_parte'], (
            'la comparación modelo↔migración no detecta una columna ausente: '
            'el detector daría verde sobre deriva real')

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


class TestLaListaBlancaVuelveAQuedarVacia:
    """TRINQUETE sobre el trinquete — la parte que faltaba.

    El 2026-09-02 `_SIN_MIGRACION_ACEPTADO` llegó a tener **24 entradas**: dos
    agentes construyendo en paralelo no podían escribir migraciones a la vez sin
    romper el head único, así que declararon la deuda acá. La decisión fue
    correcta —un head roto no despliega nada, que es peor— y las entradas
    llevaban escrito «BORRAR al encadenarla».

    **Un comentario que dice «borrar» no falla el build.** Si nadie encadena la
    migración, la lista se queda callada para siempre y el detector de deriva
    —que existe justamente para que una columna no viva solo en los modelos—
    queda ciego sobre esas columnas. Y son las peores: `confianza` es NOT NULL,
    así que una base construida desde cero por Alembic revienta en el primer
    INSERT.

    Este test convierte esa promesa en trinquete. Vaciar la lista fue el paso
    de hoy; que **siga** vacía es lo que esto protege.

    Volver a llenarla es una decisión legítima —puede repetirse el escenario de
    dos agentes en paralelo— pero ahora exige romper a propósito un test que
    dice por qué, en vez de agregar una línea que nadie ve.
    """

    def test_no_hay_ninguna_columna_eximida(self):
        assert _SIN_MIGRACION_ACEPTADO == {}, (
            f'\n{len(_SIN_MIGRACION_ACEPTADO)} columna(s) siguen eximidas del '
            f'detector de deriva:\n'
            + '\n'.join(f'  · {t}.{c}' for t, c in sorted(_SIN_MIGRACION_ACEPTADO))
            + '\n\nSi la migración ya se encadenó, borrá la entrada. Si todavía '
              'no, la pregunta no es «cómo silencio el guard» sino QUÉ PASA SI '
              'ALGUIEN CONSTRUYE LA BASE DESDE CERO HOY.'
        )
