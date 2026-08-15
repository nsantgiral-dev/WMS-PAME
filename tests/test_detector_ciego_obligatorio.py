"""
Todo `BLOQUEA` nace con el test que lo hace DISPARAR.

No el que lo hace pasar: **el que construye la violación y exige que la vea.**

Es la lección más cara de la auditoría del 2026-08-15. Seis guards estaban en
verde sobre propiedades que la vía sana satisface **por construcción**:

    TRA-01   sobre dos campos que la escritura iguala
    REP-02   sobre un flag que solo existe dentro del estado que exige
    CNT-04   sobre una fila que el camino de salto conserva en CANCELADO
    REC-01   sobre un booleano que el bloque de emergencia fuerza AL FALLAR
    bodegas  sobre una de tres copias del mismo mapa
    nivel 4  sobre una URL adyacente dentro de una función sin llamador

Los seis tenían el test de «no dispara cuando está sano». Ninguno tenía el
otro. **Los seis se habrían caído al primer intento.**

## La regla, que vale más que los seis arreglos

> ¿Qué escribe el valor que estoy comprobando, y puede el camino roto
> escribirlo igual? Si la respuesta es sí, el guard no sirve.

## Por qué la referencia y no un regex

La primera versión de esta medición fue un regex sobre los tests y **se le
escapó `CNT-07`** —su código vive en un helper, no en el cuerpo del test— diez
minutos después de escribir que los detectores de texto se atrapan solos. Séptima
vez.

Por eso la referencia se declara en el propio `@invariante(...)` y acá se
verifica **por AST que exista**: una referencia que no resuelve es peor que
ninguna, porque afirma cobertura que no hay.
"""
import ast
import pathlib

import pytest

from app.services import auditoria

_RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: Los que todavía no tienen su detector. **Solo puede encoger.**
#: Vacía hoy: los 28 `BLOQUEA` la cumplen.
SIN_DETECTOR = set()


def _bloqueantes():
    return [i for i in auditoria.registrados() if i.severidad == auditoria.BLOQUEA]


def _resuelve(ref: str) -> bool:
    """¿La referencia `archivo::Clase::test` apunta a un test que existe?"""
    partes = ref.split('::')
    ruta = _RAIZ / partes[0]
    if not ruta.exists():
        return False
    arbol = ast.parse(ruta.read_text())
    nombre = partes[-1]
    if len(partes) == 3:
        clase = next((c for c in ast.walk(arbol)
                      if isinstance(c, ast.ClassDef) and c.name == partes[1]), None)
        if clase is None:
            return False
        return any(isinstance(f, ast.FunctionDef) and f.name == nombre
                   for f in clase.body)
    return any(isinstance(f, ast.FunctionDef) and f.name == nombre
               for f in ast.walk(arbol))


class TestTodoBloqueanteTieneQuienLoHagaDisparar:

    def test_ninguno_sin_detector_fuera_de_la_lista(self):
        faltan = {i.codigo for i in _bloqueantes() if not i.detector_ciego}
        nuevos = faltan - SIN_DETECTOR
        assert not nuevos, (
            f'invariantes BLOQUEA sin `detector_ciego`: {sorted(nuevos)}.\n\n'
            f'Un guard sin un test que lo haga DISPARAR es un guard que quizá no '
            f'pueda fallar. Preguntá: ¿qué escribe el valor que estoy '
            f'comprobando, y puede el camino roto escribirlo igual?')

    def test_la_lista_solo_encoge(self):
        """Si alguien resuelve uno y no lo saca de la lista, la lista miente
        sobre lo que falta — el mismo defecto que tenía `SIN_CUBRIR`."""
        con_detector = {i.codigo for i in _bloqueantes() if i.detector_ciego}
        assert not (SIN_DETECTOR & con_detector), (
            f'ya tienen detector, borralos de SIN_DETECTOR: '
            f'{sorted(SIN_DETECTOR & con_detector)}')

    @pytest.mark.parametrize('inv', [
        pytest.param(i, id=i.codigo) for i in
        sorted((x for x in auditoria.registrados()
                if x.severidad == auditoria.BLOQUEA and x.detector_ciego),
               key=lambda x: x.codigo)
    ])
    def test_la_referencia_resuelve(self, inv):
        """Una referencia rota afirma una cobertura que no existe."""
        assert _resuelve(inv.detector_ciego), (
            f'{inv.codigo} apunta a un test que no existe: '
            f'{inv.detector_ciego!r}')


class TestElDetectorDeEstaReglaSeMide:
    """Un piso mínimo: si el registro se rompe y devuelve pocos invariantes,
    todo lo de arriba pasa por vacío."""

    def test_hay_bloqueantes_de_verdad(self):
        assert len(_bloqueantes()) >= 25, (
            'el registro devolvió menos BLOQUEA de los que hay — este archivo '
            'estaría pasando sobre un universo vacío')

    def test_una_referencia_inventada_no_resuelve(self):
        assert not _resuelve('tests/test_que_no_existe.py::X::test_y')
        assert not _resuelve('tests/test_detector_ciego_obligatorio.py::X::test_y')
