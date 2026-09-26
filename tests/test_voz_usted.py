"""La voz de la aplicación es USTED (decisión del dueño, 2026-09-25).

**La clase, no el caso.** «Revisá la bandeja» en un mensaje del servidor es el
caso. La clase es *un texto que ve una persona, escrito en voseo o en tuteo*:
un error de una ruta, el aviso de un servicio que la pantalla pinta, un correo,
un botón, un placeholder. Había cientos, repartidos entre `app/` (Python),
`flota/` y la PWA, y cada frente nuevo escribía en la voz que le salía.

**Qué se escanea — texto visible, no código ni prosa sobre el código:**

- **Python** (`app/` sin `app/static`, y `flota/`), por AST: todo literal de
  texto (también los trozos de un f-string) **salvo** los docstrings, las
  expresiones sueltas y los argumentos de `logger.*`/`log.*`/`logging.*` y
  `print`. Es más ancho que «los valores de `error`/`mensaje` de un jsonify»
  a propósito: el correo, la excepción cuyo `str()` viaja y la constante de
  mensaje entran sin que alguien tenga que acordarse de listarlos.
- **JS** (`app/static/pwa/*.js`): los literales `'…'`, `"…"` y los tramos de
  texto de las plantillas `` `…` `` (las expresiones `${…}` se lexean como
  código y aportan sus propios literales). Un lexer propio —sin dependencias:
  el CI de Railway no tiene node garantizado— que salta comentarios y
  expresiones regulares. `'a' + 'b'` se lee como una frase.
- **HTML** (`index.html`): nodos de texto, `title`/`placeholder`/`aria-label`/
  `alt`, y el JS de los `<script>` sin `src` y de los manejadores `on*`.
  Comentarios `<!-- -->` y `<style>` fuera.

**El patrón** (`hallazgos`): el voseo por vocabulario generado desde raíces
verbales (imperativo `Revisá`, presente `tenés`, clítico `Pedile`, pretérito
`contaste`, subjuntivo `que lo sumes`) más `_ACENTO_Y_CLITICO` («vaciálo»); el
tuteo por pronombre (`tu`, `te`, `ti`…), formas conjugadas (`puedes`) y
clíticos acentuados generados de las mismas raíces (`búscalo`). El imperativo
tú desnudo (`Revisa`, `Escanea`) solo al empezar una frase o cláusula: en medio
de la frase es tercera persona («el sistema revisa…»). Lo citado entre «»
(el nombre de un botón que habla el operario: «No lo encontré») no cuenta.

**Lo que NO ve, dicho:** texto armado letra a letra; voseo en MAYÚSCULAS
sostenidas (se saltan como siglas); un imperativo tú en medio de una frase
(«…y cierra la caja»); un verbo que no está en las raíces (se agregan cuando
aparece); los textos que viven en la base (motivos escritos por personas,
plantillas guardadas).

**Inventario que solo encoge:** `PENDIENTE_OTRO_FRENTE` — los archivos que
otros frentes están pasando a usted en paralelo (la PWA y `flota/`), con el
número de hallazgos medido. Un archivo que ya no tiene nada se borra de la
lista; uno que baja actualiza su número; ninguno sube. El objetivo es vacío.
"""
import ast
import functools
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'


# ═════════════════════════════════════════════════════════════════════════════
# 1 · El detector
# ═════════════════════════════════════════════════════════════════════════════

#: Raíces de verbos -ar / -er / -ir que la aplicación usa en imperativo. De
#: cada una salen sus formas voseantes y, de las -ar, el clítico tuteante.
_AR = '''revis toc escane prob esper intent reintent verific configur contact llam
seleccion ingres confirm us asign carg recarg registr cerr busc agreg quit cambi
actualiz sac dej avis complet tom guard marc anot consult aprob rechaz valid mir
cont recont cobr cancel baj forz mand indic sincroniz reclasific ajust contest
entr llev pregunt escal liquid despach empac descarg cre borr elimin edit activ
desactiv habilit vincul desvincul declar document report justific explic anul
revers liber fij asegur acord comunic qued olvid ubic identific not arm prepar
separ compar aclar recuper consider gener mostr encontr filtr tir gir pas
import export copi peg arrastr desliz presion puls empez comenz termin
llen vaci sum rest multiplic autoriz desautoriz reasign program dispar
pag rod inici necesit mencion abon desasign'''.split()
_ER = '''pon volv hac ten mov devolv resolv escog recog atend respond entend encend
establec le corr met conoc aprend'''.split()
_IR = '''eleg escrib describ correg segu decid permit recib defin añad compart abr
reabr sub imprim ped repart omit admit emit dirig'''.split()

