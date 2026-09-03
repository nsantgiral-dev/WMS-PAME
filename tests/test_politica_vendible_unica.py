"""
Trinquete: «¿esto se puede vender?» se contesta en UN solo sitio.

El 2026-08-20 la pregunta estaba escrita tres veces con tres respuestas
distintas (`tipo_zona != 'AVERIAS'` en el FEFO, `notin_(('AVERIAS',))` en la
alerta de mínimos, y un OR de tres campos en la reconciliación) — y el escritor
más grande de averías (`devolucion_service`) no ponía ninguno de los campos que
las dos primeras miran. Los dos filtros nuevos estuvieron en verde todo el
tiempo porque probaban al otro escritor.

Por eso el trinquete no verifica «el FEFO excluye averías» (eso ya lo hacen
`test_bin_averias_del_escritor.py` y `test_fefo_excluye_averias.py`): verifica
la propiedad estructural de la que dependen esos tests para significar algo —
**que no exista una segunda definición**.

## Por AST, nunca por texto

`CLAUDE.md` documenta que los detectores de texto se atraparon a sí mismos en
sus propios docstrings siete veces en una semana, y en el lote que este archivo
corrige volvió a pasar. El AST no ve comentarios (el parser los descarta) y acá
se descartan además los docstrings explícitamente, así que un detector escrito
así no puede confundir una explicación con una implementación.

## Qué se revisa, y qué NO se lleva escrito a mano

Se recorre **todo** `app/`, no una lista de archivos. La lección de los tres
`_BODEGA_CO_MAP` es que un detector con la lista de sitios escrita a mano no ve
el sitio nuevo: acá lo escrito a mano son las **excepciones conocidas**, cada
una con su razón, su fecha y su **conteo exacto** — así que una ocurrencia nueva
dentro de un archivo ya exceptuado también pone esto rojo.

Y se ejercita en las dos direcciones: se comprueba que el detector dispara
sobre código que reintroduce la copia, y que NO dispara sobre un docstring que
la menciona. Un detector que solo prueba que dispara, prueba la mitad.
"""
import ast
import pathlib

import pytest


RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'

# El módulo donde vive la política. Único autorizado a escribir los literales.
MODULO_CANONICO = 'app/services/picking_service.py'

# Los literales con los que se responde la pregunta. Si aparecen en código
# ejecutable fuera del módulo canónico, hay una segunda definición.
LITERALES_DE_ZONA = {'AVERIAS', 'CUARENTENA', 'cuarentena'}


# ─────────────────────────────────────────────────────────────────────────────
# Excepciones conocidas — con razón, fecha y CONTEO EXACTO
#
# Ninguna de estas es un permiso permanente: son archivos que en la tanda del
# 2026-08-20 pertenecen a otros agentes, así que este agente no puede migrarlos
# a la política única sin pisarlos. Están reportados. Migrar cada uno es borrar
# su fila de acá; agregar una fila nueva sin razón y sin fecha es exactamente lo
# que este trinquete existe para impedir.
# ─────────────────────────────────────────────────────────────────────────────
EXCEPCIONES = {
    'app/services/inventario_siesa_service.py': (
        3,
        '2026-08-20 · `_cuarentena_wms` mide la cuarentena con el OR de tres '
        'campos (tipo/zona/tipo_zona). Es la MISMA pregunta en dirección '
        'contraria y debe pasar a `filtro_ubicacion_averias()`. No es de este '
        'agente en esta tanda.'
    ),
    'app/routes/compras.py': (
        1,
        '2026-08-20 · la pantalla de cuarentena/averías filtra '
        "`tipo_zona == 'AVERIAS'` a mano. Debe usar `filtro_ubicacion_averias()`. "
        'No es de este agente en esta tanda.'
    ),
    # `app/services/devolucion_cliente_service.py` estuvo acá con (2, …) y se
    # retiró el 2026-08-21: es **el escritor VIVO** y ya usa
    # `campos_ubicacion_averias()`, así que hoy tiene CERO ocurrencias.
    #
    # Dejarla habría sido peor que no tenerla: una excepción de 2 sobre un
    # archivo de 0 **autoriza reintroducir el defecto exacto** — volver a
    # escribir `zona='CUARENTENA', tipo='cuarentena'` da 2, coincide con lo
    # declarado y el trinquete queda verde. Un permiso rancio no caduca solo.
    'app/services/layout_service.py': (
        4,
        '2026-08-20 · catálogo de zonas (`ZONAS_VALIDAS`, `_PREFIJO_ZONA`) y el '
        'escritor `crear_ubicacion_averias`, que sí marca `tipo_zona` bien. '
        'Debe tomar la constante de la política. No es de este agente.'
    ),
    'app/services/ubicaciones_sync_service.py': (
        1,
        '2026-08-20 · `_inferir_tipo_zona` clasifica el prefijo AVE- del código '
        'Siesa. Escritor correcto, literal duplicado. No es de este agente.'
    ),
}


