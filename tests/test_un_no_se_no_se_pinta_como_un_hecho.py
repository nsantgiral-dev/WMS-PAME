"""Trinquete: un fetch que falla no se convierte en «no hay nada».

El 2026-09-21 la misma clase apareció dos veces el mismo día, en dos pantallas
que nadie relacionaba:

| Dónde | Qué producía el hueco | Qué afirmaba la pantalla |
|---|---|---|
| Lista de conductores | la redacción de `usuario_email` por privacidad | «Sin cuenta PWA — no puede entrar a la app», sobre los 3 conductores activos, que sí tienen |
| Manifiesto de ruta | `.catch(() => ({paradas: []}))` sobre un 403 | «Sin paradas registradas», sobre la ruta 31, que tiene 3 |

Arreglar los dos casos cierra dos instancias. La clase es
**«un no sé pintado como un hecho»**, y es peor que un error: un error se
reporta, «no hay» se cree. Es la Regla 0 del lado de la pantalla — ante dato
ausente, declararlo, no rellenarlo con silencio.

## Qué mide este trinquete, y qué NO

Mide el subconjunto que se puede ver desde el código: un `.catch` cuyo cuerpo
entero es una colección vacía fabricada. **No** mide la otra mitad —si la
pantalla además afirma la ausencia—, porque eso vive en el render y el repo no
tiene un parser de JS (`test_frontend_integrity` usa `node --check` y regex).

Así que la lista de abajo es un inventario con motivo escrito, no un veredicto
automático: cada entrada declara **por qué su hueco no miente**. Las dos que
sí mentían ya no están en la lista, y la lista solo puede encoger.

## El escáner se mide a sí mismo

Al escribir esto, el detector marcó `rutas.js:947` — que es **el comentario que
explica por qué se quitó ese `.catch`**. Séptima vez en este repo que un
detector de texto se atrapa en su propia prosa. Por eso `_sin_comentarios()`
existe, y por eso hay un test que le pone una forma a mano y exige que la vea
en código y **no** la vea en un comentario.
"""
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

#: `.catch(() => <coleccion vacía>)` — el fetch falló y alguien fabricó el
#: resultado. La forma se escribe de varias maneras y la regex las cubre:
#: `() =>`, `_ =>`, `e =>`; `[]` o `({...})`.
FABRICA_VACIO = re.compile(
    r"\.catch\(\s*(?:\(\s*\)|_|e)\s*=>\s*(\[\s*\]|\(\s*\{[^{}]*\}\s*\))\s*\)")

#: Inventario declarado. Clave `archivo:símbolo`, valor: por qué su hueco NO
#: afirma nada falso. **Esta lista solo encoge.** Agregar una entrada es una
#: decisión que se escribe; quitar una es haber arreglado la pantalla.
HUECOS_QUE_NO_MIENTEN = {
    'app.js:pedidos': (
        'Panel secundario del tablero. La pantalla pinta la lista y no escribe '
        'ninguna frase sobre su tamaño — un cero se ve como un panel vacío, no '
        'como una afirmación. Si alguien le agrega «No hay pedidos», sale de '
        'esta lista.'),
    'compras_ia.js:proveedores': (
        'Llena un desplegable de filtro. Un desplegable corto es visiblemente '
        'un desplegable corto; no afirma que no existan proveedores.'),
    'recepcion.js:ordenes': (
        'Las OCs de Siesa se recargan solas cada ciclo y la pantalla tiene su '
        'propio botón de refrescar: el hueco dura hasta el siguiente barrido.'),
    'recepcion.js:recepciones_en_proceso': (
        'Lista de trabajo propia del recepcionista, que sabe si dejó algo a '
        'medias — es la única pantalla cuyo lector tiene la verdad a mano.'),
    'recepcion.js:recepciones_confirmadas': (
        'Paginada: el fallback conserva `pagina_actual`, así que la pantalla '
        'sigue diciendo en qué página está en vez de saltar a un estado nuevo.'),
    'recepcion.js:productos_busqueda': (
        'Autocompletado. «Sin resultados» en un buscador es una respuesta '
        'sobre la CONSULTA, no sobre el inventario.'),
    'recepcion.js:bultos_rechazados': (
        'Enriquecimiento de una lista que ya se pintó con su propia fuente: el '
        'hueco quita un adorno, no cambia el conteo.'),
    'tienda.js:ordenes_oc': (
        'Vista de punto de venta sobre datos de Siesa, con recarga manual.'),
}

#: Piso mínimo: si el escáner se rompe devuelve cero, y un cero se lee igual
#: que «acá no hay nada que hacer». Este número baja solo cuando alguien
#: arregla una pantalla — nunca porque el detector dejó de ver.
PISO = 6


def _sin_comentarios(txt: str) -> str:
    """Quita `//…` y `/*…*/` conservando las posiciones de línea.

    Sin esto el detector se encuentra a sí mismo: el comentario que explica
    por qué se quitó un `.catch` contiene el `.catch`.
    """
    txt = re.sub(r'/\*.*?\*/', lambda m: re.sub(r'[^\n]', ' ', m.group(0)),
                 txt, flags=re.S)
    salida = []
    for linea in txt.split('\n'):
        # No se intenta distinguir un `//` dentro de un string: acá solo
        # importa que no queden marcas de comentario, y un falso recorte hace
        # que el detector vea MENOS, lo que el piso mínimo atrapa.
        i = linea.find('//')
        salida.append(linea if i < 0 else linea[:i])
    return '\n'.join(salida)