_CLITICOS = ('lo', 'la', 'le', 'los', 'las', 'les', 'me', 'nos', 'selo', 'sela', 'selos', 'selas')
_REFLEXIVOS = {'fij', 'asegur', 'acord', 'comunic', 'qued', 'olvid', 'ubic', 'identific',
               'registr', 'mov', 'pon', 'dirig', 'sub', 'baj'}

#: Palabras que el español usa para otra cosa y que la generación produce.
_COLISIONES = {'pasas', 'notas', 'tomate', 'ponle', 'tomás', 'ajustes', 'reportes', 'pases',
               'dispares', 'importes', 'contraste', 'desgaste', 'cierres', 'leías',
               'iniciales', 'pages'}


def _formas_voseo():
    f = set()
    for raiz, v, vt in ([(r, 'á', 'a') for r in _AR] + [(r, 'é', 'e') for r in _ER]
                        + [(r, 'í', 'i') for r in _IR]):
        f.add(raiz + v)                                   # Revisá · Poné · Elegí
        f.add(raiz + v + 's')                             # revisás · ponés · elegís
        for c in _CLITICOS:
            f.add(raiz + vt + c)                          # revisalo · ponele · pedile
        f.add(raiz + v + 'selo')                          # pedíselo
        f.add(raiz + ('aste' if v == 'á' else 'iste'))    # contaste · recibiste
        if v == 'á':
            f.add(raiz + 'abas')                          # contabas
            f.add(raiz + 'es')                            # que lo sumes
        else:
            f.add(raiz + 'ías')                           # tenías
        if raiz in _REFLEXIVOS:
            f.add(raiz + vt + 'te')                       # fijate · asegurate
    f |= {'tenés', 'podés', 'querés', 'sabés', 'sos', 'debés', 'hacés', 'ponés', 'decís',
          'venís', 'salís', 'andá', 'vení', 'decí', 'hacé', 'poné', 'tené', 'salí', 'andate',
          'decile', 'decime', 'decilo', 'dale', 'vos', 'dirigite', 'estás', 'vas',
          'pusiste', 'hiciste', 'dijiste', 'tuviste', 'pudiste', 'quisiste', 'fuiste',
          'estuviste', 'pongas', 'hagas', 'digas', 'pidas', 'elijas', 'vuelvas', 'escribas',
          'abras', 'subas', 'recibas', 'cuentes', 'apruebes', 'pruebes'}
    return f - _COLISIONES


#: Imperativo tú de un verbo -ar con clítico («búscalo», «asegúrate»; usted:
#: búsquelo, asegúrese). La tilde va en la última vocal plena de la raíz, o en
#: la abierta del diptongo si el verbo diptonga.
_DIPTONGA = {'prob': 'prueb', 'cont': 'cuent', 'recont': 'recuent', 'cerr': 'cierr',
             'acord': 'acuerd', 'forz': 'fuerz', 'aprob': 'aprueb', 'comenz': 'comienz',
             'empez': 'empiez', 'mostr': 'muestr', 'encontr': 'encuentr', 'rod': 'rued'}
_TILDE = str.maketrans('aeiou', 'áéíóú')


def _con_tilde(raiz):
    if raiz == 'vaci':
        return 'vací'                     # vaciar hace hiato: vacíalo
    raiz = _DIPTONGA.get(raiz, raiz)
    vocales = [k for k, c in enumerate(raiz) if c in 'aeiou']
    if not vocales:
        return None
    k = vocales[-1]
    if k == len(raiz) - 1 and raiz[k] in 'iu' and len(vocales) > 1:
        k = vocales[-2]                   # cambi-a → cámbialo: la i es semivocal
    return raiz[:k] + raiz[k].translate(_TILDE) + raiz[k + 1:]


