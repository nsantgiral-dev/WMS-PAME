"""La marca de confianza sale como palabra, y una sola función la produce.

## El defecto

`Confianza` hereda de `str` y de `Enum`. En Python 3.11:

```
str(Confianza.DECLARADA)  →  'Confianza.DECLARADA'      ← el repr, no el valor
str(SIN_DATO)             →  'sin_dato'                 ← acá str() es un no-op
```

`/flota/health` publicaba **las dos formas en el mismo JSON**:
`km_dia_por_vehiculo` devolvía `"Confianza.DECLARADA"` y `cpk_mes` devolvía
`"declarada"`. `flota.js` imprimía la primera tal cual en el panel de salud.

**Y ningún test lo veía** porque los que leen `marca` contra el endpoint real
usan un vehículo sin lecturas, donde la marca es `SIN_DATO` — una cadena, donde
`str()` no hace nada. El defecto vivía fuera de la franja que los tests
visitaban, igual que el CPK al doble vivía fuera de las 08:00 UTC.

## Por qué una función y no un comentario

La conversión correcta estaba escrita **cuatro veces**: bien en
`gastos._tramo_de` —con el comentario explicando por qué— y en
`dominio/llantas.py`; **mal** en `medicion.km_dia_por_vehiculo`,
`api/preventivo._ritmo_json` y en los dos sitios de `api/llantas.py`.

Un comentario protege el sitio donde está escrito. Una función con nombre
protege la política. Regla 0 del WMS, corolario.
"""
import ast
import pathlib

import pytest

from flota.dominio.valores import (SIN_DATO, Confianza, palabra_de_confianza)

_RAIZ = pathlib.Path(__file__).resolve().parents[2]


class TestLaConversionEsCorrecta:
    @pytest.mark.parametrize('marca,esperado', [
        (Confianza.DECLARADA, 'declarada'),
        (Confianza.DUDOSA, 'dudosa'),
        (Confianza.VERIFICADA, 'verificada'),
    ])
    def test_cada_miembro_sale_como_su_valor(self, marca, esperado):
        assert palabra_de_confianza(marca) == esperado

    def test_y_str_NO_lo_hace(self):
        """El defecto, fijado. Si algún día Python cambia y `str()` empieza a
        devolver el valor, este test se pone rojo — y entonces la función deja
        de hacer falta y hay que decidirlo, no descubrirlo."""
        assert str(Confianza.DECLARADA) == 'Confianza.DECLARADA'

    def test_sin_dato_pasa_entero(self):
        """La otra dirección: la marca de «no se puede medir» no es un enum, y
        la función no puede romperla."""
        assert palabra_de_confianza(SIN_DATO) == 'sin_dato'

    def test_ninguna_palabra_lleva_el_nombre_de_la_clase(self):
        """Lo que llega a la pantalla nunca puede tener forma de repr."""
        for m in list(Confianza) + [SIN_DATO]:
            assert 'Confianza.' not in palabra_de_confianza(m)


class TestNadieVuelveAConvertirlaAMano:
    """TRINQUETE por AST, no por texto.

    Un detector de texto se atraparía en los comentarios de este mismo archivo
    y en los de `valores.py`, que citan `str(marca)` para explicar el defecto.
    Es la novena vez que ese patrón aparece en este repo — acá se evita
    buscando la LLAMADA, no la cadena.
    """

    #: El ÚNICO sitio donde `str(marca)` es correcto: el fallback dentro de la
    #: función canónica, que convierte lo que no es un `Confianza` (o sea,
    #: `SIN_DATO`, que ya es una cadena).
    #:
    #: La exención es por NOMBRE DE FUNCIÓN y no por número de línea ni por
    #: archivo: si mañana alguien escribe un segundo `str(marca)` en
    #: `valores.py`, el guard lo ve. Un `# noqa` o un archivo entero eximido
    #: habría tapado los dos que este trinquete acaba de encontrar.
    _DEFINICION_CANONICA = 'palabra_de_confianza'

    def _llamadas_a_str_sobre_una_marca(self):
        malas = []
        for py in sorted((_RAIZ / 'flota').rglob('*.py')):
            arbol = ast.parse(py.read_text(encoding='utf-8'))
            dentro_de_la_canonica = {
                nodo
                for f in ast.walk(arbol)
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                and f.name == self._DEFINICION_CANONICA
                for nodo in ast.walk(f)
            }
            for n in ast.walk(arbol):
                if n in dentro_de_la_canonica:
                    continue
                if not (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id == 'str'
                        and len(n.args) == 1):
                    continue
                # `str(x)` donde x se llama `marca` o termina en `.marca`
                a = n.args[0]
                nombre = (a.id if isinstance(a, ast.Name)
                          else a.attr if isinstance(a, ast.Attribute) else '')
                if nombre == 'marca' or nombre.endswith('_marca'):
                    malas.append(f'{py.relative_to(_RAIZ)}:{n.lineno}')
        return malas

    def test_ningun_sitio_de_flota_hace_str_sobre_una_marca(self):
        malas = self._llamadas_a_str_sobre_una_marca()
        assert not malas, (
            '\n'.join(malas)
            + '\n\nUsá `palabra_de_confianza`. `str()` sobre un `Confianza` '
              'devuelve `Confianza.X` y eso llega a la pantalla tal cual — '
              'invisible mientras el caso probado sea `sin_dato`.')

    def test_el_detector_encuentra_la_forma_que_busca(self):
        """La otra dirección. Un detector que no sabe reconocer el patrón
        devuelve lista vacía sobre un repo roto, y eso se lee como «está sano»."""
        arbol = ast.parse('def f(marca):\n    return str(marca)\n')
        encontrados = [
            n for n in ast.walk(arbol)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == 'str' and len(n.args) == 1
            and getattr(n.args[0], 'id', '') == 'marca'
        ]
        assert len(encontrados) == 1


    def test_la_exencion_cubre_exactamente_un_sitio(self):
        """Una exención que crece es una exención que dejó de eximir algo
        concreto. Hoy es el fallback de la función canónica y nada más."""
        fuente = (_RAIZ / 'flota' / 'dominio' / 'valores.py').read_text()
        arbol = ast.parse(fuente)
        canonica = [f for f in ast.walk(arbol)
                    if isinstance(f, ast.FunctionDef)
                    and f.name == self._DEFINICION_CANONICA]
        assert len(canonica) == 1, 'la función canónica se duplicó'
        adentro = [n for n in ast.walk(canonica[0])
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == 'str']
        assert len(adentro) == 1, (
            f'{len(adentro)} llamadas a str() dentro de la función canónica: '
            f'la exención tapa más de lo que declara')