def _encontrados():
    """`[(archivo, línea, texto)]` de cada hueco fabricado, sin comentarios."""
    fuera = []
    for f in sorted(PWA.glob('*.js')):
        if f.name == 'sw.js':
            continue
        txt = _sin_comentarios(f.read_text(encoding='utf-8'))
        for m in FABRICA_VACIO.finditer(txt):
            cuerpo = m.group(1)
            if '[]' not in cuerpo and not re.search(r':\s*(0|\{\})', cuerpo):
                continue
            fuera.append((f.name, txt[:m.start()].count('\n') + 1, m.group(0)))
    return fuera


class TestElEscanerVeLoQueDiceVer:
    """Un trinquete que se desincroniza devuelve cero, y un cero se lee igual
    que «no hay nada que hacer»."""

    def test_encuentra_al_menos_el_piso(self):
        n = len(_encontrados())
        assert n >= PISO, (
            f'el escáner encontró {n} huecos y el piso es {PISO}: o alguien '
            f'arregló pantallas y no bajó el piso, o el detector se rompió')

    def test_ve_la_forma_en_codigo(self, tmp_path):
        """El canario: se le pone la forma delante y tiene que verla."""
        assert FABRICA_VACIO.search("const d = await get('/x').catch(() => ({ paradas: [] }));")
        assert FABRICA_VACIO.search("get('/x').catch(_ => []) ")
        assert FABRICA_VACIO.search("get('/x').catch(e => ({ a: [], total: 0 }))")

    def test_NO_la_ve_en_un_comentario(self):
        """La otra dirección, que es la que este repo falló siete veces.

        El comentario de `rutaVerManifiesto` cita el `.catch` que se quitó.
        Un detector que lo cuenta reporta un defecto ya arreglado, y quien lo
        lea va a ir a arreglar un comentario."""
        codigo = ("// se quitó el .catch(() => ({ paradas: [] })) de acá\n"
                  "const d = await get('/x');\n")
        assert FABRICA_VACIO.search(codigo), 'control: sin limpiar sí se ve'
        assert not FABRICA_VACIO.search(_sin_comentarios(codigo)), (
            'el detector se está atrapando en la prosa que explica el arreglo')

    def test_NO_marca_un_catch_que_no_fabrica_nada(self):
        """Un detector que marca todo prueba la mitad."""
        for sano in ("get('/x').catch(e => { alerta(e.message); })",
                     "get('/x').catch(() => null)",
                     "get('/x').then(d => d, e => ({ ok: false, status: e.status }))"):
            assert not FABRICA_VACIO.search(sano), f'falso positivo sobre: {sano}'


class TestElInventarioSoloEncoge:

    def test_no_aparecio_un_hueco_sin_declarar(self):
        """El trinquete. Un `.catch` nuevo que fabrica una lista vacía entra
        con su motivo escrito o no entra."""
        n = len(_encontrados())
        assert n <= len(HUECOS_QUE_NO_MIENTEN), (
            f'hay {n} huecos fabricados y solo {len(HUECOS_QUE_NO_MIENTEN)} '
            f'declarados. Antes de agregar el tuyo a `HUECOS_QUE_NO_MIENTEN`, '
            f'contestá: ¿la pantalla escribe alguna frase sobre el tamaño de '
            f'esa colección? Si la escribe, el arreglo es el render, no la '
            f'lista.\nEncontrados:\n' +
            '\n'.join(f'  {a}:{l}  {t[:70]}' for a, l, t in _encontrados()))

    @pytest.mark.parametrize('clave', sorted(HUECOS_QUE_NO_MIENTEN))
    def test_cada_entrada_dice_por_que(self, clave):
        motivo = HUECOS_QUE_NO_MIENTEN[clave]
        assert len(motivo) > 60, (
            f'{clave} está declarada sin un motivo real. Una lista de '
            f'excepciones sin razones se vuelve una lista de permisos.')


class TestLasDosQueSiMentianNoVolvieron:
    """Las dos instancias que originaron el trinquete, cada una por su forma
    propia. Los tests de render ya las cubren ejecutando; esto es el cierre
    barato que no depende de node."""

    def test_el_manifiesto_no_fabrica_paradas(self):
        txt = _sin_comentarios((PWA / 'rutas.js').read_text(encoding='utf-8'))
        assert 'paradas: []' not in txt or 'ok: false' in txt, (
            'volvió la lista de paradas fabricada sin declarar el fallo')
        for _, _, t in _encontrados():
            assert 'paradas' not in t, f'volvió el hueco de paradas: {t}'

    def test_la_lista_de_conductores_pregunta_por_el_hecho(self):
        """Sobre la RAMA que escribe el aviso, no sobre el archivo.

        La primera versión de este test exigía que `conCuentaPwa(c)`
        apareciera en `rutas.js` — y aparece dos veces: en el aviso y en la
        condición del botón. Revertir solo el aviso lo dejaba en verde. Es
        justo el modo de fallo que el repo ya tiene escrito: el guard medía
        una copia cuando la propiedad era de otra."""
        txt = _sin_comentarios((PWA / 'rutas.js').read_text(encoding='utf-8'))
        assert '${conCuentaPwa(c)\n              ? ' in txt, (
            'el aviso «Sin cuenta PWA» volvió a decidirse leyendo un campo '
            'que la redacción de privacidad borra')
        assert '${c.usuario_email\n              ?' not in txt
        assert '(conCuentaPwa(c) || !puedeCrearCuentaPwa())' in txt, (
            'el botón «Crear cuenta PWA» volvió a ofrecerse a quien recibe '
            '403 al usarlo')