def _formas_tuteo_clitico():
    f = set()
    for raiz in _AR:
        t = _con_tilde(raiz)
        if not t:
            continue
        for c in ('lo', 'la', 'le', 'los', 'las', 'les', 'me', 'nos'):
            f.add(t + 'a' + c)
        if raiz in _REFLEXIVOS:
            f.add(t + 'ate')
    return f


VOSEO = frozenset(_formas_voseo())
TUTEO = frozenset({
    'tú', 'tu', 'tus', 'te', 'ti', 'contigo', 'tuyo', 'tuya', 'tienes', 'puedes',
    'quieres', 'sabes', 'debes', 'necesitas', 'eres', 'podrás', 'deberás', 'serás',
    'tendrás', 'verás', 'puedas', 'tengas', 'quieras', 'hayas', 'sepas', 'olvides',
    'toques', 'salgas', 'vayas', 'intentes', 'estés',
    'pídele', 'pídeselo', 'asegúrate', 'fíjate', 'comunícate', 'acuérdate', 'dile', 'dime',
    'detén', 'desliza', 'ábrelo', 'ábrela', 'hazlo', 'ponlo', 'ponle', 'dímelo',
    'vuélvelo', 'escríbelo', 'escríbele', 'elígelo',
} | _formas_tuteo_clitico())

#: Imperativo tú desnudo: solo al empezar frase o cláusula. `vuelve`, `mira`,
#: `pide`, `mueve`, `sal` y `ven` NO están: en esta aplicación son casi siempre
#: tercera persona («vuelve a la cola», «pide motivo escrito»).
TUTEO_IMPERATIVO = frozenset({
    'revisa', 'toca', 'escanea', 'elige', 'intenta', 'reintenta', 'verifica', 'selecciona',
    'ingresa', 'confirma', 'usa', 'contacta', 'llama', 'pon', 'haz', 'abre', 'cierra',
    'agrega', 'avisa', 'negocia', 'sube', 'escribe', 'presiona', 'pulsa', 'recarga',
    'busca', 'elimina', 'borra', 'asigna', 'sincroniza', 'contesta', 'anota', 'cambia',
    'completa', 'corrige', 'decide', 'escoge', 'indica', 'recibe', 'define', 'declara',
    'imprime', 'describe', 'aprueba', 'rechaza', 'valida', 'cancela', 'guarda', 'recuerda',
    'olvida', 'espera', 'configura',
})
#: Lo que tiene que seguir al imperativo para no leerlo como adjetivo o
#: tercera persona («· completa», «Volumen — confirma», «espera autorización»).
_SIGUE_PALABRA = re.compile(r"\s+[a-záéíóúñ¿«\"'<]", re.I)
_SIGUE = {'espera': re.compile(r"\s+(?:a\s+que|que)\b"),
          **{v: _SIGUE_PALABRA for v in ('completa', 'confirma', 'valida', 'declara',
                                         'cancela', 'guarda', 'define', 'decide')}}
_SIGUE_CUALQUIERA = re.compile(r'')

_L = 'A-Za-zÁÉÍÓÚáéíóúñÑüÜ'
#: Una palabra que no es parte de un identificador (`tu_rol`, `.prueba-x`,
#: `/api/…`, `#id`, `$x`).
_PALABRA = re.compile(rf'(?<![{_L}0-9_\-/.#$])[{_L}]+(?![{_L}0-9_\-/])')
_CITA = re.compile(r'«[^»]{0,80}»')
#: Empieza frase o cláusula: inicio, puntuación, raya, viñeta, cierre de
#: etiqueta, `\n` escrito, línea en blanco, o «… o …».
_INICIO = re.compile(r"(?:^|[.:;!?¡¿—–(•·>,]|\\n|\n[ \t]*\n|\so)\s*$")
#: «vaciálo»: tilde en la vocal que precede al clítico. En español normativo
#: esa vocal no la lleva (revísalo, revíselo): es voseo.
_ACENTO_Y_CLITICO = re.compile(r'[a-zñ]{3,}[áéí](?:lo|la|le|los|las|les|me|nos|te)$')


