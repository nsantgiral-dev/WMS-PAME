"""La mitad de los invariantes de flota no se prueba contra el motor real, y eso
se dice en voz alta.

`railway.toml` corre la suite con `-m "not postgres"` y el contenedor de build
no tiene una base. Entonces los **27 tests de `test_constraints_postgres.py`
nunca han corrido en CI** — y son justo los que miden la propiedad en vez de la
proxy:

    · que los CHECK lleguen de verdad a la base
    · que el DELETE sobre `flota_lectura_odometro` esté bloqueado — protección
      que SOLO existe en PostgreSQL (se omitió en SQLite porque rompía el
      teardown de otros tests)
    · que los índices únicos parciales funcionen
    · que `flota_limpiar_vehiculo.py` RESTAURE el trigger que deshabilita
    · que la migración corra en el orden correcto contra datos reales

Los que sí corren, corren contra SQLite en memoria.

## Por qué un archivo entero para decir esto

Porque un hueco que nadie nombra deja de existir. `-m "not postgres"` es una
bandera en un `.toml`: no aparece en ningún reporte, no sale en la salida de
pytest más que como un `27 deselected` que nadie lee, y el día que alguien
pregunte «¿está probado?» la respuesta honesta —«la mitad, contra el motor
equivocado»— no está escrita en ninguna parte.

Este archivo **no reemplaza correrlos**. Hace dos cosas más chicas y que sí
caben en la suite normal:

1. Que el archivo de invariantes y su corredor sigan existiendo. Si alguien
   borra esos tests, o el script que los corre, esto se pone rojo. Un hueco
   declarado que se puede borrar en silencio es peor que ninguno.
2. Que el número de tests declarado acá no se separe de la realidad.

## Cómo se cierra el hueco

    ./scripts/verificar_flota_postgres.sh

Crea una base temporal, los corre, y la borra. Se niega a correr contra un
host que no sea local — estos tests crean, borran y deshabilitan triggers.

**Última corrida verde: 2026-09-01, PostgreSQL 17 local, 27 passed en 4,05 s.**
La primera corrida en este entorno fue ese mismo día, con 24.

> La forma que este repo persigue: un guard que da verde sin mirar. La suite
> entera en verde es cierta y no significa lo que parece — la mitad del
> respaldo de estos invariantes vive en un motor que la suite no toca.
"""
import ast
import pathlib
import re

import pytest

_RAIZ = pathlib.Path(__file__).resolve().parents[2]
_INVARIANTES = _RAIZ / 'tests' / 'flota' / 'test_constraints_postgres.py'
_CORREDOR = _RAIZ / 'scripts' / 'verificar_flota_postgres.sh'

#: Al 2026-09-01. Si cambia, este archivo tiene que cambiar con él — es el
#: punto: que el número declarado no se despegue de la realidad.
#:
#: Pasó de 24 a 27 el mismo día, al agregar la semántica de corrección
#: superseding (el caso THP696). Este guard lo atrapó solo: el cambio de
#: comportamiento del trigger de producción llegó con sus tres pruebas
#: contra PostgreSQL, no solo contra SQLite.
#:
#: Y de 27 a 33 al nacer `flota_hallazgo`: su CHECK de desenlace es el único
#: escrito con `CASE WHEN` —forma elegida justamente para no repetir el bug de
#: booleanos sumados que costó el release del 2026-08-01— y el vocabulario de
#: `origen` se ensanchó con `'hallazgo'` en la misma migración. Las dos cosas
#: son invisibles para SQLite: la primera compila igual, la segunda se rehace
#: sola desde el modelo. Solo el motor real las juzga.
#:
#: Y de 33 a 45 el 2026-09-02, con m018: `ck_flota_insp_veredicto_coherente`
#: es **otro `CASE WHEN`** —la forma elegida para no repetir el bug de
#: booleanos sumados del 2026-08-01— y es el que impone la regla 1 en la base:
#: `apto` con un ítem sin responder queda rechazado por el motor, no solo por
#: Python. Contar que el constraint existe no prueba que decida bien.
_TESTS_ESPERADOS = 45


def _tests_del_archivo(ruta: pathlib.Path) -> list:
    """Nombres de test contando los métodos de clase, por AST.

    Por AST y no por `grep 'def test'`: este repo tiene documentado que los
    detectores de texto se atrapan en sus propios docstrings, y el docstring de
    arriba menciona varias veces la palabra que un regex buscaría.
    """
    arbol = ast.parse(ruta.read_text())
    nombres = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if nodo.name.startswith('test_'):
                nombres.append(nodo.name)
    return nombres


