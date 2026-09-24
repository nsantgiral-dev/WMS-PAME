"""Trinquete: **todo texto de la PWA se lee, en los dos temas.**

## El error que este archivo hace imposible repetir

El dueño lo reportó con capturas (2026-09-24): en tema claro, el tablero del
líder de Inventario Cíclico mostraba el aviso de rezago —«4.862 pendientes = 81
días de cupo…»— con **fondo ámbar pálido y texto ámbar pálido**. Ilegible justo
en el aviso que dice que el generador está parado.

El caso fue ese. La CLASE es otra y es la que se cierra acá: **un color de texto
elegido a mano que no sabe sobre qué fondo se pinta.** La PWA arma su HTML con
estilos inline, y cada módulo escribía `color:#fde68a`, `#555`, `#415A70`…
pensados para el fondo oscuro. El tema claro los parcheaba con selectores
`body.light [style*="color:#…"]` sueltos; lo que nadie parcheó quedó ilegible,
y **no había nada que lo midiera**: el guard del Nivel 4 de
`test_frontend_integrity` mira fondos, no texto.

Tres propiedades, tres trinquetes:

1. **Los tokens pasan WCAG 2.2 AA** (4.5:1) en los dos temas. Se calcula el
   contraste leyendo `index.html`, no se confía en un comentario.
2. **Nada bajo 12px.** Ni inline ni en reglas ni en variables de tamaño.
3. **Ningún color de texto hex nuevo.** El texto se pinta con token
   (`var(--tx2)`, `var(--warn-tx)`…), que cada tema define. Se admite hex solo
   cuando **el mismo estilo declara su fondo** —un botón sólido, una pastilla
   blanca de cámara—, y ese par fijo se mide también.

Lo que quedó sin convertir está declarado por archivo, con su porqué, y la
lista **solo encoge** (la forma de `test_inventario_no_se_resta_sin_destino`).

## Lo que este guard NO ve — dicho para que nadie lo suponga cubierto

- Colores que llegan por variable: `color:${e.badge}` con `e` de un mapa
  `{badge:'#b45309'}`. El detector ve el hex literal dentro de `color:${…}`, no
  el que viaja por un identificador.
- Canvas (Chart.js): sus `ticks.color` no son CSS.
- Tamaños calculados: `font-size:${n}px`.
- Un token sano sobre un fondo hex que el tema claro no convierte. El Nivel 4
  de `test_frontend_integrity` obliga a declarar todo fondo oscuro; este
  archivo mide el texto.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
AA = 4.5


# ═════════════════════════════════════════════════════════════════════════════
# Contraste WCAG
# ═════════════════════════════════════════════════════════════════════════════

def _rgba(hexa):
    h = hexa.strip().lstrip('#')
    if len(h) in (3, 4):
        h = ''.join(c * 2 for c in h)
    if len(h) not in (6, 8):
        raise ValueError(f'color no hex: {hexa!r}')
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    return r, g, b, a


def _sobre(frente, fondo):
    """`frente` (con alpha) compuesto sobre `fondo` opaco → hex opaco."""
    r, g, b, a = _rgba(frente)
    R, G, B, _ = _rgba(fondo)
    return '#' + ''.join(f'{round(a * x + (1 - a) * y):02x}' for x, y in ((r, R), (g, G), (b, B)))


def _luminancia(hexa):
    r, g, b, _ = _rgba(hexa)

    def canal(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * canal(r) + 0.7152 * canal(g) + 0.0722 * canal(b)


def contraste(a, b):
    la, lb = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ═════════════════════════════════════════════════════════════════════════════
# 1 · Los tokens de index.html pasan AA en los dos temas
# ═════════════════════════════════════════════════════════════════════════════

_DECL = re.compile(r'(--[\w-]+)\s*:\s*([^;]+);')


def _bloque(css, selector):
    """Las declaraciones de la regla cuyo selector EMPIEZA con `selector`
    (admite una lista: `body.light, #cond-contenido, .tema-claro-fijo {`)."""
    m = re.search(r'(?m)^\s*' + re.escape(selector) + r'\s*(?:,[^{};]*)?\{', css)
    if not m:
        return ''
    fin = css.index('}', m.end())
    return re.sub(r'/\*.*?\*/', '', css[m.end():fin], flags=re.S)


def _tokens_por_tema(html):
    """{'oscuro': {...}, 'claro': {...}} con cada `var()` resuelto y el alpha
    compuesto sobre el `--bg` del tema. El claro hereda del oscuro lo que no
    redefine, igual que en el navegador (`body.light` está debajo de `:root`)."""
    raiz = dict(_DECL.findall(_bloque(html, ':root')))
    claro = {**raiz, **dict(_DECL.findall(_bloque(html, 'body.light')))}

    def resolver(tabla):
        salida = {}
        for k in tabla:
            v, vistos = tabla[k].strip(), set()
            while v.startswith('var('):
                ref = re.match(r'var\((--[\w-]+)\)', v).group(1)
                if ref in vistos:
                    break
                vistos.add(ref)
                v = tabla.get(ref, '').strip()
            salida[k] = v
        bg = salida.get('--bg', '')
        for k, v in list(salida.items()):
            if re.fullmatch(r'#[0-9a-fA-F]{8}', v) and bg.startswith('#'):
                salida[k] = _sobre(v, bg)
        return salida
    return {'oscuro': resolver(raiz), 'claro': resolver(claro)}


SUPERFICIES = ('--bg', '--bg-s', '--bg-s2', '--bg-input')
ESTADOS = ('warn', 'ok', 'err', 'info', 'lila', 'acento')
SEMAFORO = {'--green': '--gbg', '--red': '--rbg', '--yellow': '--ybg',
            '--blue': '--bbg', '--purple': '--pbg', '--orange': '--obg'}


def _pares():
    """(texto, fondo) que la PWA pinta de verdad. `--tx3` entra con el mismo
    4.5:1 que los otros dos: el terciario también es texto que alguien lee."""
    pares = [(t, s) for t in ('--tx', '--tx2', '--tx3') for s in SUPERFICIES]
    for e in ESTADOS:
        pares += [(f'--{e}-tx', f'--{e}-bg'), (f'--{e}-tx', '--bg'), (f'--{e}-tx', '--bg-s')]
    for c, fondo in SEMAFORO.items():
        pares += [(c, fondo), (c, '--bg'), (c, '--bg-s')]
    return pares


def _pares_bajo_aa(html):
    malos, medidos = [], 0
    for tema, tabla in _tokens_por_tema(html).items():
        for t, f in _pares():
            a, b = tabla.get(t), tabla.get(f)
            if not (a and b and a.startswith('#') and b.startswith('#')):
                malos.append((tema, t, f, 'sin valor hex resoluble'))
                continue
            medidos += 1
            r = contraste(a, b)
            if r < AA:
                malos.append((tema, t, f, f'{a} sobre {b} = {r:.2f}:1'))
    return malos, medidos


def _html():
    return (PWA / 'index.html').read_text(encoding='utf-8')


class TestLosTokensPasanAA:

    def test_todo_par_texto_fondo_pasa_4_5_en_los_dos_temas(self):
        malos, _ = _pares_bajo_aa(_html())
        assert not malos, (
            '\nPares de tokens bajo WCAG 2.2 AA (4.5:1):\n'
            + '\n'.join(f'  · [{t}] {a} sobre {b}: {d}' for t, a, b, d in malos)
            + '\n\nOscurecé (tema claro) o aclarás (tema oscuro) el texto. No '
              'bajes el umbral: 4.5 es el piso de texto normal.')

    def test_piso_de_pares_medidos(self):
        """Un parser que no encuentra `:root` devuelve cero pares y cero
        fallas. Ese cero no puede leerse como «todo pasa»."""
        _, medidos = _pares_bajo_aa(_html())
        assert medidos >= 2 * 45, f'se midieron {medidos} pares: ¿cambió el formato de los tokens?'

    def test_el_tema_claro_redefine_los_pares_de_estado(self):
        """Si `body.light` olvida un `--X-tx`, hereda el del oscuro (claro sobre
        claro) y el par igual podría «pasar» contra un fondo que tampoco se
        redefinió. Cada token de estado tiene que existir en los dos bloques."""
        claro = dict(_DECL.findall(_bloque(_html(), 'body.light')))
        faltan = [f'--{e}-{p}' for e in ESTADOS for p in ('bg', 'tx', 'brd')
                  if f'--{e}-{p}' not in claro]
        faltan += [t for t in ('--tx', '--tx2', '--tx3', *SUPERFICIES) if t not in claro]
        assert not faltan, f'body.light no redefine: {faltan}'


class TestElMedidorDeContrasteMuerde:
    """Meta-tests: el cálculo y el parser, contra valores conocidos y contra un
    `index.html` roto a propósito (en memoria)."""

    def test_valores_de_referencia_wcag(self):
        assert round(contraste('#000', '#fff'), 1) == 21.0
        assert round(contraste('#777', '#fff'), 2) == 4.48       # el clásico que NO pasa
        assert contraste('#767676', '#fff') >= 4.5              # el que sí

    def test_ve_un_texto_igual_a_su_fondo(self):
        roto = re.sub(r'--warn-tx:\s*#92400E', '--warn-tx:   #FFFBEB', _html(), count=1)
        assert roto != _html(), 'la mutación no encontró el token claro'
        malos, _ = _pares_bajo_aa(roto)
        assert any(t == 'claro' and a == '--warn-tx' for t, a, _b, _d in malos), malos

    def test_ve_un_token_que_desaparece(self):
        roto = re.sub(r'--tx3:\s*#7F98B0;', '', _html(), count=1)
        assert roto != _html()
        malos, _ = _pares_bajo_aa(roto)
        assert any(a == '--tx3' for _t, a, _b, _d in malos), malos

    def test_compone_el_alpha_sobre_el_fondo(self):
        assert _sobre('#ffffff80', '#000000') in ('#808080', '#7f7f7f')


# ═════════════════════════════════════════════════════════════════════════════
# Fuentes: qué se escanea
# ═════════════════════════════════════════════════════════════════════════════

def _sin_comentarios_css_html(texto):
    texto = re.sub(r'<!--.*?-->', lambda m: ' ' * len(m.group(0)), texto, flags=re.S)
    return re.sub(r'(?m)^[ \t]*/\*.*?\*/', lambda m: ' ' * len(m.group(0)), texto, flags=re.S)


def _sin_comentarios_js(texto):
    """Solo los comentarios que empiezan una línea: `//` y `/* … */`/JSDoc.
    Un `//` dentro de una URL o un regex no es comentario, y cortarlo ahí
    desincroniza el escáner (lección de `TestNingunDatoLlegaCrudoAlInnerHTML`)."""
    texto = re.sub(r'(?m)^[ \t]*/\*.*?\*/', lambda m: re.sub(r'[^\n]', ' ', m.group(0)), texto, flags=re.S)
    return re.sub(r'(?m)^[ \t]*//[^\n]*', lambda m: ' ' * len(m.group(0)), texto)


def _fuentes():
    """{archivo: texto sin comentarios}. De `index.html` se saca el bloque
    `@media print` (etiquetas térmicas de 80mm: papel, no pantalla) y los
    selectores `[style*="…"]` del tema claro, que citan colores sin pintarlos."""
    salida = {}
    for f in sorted(PWA.glob('*.js')):
        salida[f.name] = _sin_comentarios_js(f.read_text(encoding='utf-8'))
    html = _sin_comentarios_css_html(_html())
    i = html.index('@media print')
    fin = html.index('</style>', i)
    html = html[:i] + ' ' * (fin - i) + html[fin:]
    html = re.sub(r'\[style\*="[^"]*"\]', lambda m: ' ' * len(m.group(0)), html)
    salida['index.html'] = html
    return salida


_FUNCION = re.compile(r'(?m)^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(')


def _funcion_en(texto, pos):
    """Nombre de la función de primer nivel que contiene `pos` ('' si ninguna)."""
    nombre = ''
    for m in _FUNCION.finditer(texto, 0, pos):
        nombre = m.group(1)
    return nombre


#: Funciones que arman un documento de IMPRESIÓN (etiqueta térmica, planilla,
#: manifiesto). Se imprimen en papel a tamaño físico: 9px en una etiqueta de
#: 80mm es un tamaño de impresora, no de pantalla, y el color es tinta negra
#: sobre papel blanco en los dos temas. Solo encoge.
IMPRESION = {
    'app.js::imprimirDocumento': 'Abre la ventana de impresión del documento de Siesa (remisión/factura).',
    'rutas.js::muelleImprimirManifiesto': 'Planilla de cargue impresa en carta: tabla densa en papel.',
    'temporada.js::temporadaExportar': 'Exporta la propuesta de temporada a una hoja imprimible (acta).',
    'etiquetas.js::etqImprimir': 'Etiqueta de producto para la impresora térmica.',
    'packing.js::imprimirEtiquetaLPN': 'Etiqueta LPN para la impresora térmica.',
    'packing.js::empImprimirEtiquetas': 'Etiqueta de bulto para la impresora térmica.',
    'layout.js::layoutImprimirEtiquetasCuerpo': 'Etiquetas de ubicación para la impresora térmica.',
}


def _es_impresion(archivo, texto, pos):
    return f'{archivo}::{_funcion_en(texto, pos)}' in IMPRESION


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Nada bajo 12px
# ═════════════════════════════════════════════════════════════════════════════

_FS = re.compile(r'(?<![-\w])font-size\s*:\s*(\d+(?:\.\d+)?)px')
_FS_VAR = re.compile(r'(--[\w-]*(?:font|fs)[\w-]*)\s*:\s*(\d+(?:\.\d+)?)px')
PISO_PX = 12


def _fuentes_chicas(fuentes=None):
    """([(archivo, funcion, px)], total_medido). Cuenta también las variables
    de tamaño (`--esq-font-hueco: 8px`): la regla que las usa dice
    `var(--…)` y un escáner de `font-size:Npx` no las vería."""
    fuentes = _fuentes() if fuentes is None else fuentes
    chicas, total = [], 0
    for archivo, texto in fuentes.items():
        total += len(re.findall(r'(?<![-\w])font-size\s*:', texto))
        for rx in (_FS, _FS_VAR):
            for m in rx.finditer(texto):
                px = float(m.group(m.lastindex))
                if px >= PISO_PX or _es_impresion(archivo, texto, m.start()):
                    continue
                chicas.append((archivo, _funcion_en(texto, m.start()), px))
    return chicas, total


#: Sitios que todavía pintan bajo 12px, por archivo, con su porqué. Nace
#: vacía: el barrido del 2026-09-24 los llevó todos a la escala `--fs-*`.
FUENTE_CHICA_DECLARADA = {}


class TestNadaBajoDoce:

    def test_ningun_sitio_nuevo_bajo_12px(self):
        chicas, _ = _fuentes_chicas()
        por_archivo = {}
        for a, f, px in chicas:
            por_archivo.setdefault(a, []).append(f'{f or "<global>"}: {px:g}px')
        nuevos = {a: v for a, v in por_archivo.items()
                  if len(v) > FUENTE_CHICA_DECLARADA.get(a, (0, ''))[0]}
        assert not nuevos, (
            '\nTexto bajo 12px:\n'
            + '\n'.join(f'  · {a}: {v}' for a, v in nuevos.items())
            + '\n\nUsá la escala: var(--fs-xs) (12px) para etiquetas, '
              'var(--fs-sm) (14px) para texto secundario, var(--fs-md) (16px) '
              'para cuerpo. Si es un documento impreso, declarará la función '
              'en IMPRESION con su porqué.')

    def test_la_lista_solo_encoge(self):
        chicas, _ = _fuentes_chicas()
        sobran = {a: e for a, (e, _m) in FUENTE_CHICA_DECLARADA.items()
                  if sum(1 for x in chicas if x[0] == a) < e}
        assert not sobran, f'Declaradas de más: {sobran}. Bajá el conteo.'

    def test_piso_de_tamanos_medidos(self):
        """Hoy hay ~2.000 `font-size` en la PWA. Si el escáner ve menos de mil,
        se rompió, y un cero se lee igual que «todo mide 12 o más»."""
        _, total = _fuentes_chicas()
        assert total >= 1000, total


class TestElDetectorDeFuentesMuerde:

    def _en(self, js):
        return _fuentes_chicas({'x.js': js})[0]

    def test_ve_el_inline(self):
        assert self._en('const a = `<div style="font-size:10px;">x</div>`;')

    def test_ve_la_variable_de_tamano(self):
        """El esquema de cuerpo pintaba a 8px por `--esq-font-hueco`."""
        assert self._en(':root { --esq-font-hueco: 8px; }')

    def test_no_marca_la_escala_ni_12px(self):
        assert not self._en('`<div style="font-size:12px;">a</div><b style="font-size:var(--fs-xs)">b</b>`')

    def test_no_confunde_letter_spacing_ni_line_height(self):
        assert not self._en('`<i style="line-height:9px;letter-spacing:1px;min-font-size:3px">`')

    def test_exime_solo_la_impresion_declarada(self):
        js = 'function etqImprimir() {\n  return `<i style="font-size:9px">`;\n}\n'
        assert not _fuentes_chicas({'etiquetas.js': js})[0]
        assert _fuentes_chicas({'otro.js': js})[0], 'la exención es por archivo::función'


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Ningún color de texto hex nuevo
# ═════════════════════════════════════════════════════════════════════════════

_COLOR_HEX = re.compile(r'(?<![-\w])color\s*:\s*(#[0-9a-fA-F]{3,8})\b')
_COLOR_DYN = re.compile(r'(?<![-\w])color\s*:\s*\$\{')
_STYLE_COLOR = re.compile(r'\.style\.color\s*=\s*([\'"])(#[0-9a-fA-F]{3,8})\1')
_FONDO = re.compile(r'(?<![-\w])background(?:-color)?\s*:\s*([^;"\'`}]+)')
_ESTILO_ABRE = re.compile(r'style\s*=\s*(\\?["\'])|cssText\s*=\s*([\'"`])')


def _estilo_de(texto, pos):
    """El conjunto de declaraciones que contiene `pos`: el atributo `style="…"`
    (o el `cssText = '…'`) si `pos` está adentro, o la regla CSS `{…}`."""
    ini = max(0, pos - 2000)
    previo = texto[ini:pos]
    abre = None
    for m in _ESTILO_ABRE.finditer(previo):
        abre = m
    if abre:
        q = (abre.group(1) or abre.group(2))[-1]
        if q not in previo[abre.end():]:
            fin = texto.find(q, pos)
            return texto[ini + abre.end(): fin if fin >= 0 else pos]
    # Un estilo armado en una cadena JS (`const _bt = 'background:…;color:…'`):
    # la cadena de la misma línea que encierra `pos`.
    ini_l = texto.rfind('\n', 0, pos) + 1
    fin_l = texto.find('\n', pos)
    linea = texto[ini_l:fin_l if fin_l >= 0 else len(texto)]
    rel = pos - ini_l
    for i in range(rel - 1, -1, -1):
        q = linea[i]
        if q in '\'"`' and (i == 0 or linea[i - 1] != '\\'):
            j = linea.find(q, rel)
            if j >= 0 and ('style' not in linea[max(0, i - 8):i]):
                return linea[i + 1:j]
            break
    # Una regla CSS (index.html).
    a = texto.rfind('{', 0, pos)
    b = texto.find('}', pos)
    if a >= 0 and b >= 0 and '}' not in texto[a:pos]:
        return texto[a + 1:b]
    return ''


tokens_con_fondo = []


def _textos_hex(fuentes=None):
    """([(archivo, funcion, hex, fondo_del_mismo_estilo | None)], total_color).

    Tres formas de escribir lo mismo, y un regex de `color:#` ve una sola:
    `color:#fde68a`, `color:${ok ? '#4ade80' : '#f87171'}` y
    `el.style.color = '#fde68a'`."""
    fuentes = _fuentes() if fuentes is None else fuentes
    hallados, total = [], 0
    tokens_con_fondo.clear()
    for archivo, texto in fuentes.items():
        total += len(re.findall(r'(?<![-\w])color\s*:', texto))
        sitios = [(m.start(), m.group(1), True) for m in _COLOR_HEX.finditer(texto)]
        for m in _COLOR_DYN.finditer(texto):
            j, prof = m.end(), 1
            while j < len(texto) and prof:
                prof += {'{': 1, '}': -1}.get(texto[j], 0)
                j += 1
            sitios += [(m.start(), h, True) for h in re.findall(r'[\'"](#[0-9a-fA-F]{3,8})[\'"]', texto[m.end():j])]
        # `el.style.color = …` no tiene «mismo estilo»: nunca declara su fondo.
        sitios += [(m.start(), m.group(2), False) for m in _STYLE_COLOR.finditer(texto)]
        for pos, h, en_estilo in sitios:
            if _es_impresion(archivo, texto, pos):
                continue
            fondo = _FONDO.search(_estilo_de(texto, pos)) if en_estilo else None
            hallados.append((archivo, _funcion_en(texto, pos), h.lower(),
                             fondo.group(1).strip() if fondo else None))
        # El par con texto por token y fondo declarado: la otra mitad de la
        # medición de pares fijos (`background:#fff;color:var(--tx)` en tema
        # oscuro es texto claro sobre blanco).
        for m in re.finditer(r'(?<![-\w])color\s*:\s*(var\(--[\w-]+\))', texto):
            if _es_impresion(archivo, texto, m.start()):
                continue
            fondo = _FONDO.search(_estilo_de(texto, m.start()))
            if fondo:
                tokens_con_fondo.append((archivo, _funcion_en(texto, m.start()),
                                         m.group(1), fondo.group(1).strip()))
    return hallados, total


def _sin_fondo_propio(hallados):
    por_archivo = {}
    for a, f, h, fondo in hallados:
        if fondo is None:
            por_archivo.setdefault(a, []).append(f'{f or "<global>"}: {h}')
    return por_archivo


#: Texto con hex que NO declara su fondo en el mismo estilo, por archivo. Solo
#: encoge. Lo que queda es texto sobre un fondo que el propio archivo fija por
#: otro lado — el ancestro es un relleno sólido o una pantalla de paleta fija —
#: y por eso el barrido del 2026-09-24 no lo convirtió a ciegas.
COLOR_HEX_DECLARADO = {
    'index.html': (
        7,
        'Texto sobre un relleno que pone el ANCESTRO, no el propio estilo: el '
        'selector «Pedir desde» (blanco y #e6f4f6 sobre --pm-fill, 4.75:1), las '
        'dos cabeceras del esquema de cuerpo (blanco sobre el color de zona que '
        'JS asigna al abrir el detalle) y el estado de la barra de sincronización '
        'del conductor (#b45309 sobre la barra #fffbeb, fija en los dos temas).'),
}


class TestNingunTextoConHexNuevo:

    def test_ningun_hex_de_texto_sin_su_fondo(self):
        actual = _sin_fondo_propio(_textos_hex()[0])
        nuevos = {a: v for a, v in actual.items()
                  if len(v) > COLOR_HEX_DECLARADO.get(a, (0, ''))[0]}
        assert not nuevos, (
            '\nColor de texto hex sin fondo declarado en el mismo estilo:\n'
            + '\n'.join(f'  · {a} ({len(v)}): {v[:8]}{" …" if len(v) > 8 else ""}'
                        for a, v in nuevos.items())
            + '\n\nUn hex no sabe sobre qué fondo se pinta: en el otro tema queda '
              'ilegible. Usá un token — var(--tx), var(--tx2), var(--tx3), '
              'var(--warn-tx), var(--ok-tx), var(--err-tx), var(--info-tx), '
              'var(--lila-tx), var(--acento-tx) — o declará el fondo en el mismo '
              'estilo (un relleno sólido), y ese par se mide abajo.')

    def test_la_lista_solo_encoge(self):
        actual = _sin_fondo_propio(_textos_hex()[0])
        sobran = {a: e for a, (e, _m) in COLOR_HEX_DECLARADO.items()
                  if len(actual.get(a, [])) < e}
        assert not sobran, f'Declarados de más: {sobran}. Bajá el conteo.'

    def test_toda_declaracion_dice_por_que(self):
        flojas = [a for a, (_n, m) in COLOR_HEX_DECLARADO.items() if len(m) < 60]
        assert not flojas, flojas

    def test_piso_de_colores_medidos(self):
        _, total = _textos_hex()
        assert total >= 1500, f'solo {total} `color:` vistos: ¿se rompió el escáner?'


class TestElDetectorDeHexMuerde:

    def _sin_fondo(self, js):
        return _sin_fondo_propio(_textos_hex({'x.js': js})[0])

    def test_ve_el_caso_del_aviso_de_rezago(self):
        assert self._sin_fondo('`<div style="font-size:12px;color:#fde68a;">4.862 pendientes</div>`')

    def test_ve_el_hex_dentro_de_un_ternario(self):
        assert self._sin_fondo("`<b style=\"color:${ok ? '#4ade80' : '#f87171'};\">x</b>`")

    def test_ve_la_asignacion_por_style_color(self):
        assert self._sin_fondo("el.style.color = '#fde68a';")

    def test_no_marca_tokens(self):
        assert not self._sin_fondo('`<div style="color:var(--warn-tx);">x</div>`')

    def test_no_confunde_fondo_ni_borde(self):
        assert not self._sin_fondo('`<div style="background-color:#fff;border-color:#333;">x</div>`')

    def test_exime_el_par_declarado_en_el_mismo_estilo(self):
        assert not self._sin_fondo('`<button style="background:#15803d;color:#fff;">OK</button>`')

    def test_el_fondo_de_OTRO_elemento_no_exime(self):
        js = '`<div style="background:#15803d;"></div><span style="color:#fde68a;">x</span>`'
        assert self._sin_fondo(js), 'el estilo se cerró: ese fondo es de otro elemento'


# ═════════════════════════════════════════════════════════════════════════════
# 3b · El par fijo (hex sobre hex en el mismo estilo) también pasa AA
# ═════════════════════════════════════════════════════════════════════════════

def _cubiertos_por_el_tema_claro(html):
    """Fondos hex que el bloque MODO CLARO reemplaza (con `color … !important`):
    en tema claro ese par no se pinta como está escrito."""
    i = html.index('MODO CLARO — neutralizar')
    f = html.index('/* ── Reset ── */')
    return {h.lower() for h in re.findall(r'\[style\*="background:(#[0-9a-fA-F]{3,6})"\]', html[i:f])}


#: Funciones que pintan DENTRO de una zona de paleta clara fija: `#cond-contenido`
#: (la pantalla del conductor, que se lee al sol) o un contenedor
#: `.tema-claro-fijo` (el manifiesto de ruta). Ahí los tokens valen lo del tema
#: claro aunque la app esté en oscuro —la regla `body.light, #cond-contenido,
#: .tema-claro-fijo` de index.html—, y así se miden.
PALETA_CLARA_FIJA = {
    'rutas.js': re.compile(r'^(_?cond[A-Z]\w*|cargarRutasConductor|rutaVerManifiesto)$'),
}


def _en_paleta_clara(archivo, funcion):
    rx = PALETA_CLARA_FIJA.get(archivo)
    return bool(rx and rx.match(funcion or ''))


def _pares_fijos_bajo_aa(fuentes=None):
    """Todo par texto/fondo declarado en el MISMO estilo, medido en cada tema:
    hex sobre hex, hex sobre `var(--…)` (el blanco sobre `var(--pm)`) y
    `var(--…)` sobre hex. Un token se resuelve con el valor de ese tema."""
    html = _html()
    temas = _tokens_por_tema(html)
    cubiertos = _cubiertos_por_el_tema_claro(html)
    hallados, _ = _textos_hex(fuentes)
    pares = [(a, f, h, fondo) for a, f, h, fondo in hallados if fondo] + list(tokens_con_fondo)

    def valor(v, tema):
        v = v.strip()
        if re.fullmatch(r'#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}', v):
            return v
        m = re.fullmatch(r'var\((--[\w-]+)\)', v)
        if m and re.fullmatch(r'#[0-9a-fA-F]{6}', temas[tema].get(m.group(1), '')):
            return temas[tema][m.group(1)]
        return None

    malos, medidos = [], 0
    for a, f, fg, bg in pares:
        bg = bg.split()[0] if bg.startswith('#') else bg
        clara = _en_paleta_clara(a, f)
        for tema in ('oscuro', 'claro'):
            if tema == 'claro' and bg.lower() in cubiertos and not clara:
                continue
            tabla = 'claro' if clara else tema
            x, y = valor(fg, tabla), valor(bg, tabla)
            if not (x and y):
                continue
            medidos += 1
            r = contraste(x, y)
            if r < AA:
                malos.append((a, f, f'[{tema}] {fg} sobre {bg} = {r:.2f}:1'))
    return malos, medidos


#: Pares fijos (texto hex + fondo hex en el mismo estilo) bajo 4.5:1, por
#: archivo. Solo encoge.
PAR_FIJO_BAJO_AA = {}


class TestLaZonaClaraFijaExiste:
    """`PALETA_CLARA_FIJA` hace que el medidor resuelva los tokens de esas
    funciones con el tema claro. Eso solo es verdad si el CSS de verdad les da
    los tokens claros: si alguien saca `#cond-contenido` de la regla, el
    medidor seguiría en verde con el conductor ilegible en tema oscuro."""

    def test_la_regla_del_tema_claro_cubre_las_zonas_fijas(self):
        m = re.search(r'(?m)^\s*(body\.light\s*,[^{]*)\{', _html())
        assert m, 'el bloque de tokens del tema claro ya no es una lista de selectores'
        selectores = {x.strip() for x in m.group(1).split(',')}
        assert {'body.light', '#cond-contenido', '.tema-claro-fijo'} <= selectores, selectores

    def test_el_manifiesto_se_monta_en_la_zona_clara(self):
        js = (PWA / 'rutas.js').read_text(encoding='utf-8')
        i = js.index('function rutaVerManifiesto(')
        cuerpo = js[i:js.index('\n}\n', i)]
        assert "className = 'tema-claro-fijo'" in cuerpo

    def test_el_contenido_del_conductor_existe(self):
        assert 'id="cond-contenido"' in _html()


class TestLosParesFijosPasanAA:

    def test_ningun_par_fijo_nuevo_bajo_aa(self):
        malos, _ = _pares_fijos_bajo_aa()
        por = {}
        for a, f, d in malos:
            por.setdefault(a, []).append(f'{f}: {d}')
        nuevos = {a: v for a, v in por.items() if len(v) > PAR_FIJO_BAJO_AA.get(a, (0, ''))[0]}
        assert not nuevos, (
            '\nPares texto/fondo fijos bajo 4.5:1:\n'
            + '\n'.join(f'  · {a}: {v}' for a, v in nuevos.items())
            + '\n\nOscurecé el relleno (#15803d, #b91c1c, #b45309, #1d4ed8 pasan '
              'con blanco) o usá texto oscuro sobre rellenos claros.')

    def test_la_lista_solo_encoge(self):
        malos, _ = _pares_fijos_bajo_aa()
        sobran = {a: e for a, (e, _m) in PAR_FIJO_BAJO_AA.items()
                  if sum(1 for x in malos if x[0] == a) < e}
        assert not sobran, f'Declarados de más: {sobran}. Bajá el conteo.'

    def test_el_medidor_ve_blanco_sobre_verde_claro(self):
        malos, medidos = _pares_fijos_bajo_aa({'x.js': '`<b style="background:#22c55e;color:#fff;">`'})
        assert medidos == 2 and len(malos) == 2, (medidos, malos)

    def test_el_medidor_resuelve_el_token_del_fondo(self):
        """Blanco sobre `var(--pm)`: el hex no sabe que el token vale #1E8395."""
        malos, _ = _pares_fijos_bajo_aa({'x.js': '`<b style="background:var(--pm);color:#fff;">`'})
        assert malos, 'blanco sobre el teal de marca da 4.43:1'

    def test_el_medidor_ve_el_token_de_texto_sobre_un_blanco_fijo(self):
        malos, _ = _pares_fijos_bajo_aa({'x.js': '`<b style="background:#fff;color:var(--tx);">`'})
        assert any('[oscuro]' in d for _a, _f, d in malos), malos

    def test_no_mide_el_tema_claro_de_un_fondo_que_el_tema_claro_reemplaza(self):
        malos, medidos = _pares_fijos_bajo_aa({'x.js': '`<b style="background:#7f1d1d;color:#7f1d1d;">`'})
        assert medidos == 1 and malos

    def test_piso_de_pares_fijos_medidos(self):
        _, medidos = _pares_fijos_bajo_aa()
        assert medidos >= 150, medidos