def _literales_en_codigo(fuente: str):
    """
    Ocurrencias de los literales de zona en código EJECUTABLE.

    Descarta los docstrings (todo `Expr` cuyo valor es una cadena) — los
    comentarios ni siquiera llegan al AST. Devuelve [(linea, literal)].
    """
    arbol = ast.parse(fuente)
    docstrings = {
        id(nodo.value)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Expr)
        and isinstance(nodo.value, ast.Constant)
        and isinstance(nodo.value.value, str)
    }
    return [
        (nodo.lineno, nodo.value)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Constant)
        and isinstance(nodo.value, str)
        and nodo.value in LITERALES_DE_ZONA
        and id(nodo) not in docstrings
    ]


def _nombres_usados(fuente: str, funcion: str):
    """Nombres (Name/Attribute) que aparecen dentro de una función dada."""
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == funcion:
            usados = set()
            for hijo in ast.walk(nodo):
                if isinstance(hijo, ast.Name):
                    usados.add(hijo.id)
                elif isinstance(hijo, ast.Attribute):
                    usados.add(hijo.attr)
            return usados
    raise AssertionError(f'No existe la función {funcion}()')


def _fuente(ruta_relativa: str) -> str:
    return (RAIZ / ruta_relativa).read_text()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Nadie más define la política
# ─────────────────────────────────────────────────────────────────────────────

class TestUnaSolaDefinicionDeVendible:

    def test_ningun_archivo_de_app_reescribe_la_politica(self):
        """
        Barrido completo de `app/`. Cualquier archivo que responda «¿es
        vendible?» con un literal propio aparece acá — incluidos los que
        todavía no existen.
        """
        hallazgos = {}
        for ruta in sorted(APP.rglob('*.py')):
            relativa = ruta.relative_to(RAIZ).as_posix()
            if relativa == MODULO_CANONICO:
                continue
            ocurrencias = _literales_en_codigo(ruta.read_text())
            esperadas, _razon = EXCEPCIONES.get(relativa, (0, ''))
            # Sin `continue` antes de comparar, a propósito. La versión anterior
            # salteaba el archivo limpio (`if not ocurrencias: continue`), así que
            # una excepción que se arreglaba **nunca se volvía a mirar** y su
            # permiso quedaba vigente para siempre: reintroducir el defecto exacto
            # daba el número declarado y el trinquete seguía verde.
            #
            # Ahora 0 reales contra N declaradas también es hallazgo. Arreglar un
            # sitio obliga a retirar su excepción — que es lo único que hace que
            # esta lista se achique en vez de envejecer.
            if len(ocurrencias) != esperadas:
                hallazgos[relativa] = (len(ocurrencias), esperadas, ocurrencias)

        assert not hallazgos, (
            'Hay una segunda definición de «¿esto se puede vender?». Usá '
            '`picking_service.filtro_ubicacion_vendible()` / '
            '`filtro_ubicacion_averias()` / `campos_ubicacion_averias()`.\n'
            'Si de verdad no se puede migrar ahora, agregá el archivo a '
            'EXCEPCIONES con razón y fecha — nunca borres el test.\n'
            + '\n'.join(
                f'  {archivo}: {hay} ocurrencia(s), esperadas {esp} → {det}'
                for archivo, (hay, esp, det) in hallazgos.items()
            )
        )

    def test_toda_excepcion_declara_razon_y_fecha(self):
        """
        Un canal de excepciones sin razón ni fecha se llena de entradas y deja
        de leerse — y entonces la excepción nueva y real pasa invisible.
        """
        for archivo, (cantidad, razon) in EXCEPCIONES.items():
            assert (RAIZ / archivo).exists(), f'Excepción para un archivo que ya no existe: {archivo}'
            assert cantidad > 0, f'Excepción con conteo 0 — borrala: {archivo}'
            assert razon.startswith('2026-'), f'Excepción sin fecha: {archivo}'
            assert len(razon) > 60, f'Excepción sin razón utilizable: {archivo}'


# ─────────────────────────────────────────────────────────────────────────────
# 2. Los consumidores usan la función, no una copia
# ─────────────────────────────────────────────────────────────────────────────

