"""La fila de montaje decía «sin gasto asociado (rotación)» y era falso.

`MontajeLlanta.gasto_id` existe en el modelo y la API lo valida — pero el
formulario de montaje nunca lo manda (`flotaMontarLlanta` arma
`{placa, llanta_id, posicion, km, observacion}`) y el id del gasto tampoco se
muestra en ninguna pantalla, así que ni copiándolo a mano.

O sea que el campo es SIEMPRE nulo, y la pantalla afirmaba «rotación» también
cuando hubo factura. Un identificador que promete una cosa y hace otra no
falla: engaña con confianza.

La regla que este archivo fija: **la pantalla dice lo que sabe, no lo que
supone.** «No hay gasto atado» es un hecho observable; «fue una rotación» es
una inferencia sobre un dato ausente — y la inferencia es falsa siempre que
alguien haya pagado una llanta.

## Límite conocido de este guard

Busca la frase por TEXTO sobre el archivo entero, así que se atrapa también en
un comentario que la cite — y se atrapó: la primera versión del comentario que
explica este arreglo repetía la frase y puso el test en rojo. Es el mismo
tropiezo que `CLAUDE.md` documenta siete veces en una semana.

Se deja así igual, y con el límite escrito: acá la propiedad es «esa frase no
existe en el archivo», y un comentario que la reintroduzca es casi siempre el
primer paso para que alguien la vuelva a poner en la pantalla. El falso
positivo cuesta reescribir una línea de comentario; el falso negativo cuesta
una pantalla que miente.
"""
from pathlib import Path

JS = (Path(__file__).resolve().parents[2] / 'app' / 'static' / 'pwa'
      / 'flota.js')


def _fuente():
    return JS.read_text(encoding='utf-8')


def test_no_afirma_rotacion_cuando_no_hay_gasto():
    s = _fuente()
    assert 'sin gasto asociado (rotación)' not in s, (
        'la fila de montaje volvió a afirmar que fue una rotación. No lo '
        'sabe: `gasto_id` es inalcanzable desde el formulario, así que es '
        'nulo SIEMPRE — también cuando hubo factura.')


def test_sigue_diciendo_lo_que_si_sabe():
    """La otra mitad: quitar la mentira no puede dejar la fila muda. Que no
    haya gasto atado es información real y hay que seguir dándola."""
    s = _fuente()
    assert 'sin gasto atado' in s, (
        'la fila dejó de decir que el montaje no tiene gasto atado — eso sí '
        'es un hecho, y quien audita el costo de las llantas lo necesita')


def test_cuando_SI_hay_gasto_lo_dice():
    s = _fuente()
    assert 'factura registrada' in s, (
        'la fila ya no distingue el montaje con factura del que no la tiene')


def test_la_rama_sigue_colgando_de_gasto_id():
    """Si alguien cambia el criterio a otra cosa —la observación, el motivo—
    la fila volvería a inferir en vez de observar."""
    s = _fuente()
    i = s.index('const doc = m.gasto_id')
    assert 'factura registrada' in s[i:i + 400], (
        'la rama que decide el texto dejó de mirar `gasto_id`')