def hallazgos(texto):
    """[(palabra, 'voseo' | 'tuteo')] de un texto visible."""
    t = _CITA.sub('«cita»', texto)
    un_token = not re.search(r'\s', texto.strip())
    out = []
    for m in _PALABRA.finditer(t):
        w = m.group(0)
        lw = w.lower()
        if w.isupper() and len(w) > 1:
            continue                      # SIGLAS y CONSTANTES (RECONTAR_TU, TI)
        if _ACENTO_Y_CLITICO.search(lw) or lw in VOSEO:
            out.append((w, 'voseo'))
        elif lw == 'cierres' and re.search(r'\bno\s+$', t[:m.start()], re.I):
            out.append((w, 'tuteo'))      # «No cierres la pestaña»; «los cierres» no
        elif lw in TUTEO:
            out.append((w, 'tuteo'))
        elif (lw in TUTEO_IMPERATIVO and not un_token
              and _INICIO.search(t[:m.start()])
              and _SIGUE.get(lw, _SIGUE_CUALQUIERA).match(t, m.end())):
            out.append((w, 'tuteo'))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Los extractores de texto visible
# ═════════════════════════════════════════════════════════════════════════════

_LOG_METODOS = {'debug', 'info', 'warning', 'warn', 'error', 'exception', 'critical', 'log'}


def _nombre(nodo):
    if isinstance(nodo, ast.Attribute):
        return nodo.attr
    if isinstance(nodo, ast.Name):
        return nodo.id
    return ''


def textos_py(src):
    """[(línea, texto)] de los literales de texto de un módulo Python, sin
    docstrings, sin expresiones sueltas y sin lo que va a un log o a print."""
    arbol = ast.parse(src)
    fuera = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            fuera.add(id(n.value))        # docstrings y cadenas sueltas
        if isinstance(n, ast.Call):
            f = n.func
            es_log = (isinstance(f, ast.Attribute) and f.attr in _LOG_METODOS
                      and 'log' in _nombre(f.value).lower())
            if es_log or _nombre(f) == 'print':
                fuera.update(id(s) for s in ast.walk(n))
    return [(n.lineno, n.value) for n in ast.walk(arbol)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in fuera]


_REGEX_TRAS_PALABRA = {'return', 'typeof', 'case', 'in', 'of', 'new', 'delete', 'void',
                       'throw', 'else', 'do', 'instanceof', 'yield', 'await'}
_REGEX_TRAS_SIGNO = set('(,=:[!&|?{};+-*%<>~^')


class JsIlegible(ValueError):
    """El lexer perdió el hilo. Nunca se traga: un archivo que no se pudo leer
    devolvería cero hallazgos, que se lee igual que «está limpio»."""