class TestLosConsumidoresLlamanALaPolitica:

    def test_el_fefo_filtra_con_la_politica(self):
        usados = _nombres_usados(_fuente(MODULO_CANONICO), 'calcular_fefo')
        assert 'filtro_ubicacion_vendible' in usados

    def test_el_buscador_de_bin_de_averias_usa_la_misma_politica(self):
        """La dirección contraria de la pregunta tiene que leer el mismo campo:
        si «dame un bin de averías» y «excluí las averías» usan criterios
        distintos, hay stock que no está en ninguno de los dos lados."""
        usados = _nombres_usados(_fuente(MODULO_CANONICO), '_ubicacion_averias_disponible')
        assert 'filtro_ubicacion_averias' in usados

    def test_la_alerta_de_minimos_filtra_con_la_politica(self):
        usados = _nombres_usados(
            _fuente('app/services/dashboard_service.py'),
            'consulta_productos_bajo_minimo',
        )
        assert 'filtro_ubicacion_vendible' in usados

    def test_el_escritor_de_devoluciones_marca_con_la_politica(self):
        """El agujero original: el escritor y el lector no compartían código."""
        usados = _nombres_usados(
            _fuente('app/services/devolucion_service.py'),
            'confirmar_ubicacion',
        )
        assert '_campos_ubicacion_averias' in usados

    def test_los_campos_que_escribe_el_escritor_satisfacen_el_filtro(self, app):
        """
        Contrato de ida y vuelta, ejecutado (no leído): lo que
        `campos_ubicacion_averias()` escribe tiene que hacer que
        `es_ubicacion_vendible()` diga que NO. Sin esto, las dos mitades pueden
        estar unificadas y aun así no coincidir.
        """
        from app.models.ubicacion import Ubicacion
        from app.services.picking_service import (
            campos_ubicacion_averias, es_ubicacion_vendible,
        )

        ub = Ubicacion(codigo='AVERIADOS', almacen_id=1, activo=True,
                       **campos_ubicacion_averias())
        assert es_ubicacion_vendible(ub) is False

        vendible = Ubicacion(codigo='PIK-01', almacen_id=1, tipo_zona='PICKING')
        assert es_ubicacion_vendible(vendible) is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. El detector, en las dos direcciones
# ─────────────────────────────────────────────────────────────────────────────

class TestElDetectorFuncionaEnLasDosDirecciones:

    def test_dispara_sobre_una_copia_reintroducida(self):
        fuente = (
            'def stock_bueno(q):\n'
            "    return q.filter(Ubicacion.tipo_zona != 'AVERIAS')\n"
        )
        assert _literales_en_codigo(fuente) == [(2, 'AVERIAS')]

    def test_dispara_sobre_un_escritor_que_marca_a_mano(self):
        fuente = (
            "ub = Ubicacion(codigo='AVERIADOS', zona='CUARENTENA', tipo='cuarentena')\n"
        )
        assert sorted(v for _, v in _literales_en_codigo(fuente)) == ['CUARENTENA', 'cuarentena']

    def test_no_dispara_sobre_un_docstring_que_la_menciona(self):
        """
        La razón exacta de usar AST. Un `grep` sobre este mismo repositorio se
        atrapó siete veces en una semana explicando el defecto que buscaba.
        """
        fuente = (
            '"""Excluye AVERIAS del FEFO — ver CUARENTENA y cuarentena."""\n'
            'def f():\n'
            '    """Otra explicación con AVERIAS adentro."""\n'
            '    return 1\n'
        )
        assert _literales_en_codigo(fuente) == []

    def test_no_dispara_sobre_literales_que_solo_contienen_la_palabra(self):
        """`'TRASLADO_AVERIAS'` (tipo de SiesaJob) y `'SIESA_BODEGA_AVERIAS'`
        (variable de entorno) no son la política. Un detector que los marcara
        obligaría a exceptuar archivos sanos, y una lista de excepciones inflada
        es una lista que nadie lee."""
        fuente = (
            "job = SiesaJob(tipo='TRASLADO_AVERIAS')\n"
            "bodega = os.getenv('SIESA_BODEGA_AVERIAS', 'AV1')\n"
        )
        assert _literales_en_codigo(fuente) == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. El backfill — el `WHERE` ejercitado contra una base real
# ─────────────────────────────────────────────────────────────────────────────

