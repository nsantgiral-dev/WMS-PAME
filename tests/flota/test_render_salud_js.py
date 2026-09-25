"""Los números del health de flota, EJECUTADOS — no leídos.

## Por qué existe

`GET /flota/health` mide ~50 campos y durante un mes **no tuvo un solo
consumidor de producción**. `flotaBloqueSalud()` lo cerró: pintaba 41 renglones
de prosa arriba de la pestaña del encargado. El 2026-09-24 la bandeja lo
reemplazó por Hoy/Pendientes (con placa y botón) y la Salud se retiró; lo que no
es accionable del día quedó en el **Diagnóstico técnico**, plegado al final de
Analítica (`flotaBandejaDiagnosticoHtml`), en palabras y agrupado.

## Se ejecuta el JS real, en Node, con `util.js` real

Y se mira **lo que quedó pintado**: un test que busque el nombre del campo en
el texto de `flota.js` pasa con la función desconectada.

## El trinquete sigue

`TestNingunCampoDelHealthQuedaMudo`: todo campo que el health publica se nombra
en algún `flota*.js`. Sin él, agregar un campo es una línea y pintarlo son seis.
"""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const H = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const el = () => ({ style: {}, innerHTML: '', classList: { add() {}, remove() {} } });
const ctx = {
  console, document: { getElementById: () => el(), querySelector: () => null,
    querySelectorAll: () => [], addEventListener() {}, createElement: el },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true }, setTimeout, clearTimeout, setInterval: () => 0,
  clearInterval() {}, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  alerta: () => {}, TOKEN: 'x', API: '', horaColombia: (x) => String(x),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const f of ['util.js', 'flota.js', 'flota_analitica.js', 'flota_bandeja.js']) {
  vm.runInContext(fs.readFileSync(PWA + '/' + f, 'utf-8'), ctx, { filename: f });
}
process.stdout.write(JSON.stringify(ctx.flotaBandejaDiagnosticoHtml(H)));
"""


def _campos_del_health():
    arbol = ast.parse((RAIZ / 'flota' / 'api' / 'health.py').read_text(encoding='utf-8'))
    for n in ast.walk(arbol):
        if (isinstance(n, ast.Assign)
                and any(getattr(t, 'id', '') == '_CAMPOS' for t in n.targets)):
            return [e.value for e in n.value.elts]
    raise AssertionError('no se encontró `_CAMPOS`')


def _diag(tmp_path, health: dict) -> str:
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps(health), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)], capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, f'el diagnóstico reventó:\n{p.stderr}'
    return json.loads(p.stdout)


def _sano(**extra) -> dict:
    base = {c: 0 for c in _campos_del_health()}
    base.update({
        'ambiente': 'produccion', 'datos_reales': True,
        'salto_km_maximo_30d': {'delta_km': None, 'nota': 'sin lecturas'},
        'segundos_llenado_30d': {'n': 0, 'minimo': None, 'mediana': None,
                                 'nota': 'ninguna inspección'},
        'documentos_por_vehiculo': [], 'custodias_por_vehiculo': [],
    })
    base.update(extra)
    return base


class TestElDiagnosticoDiceCadaNumero:

    def test_cada_contador_se_pinta_con_su_valor(self, tmp_path):
        """Un valor distinto por campo numérico: si alguno no se pinta, su
        número no aparece."""
        numericos = [c for c, v in _sano().items() if isinstance(v, int)
                     and not isinstance(v, bool)]
        h = _sano(**{c: 1000 + i for i, c in enumerate(numericos)})
        html = _diag(tmp_path, h)
        faltan = [c for i, c in enumerate(numericos)
                  if f'{1000 + i:,}'.replace(',', '.') not in html]
        # Los que no son contadores del diagnóstico (se pintan en su panel de
        # Analítica: preventivo, CPK, llantas, taller, ritmo…) quedan fuera.
        propios = {'vehiculos_activos', 'fichas_completas', 'conductores_activos_sin_cuenta',
                   'rutas_historicas_sin_placa', 'vehiculos_sin_lectura',
                   'lecturas_sin_foto', 'lecturas_dudosas_pendientes',
                   'lecturas_verificadas_30d', 'lecturas_correccion_30d',
                   'lecturas_ts_duplicado', 'fichas_con_ancla_incoherente',
                   'documentos_vencidos', 'documentos_por_vencer_30d',
                   'documentos_no_encontrados', 'vehiculos_sin_custodia_activa',
                   'custodias_cerradas_forzadas', 'custodias_sin_foto_completa',
                   'custodias_pendiente_sede', 'hallazgos_abiertos',
                   'vehiculos_sin_inspeccion_hoy', 'inspecciones_incompletas_hoy',
                   'tanqueos_sobre_capacidad', 'tanqueos_sin_capacidad_declarada',
                   'gastos_sin_documento', 'fotos_pendiente_evidencia'}
        assert not (set(faltan) & propios), sorted(set(faltan) & propios)
        assert propios <= set(numericos), propios - set(numericos)

    def test_null_es_sin_dato_y_no_cero(self, tmp_path):
        html = _diag(tmp_path, _sano(lecturas_sin_foto=None, documentos_vencidos=None))
        assert 'Lecturas sin foto del tablero: <b>sin dato (la tabla no existe)</b>' in html
        assert 'Papeles vencidos: <b>sin dato (la tabla no existe)</b>' in html

    def test_miles_con_punto(self, tmp_path):
        assert 'Lecturas sin foto del tablero: <b>1.234</b>' in _diag(
            tmp_path, _sano(lecturas_sin_foto=1234))


class TestElSaltoEsUnHechoNoUnaAlarma:

    def test_con_su_magnitud_y_sus_horas(self, tmp_path):
        html = _diag(tmp_path, _sano(salto_km_maximo_30d={
            'delta_km': 16_300_000, 'horas': 2.5, 'vehiculo_id': 1, 'valor_km': 1}))
        assert '+16.300.000 km en 2,5 h' in html

    def test_sin_lectura_anterior_no_inventa_una_duracion(self, tmp_path):
        html = _diag(tmp_path, _sano(salto_km_maximo_30d={
            'delta_km': 500, 'horas': None, 'vehiculo_id': 1, 'valor_km': 1}))
        assert '+500 km sin hora anterior' in html

    def test_sin_datos_dice_la_nota(self, tmp_path):
        assert 'sin lecturas' in _diag(tmp_path, _sano())


class TestElTiempoDeLlenadoEsUnHecho:

    def test_con_sus_items_al_lado(self, tmp_path):
        html = _diag(tmp_path, _sano(segundos_llenado_30d={
            'n': 12, 'mediana': 95, 'minimo': {'segundos': 20, 'items': 27,
                                               'veredicto': 'apto'}}))
        # El veredicto de la más rápida va en palabras al lado (integración
        # 2026-09-24: el panel de Analítica que lo decía se mudó acá).
        assert ('mediana 95 s · la más rápida 20 s para 27 ítems (apta) · 12 inspecciones'
                in html)

    def test_sin_inspecciones_dice_la_nota(self, tmp_path):
        assert 'ninguna inspección' in _diag(tmp_path, _sano())


class TestLaBaseSeDeclara:

    def test_un_tablero_de_qa_lo_dice(self, tmp_path):
        html = _diag(tmp_path, _sano(datos_reales=False, ambiente='qa'))
        assert 'NO son de la operación real' in html and 'qa' in html

    def test_en_produccion_no_ensucia(self, tmp_path):
        assert 'NO son de la operación real' not in _diag(tmp_path, _sano())

    def test_lo_que_viene_del_servidor_llega_escapado(self, tmp_path):
        html = _diag(tmp_path, _sano(ambiente='<img src=x onerror=alert(1)>'))
        assert '<img' not in html and '&lt;img' in html


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
        ('kardex.js', 'ambiente'): (
            'kardex.js dice «(ambiente de PRUEBAS)» en el semáforo de salud del '
            'kardex cuando el host de Siesa es QA (`/api/kardex/salud`). No es '
            'un lector de `/flota/health`.'),
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


