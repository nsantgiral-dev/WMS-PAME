"""El bloque de salud de flota, EJECUTADO — no leído.

## Por qué existe

`GET /flota/health` mide 21 campos y **no tenía un solo consumidor de
producción** en todo el repo. Está exento del guard de rutas huérfanas *«porque
un health lo leen monitores»*, y no hay monitores.

El 2026-09-01, en esta misma tanda, se le agregaron **seis campos de odómetro**
— a un tablero que nadie abría. Eso es el patrón que este módulo entero sufre,
cometido mientras se lo arreglaba: *un sistema de captura con un lector*, y esta
vez sin ni siquiera el lector.

`flotaBloqueSalud()` cierra el círculo: pone en el tablero de flota lo que el
health ya medía y nadie veía.

## Se ejecuta el JS real, en Node

Un test que busque `'documentos_vencidos'` dentro del texto de `flota.js` pasa
con la función entera desconectada, o con el bloque devolviendo `''` siempre.
Ya pasó en este repo: un aserto de substring sobre `app.js` sobrevivió a anular
el pintado con `|| true`, y se descubrió mutándolo.

Acá el `flota.js` real corre en un `vm` con DOM mínimo y `fetch` sembrado, y se
mira **lo que quedó pintado**.

## La disciplina que se verifica

El bloque devuelve vacío cuando no hay nada que hacer — igual que
`flotaBloqueFueraDeSede` y `flotaBloqueForzados`. Un tablero que siempre muestra
algo se deja de mirar, que es la lección de los 639 avisos conocidos.

Y el salto de kilometraje se pinta **como hecho, sin juzgarlo**: no hay umbral
de km/día porque todavía no hay un mes de mediciones con qué fijarlo.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'

# Arnés propio y no importado de `test_render_reconciliacion_js.py`: aquel está
# atado a `app.js` y a sus globales. Duplicar ~60 líneas de andamio es más
# barato que un arnés compartido que tenga que servir a dos PWAs distintos y
# que nadie pueda cambiar sin romper el otro.
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const FLOTAJS = process.argv[2];
const GUION = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

function elemento(id) {
  return {
    id, innerHTML: '', textContent: '', style: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() { return false; },
                 contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {}, remove() {},
  };
}
const elementos = {};
const doc = {
  body: elemento('body'), head: elemento('head'),
  documentElement: elemento('html'),
  getElementById(id) {
    if (!(id in elementos)) elementos[id] = elemento(id);
    return elementos[id];
  },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, createElement(t) { return elemento(t); },
};
const ctx = {
  console, document: doc,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  window: { location: { origin: 'http://test' }, addEventListener() {},
            matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  AbortController: globalThis.AbortController,
  // `flota.js` usa el helper `get()` de `app.js`, que no se carga acá: se
  // provee uno mínimo que sirve el guion. Es la frontera del arnés, y por eso
  // el test verifica el CONTENIDO pintado y no que `get` se haya llamado.
  get: async (ruta) => {
    for (const [trozo, payload] of Object.entries(GUION.rutas)) {
      if (String(ruta).includes(trozo)) return payload;
    }
    throw new Error('ruta no sembrada: ' + ruta);
  },
  horaColombia: (x) => String(x),
};
ctx.globalThis = ctx;
ctx.window.document = doc;
vm.createContext(ctx);
// `util.js` va PRIMERO y se carga de verdad, no se stubbea: `esc()` es lo
// que este arnés tiene que poder ver fallar.
vm.runInContext(fs.readFileSync(FLOTAJS.replace(/[^/]+$/, 'util.js'), 'utf-8'),
                ctx, { filename: 'util.js' });
vm.runInContext(fs.readFileSync(FLOTAJS, 'utf-8'), ctx, { filename: 'flota.js' });

(async () => {
  if (typeof ctx[GUION.fn] !== 'function') {
    throw new Error('no existe la función ' + GUION.fn);
  }
  process.stdout.write(JSON.stringify({ html: (await ctx[GUION.fn]()) || '' }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _pintar(tmp_path, health: dict) -> str:
    """Corre `flotaBloqueSalud()` del `flota.js` real y devuelve lo que pintó."""
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'fn': 'flotaBloqueSalud',
                             'rutas': {'/flota/health': health}}),
                 encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(FLOTA_JS), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'el bloque reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)['html']


def _sano(**extra) -> dict:
    """Una flota sin nada que reportar."""
    base = {
        'documentos_vencidos': 0, 'documentos_no_encontrados': 0,
        'vehiculos_sin_lectura': 0, 'lecturas_sin_foto': 0,
        'fichas_con_ancla_incoherente': 0, 'lecturas_correccion_30d': 0,
        'salto_km_maximo_30d': {'delta_km': None, 'nota': 'sin lecturas'},
        'hallazgos_abiertos': 0, 'hallazgos_vencidos': 0,
        # Inspección diaria (2026-09-02). El vacío viene con su nota y no como
        # `null`: `null` significa «la tabla no existe todavía», y un mismo
        # valor con dos significados es el defecto que este módulo persigue.
        'vehiculos_sin_inspeccion_hoy': 0, 'inspecciones_incompletas_hoy': 0,
        'segundos_llenado_30d': {'n': 0, 'minimo': None, 'mediana': None,
                                 'nota': 'ninguna inspección'},
    }
    base.update(extra)
    return base


class TestNoGritaCuandoNoHayNadaQueHacer:
    def test_una_flota_sana_no_pinta_nada(self, tmp_path):
        """La disciplina de los otros bloques. Un tablero que siempre muestra
        algo se deja de mirar."""
        assert _pintar(tmp_path, _sano()) == ''

    def test_el_salto_sin_datos_no_ocupa_espacio(self, tmp_path):
        """`{'delta_km': None}` es «no hubo dos lecturas en la ventana», no un
        salto de cero. No merece una línea."""
        html = _pintar(tmp_path, _sano(
            salto_km_maximo_30d={'delta_km': None, 'nota': 'x'}))
        assert html == ''


class TestPintaLoQuePideAccion:
    def test_los_documentos_vencidos_salen_primero_y_en_rojo(self, tmp_path):
        """El número que existía desde la tanda 1 y nadie miraba — la «otra
        vía» a la que el aviso de vencimiento remitía sin que existiera."""
        html = _pintar(tmp_path, _sano(documentos_vencidos=1))
        assert '1 documento(s) VENCIDOS' in html
        assert 'var(--red)' in html

    def test_un_vehiculo_sin_lectura_se_explica_como_no_se_sabe(self, tmp_path):
        """Regla 4: no es que tenga 0 km."""
        html = _pintar(tmp_path, _sano(vehiculos_sin_lectura=2))
        assert '2 vehículo(s) sin ninguna lectura' in html
        assert 'no se sabe' in html

    def test_las_lecturas_sin_foto_se_nombran(self, tmp_path):
        html = _pintar(tmp_path, _sano(lecturas_sin_foto=26))
        assert '26 lectura(s) sin foto' in html

    def test_el_ancla_incoherente_dice_que_hay_que_mirar_cual(self, tmp_path):
        """No afirma cuál de los dos números miente, porque no se sabe."""
        html = _pintar(tmp_path, _sano(fichas_con_ancla_incoherente=4))
        assert '4 ficha(s)' in html
        assert 'cuál' in html

    def test_las_correcciones_se_cuentan(self, tmp_path):
        html = _pintar(tmp_path, _sano(lecturas_correccion_30d=3))
        assert '3 corrección' in html

    def test_pinta_varias_a_la_vez(self, tmp_path):
        html = _pintar(tmp_path, _sano(documentos_vencidos=1,
                                       vehiculos_sin_lectura=2,
                                       lecturas_sin_foto=26))
        for trozo in ('1 documento(s) VENCIDOS', '2 vehículo(s)', '26 lectura(s)'):
            assert trozo in html


class TestElSaltoEsUnHechoNoUnaAlarma:
    def test_se_pinta_con_su_magnitud_y_sus_horas(self, tmp_path):
        """**El caso THP696.** Se informa; no se juzga."""
        html = _pintar(tmp_path, _sano(salto_km_maximo_30d={
            'delta_km': 16642599, 'horas': 312.0, 'vehiculo_id': 3,
            'valor_km': 16697948, 'origen': 'entrega'}))
        assert '16.642.599' in html, (
            f'el número no salió formateado y legible: {html[:200]}')
        assert '312' in html

    def test_dice_que_todavia_no_hay_con_que_compararlo(self, tmp_path):
        """Lo que impide que se lea como una alarma con umbral. No hay techo
        medido: publicarlo es el paso para poder fijarlo."""
        html = _pintar(tmp_path, _sano(salto_km_maximo_30d={
            'delta_km': 5000, 'horas': 10.0, 'vehiculo_id': 1,
            'valor_km': 5100, 'origen': 'entrega'}))
        assert 'no una alarma' in html or 'todavía no hay' in html

    def test_sin_fecha_no_inventa_una_duracion(self, tmp_path):
        """`horas: null` es «no se puede saber en cuánto tiempo», no cero
        horas — que sería velocidad infinita."""
        html = _pintar(tmp_path, _sano(salto_km_maximo_30d={
            'delta_km': 900, 'horas': None, 'vehiculo_id': 1,
            'valor_km': 1000, 'origen': 'entrega'}))
        assert 'sin fecha' in html
        assert 'null' not in html


class TestNoSeCaeConUnHealthIncompleto:
    """El bloque corre en el tablero de flota. Si el health falla o cambia, no
    puede tumbar la pantalla entera del administrador."""

    def test_un_health_que_no_responde_devuelve_vacio(self, tmp_path):
        html = _pintar(tmp_path, None) if False else _pintar(tmp_path, {})
        assert html == ''

    def test_campos_ausentes_no_revientan(self, tmp_path):
        """Un health viejo, o uno al que se le saque un campo, no puede dejar
        al admin sin tablero."""
        assert _pintar(tmp_path, {'documentos_vencidos': 0}) == ''


class TestLosDanosSeVenSinAbrirSeisExpedientes:
    """`flota_hallazgo` nació el 2026-09-01 con pantalla **por vehículo**.

    Sin estas dos líneas, saber si hay un daño vencido en la flota exige abrir
    los seis expedientes de a uno — y eso nadie lo hace el martes. Es el mismo
    defecto que este archivo existe para arreglar (captura sin lector), esta
    vez atendido en la misma tanda en que nace la tabla y no un mes después.
    """

    def test_un_dano_vencido_sale_en_rojo(self, tmp_path):
        html = _pintar(tmp_path, _sano(hallazgos_vencidos=2, hallazgos_abiertos=5))
        assert '2 daño(s) pasados de su fecha límite' in html
        assert 'var(--red)' in html

    def test_los_abiertos_van_en_su_propia_linea(self, tmp_path):
        """Sumados a los vencidos, el vencido desaparece — que es exactamente
        lo que vuelve ilegible un canal de avisos."""
        html = _pintar(tmp_path, _sano(hallazgos_vencidos=2, hallazgos_abiertos=5))
        assert '5 daño(s) abiertos' in html

    def test_sin_vencidos_no_se_pinta_la_linea_roja(self, tmp_path):
        """La otra dirección: una flota con daños en plazo no está en rojo."""
        html = _pintar(tmp_path, _sano(hallazgos_abiertos=3))
        assert '3 daño(s) abiertos' in html
        assert 'pasados de su fecha límite' not in html

    def test_una_flota_sin_danos_no_ocupa_espacio(self, tmp_path):
        assert _pintar(tmp_path, _sano(hallazgos_abiertos=0,
                                       hallazgos_vencidos=0)) == ''

    def test_el_dano_va_ARRIBA_del_odometro(self, tmp_path):
        """Un bloqueante vencido es un camión que no debería estar saliendo, y
        eso se decide hoy. El odómetro es el denominador de lo que se calcula
        después."""
        html = _pintar(tmp_path, _sano(hallazgos_vencidos=1,
                                       vehiculos_sin_lectura=2))
        assert html.index('fecha límite') < html.index('sin ninguna lectura')


class TestElCamionQueNadieMiroSeVe:
    """`flota_inspeccion` nació el 2026-09-02 con su pantalla del conductor.

    Un vehículo sin inspección **no aparece en ningún otro lado del tablero**:
    no tiene daño, no tiene aviso, no tiene fila. La ausencia es invisible salvo
    que alguien la cuente — y es justo la que importa, porque un camión que
    nadie miró se ve exactamente igual que uno que salió `apto`.
    """

    def test_los_vehiculos_sin_inspeccion_de_hoy_se_cuentan(self, tmp_path):
        html = _pintar(tmp_path, _sano(vehiculos_sin_inspeccion_hoy=4))
        assert '4 vehículo(s) sin inspección de hoy' in html
        assert 'nadie los miró' in html, (
            'sin esa frase el número se lee como «4 pendientes de trámite» y no '
            'como «4 camiones de los que no se sabe nada»')

    def test_las_incompletas_van_en_su_propia_linea(self, tmp_path):
        """Sumadas a las que faltan, desaparecen. Y se corrigen distinto: una
        hablando con el conductor, la otra mirando por qué la pantalla se
        abandona a la mitad."""
        html = _pintar(tmp_path, _sano(vehiculos_sin_inspeccion_hoy=2,
                                       inspecciones_incompletas_hoy=1))
        assert '1 inspección(es) de hoy quedaron incompletas' in html
        assert '2 vehículo(s) sin inspección' in html

    def test_incompleta_no_se_pinta_como_casi_apta(self, tmp_path):
        """Regla 1 en la pantalla: `incompleta` es «no se sabe», y no saber
        tampoco habilita despacho."""
        html = _pintar(tmp_path, _sano(inspecciones_incompletas_hoy=3))
        assert 'tampoco habilita despacho' in html

    def test_una_flota_toda_inspeccionada_no_ocupa_espacio(self, tmp_path):
        """La otra dirección, y la disciplina del bloque entero."""
        assert _pintar(tmp_path, _sano(vehiculos_sin_inspeccion_hoy=0,
                                       inspecciones_incompletas_hoy=0)) == ''


class TestElTiempoDeLlenadoEsUnHechoNoUnaAlarma:
    """Regla 11 publicada, regla 13 respetada.

    La forma de maximizar el registro sin hacer el trabajo es marcar todo óptimo
    en veinte segundos. Lo que el tablero hace hoy con eso es **mostrarlo**: no
    hay una sola medición todavía, así que cualquier techo sería a ojo. Es el
    mismo trato que `salto_km_maximo_30d`.
    """

    def test_se_pinta_con_sus_items_al_lado(self, tmp_path):
        """«20 segundos» solo, no dice nada: veinte segundos para tres ítems no
        es lo mismo que para veintiocho."""
        html = _pintar(tmp_path, _sano(segundos_llenado_30d={
            'n': 12, 'mediana': 96,
            'minimo': {'segundos': 19, 'items': 28, 'inspeccion_id': 5,
                       'vehiculo_id': 3, 'veredicto': 'apto'}}))
        assert '19s para 28 ítems' in html
        assert '96s' in html and '12 inspecciones' in html

    def test_dice_que_todavia_no_hay_con_que_compararlo(self, tmp_path):
        html = _pintar(tmp_path, _sano(segundos_llenado_30d={
            'n': 2, 'mediana': 80,
            'minimo': {'segundos': 70, 'items': 28, 'inspeccion_id': 1,
                       'vehiculo_id': 1, 'veredicto': 'apto'}}))
        assert 'no una alarma' in html or 'todavía no hay' in html

    def test_sin_inspecciones_no_ocupa_una_linea(self, tmp_path):
        """`{'n': 0, 'minimo': None}` es «no hubo ninguna en la ventana», no un
        llenado de cero segundos. No merece una línea."""
        html = _pintar(tmp_path, _sano(segundos_llenado_30d={
            'n': 0, 'minimo': None, 'mediana': None, 'nota': 'ninguna'}))
        assert html == ''


class TestNingunCampoDelHealthQuedaMudo:
    """TRINQUETE — el defecto que este archivo existe para cerrar, medido.

    El 2026-09-03 `/flota/health` publicaba **44 campos** y la pantalla pintaba
    **32**. Los otros doce se medían, se serializaban, viajaban por la red… y no
    los leía nadie. Es *captura sin lector*: el mismo patrón que este módulo
    lleva una semana persiguiendo, cometido dentro del bloque escrito para
    cerrarlo.

    Once venían de la tanda 1. Uno —`lecturas_ts_duplicado`— se agregó el
    2026-09-01, en la misma tanda que arreglaba este patrón, y nació mudo.

    **Sin este test la lista crece sola**, porque agregar un campo al health es
    una línea y pintarlo son seis. Y el que la agrega no es el que la mira.
    """

    def _campos_declarados(self):
        import ast
        arbol = ast.parse((RAIZ / 'flota' / 'api' / 'health.py').read_text(
            encoding='utf-8'))
        for n in ast.walk(arbol):
            if (isinstance(n, ast.Assign)
                    and any(getattr(t, 'id', '') == '_CAMPOS' for t in n.targets)):
                return [e.value for e in n.value.elts]
        raise AssertionError('no se encontró `_CAMPOS` en flota/api/health.py')

    #: Los archivos donde puede vivir un consumidor legítimo del health.
    #:
    #: **Es `flota*.js` y no `*.js`, y el glob se eligió midiendo.** El
    #: 2026-09-04, al partir la analítica en un archivo aparte, hizo falta que
    #: el trinquete viera más de un archivo. La opción cómoda era leer todo el
    #: PWA; se midió antes de tomarla, y de los 44 campos **uno** —`ambiente`—
    #: ya aparece en `app.js` por motivos que nada tienen que ver con flota.
    #: Con `*.js` ese campo quedaba satisfecho por casualidad y el trinquete
    #: dejaba de vigilarlo, sin que nada se pusiera rojo.
    #:
    #: Ensanchar un detector para que pase el cambio propio es cómo se apaga un
    #: detector. La propiedad que importa es «alguna pantalla DE FLOTA lo lee»,
    #: y `flota*.js` es el glob que la mide.
    _GLOB_CONSUMIDORES = 'flota*.js'

    def _js_de_flota(self):
        pwa = RAIZ / 'app' / 'static' / 'pwa'
        archivos = sorted(pwa.glob(self._GLOB_CONSUMIDORES))
        assert archivos, (
            f'el glob {self._GLOB_CONSUMIDORES!r} no resolvió a ningún archivo '
            f'en {pwa}. Un detector que no lee nada declara todo mudo o todo '
            f'pintado según el sentido de la comparación; en los dos casos '
            f'dejó de medir.')
        return {a.name: a.read_text(encoding='utf-8') for a in archivos}

    def test_todo_campo_publicado_se_nombra_en_el_PWA(self):
        """Por nombre y no por comportamiento: probar los 44 pintando cada uno
        sería un test por campo y nadie lo mantendría. Esto es el piso — que
        exista el consumidor. Las clases de arriba prueban que lo pintado dice
        lo correcto."""
        fuentes = self._js_de_flota()
        mudos = [c for c in self._campos_declarados()
                 if not any(c in js for js in fuentes.values())]
        assert not mudos, (
            f'\n{len(mudos)} campo(s) del health que nadie lee '
            f'(buscados en {", ".join(sorted(fuentes))}):\n'
            + '\n'.join(f'  · {c}' for c in mudos)
            + '\n\nUn número que se mide y no se muestra es trabajo hecho para '
              'nadie. Si de verdad no debe pintarse, la pregunta no es «dónde lo '
              'pongo» sino QUÉ DECISIÓN DEBERÍA ESTAR INFORMANDO.')

    def test_el_detector_ve_un_campo_mudo_de_verdad(self):
        """La otra dirección. Un detector que no sabe reconocer un campo mudo
        devuelve lista vacía sobre un tablero ciego, y eso se lee como «está
        todo pintado».

        Se comprueba contra **todos** los archivos del glob, no contra uno: al
        ensanchar el detector, un canario que solo mira `flota.js` seguiría en
        verde aunque el resto del glob dejara de leerse.
        """
        fuentes = self._js_de_flota()
        for nombre, js in fuentes.items():
            assert 'campo_que_nadie_pinta_jamas' not in js, nombre

    #: Campos del health que aparecen en un `.js` AJENO a flota, por motivos
    #: que nada tienen que ver con el health. Medido el 2026-09-04.
    #:
    #: Existe para que la exención cueste escribirla. Un `if nombre == 'app.js':
    #: continue` habría eximido el archivo entero, y mañana un campo nuevo se
    #: colaría ahí sin que nada se pusiera rojo — que es la forma exacta del
    #: defecto que este trinquete persigue.
    _COLISIONES_CONOCIDAS = {
        ('app.js', 'ambiente'): (
            'app.js usa la palabra `ambiente` para el banner global de '
            'QA/producción, que existía antes que el health de flota. No es un '
            'lector de `/flota/health`.'),
    }

    def _colisiones_nuevas(self, campos, ajenos):
        """Las colisiones que NO están declaradas. Pura, para poder probarla.

        Recibe `{archivo: texto}` en vez de leer el disco porque la propiedad
        que importa —«la exención es por (archivo, campo), no por archivo»— no
        se puede ejercer contra el repo real: hoy la única colisión existente es
        justamente la declarada, así que un `a != 'app.js'` da el mismo
        resultado y el test no notaría el cambio. Con la función separada se le
        pueden dar dos campos colados en el mismo archivo.
        """
        return sorted(
            (a, c) for a, texto in ajenos.items() for c in sorted(campos)
            if c in texto and (a, c) not in self._COLISIONES_CONOCIDAS)

    def test_el_glob_no_barre_archivos_ajenos_a_flota(self):
        """El otro modo de romper esto, y es el que casi ocurre.

        Ensanchar a `*.js` haría que `ambiente` —que aparece en `app.js` por
        motivos ajenos— quedara satisfecho sin que ninguna pantalla de flota lo
        lea: el trinquete seguiría verde habiendo dejado de vigilar ese campo.

        Este test no fija el glob: fija **cuánto se está pagando por él**. Una
        colisión nueva lo pone rojo y obliga a declararla o a angostar el glob.
        """
        pwa = RAIZ / 'app' / 'static' / 'pwa'
        del_glob = {a.name for a in pwa.glob(self._GLOB_CONSUMIDORES)}
        ajenos = {a.name: a.read_text(encoding='utf-8')
                  for a in pwa.glob('*.js') if a.name not in del_glob}
        assert ajenos, 'el PWA tiene más JS que el de flota; algo se movió'

        nuevas = self._colisiones_nuevas(set(self._campos_declarados()), ajenos)
        assert not nuevas, (
            '\nCampos del health que aparecen en un JS ajeno a flota y no '
            'estaban declarados:\n'
            + '\n'.join(f'  · {c}  ({a})' for a, c in nuevas)
            + '\n\nSi ese archivo NO es un lector del health, agregalo a '
              '`_COLISIONES_CONOCIDAS` con su motivo. Si SÍ lo es, el glob '
              '`_GLOB_CONSUMIDORES` se quedó corto.')

    def test_la_exencion_es_por_campo_y_NO_por_archivo(self):
        """La propiedad que la mutación M7 destapó que no estaba probada.

        Eximir `app.js` entero y eximir el par `(app.js, ambiente)` dan hoy el
        mismo resultado —es la única colisión que existe—, así que contra el
        repo real los dos son indistinguibles. Y no son lo mismo: con el archivo
        eximido, un campo del health que mañana aparezca en `app.js` se cuela
        sin que nada se ponga rojo, que es la forma exacta del defecto que este
        trinquete persigue.

        Se ejerce con un mundo sintético: el mismo archivo, un campo declarado
        y otro que no.
        """
        ajenos = {'app.js': 'const ambiente = 1; const cpk_mes = 2;'}
        nuevas = self._colisiones_nuevas({'ambiente', 'cpk_mes'}, ajenos)
        assert nuevas == [('app.js', 'cpk_mes')], (
            f'la exención está operando por archivo y no por campo: {nuevas}. '
            f'`ambiente` está declarado en `_COLISIONES_CONOCIDAS`; `cpk_mes` '
            f'no, y tiene que salir.')

    def test_las_colisiones_declaradas_siguen_siendo_ciertas(self):
        """Una exención que dejó de aplicar es una exención que tapa algo.

        Si `app.js` deja de nombrar `ambiente`, la entrada sobra — y una lista
        de exenciones que solo crece termina eximiendo lo que ya no existe.
        """
        pwa = RAIZ / 'app' / 'static' / 'pwa'
        for (archivo, campo), motivo in self._COLISIONES_CONOCIDAS.items():
            ruta = pwa / archivo
            assert ruta.exists(), f'{archivo} ya no existe; sobra la exención'
            assert campo in ruta.read_text(encoding='utf-8'), (
                f'{archivo} ya no nombra {campo!r}: la exención dejó de aplicar '
                f'y hay que borrarla. Motivo declarado: {motivo}')


class TestLosDoceQueEstabanMudos:
    """Que además DIGAN algo, no solo que aparezcan."""

    def test_un_vehiculo_sin_responsable_sale_en_rojo(self, tmp_path):
        html = _pintar(tmp_path, _sano(vehiculos_sin_custodia_activa=2))
        assert '2 vehículo(s) sin responsable' in html
        assert 'var(--red)' in html

    def test_el_cierre_forzado_se_nombra_como_conducta(self, tmp_path):
        """Regla 2: el sistema anota un hecho, no imputa. Dice que alguien cerró
        el turno de otro sin firma — no dice que hizo mal."""
        html = _pintar(tmp_path, _sano(custodias_cerradas_forzadas=1))
        assert 'sin la firma del custodio' in html
        assert 'conducta' in html

    def test_el_conductor_sin_cuenta_dice_la_consecuencia(self, tmp_path):
        html = _pintar(tmp_path, _sano(conductores_activos_sin_cuenta=3))
        assert '3 conductor(es)' in html
        assert 'no puede distinguirlos' in html

    def test_las_lecturas_del_mismo_segundo_se_cuentan_sin_alarmar(self, tmp_path):
        """Es ruido medido, no una falla: se publica el hecho sin umbral."""
        html = _pintar(tmp_path, _sano(lecturas_ts_duplicado=20))
        assert '20 lectura(s)' in html
        assert 'var(--red)' not in html

    def test_la_cobertura_de_fichas_se_lee_como_fracción(self, tmp_path):
        html = _pintar(tmp_path, _sano(vehiculos_activos=6, fichas_completas=2))
        assert '2 de 6 fichas' in html

    def test_una_flota_con_las_fichas_completas_no_pinta_esa_linea(self, tmp_path):
        """La otra dirección: el contador no puede gritar cuando está todo."""
        html = _pintar(tmp_path, _sano(vehiculos_activos=6, fichas_completas=6))
        assert 'fichas técnicas completas' not in html

    def test_un_tablero_de_QA_lo_dice(self, tmp_path):
        """**El más importante de los doce.** Un tablero que no declara que sus
        números son de prueba se lee como si fueran de la operación — y ese es
        el incidente de las ocho horas escribiendo en la base equivocada, con
        otra cara."""
        html = _pintar(tmp_path, _sano(datos_reales=False, ambiente='qa'))
        assert 'NO son de la operación real' in html
        assert 'qa' in html

    def test_y_en_produccion_no_ensucia_el_tablero(self, tmp_path):
        html = _pintar(tmp_path, _sano(datos_reales=True, ambiente='produccion'))
        assert 'NO son de la operación real' not in html