def literales_js(src):
    """[(línea, texto)] de los literales de texto de un JS: '…', "…" y los tramos
    de texto de las plantillas. Comentarios y expresiones regulares fuera;
    'a' + 'b' se une en una frase."""
    out = []
    n = len(src)
    i = 0
    pila = []                 # profundidad de llaves al abrir cada ${
    prof = 0
    prev = ''                 # último token significativo ('' = inicio)
    desde = [None]            # tokens desde el último literal (None = ninguno)

    def linea(k):
        return src.count('\n', 0, k) + 1

    def emitir(k, texto, nuevo):
        if nuevo and desde[0] == ['+'] and out:
            ln, anterior = out[-1]
            out[-1] = (ln, anterior + texto)
        else:
            out.append((linea(k), texto))
        desde[0] = []

    def marca(t):
        if desde[0] is not None:
            desde[0].append(t)

    def plantilla(k, nuevo=False):
        buf, ini = [], k
        while k < n:
            c = src[k]
            if c == '\\':
                buf.append(src[k:k + 2])
                k += 2
                continue
            if c == '`':
                emitir(ini, ''.join(buf), nuevo)
                return k + 1, False
            if c == '$' and k + 1 < n and src[k + 1] == '{':
                emitir(ini, ''.join(buf), nuevo)
                return k + 2, True
            buf.append(c)
            k += 1
        raise JsIlegible(f'plantilla sin cerrar (línea {linea(ini)})')

    while i < n:
        c = src[i]
        if c in ' \t\r\n':
            i += 1
            continue
        if src.startswith('//', i):
            j = src.find('\n', i)
            i = n if j == -1 else j
            continue
        if src.startswith('/*', i):
            j = src.find('*/', i + 2)
            if j == -1:
                raise JsIlegible(f'comentario sin cerrar (línea {linea(i)})')
            i = j + 2
            continue
        if c in '"\'':
            j, buf = i + 1, []
            while j < n and src[j] != c:
                if src[j] == '\\':
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if src[j] == '\n':
                    raise JsIlegible(f'texto sin cerrar (línea {linea(i)})')
                buf.append(src[j])
                j += 1
            if j >= n:
                raise JsIlegible(f'texto sin cerrar (línea {linea(i)})')
            emitir(i, ''.join(buf), True)
            i, prev = j + 1, 'lit'
            continue
        if c == '`':
            i, abre = plantilla(i + 1, True)
            if abre:
                pila.append(prof)
                prof += 1
            prev = '{' if abre else 'lit'
            continue
        if c == '/':
            if prev == '' or prev in _REGEX_TRAS_SIGNO or prev in _REGEX_TRAS_PALABRA:
                j, clase = i + 1, False
                while j < n:
                    d = src[j]
                    if d == '\\':
                        j += 2
                        continue
                    if d == '\n':
                        raise JsIlegible(f'expresión regular sin cerrar (línea {linea(i)})')
                    if clase:
                        clase = d != ']'
                    elif d == '[':
                        clase = True
                    elif d == '/':
                        break
                    j += 1
                j += 1
                while j < n and src[j].isalnum():
                    j += 1
                i, prev = j, 'lit'
                marca('re')
                continue
            prev = '/'
            marca('/')
            i += 1
            continue
        if c == '{':
            prof += 1
            prev = '{'
            marca('{')
            i += 1
            continue
        if c == '}':
            prof -= 1
            if pila and prof == pila[-1]:
                pila.pop()
                i, abre = plantilla(i + 1)
                if abre:
                    pila.append(prof)
                    prof += 1
                prev = '{' if abre else 'lit'
                continue
            prev = '}'
            marca('}')
            i += 1
            continue
        if c.isalnum() or c in '_$':
            j = i
            while j < n and (src[j].isalnum() or src[j] in '_$'):
                j += 1
            prev = src[i:j]
            marca(prev)
            i = j
            continue
        prev = c
        marca(c)
        i += 1
    if pila:
        raise JsIlegible('plantilla abierta al final del archivo')
    return out


_ATTR_TEXTO = re.compile(r'\b(?:title|placeholder|aria-label|alt)\s*=\s*"([^"]*)"', re.I)
_ATTR_JS = re.compile(r'\bon[a-z]+\s*=\s*"([^"]*)"', re.I)


def textos_html(src):
    """[(línea, texto)] visibles de un HTML: nodos de texto, atributos que se
    leen y los literales del JS en línea. Comentarios y <style> fuera."""
    limpio = list(src)

    def linea(k):
        return src.count('\n', 0, k) + 1

    def borrar(a, b):
        for k in range(a, b):
            if limpio[k] != '\n':
                limpio[k] = ' '

    out = []
    for m in re.finditer(r'<!--.*?-->', src, re.S):
        borrar(m.start(), m.end())
    for m in re.finditer(r'<style\b.*?</style>', ''.join(limpio), re.S | re.I):
        borrar(m.start(), m.end())
    txt = ''.join(limpio)
    for m in re.finditer(r'<script\b[^>]*>(.*?)</script>', txt, re.S | re.I):
        base = linea(m.start(1)) - 1
        out.extend((base + ln, t) for ln, t in literales_js(m.group(1)))
        borrar(m.start(), m.end())
    txt = ''.join(limpio)
    for m in _ATTR_JS.finditer(txt):
        base = linea(m.start()) - 1
        codigo = m.group(1).replace('&quot;', '"').replace('&#39;', "'")
        out.extend((base + ln, t) for ln, t in literales_js(codigo))
    out.extend((linea(m.start()), m.group(1)) for m in _ATTR_TEXTO.finditer(txt))
    out.extend((linea(m.start(1)), m.group(1)) for m in re.finditer(r'>([^<>]+)<', txt)
               if m.group(1).strip())
    return out


# ═════════════════════════════════════════════════════════════════════════════
# 3 · El corpus
# ═════════════════════════════════════════════════════════════════════════════

def _archivos_py():
    return sorted([p for p in (RAIZ / 'app').rglob('*.py') if 'static' not in p.parts]
                  + list((RAIZ / 'flota').rglob('*.py')))


def _archivos_web():
    return sorted(PWA.glob('*.js')) + [PWA / 'index.html']