class TestElHuecoSigueDeclarado:
    """No prueba los invariantes: prueba que el hueco no se tape solo."""

    def test_el_archivo_de_invariantes_existe(self):
        assert _INVARIANTES.exists(), (
            f'{_INVARIANTES.relative_to(_RAIZ)} no está. Eran los únicos tests '
            f'que ejercían los CHECK, los triggers y los índices parciales '
            f'contra PostgreSQL — el motor de producción. Borrarlos deja los '
            f'invariantes de flota probados solo contra SQLite, que es lo que '
            f'ya costó un release el 2026-08-01.')

    def test_el_corredor_existe_y_es_ejecutable(self):
        """Sin el script, correrlos exige recordar la variable de entorno y el
        marcador. Lo que hay que recordar, no se hace."""
        assert _CORREDOR.exists(), (
            f'falta {_CORREDOR.relative_to(_RAIZ)} — el único camino barato '
            f'para ejercer los invariantes de PostgreSQL')
        import os
        assert os.access(_CORREDOR, os.X_OK), (
            f'{_CORREDOR.relative_to(_RAIZ)} no es ejecutable')

    def test_el_corredor_se_niega_contra_un_host_remoto(self):
        """La guarda que impide que estos tests —que crean, borran y
        deshabilitan triggers— corran contra la base del negocio.

        Se comprueba por AST del shell no: es un `.sh`. Se comprueba que la
        lista blanca de hosts y el `exit 2` sigan escritos. Verificado
        ejecutando el 2026-09-01: `PGHOST=metro.proxy.rlwy.net` → exit 2."""
        fuente = _CORREDOR.read_text()
        assert 'localhost|127.0.0.1' in fuente, (
            'el corredor perdió la lista blanca de hosts locales')
        assert re.search(r'exit\s+2', fuente), (
            'el corredor ya no aborta ante un host remoto')

    def test_la_cuenta_declarada_no_se_despega_de_la_realidad(self):
        """Si alguien agrega invariantes de PostgreSQL, este número tiene que
        subir con ellos. Un hueco cuya medida envejece deja de informar."""
        reales = _tests_del_archivo(_INVARIANTES)
        assert len(reales) == _TESTS_ESPERADOS, (
            f'el archivo de invariantes tiene {len(reales)} tests y acá se '
            f'declaran {_TESTS_ESPERADOS}. Actualizá `_TESTS_ESPERADOS` y la '
            f'fecha de la última corrida verde en el encabezado — el punto de '
            f'este archivo es que el número declarado sea cierto.')

    def test_siguen_marcados_para_quedar_fuera_de_la_suite_normal(self):
        """El marcador es lo que los excluye. Si desapareciera, correrían en el
        build de Railway —que no tiene Postgres— y romperían el deploy.

        **Por AST y no por substring.** La primera versión de este test hacía
        `'pytestmark = pytest.mark.postgres' in fuente`, y una mutación que
        comentaba la línea con `#` lo dejaba en verde: la subcadena sigue
        estando dentro del comentario. Es el detector de texto que no distingue
        código de prosa, cometido dentro del archivo que existe para declarar
        huecos. Se descubrió mutándolo el 2026-09-01."""
        arbol = ast.parse(_INVARIANTES.read_text())
        vivo = any(
            isinstance(n, ast.Assign)
            and any(getattr(t, 'id', None) == 'pytestmark' for t in n.targets)
            and 'postgres' in ast.unparse(n.value)
            for n in ast.walk(arbol)
        )
        assert vivo, (
            'los invariantes perdieron el marcador `postgres` (o quedó '
            'comentado): van a correr en el build de Railway, que no tiene '
            'base, y el deploy falla')

    @pytest.mark.parametrize('invariante', [
        'TestElEsquemaSeCreaEnPostgres',
        'TestInvariante4EnPostgres',
        'TestIndiceParcialEnPostgres',
        'TestTriggersEnPostgres',
        'TestElScriptDeLimpiezaDejaElTriggerComoEstaba',
    ])
    def test_los_cinco_grupos_siguen_estando(self, invariante):
        """Nombrados uno por uno a propósito: si mañana alguien borra el grupo
        de triggers, la cuenta de tests fallaría, pero el mensaje diría
        «cambió la cuenta» y no «desapareció la única prueba de que el DELETE
        del odómetro está bloqueado»."""
        arbol = ast.parse(_INVARIANTES.read_text())
        clases = {n.name for n in ast.walk(arbol) if isinstance(n, ast.ClassDef)}
        assert invariante in clases, (
            f'desapareció {invariante} de los invariantes de PostgreSQL')