class TestBackfillM016:

    def _ubicacion(self, db, almacen, **campos):
        from app.models.ubicacion import Ubicacion
        ub = Ubicacion(almacen_id=almacen.id, activo=True, **campos)
        db.session.add(ub)
        db.session.commit()
        return ub

    def _correr_backfill(self, db):
        import importlib.util
        from sqlalchemy import text

        ruta = RAIZ / 'migrations' / 'versions' / 'm016_bin_averiados_sin_tipo_zona.py'
        spec = importlib.util.spec_from_file_location('m016', ruta)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        resultado = db.session.execute(text(modulo.SQL_BACKFILL))
        db.session.commit()
        return resultado.rowcount

    def test_corrige_el_bin_que_dejo_el_escritor_viejo(self, app, db, almacen, producto):
        """
        La fila tal como está hoy en producción: `AVERIADOS`, cuarentena en
        `zona`/`tipo`, y `tipo_zona` en el default 'GENERAL'. Después del
        backfill el FEFO deja de verla.
        """
        from app.models.inventario import UbicacionProducto
        from app.services.picking_service import PickingService

        ub = self._ubicacion(db, almacen, codigo='AVERIADOS', zona='CUARENTENA',
                             tipo='cuarentena', tipo_zona='GENERAL')
        db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=producto.id,
                                         cantidad=40, reservado=0, bloqueado=0))
        db.session.commit()

        antes = PickingService.calcular_fefo(producto.id, 5, almacen.id)
        assert [a['ubicacion_codigo'] for a in antes['asignaciones']] == ['AVERIADOS']

        assert self._correr_backfill(db) == 1

        db.session.expire_all()
        despues = PickingService.calcular_fefo(producto.id, 5, almacen.id)
        assert despues['asignaciones'] == []
        assert despues['cantidad_faltante'] == 5

    def test_es_idempotente(self, app, db, almacen):
        self._ubicacion(db, almacen, codigo='AVERIADOS', zona='CUARENTENA',
                        tipo='cuarentena', tipo_zona='GENERAL')
        assert self._correr_backfill(db) == 1
        assert self._correr_backfill(db) == 0, (
            'La segunda corrida tocó filas — no sabemos qué copias de la base '
            'existen ni cuál ya pasó por acá'
        )

    def test_no_toca_una_estanteria_normal_que_se_llama_igual(self, app, db, almacen, producto):
        """
        El error caro en la dirección contraria. Una ubicación llamada
        `AVERIADOS` pero sin ninguna marca de cuarentena es una estantería
        normal: marcarla la sacaría del FEFO y escondería stock vendible —
        un agotado inventado, que nadie detecta porque no da error.
        """
        from app.models.inventario import UbicacionProducto
        from app.services.picking_service import PickingService

        ub = self._ubicacion(db, almacen, codigo='AVERIADOS', zona='GENERAL',
                             tipo='estanteria', tipo_zona='PICKING')
        db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=producto.id,
                                         cantidad=40, reservado=0, bloqueado=0))
        db.session.commit()

        assert self._correr_backfill(db) == 0

        db.session.expire_all()
        assert ub.tipo_zona == 'PICKING'
        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)
        assert [a['ubicacion_codigo'] for a in fefo['asignaciones']] == ['AVERIADOS']

    def test_no_toca_ninguna_otra_ubicacion(self, app, db, almacen):
        """Sin `LIKE`, sin prefijos: solo el código exacto."""
        pik = self._ubicacion(db, almacen, codigo='PIK-01-Z', tipo_zona='PICKING',
                              zona='CUARENTENA', tipo='cuarentena')
        gen = self._ubicacion(db, almacen, codigo='SIESA-GENERAL', tipo_zona='GENERAL')

        assert self._correr_backfill(db) == 0

        db.session.expire_all()
        assert pik.tipo_zona == 'PICKING'
        assert gen.tipo_zona == 'GENERAL'

    def test_la_cadena_de_migraciones_tiene_un_solo_head(self):
        """`releaseCommand` corre `flask db upgrade` en cada deploy: con dos
        heads el release falla y no se entera nadie hasta el corte.

        **Mide la propiedad, no el nombre.** Hasta el 2026-09-01 este test
        exigía `heads == ['m016binaveriadossintipozona']`: o sea, se ponía rojo
        con *cada* migración nueva —incluidas las correctas— y el arreglo era
        reescribir un literal. Un guard que se rompe por lo que debe pasar
        enseña a actualizarlo sin leerlo, y el día que se rompa por un segundo
        head la mano ya está entrenada para escribir el nombre nuevo y seguir.

        Lo que el literal SÍ protegía —que este backfill siga en la cadena— se
        afirma abajo, directo y sin depender de que sea la punta.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(RAIZ / 'migrations' / 'alembic.ini'))
        cfg.set_main_option('script_location', str(RAIZ / 'migrations'))
        guion = ScriptDirectory.from_config(cfg)
        heads = list(guion.get_heads())
        assert len(heads) == 1, f'la cadena se bifurcó: {heads}'

        revisiones = {r.revision for r in guion.walk_revisions()}
        assert 'm016binaveriadossintipozona' in revisiones, (
            'el backfill de tipo_zona desapareció de la cadena: una base nueva '
            'quedaría con las averías sin clasificar y nadie lo vería hasta el '
            'primer picking'
        )