def _rel(p):
    return p.relative_to(RAIZ).as_posix()


@functools.lru_cache(maxsize=1)
def _corpus():
    """{archivo: [(línea, texto)]} de todo el texto visible, más cuántos
    textos se leyeron por tipo (el piso)."""
    textos, leidos = {}, {'py': 0, 'js': 0, 'html': 0}
    for p in _archivos_py():
        t = textos_py(p.read_text(encoding='utf-8'))
        textos[_rel(p)] = t
        leidos['py'] += len(t)
    for p in _archivos_web():
        src = p.read_text(encoding='utf-8')
        t = textos_html(src) if p.suffix == '.html' else literales_js(src)
        textos[_rel(p)] = t
        leidos['html' if p.suffix == '.html' else 'js'] += len(t)
    return textos, leidos


@functools.lru_cache(maxsize=1)
def _hallazgos_por_archivo():
    textos, _ = _corpus()
    por = {}
    for archivo, lista in textos.items():
        for ln, texto in lista:
            for palabra, tipo in hallazgos(texto):
                por.setdefault(archivo, []).append((ln, palabra, tipo, texto))
    return por


def _muestra(filas, n=12):
    lineas = []
    for ln, palabra, tipo, texto in filas[:n]:
        k = texto.find(palabra)
        trozo = texto[max(0, k - 40):k + len(palabra) + 40].replace('\n', ' ')
        lineas.append(f'    línea {ln}: {palabra!r} ({tipo}) … {trozo!r}')
    if len(filas) > n:
        lineas.append(f'    … y {len(filas) - n} más')
    return '\n'.join(lineas)


# ═════════════════════════════════════════════════════════════════════════════
# 4 · El trinquete
# ═════════════════════════════════════════════════════════════════════════════

#: Archivos que otro frente está pasando a usted en paralelo (2026-09-26): la
#: PWA (un agente) y `flota/` con `flota*.js` (otro). **El integrador la vacía**
#: al juntar los tres frentes: un archivo sin hallazgos sale de la lista; uno
#: que baja actualiza su número. Número = hallazgos medidos en este worktree.
_MOTIVO_PWA = 'barrido de voz de la PWA (otro agente, en paralelo)'
_MOTIVO_FLOTA = 'barrido de voz de flota/ y flota*.js (otro agente, en paralelo)'
PENDIENTE_OTRO_FRENTE = {}

#: Textos que el detector marca y que NO son la voz de la aplicación. Vacío:
#: un falso positivo se arregla en el detector, no se exime acá.
EXCEPCIONES = {}


class TestNingunTextoVisibleEnVoseoNiTuteo:

    def test_fuera_de_lo_pendiente_no_queda_nada(self):
        por = _hallazgos_por_archivo()
        nuevos = {a: f for a, f in por.items()
                  if a not in PENDIENTE_OTRO_FRENTE and a not in EXCEPCIONES}
        assert not nuevos, (
            'Texto visible en voseo o tuteo. La voz de la aplicación es USTED '
            '(«Revise», «tiene», «su pedido», «pídaselo»):\n'
            + '\n'.join(f'  {a} ({len(f)}):\n{_muestra(f)}' for a, f in sorted(nuevos.items())))

    @pytest.mark.parametrize('archivo', sorted(PENDIENTE_OTRO_FRENTE))
    def test_lo_pendiente_no_crece(self, archivo):
        declarado = PENDIENTE_OTRO_FRENTE[archivo][0]
        hay = _hallazgos_por_archivo().get(archivo, [])
        assert len(hay) <= declarado, (
            f'{archivo}: {len(hay)} hallazgos, declarados {declarado}. Lo pendiente '
            f'solo encoge: el texto nuevo va en usted.\n{_muestra(hay)}')

    @pytest.mark.parametrize('archivo', sorted(PENDIENTE_OTRO_FRENTE))
    def test_lo_pendiente_solo_encoge(self, archivo):
        declarado = PENDIENTE_OTRO_FRENTE[archivo][0]
        hay = len(_hallazgos_por_archivo().get(archivo, []))
        assert hay >= declarado, (
            f'{archivo}: quedan {hay} de {declarado}. '
            + ('Sáquelo de PENDIENTE_OTRO_FRENTE.' if hay == 0
               else f'Baje su número a {hay}.'))

    def test_cada_pendiente_dice_por_que(self):
        for archivo, (n, motivo) in PENDIENTE_OTRO_FRENTE.items():
            assert isinstance(n, int) and n > 0, archivo
            assert motivo and motivo.strip(), archivo
            assert (RAIZ / archivo).exists(), f'{archivo} ya no existe: sáquelo'

    def test_las_excepciones_siguen_vivas(self):
        por = _hallazgos_por_archivo()
        for (archivo, palabra), motivo in EXCEPCIONES.items():
            assert motivo.strip(), (archivo, palabra)
            assert any(p == palabra for _, p, _, _ in por.get(archivo, [])), (
                f'Excepción muerta: {archivo} ya no dice {palabra!r}. Sáquela.')


