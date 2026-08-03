"""
Trinquetes del módulo `flota/`. Alcance limitado a `flota/`; los trinquetes
globales del repo no se tocan.

TODOS ARRANCAN EN CERO. Es la diferencia que hace que valgan algo: los
trinquetes de `app/` nacieron con deuda —83 endpoints huérfanos, 224 llamadas
legacy, 637 advertencias— y un trinquete que arranca en 83 es arqueología. Uno
que arranca en 0 es una garantía, y solo se puede tener antes de la primera
línea. Esta es esa línea.

Cada uno trae su regla del `flota/CLAUDE.md`. Si alguno se pone rojo, la
respuesta es arreglar el código, no subir el tope.
"""
import ast
import os
import re
import warnings

import pytest

_RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FLOTA = os.path.join(_RAIZ, 'flota')
_DOMINIO = os.path.join(_FLOTA, 'dominio')


def _archivos_py(directorio):
    encontrados = []
    for raiz, _dirs, archivos in os.walk(directorio):
        if '__pycache__' in raiz:
            continue
        for a in archivos:
            if a.endswith('.py'):
                encontrados.append(os.path.join(raiz, a))
    return sorted(encontrados)


def _leer(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


def _rel(ruta):
    return os.path.relpath(ruta, _RAIZ)


# ══════════════════════════════════════════════════════════════════════════
# TRINQUETE 1 — frontera del dominio (tope: 0)
#
# El dominio no importa Flask, ni SQLAlchemy, ni `app.*`. No es preferencia
# estética: es lo único que hace que las políticas se puedan probar sin base y
# que un `db.session` no se cuele adentro de una regla de negocio.
#
# En `app/` esta frontera no existe y por eso hubo que documentar a mano una
# tabla de "dispatchers fuera de su módulo" en CLAUDE.md — cada tabla así es
# memoria humana pagando lo que una herramienta hace gratis.
# ══════════════════════════════════════════════════════════════════════════

_PROHIBIDOS_EN_DOMINIO = ('flask', 'sqlalchemy', 'app.', 'app')


class TestTrinqueteFronteraDominio:

    def _imports_de(self, ruta):
        arbol = ast.parse(_leer(ruta), filename=ruta)
        modulos = []
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                modulos += [a.name for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                modulos.append(nodo.module)
        return modulos

    def test_el_dominio_no_importa_framework_ni_base(self):
        violaciones = []
        for ruta in _archivos_py(_DOMINIO):
            for modulo in self._imports_de(ruta):
                raiz = modulo.split('.')[0]
                if raiz in _PROHIBIDOS_EN_DOMINIO or modulo.startswith('app.'):
                    violaciones.append(f'{_rel(ruta)} → import {modulo}')
        assert not violaciones, (
            '\nEl dominio de flota dejó de ser puro:\n'
            + '\n'.join(f'  · {v}' for v in violaciones)
            + '\n\nEso va en flota/adaptadores/. El dominio solo consume Protocols '
              'de flota/puertos.py.'
        )

    def test_el_dominio_no_importa_adaptadores_ni_api(self):
        """La dirección es una sola: api → adaptadores → puertos → dominio."""
        violaciones = []
        for ruta in _archivos_py(_DOMINIO):
            for modulo in self._imports_de(ruta):
                if modulo.startswith(('flota.adaptadores', 'flota.api')):
                    violaciones.append(f'{_rel(ruta)} → import {modulo}')
        assert not violaciones, (
            '\nEl dominio miró hacia arriba:\n' + '\n'.join(f'  · {v}' for v in violaciones)
        )


# ══════════════════════════════════════════════════════════════════════════
# TRINQUETE 2 — ninguna degradación silenciosa (tope: 0)
#
# Regla 5 del módulo: ningún adaptador degrada hacia algo que se parezca al
# éxito. Prohibido heredar el `except Exception: pass` de
# `ruta_service.py:633`, que hoy guarda la foto sin comprimir y no se lo cuenta
# a nadie.
#
# Los defaults peligrosos de este dominio son "el último conductor conocido",
# "el último odómetro conocido" y "el custodio del acta original" — este último
# es exactamente lo que está pasando en papel desde septiembre de 2025.
# ══════════════════════════════════════════════════════════════════════════

class TestTrinqueteSinDegradacionSilenciosa:

    def test_ningun_except_que_traga(self):
        violaciones = []
        for ruta in _archivos_py(_FLOTA):
            arbol = ast.parse(_leer(ruta), filename=ruta)
            for nodo in ast.walk(arbol):
                if not isinstance(nodo, ast.ExceptHandler):
                    continue
                cuerpo = [n for n in nodo.body if not isinstance(n, ast.Expr)]
                if not cuerpo or all(isinstance(n, ast.Pass) for n in cuerpo):
                    violaciones.append(f'{_rel(ruta)}:{nodo.lineno} — except que traga')
        assert not violaciones, (
            '\n' + '\n'.join(f'  · {v}' for v in violaciones)
            + '\n\nO se maneja de verdad, o se propaga. Un fallo silencioso acá '
              'produce evidencia falsa de que todo está bien.'
        )

    def test_ningun_get_con_default_ni_getattr_con_default(self):
        """`.get(x, default)` en una frontera es un bug: o funciona, o falla ruidosamente.

        Sin exenciones. La lectura de variables de entorno se escribe con
        `os.getenv(X)` de un solo argumento y una comparación explícita — así
        "no configurado" queda como un estado que se decide, no como un default
        que se hereda sin mirar.

        Se comprueba por AST y no por texto: lo que hace ilegal a la llamada es
        el ARGUMENTO DE MÁS, no la palabra. `getattr(obj, nombre)` de dos
        argumentos es legítimo —levanta si no está— y una regex que cuente comas
        lo marca igual. Un guard con falsos positivos se termina desactivando, y
        un guard desactivado es peor que no tenerlo.
        """
        violaciones = []
        for ruta in _archivos_py(_FLOTA):
            arbol = ast.parse(_leer(ruta), filename=ruta)
            for nodo in ast.walk(arbol):
                if not isinstance(nodo, ast.Call):
                    continue
                fn = nodo.func
                # `db.session.get(Modelo, pk)` NO es `dict.get(k, default)`:
                # es el identity-map de SQLAlchemy 2.0, y es exactamente lo que
                # el trinquete de deuda legacy del repo PREMIA frente a
                # `Modelo.query.get(id)`. Un guard que castiga la migración que
                # otro guard exige es un guard que alguien va a desactivar.
                # Se distingue por el receptor, no por el nombre del método.
                receptor = getattr(fn, 'value', None)
                es_sesion = (
                    (isinstance(receptor, ast.Attribute) and receptor.attr == 'session')
                    or (isinstance(receptor, ast.Name) and receptor.id == 'session')
                )
                con_default = (not es_sesion) and (
                    (isinstance(fn, ast.Attribute)
                     and fn.attr in ('get', 'getenv')
                     and len(nodo.args) >= 2)
                    or (isinstance(fn, ast.Name)
                        and fn.id in ('getattr', 'getenv')
                        and len(nodo.args) >= 3)
                )
                if con_default:
                    nombre = fn.attr if isinstance(fn, ast.Attribute) else fn.id
                    violaciones.append(f'{_rel(ruta)}:{nodo.lineno} — {nombre}() con default')
        assert not violaciones, (
            '\n' + '\n'.join(f'  · {v}' for v in violaciones)
            + '\n\nSi el dato puede faltar, decláralo con palabras (SIN_DATO) o levanta.'
        )


# ══════════════════════════════════════════════════════════════════════════
# TRINQUETE 3 — ninguna foto en la base (tope: 0)
#
# Regla 7: la base guarda referencia, hash, bytes y dimensiones. Nunca el
# binario. Las fotos viejas de `recaudo_entrega` (base64 en columna Text) se
# quedan donde están; este módulo no las hereda ni las migra.
# ══════════════════════════════════════════════════════════════════════════

class TestTrinqueteFotosFueraDeLaBase:

    def test_ninguna_columna_de_texto_guarda_binario(self):
        sospechosos = re.compile(
            r'(b64encode|b64decode|base64\.|toDataURL|data:image/)', re.IGNORECASE
        )
        violaciones = []
        for ruta in _archivos_py(_FLOTA):
            for i, linea in enumerate(_leer(ruta).splitlines(), 1):
                sin_comentario = linea.split('#')[0]
                if sospechosos.search(sin_comentario):
                    violaciones.append(f'{_rel(ruta)}:{i} — {linea.strip()}')
        assert not violaciones, (
            '\n' + '\n'.join(f'  · {v}' for v in violaciones)
            + '\n\nEl binario va a object storage; la base guarda storage_ref, '
              'hash_sha256, bytes y dimensiones.'
        )


# ══════════════════════════════════════════════════════════════════════════
# TRINQUETE 4 — cobertura (tope: 0 módulos sin test)
#
# Mismo criterio que el guard global: no mide % de líneas, mide si EXISTE al
# menos un test que referencie el módulo. Un módulo con 1 test es peor que
# tres; 0 es inaceptable.
# ══════════════════════════════════════════════════════════════════════════

_TESTS_FLOTA = os.path.dirname(os.path.abspath(__file__))


def _todo_el_texto_de_tests():
    texto = ''
    for a in sorted(os.listdir(_TESTS_FLOTA)):
        if a.startswith('test_') and a.endswith('.py'):
            texto += _leer(os.path.join(_TESTS_FLOTA, a))
    return texto


class TestTrinqueteCobertura:

    def test_todo_modulo_de_flota_esta_referenciado_por_un_test(self):
        texto = _todo_el_texto_de_tests()
        sin_test = []
        for ruta in _archivos_py(_FLOTA):
            nombre = os.path.basename(ruta)[:-3]
            if nombre == '__init__':
                continue
            if nombre not in texto:
                sin_test.append(_rel(ruta))
        assert not sin_test, (
            '\nMódulos de flota sin ningún test que los nombre:\n'
            + '\n'.join(f'  · {m}' for m in sin_test)
        )


# ══════════════════════════════════════════════════════════════════════════
# TRINQUETE 5 — endpoints sin consumidor (tope: 0 no declarados)
#
# Es el guard que en `app/` nació con 83 huérfanos heredados. Acá arranca
# limpio. Su razón de ser: `descargar_kardex` bloqueaba 4 modelos y no tenía
# botón; `alimentar_adopcion_picking` era la métrica del go-live y no tenía ni
# cron ni botón. Ninguna de las dos se detecta leyendo código: cada pieza está
# bien, lo que falta es el gesto que la enciende.
#
# El guard global de `app/` solo mira rutas que empiezan por `/api/`, así que
# `/flota/...` le es invisible. Por eso el módulo necesita el suyo.
# ══════════════════════════════════════════════════════════════════════════

# Exentos POR NATURALEZA, no por deuda. Mismo criterio que `_EXENTOS_POR_REGLA`
# en el guard global, donde `/api/health/` ya está exento: un health lo leen
# monitores y operación desde consola, no una pantalla.
_EXENTOS_POR_REGLA = ('/flota/health',)

_PWA = os.path.join(_RAIZ, 'app', 'static', 'pwa')


class TestTrinqueteEndpointsSinConsumidor:

    def _rutas_de_flota(self, app):
        return [str(r) for r in app.url_map.iter_rules() if str(r).startswith('/flota')]

    def test_health_esta_montado(self, app):
        assert '/flota/health' in self._rutas_de_flota(app)

    def test_ningun_endpoint_de_flota_sin_consumidor(self, app):
        blob = ''
        for a in os.listdir(_PWA):
            if a.endswith(('.js', '.html')):
                blob += _leer(os.path.join(_PWA, a))

        huerfanos = []
        for ruta in self._rutas_de_flota(app):
            if ruta in _EXENTOS_POR_REGLA:
                continue
            literales = [s for s in re.split(r'<[^>]+>', ruta) if s.strip('/')]
            if not all(s.rstrip('/') in blob for s in literales):
                huerfanos.append(ruta)

        assert not huerfanos, (
            f'\n{len(huerfanos)} endpoint(s) de flota sin consumidor:\n'
            + '\n'.join(f'  · {r}' for r in huerfanos)
            + '\n\nConéctalo desde el JS. Si de verdad no debe tener pantalla, '
              'la pregunta no es "qué pantalla le falta" sino QUÉ DECISIÓN '
              'DEBERÍA ESTAR INFORMANDO — y eso se escribe acá antes de eximirlo.'
        )

    def test_ninguna_funcion_de_flota_js_es_inalcanzable(self):
        """TRINQUETE 5b — el agujero que el 5 no veía, y por el que me caí.

        El trinquete 5 verifica que cada endpoint aparezca MENCIONADO en el JS.
        Eso no alcanza: `flotaGuardarFicha` mencionaba `/flota/vehiculo/.../ficha`
        y no tenía un solo caller. El endpoint parecía consumido, la función
        existía, estaba probada y desplegada — y no había botón que la
        encendiera. Quinta aparición del patrón en este repo, cometida dentro
        del módulo construido para evitarlo.

        Una función es alcanzable si un `onclick` del HTML la nombra, o si otra
        función de flota la llama. Lo que no vale es que exista y nadie la toque.
        """
        js = _leer(os.path.join(_PWA, 'flota.js'))
        definidas = set(re.findall(r'(?:async\s+)?function\s+(flota\w+|cargarFlota)', js))

        # El caller puede vivir en CUALQUIER archivo del PWA: `cargarFlota` la
        # llama el dispatcher de app.js. Buscar solo en flota.js daría un falso
        # positivo, y un guard con falsos positivos termina desactivado.
        blob = ''
        for a in sorted(os.listdir(_PWA)):
            if a.endswith(('.js', '.html')):
                blob += _leer(os.path.join(_PWA, a))
        # Referencias EXCLUYENDO las líneas de definición.
        cuerpo = re.sub(r'(?:async\s+)?function\s+\w+', '', blob)
        alcanzables = {f for f in definidas if re.search(rf'\b{f}\s*\(', cuerpo)}

        huerfanas = sorted(definidas - alcanzables)
        assert not huerfanas, (
            f'\nFunciones de flota.js que nadie llama: {huerfanas}\n'
            'El módulo no está incompleto: está desconectado del gesto que lo '
            'enciende, y eso no se ve leyendo el código porque cada pieza está bien.'
        )

    def test_la_lista_de_exentos_no_crece(self):
        """Anti-podredumbre: la exención es una categoría, no un basurero."""
        assert len(_EXENTOS_POR_REGLA) <= 1, (
            'Se agregó un exento nuevo. Un endpoint sin consumidor casi nunca '
            'necesita un tab: suele necesitar ser procedencia dentro de la '
            'pantalla que ya usa ese dato.'
        )


# ══════════════════════════════════════════════════════════════════════════
# TRINQUETE 6 — canal de advertencias (tope: 0)
#
# `app/` llegó a 639 advertencias por corrida, 637 de una sola clase conocida.
# Entre ese ruido había una real —clave HMAC de 15 bytes contra los 32 de
# RFC 7518— que nadie veía. Lo que se normaliza, se esconde.
#
# Acá el tope es 0 desde el principio: la próxima advertencia que aparezca en
# `flota/` será real.
# ══════════════════════════════════════════════════════════════════════════

class TestTrinqueteTamanoDelClaudeMd:
    """TRINQUETE 7 — el CLAUDE.md del módulo no se sedimenta.

    ══════════════════════════════════════════════════════════════════════
    POR QUÉ ESTE TRINQUETE CAMBIÓ DE MECANISMO EL 2026-08-01

    Nació como un tope de 100 líneas totales, puesto cuando el archivo tenía
    98. Dos líneas de holgura no son un trinquete: son un cable trampa sobre
    la próxima regla legítima. La regla 13 lo hizo saltar —106 líneas— sin que
    hubiera sedimento alguno: 13 reglas, promedio 8,2 líneas, máximo 12,
    ninguna sección que no fuera una regla.

    O sea que el tope midió lo que NO decía. El archivo dice "algo se está
    sedimentando"; el conteo total no distingue sedimento de crecimiento
    legítimo, y un guard con falsos positivos termina desactivado —que es
    justo la muerte que este proyecto ya vio.

    Ahora mide sedimento directamente, en sus dos formas reales:
      · una sección que no es una regla (un documento colándose)
      · una regla que se hincha hasta ser un ensayo

    Crecer en cantidad de reglas ya no dispara nada. Eso es lo que debía ser.
    ══════════════════════════════════════════════════════════════════════
    """

    MAX_LINEAS_POR_REGLA = 15
    _CLAUDE_MD = os.path.join(_FLOTA, 'CLAUDE.md')

    def _reglas(self):
        """[(numero, titulo, largo_en_lineas)] de cada sección del archivo."""
        lineas = _leer(self._CLAUDE_MD).splitlines()
        cortes = [i for i, l in enumerate(lineas) if l.startswith('## ')] + [len(lineas)]
        return [
            (lineas[a][3:].split('.')[0], lineas[a][3:].strip(), b - a)
            for a, b in zip(cortes, cortes[1:])
        ]

    def test_toda_seccion_es_una_regla_numerada(self):
        """Una sección sin número es un documento colándose entre las reglas."""
        intrusas = [titulo for num, titulo, _ in self._reglas() if not num.isdigit()]
        assert not intrusas, (
            f'\nSecciones que no son reglas numeradas: {intrusas}\n'
            'El estado va a docs/flota/ESTADO.md y la referencia a ESPECIFICACION_T1.md.'
        )

    def test_las_reglas_van_numeradas_de_1_en_adelante_sin_huecos(self):
        nums = [int(n) for n, _, _ in self._reglas() if n.isdigit()]
        assert nums == list(range(1, len(nums) + 1)), (
            f'Numeración con huecos o repetida: {nums}. Una regla se cita por su '
            'número —"regla 5"— y renumerar rompe cada cita que ya se escribió.'
        )

    def test_ninguna_regla_se_hincha_hasta_ser_un_ensayo(self):
        gordas = [(t, n) for _, t, n in self._reglas() if n > self.MAX_LINEAS_POR_REGLA]
        assert not gordas, (
            f'\nReglas por encima de {self.MAX_LINEAS_POR_REGLA} líneas:\n'
            + '\n'.join(f'  · {t} ({n} líneas)' for t, n in gordas)
            + '\n\nUna regla que necesita más es un documento: va a docs/flota/.'
        )

    def test_el_tope_del_trinquete_es_el_que_declara_el_archivo(self):
        """La regla y su mecanismo no pueden decir números distintos.

        Si alguien relaja el texto del CLAUDE.md sin tocar el trinquete —o al
        revés— queda una regla que dice una cosa y un guard que hace otra. Es
        `nombre-que-miente` aplicado a un tope.
        """
        declarados = re.findall(r'pasa de (\d+) líneas', _leer(self._CLAUDE_MD))
        assert declarados == [str(self.MAX_LINEAS_POR_REGLA)], (
            f'El archivo declara {declarados} y el trinquete usa '
            f'{self.MAX_LINEAS_POR_REGLA}.'
        )

    def test_el_archivo_no_reabsorbe_lo_que_se_movio(self):
        """Anti-podredumbre: compuertas y secuencia viven en ESTADO.md."""
        texto = _leer(self._CLAUDE_MD)
        for reincidente in ('Compuertas de las tandas', 'medir → corregir → imponer'):
            assert reincidente not in texto, (
                f'"{reincidente}" volvió al CLAUDE.md. Es estado del proyecto, '
                'no una regla de decisión: va en docs/flota/ESTADO.md.'
            )

    def test_los_punteros_del_claude_md_existen(self):
        """Un puntero a un archivo que no existe es documentación que miente."""
        rotos = []
        for ref in re.findall(r'`(docs/flota/[^`]+\.md)`', _leer(self._CLAUDE_MD)):
            if not os.path.exists(os.path.join(_RAIZ, ref)):
                rotos.append(ref)
        assert not rotos, f'El CLAUDE.md apunta a archivos que no existen: {rotos}'


class TestTrinqueteMotorDeProduccion:
    """TRINQUETE 9 — que los CHECK se sigan verificando contra PostgreSQL.

    El 2026-08-01 un CHECK con `(bool) + (bool)` pasó 25 tests contra SQLite y
    reventó el CREATE TABLE en el release. La cura no es el trinquete 8 —ese
    ataja una clase concreta— sino que los constraints se ejerzan contra el
    motor de producción. Esto vigila que ese arreglo no se desarme.
    """

    _PG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'test_constraints_postgres.py')

    def test_existe_la_suite_contra_postgres(self):
        assert os.path.exists(self._PG), (
            'Se borró tests/flota/test_constraints_postgres.py. Sin ella, '
            '"la base impone el invariante" vuelve a significar "SQLite lo impone".'
        )

    def test_el_marcador_esta_registrado_y_el_build_lo_desactiva_declarandolo(self):
        """El silencio tiene que ser declarado, no accidental.

        Los tests de PostgreSQL quedan fuera del build porque el contenedor no
        tiene base de pruebas. Eso está bien; lo que no vale es que queden fuera
        porque nadie se acordó de correrlos.
        """
        assert 'postgres:' in _leer(os.path.join(_RAIZ, 'pytest.ini')), \
            'pytest.ini no registra el marcador `postgres` con su razón'
        assert 'not postgres' in _leer(os.path.join(_RAIZ, 'railway.toml')), \
            'railway.toml no deselecciona `postgres` de forma explícita'

    def test_los_tests_de_postgres_fallan_en_vez_de_saltarse(self):
        """Un skip deja el reporte en verde y la propiedad sin verificar.

        Ese falso negativo silencioso es exactamente el defecto del que salió
        todo esto — no se puede reintroducir como "solución".
        """
        pg = _leer(self._PG)
        assert 'pytest.fail(' in pg, 'la suite de PostgreSQL debe FALLAR sin base'
        assert 'pytest.skip(' not in pg, 'un skip acá reintroduce el falso negativo'

    def test_el_conteo_de_CHECK_esperado_coincide_con_los_modelos(self, app):
        """Si alguien agrega un CHECK, la suite de PostgreSQL tiene que saberlo.

        La cifra vive en dos lados a propósito: en los modelos y en la
        expectativa del test contra PostgreSQL. Compararlas es lo que obliga a
        correr el nuevo constraint contra el motor real antes de desplegarlo.
        """
        from flota.adaptadores import modelos as m

        reales = sum(
            1
            for M in (m.Foto, m.FichaTecnica, m.DocumentoVehiculo,
                      m.LecturaOdometro, m.Custodia,
                      m.PlantillaInspeccion, m.ItemInspeccion)
            for c in M.__table__.constraints
            if c.__class__.__name__ == 'CheckConstraint'
        )
        declarado = re.search(r'assert n == (\d+)', _leer(self._PG))
        assert declarado, 'no se encontró la expectativa de CHECK en la suite de PostgreSQL'
        assert int(declarado.group(1)) == reales, (
            f'Los modelos tienen {reales} CHECK y la suite de PostgreSQL espera '
            f'{declarado.group(1)}. Actualizá el número Y corré la suite contra '
            f'PostgreSQL — el constraint nuevo no está verificado en el motor real.'
        )


class TestTrinqueteAdvertencias:

    def test_importar_flota_no_emite_advertencias(self):
        import importlib

        modulos = [
            'flota.dominio.valores',
            'flota.dominio.odometro',
            'flota.dominio.custodia',
            'flota.dominio.fotos',
            'flota.dominio.errores',
            'flota.puertos',
        ]
        with warnings.catch_warnings(record=True) as capturadas:
            warnings.simplefilter('always')
            for m in modulos:
                importlib.reload(importlib.import_module(m))

        assert not capturadas, (
            '\nAdvertencias al importar flota:\n'
            + '\n'.join(f'  · {w.category.__name__}: {w.message}' for w in capturadas)
            + '\n\nEl tope es 0 y se silencia con razón y fecha en pytest.ini, '
              'nunca con un ignore desnudo.'
        )


class TestPlacaSiempreVisible:
    """La placa no puede perderse de vista mientras se llena el formulario.

    No es estética: un odómetro registrado en el camión equivocado se convierte
    en el `km_inicial` de otro vehículo y contamina todo lo que cuelgue de él —
    el preventivo por kilómetro, el CPK, la comparación entre turnos.
    """

    _FLOTA_JS = os.path.join(_PWA, 'flota.js')

    def test_los_formularios_van_en_modal_y_no_debajo_de_la_lista(self):
        js = _leer(self._FLOTA_JS)
        for abrir in ('flotaAbrirRecibo', 'flotaAbrirFicha',
                      'flotaAbrirOdometro', 'flotaAbrirDocumentos'):
            cuerpo = js[js.index(f'function {abrir}('):][:900]
            assert 'flotaAbrirModal(' in cuerpo, (
                f'{abrir} no abre el modal: el formulario queda debajo de la '
                f'lista y en celular hay que pasar cuatro placas ajenas para '
                f'llegar a él.'
            )

    def test_el_encabezado_del_modal_es_pegajoso(self):
        """Si la placa se va con el scroll, el formulario largo la esconde
        justo cuando el conductor llega al botón de guardar.

        Se busca la PROPIEDAD en todo el PWA y no en un archivo concreto: el
        estilo puede estar inline o en la hoja, y un test que exige un lugar
        falla cuando alguien mueve el estilo sin romper nada. Es el mismo error
        de medir una proxy en vez de la propiedad.
        """
        blob = _leer(self._FLOTA_JS) + _leer(os.path.join(_PWA, 'index.html'))
        assert 'flota-modal-cabeza' in blob
        i = blob.index('.flota-modal-cabeza') if '.flota-modal-cabeza' in blob else 0
        assert 'position: sticky' in blob[i:i + 400] or 'position:sticky' in blob, (
            'El encabezado del modal dejó de ser pegajoso: con un formulario '
            'largo, la placa desaparece justo al llegar al botón de guardar.'
        )

    def test_la_placa_se_escribe_en_el_encabezado_al_abrir(self):
        js = _leer(self._FLOTA_JS)
        assert "getElementById('flota-modal-placa').textContent = placa" in js


class TestTrinqueteSistemaDeDiseno:
    """TRINQUETE 10 — el módulo usa las clases del WMS, no unas inventadas.

    Nació de un hallazgo concreto: `flota.js` usaba `class="card"` y
    `class="btn"` durante tres días, y **ninguna de las dos existe en este WMS**.
    Por eso las pantallas salían sin estilo. El navegador no avisa de una clase
    inexistente: ignora el atributo y sigue.

    Es la familia del nombre que miente, aplicada al CSS — el código dice que
    hay un estilo y no lo hay, sin error en ninguna parte.
    """

    _FLOTA_JS = os.path.join(_PWA, 'flota.js')
    _HTML = os.path.join(_PWA, 'index.html')

    def _clases_definidas(self):
        return set(re.findall(r'\.([a-z][a-z0-9-]+)\s*[,{]', _leer(self._HTML)))

    def test_toda_clase_usada_existe_en_la_hoja_de_estilos(self):
        usadas = set(re.findall(r'class="([a-z0-9 -]+)"', _leer(self._FLOTA_JS)))
        usadas = {c for grupo in usadas for c in grupo.split() if c}
        # `ok` es modificador de btn-flota; se declara junto con ella.
        inexistentes = sorted(usadas - self._clases_definidas() - {'ok'})
        assert not inexistentes, (
            f'\nClases que no existen en index.html: {inexistentes}\n'
            'El navegador ignora una clase inexistente sin avisar: la pantalla '
            'sale sin estilo y nada falla.'
        )

    def test_los_botones_del_patio_tienen_tamano_tactil(self):
        """El conductor los toca a las 5 a.m., con lluvia y a veces con guantes.

        48px es el mínimo táctil accesible. En celular sube, porque ahí es donde
        se usa de verdad — y un error de pulgar produce un dato falso que nadie
        detecta.
        """
        html = _leer(self._HTML)
        i = html.index('.btn-flota')
        assert 'min-height: 48px' in html[i:i + 400]
        assert '@media (max-width: 480px)' in html
        movil = html[html.index('@media (max-width: 480px)'):][:500]
        assert 'min-height: 54px' in movil, 'en celular los botones no crecen'

    def test_la_placa_manda_en_tamano(self):
        """Es lo que se busca con la vista antes de tocar nada."""
        html = _leer(self._HTML)
        i = html.index('.flota-placa')
        assert 'font-size: 22px' in html[i:i + 300]