# ═════════════════════════════════════════════════════════════════════════════
# 3c · Blanco táctil de 44px en las pantallas del operario
# ═════════════════════════════════════════════════════════════════════════════

PANTALLAS_OPERARIO = ('operario', 'empacador', 'abastecedor', 'recepcion', 'tienda', 'conductor')


class TestBotonesDelOperarioDe44px:

    def test_cada_pantalla_de_operacion_tiene_su_minimo_tactil(self):
        css = _fuentes()['index.html']
        regla = re.search(r'([^{}]*#pantalla-operario button[^{}]*)\{([^}]*)\}', css)
        assert regla, 'no está la regla de blanco táctil de las pantallas del operario'
        selectores, cuerpo = regla.group(1), regla.group(2)
        faltan = [p for p in PANTALLAS_OPERARIO if f'#pantalla-{p} button' not in selectores]
        assert not faltan, faltan
        alto = re.search(r'min-height\s*:\s*(\d+)px', cuerpo)
        assert alto and int(alto.group(1)) >= 44, cuerpo

    def test_las_pantallas_existen(self):
        """Una pantalla renombrada deja el selector apuntando a nada."""
        html = _html()
        assert all(f'id="pantalla-{p}"' in html for p in PANTALLAS_OPERARIO)


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Render real: el aviso de rezago del tablero del líder
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const dir = process.argv.slice(1).filter(a => a !== '--')[0];
const ctx = { console, document: { getElementById: () => null }, window: {} };
vm.createContext(ctx);
for (const f of ['util.js', 'conteo.js']) {
  vm.runInContext(fs.readFileSync(path.join(dir, f), 'utf8'), ctx, { filename: f });
}
const d = {
  almacen: 'NB1', al_dia_operativo: '2026-09-24', fuente: 'x', permisos: { cancelar_rezago: true },
  resumen: { decisiones_pendientes: 0, por_bloque: {} }, decisiones: {}, fuera_del_plan: {},
  hoy: { dia: 'hoy', cerrados: 0, cupo_diario: 60, pendientes_vivas: 4862, por_persona: {} },
  rezago: { hay_aviso: true, a_cancelar: 4000, pendientes_vivas: 4862, cupo_diario: 60,
            dias_de_cupo_pendientes: 81, por_antiguedad_dias: {} },
};
console.log(JSON.stringify({ html: vm.runInContext('liderTableroHtml', ctx)(d) }));
"""


def _tablero():
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(PWA)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])['html']


def _estilo_del_texto(html, frase):
    """El `style` del elemento que contiene `frase` y el del contenedor que
    le pone el fondo (el primer ancestro con `background`)."""
    i = html.index(frase)
    abiertos = []
    for m in re.finditer(r'<(/?)(\w+)([^>]*)>', html[:i]):
        if m.group(1):
            if abiertos:
                abiertos.pop()
        else:
            est = re.search(r'style="([^"]*)"', m.group(3))
            abiertos.append(est.group(1) if est else '')
    propio = abiertos[-1]
    fondo = next((s for s in reversed(abiertos) if _FONDO.search(s)), '')
    return propio, fondo


class TestElAvisoDeRezagoSeLeeEnLosDosTemas:
    """El caso del reporte, renderizado: no se mira el diff sino lo que pinta
    `liderTableroHtml`, y se calcula el contraste con los tokens de cada tema."""

    def _par(self, frase):
        propio, fondo = _estilo_del_texto(_tablero(), frase)
        color = re.search(r'(?<![-\w])color\s*:\s*([^;]+)', propio).group(1).strip()
        bg = _FONDO.search(fondo).group(1).strip()
        return color, bg

    @pytest.mark.parametrize('frase', ['El generador está detenido por rezago', 'días de cupo'])
    def test_texto_y_fondo_son_tokens(self, frase):
        color, bg = self._par(frase)
        assert color.startswith('var(--') and bg.startswith('var(--'), (
            f'«{frase}» se pinta con {color} sobre {bg}: un hex no sabe en qué tema está')

    @pytest.mark.parametrize('frase', ['El generador está detenido por rezago', 'días de cupo'])
    def test_contraste_en_los_dos_temas(self, frase):
        color, bg = self._par(frase)
        tok = lambda v: re.fullmatch(r'var\((--[\w-]+)\)', v).group(1)
        for tema, tabla in _tokens_por_tema(_html()).items():
            r = contraste(tabla[tok(color)], tabla[tok(bg)])
            assert r >= AA, f'[{tema}] «{frase}»: {color} sobre {bg} = {r:.2f}:1'

    def test_el_numero_del_reporte_se_pinta(self):
        """Piso: el arnés pinta el aviso de verdad, con la cifra del reporte."""
        html = _tablero()
        assert '4.862' in html or '4862' in html, html[:400]