# ═════════════════════════════════════════════════════════════════════════════
# 5 · El detector se mide (meta-tests y pisos)
# ═════════════════════════════════════════════════════════════════════════════

class TestElDetectorVeLoQueTieneQueVer:

    @pytest.mark.parametrize('texto,palabra', [
        ('Revisá la bandeja', 'Revisá'),
        ('Si no lo tenés, avisá', 'tenés'),
        ('No podés cerrar esta tarea', 'podés'),
        ('Sos el responsable', 'Sos'),
        ('Esa recepción no pertenece a tu punto de venta', 'tu'),
        ('Solo puede enviar tus solicitudes', 'tus'),
        ('Esta tarea no te pertenece', 'te'),
        ('Esta tarea no está asignada a ti', 'ti'),
        ('Pedile a administración que la vincule', 'Pedile'),
        ('pedíselo al encargado', 'pedíselo'),
        ('vaciálo primero', 'vaciálo'),
        ('Escanealo con la caja vacía', 'Escanealo'),
        ('Si no encontraste el producto', 'encontraste'),
        ('mientras contabas', 'contabas'),
        ('para que lo sumes acá', 'sumes'),
        ('No puedes cambiar su rol', 'puedes'),
        ('Producto no encontrado — búscalo por nombre', 'búscalo'),
        ('Asegúrate de cerrar', 'Asegúrate'),
        ('Escanea el código LPN primero', 'Escanea'),
        ('Error interno — reintenta', 'reintenta'),
        ('Sync ya en proceso — espera que termine', 'espera'),
        ('Si se usa trigger_despacho, agrega la variable', 'agrega'),
        ('Verifica que la OC exista o asigna el código', 'asigna'),
        ('No cierres esta pantalla', 'cierres'),
        ('Clase inválida. Usa A, B o C.', 'Usa'),
        ('<div>Selecciona una ruta</div>', 'Selecciona'),
        ('Contado contraentrega — cobrá al entregar', 'cobrá'),
        ('Recontá este producto con cuidado', 'Recontá'),
    ])
    def test_ve(self, texto, palabra):
        assert palabra in [w for w, _ in hallazgos(texto)], (texto, hallazgos(texto))

    @pytest.mark.parametrize('texto', [
        'La tarea está asignada, acá y allá, ya',
        'Bogotá, Santo Tomás, Pulí, Chiquinquirá, Ibagué',
        'Revise la bandeja y tenga en cuenta su pedido',
        'Pídale a administración que se la configure. Hágalo hoy; resuélvalo',
        'Actualizar · Actual · estatus · titular · Tuluá',
        'RECONTAR_TU', 'tu_rol', 'toca_avisar_vencimiento', 'decide_el_lider',
        'Use «No lo encontré» si no está',
        'Los cierres forzados de la semana',
        'Conteo reabierto — vuelve a la cola',
        'El sistema revisa y confirma el pedido',
        'Recogida completa', '· completa', 'Valor monetario — confirma',
        'El pedido espera autorización',
        'Consulta de stock · Carga masiva · Marca · Prueba',
        'Contraste · Ajustes · Reportes · días · menos · anómala · iniciales',
        'completa', 'sube', 'prueba',
        '/api/rutas/liquidar', '.prueba-x',
        'Tendencia 7 días', 'No sé / no dice nada',
    ])
    def test_no_marca_lo_sano(self, texto):
        assert hallazgos(texto) == [], (texto, hallazgos(texto))

    def test_el_vocabulario_no_tiene_las_colisiones(self):
        assert not (VOSEO & _COLISIONES)
        for sano in ('está', 'acá', 'ya', 'más', 'qué', 'sí', 'después', 'será', 'podrá'):
            assert sano not in VOSEO and sano not in TUTEO, sano


class TestLosExtractoresLeenSoloTextoVisible:

    def test_python_sin_docstrings_logs_ni_print(self):
        src = '''
"""Docstring del módulo: revisá esto."""
import logging
logger = logging.getLogger(__name__)
MENSAJE = 'Revisá la constante'
def f(x):
    """Docstring: tenés que leer."""
    # comentario: podés ignorarlo
    'cadena suelta: sabés'
    logger.warning('Log: revisá el log')
    logging.info('Log: tenés')
    print('Print: podés')
    if x:
        raise ValueError('No podés hacer eso')
    return {'error': f'Revisá {x} ya', 'ok': False}
'''
        textos = ' | '.join(t for _, t in textos_py(src))
        for dentro in ('Revisá la constante', 'No podés hacer eso', 'Revisá '):
            assert dentro in textos, dentro
        for fuera in ('Docstring', 'comentario', 'cadena suelta', 'Log:', 'Print:'):
            assert fuera not in textos, fuera

    def test_js_sin_comentarios_con_regex_y_plantillas(self):
        src = r'''
// Revisá este comentario
/* tenés un bloque */
const r = /['"]/g, d = a / b / c;
const x = 'Hola ' + 'mundo';
const y = `Hoy ${fn('dentro')} y ${n > 1 ? `anidado ${z}` : "otro"} fin`;
const url = "https://x/y"; // podés
'''
        lit = [t for _, t in literales_js(src)]
        assert 'Hola mundo' in lit, lit                     # 'a' + 'b' es una frase
        assert 'dentro' in lit and 'otro' in lit and 'anidado ' in lit, lit
        assert 'https://x/y' in lit, lit
        todo = ' '.join(lit)
        assert 'comentario' not in todo and 'bloque' not in todo and 'podés' not in todo

    def test_js_que_pierde_el_hilo_se_queja(self):
        with pytest.raises(JsIlegible):
            literales_js("const x = 'sin cerrar;\n")
        with pytest.raises(JsIlegible):
            literales_js('const x = `sin cerrar ${y}')

    def test_html_sin_comentarios_ni_estilos(self):
        src = '''<!-- <div>Revisá el comentario</div> --><style>.tenes{}</style>
<input placeholder="Escanea o escribe"><div title="Sin cambios">Texto visible</div>
<button onclick="alerta('Podés seguir')">Guardar</button>
<script>const m = 'Del script';</script>'''
        textos = [t for _, t in textos_html(src)]
        assert 'Escanea o escribe' in textos and 'Texto visible' in textos
        assert 'Podés seguir' in textos and 'Del script' in textos, textos
        assert not any('comentario' in t or 'tenes' in t for t in textos), textos


class TestElEscanerSeMide:
    """Un escáner que se desincroniza devuelve cero, y un cero se lee igual que
    «está limpio»: pisos sobre lo que leyó."""

    def test_lee_todos_los_archivos(self):
        textos, _ = _corpus()
        js = [a for a in textos if a.endswith('.js')]
        py = [a for a in textos if a.endswith('.py')]
        assert len(js) >= 30, len(js)
        assert len(py) >= 250, len(py)
        assert 'app/static/pwa/index.html' in textos

    def test_lee_miles_de_textos(self):
        _, leidos = _corpus()
        assert leidos['py'] >= 20_000, leidos
        assert leidos['js'] >= 15_000, leidos
        assert leidos['html'] >= 600, leidos

    def test_cada_js_de_la_pwa_se_lexea_entero(self):
        for p in sorted(PWA.glob('*.js')):
            literales_js(p.read_text(encoding='utf-8'))    # JsIlegible si pierde el hilo

    def test_el_detector_encuentra_algo_en_lo_pendiente(self):
        """Mientras haya pendientes, el detector tiene que verlos: si esto da
        cero con archivos declarados, se rompió el detector, no se arregló la
        voz."""
        por = _hallazgos_por_archivo()
        if PENDIENTE_OTRO_FRENTE:
            assert sum(len(por.get(a, [])) for a in PENDIENTE_OTRO_FRENTE) > 0
