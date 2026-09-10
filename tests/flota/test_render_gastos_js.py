"""
La pantalla de la plata que sale, EJECUTADA — no leída.

`flota/dominio/costos.py` tenía 415 líneas, su canon y **cero callers**. Estos
tests corren el `flota.js` real en Node y miran lo que quedó pintado, porque un
aserto de substring sobre el archivo pasa con la función entera desconectada:
ya pasó en este repo con un `|| true` que anuló el pintado y sobrevivió a la
suite.

Se prueban dos cosas distintas:

1. **El bloque de salud** — los cuatro campos nuevos del health. Reusa el arnés
   de `test_render_salud_js.py` en vez de copiarlo por tercera vez (regla 0): si
   alguien lo cambia, esto se rompe y se entera, que es lo que hay que querer.
2. **El expediente del vehículo** — con su propio arnés, porque lo que hay que
   leer no es el valor devuelto sino **el HTML que quedó en `#flota-recibo`**.

## Lo que se verifica y por qué no es cosmético

· Un CPK que no se puede calcular se dice **con palabras y con el motivo**,
  jamás como `$0`: un CPK de cero se lee como un vehículo gratis, y un vehículo
  gratis no se investiga.
· El tanqueo que excede la capacidad **no acusa a nadie** (regla 2). El renglón
  dice que dos datos no pueden ser los dos ciertos y enumera las cuatro
  explicaciones posibles.
· El estado del tanque **no trae ninguna opción marcada**. Un `lleno` puesto por
  inercia no produce un error: produce una ventana basura que infla o hunde el
  rendimiento sin que nada se vea raro.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.flota.test_render_salud_js import _pintar, _sano

RAIZ = Path(__file__).resolve().parents[2]
FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'


# ══════════════════════════════════════════════════════════════════════════
# 1 — El bloque de salud
# ══════════════════════════════════════════════════════════════════════════

def _plata(**extra):
    """Una flota sana **y sin un peso registrado**: el estado de hoy."""
    base = {'tanqueos_sobre_capacidad': 0, 'tanqueos_sin_capacidad_declarada': 0,
            'gastos_sin_documento': 0, 'cpk_mes': []}
    base.update(extra)
    return _sano(**base)


class TestLaPlataNoGritaCuandoNoHayNada:

    def test_sin_gastos_registrados_no_pinta_nada(self, tmp_path):
        """La disciplina de los otros bloques. Un tablero que siempre muestra
        algo se deja de mirar — la lección de los 639 avisos conocidos."""
        assert _pintar(tmp_path, _plata()) == ''

    def test_un_health_sin_los_campos_nuevos_no_revienta(self, tmp_path):
        """Un health viejo —o el de un despliegue a medias— no puede dejar al
        administrador sin tablero."""
        assert _pintar(tmp_path, _sano()) == ''


class TestElDetectorDeCapacidadSeVeYNoAcusa:

    def test_el_exceso_sale_en_rojo(self, tmp_path):
        html = _pintar(tmp_path, _plata(tanqueos_sobre_capacidad=2))
        assert '2 tanqueo(s) con más galones de los que caben' in html
        assert 'var(--red)' in html

    def test_el_renglon_enumera_las_explicaciones_en_vez_de_dictar_sentencia(
            self, tmp_path):
        """**Regla 2 en la línea que alguien lee.**

        Sin las alternativas escritas, «más galones de los que caben» se lee
        como una acusación — y una imputación automática es un pasivo laboral
        que mata la adopción, que es el recurso más escaso del proyecto."""
        html = _pintar(tmp_path, _plata(tanqueos_sobre_capacidad=1))
        assert 'no pueden ser los dos' in html
        assert 'tanque auxiliar' in html
        assert 'hay que mirar cuál' in html

    def test_ninguna_palabra_imputa_un_delito(self, tmp_path):
        html = _pintar(tmp_path, _plata(tanqueos_sobre_capacidad=9,
                                        tanqueos_sin_capacidad_declarada=3,
                                        gastos_sin_documento=4)).lower()
        for palabra in ('robo', 'hurto', 'ladrón', 'ladron', 'culpable',
                        'sisar', 'sifón'):
            assert palabra not in html

    def test_los_que_no_se_pudieron_revisar_tienen_su_PROPIA_linea(self, tmp_path):
        """**El campo que impide que el detector se apague en silencio.**

        Cero excesos con cien tanqueos sin revisar se ve idéntico a una flota
        limpia. Sin este renglón, un parque entero sin capacidad en la ficha
        sale verde para siempre — la misma forma que `REC-01` y las otras cinco
        de la auditoría del 2026-08-15."""
        html = _pintar(tmp_path, _plata(tanqueos_sin_capacidad_declarada=5))
        assert '5 tanqueo(s) que no se pudieron revisar' in html
        assert 'no es que estén bien' in html.lower()

    def test_sin_excesos_no_se_pinta_la_linea_roja(self, tmp_path):
        """La otra dirección: una flota con tanqueos normales no está en rojo.

        Sin esto, un renglón incondicional pasaría los dos tests de arriba y
        marcaría todos los tanqueos del parque."""
        html = _pintar(tmp_path, _plata(tanqueos_sin_capacidad_declarada=5))
        assert 'con más galones de los que caben' not in html


class TestLosGastosSinFacturaSeCuentan:

    def test_se_ven_y_se_explican(self, tmp_path):
        html = _pintar(tmp_path, _plata(gastos_sin_documento=7))
        assert '7 gasto(s) sin número de factura' in html
        assert 'causación' in html

    def test_cero_no_ocupa_espacio(self, tmp_path):
        assert _pintar(tmp_path, _plata(gastos_sin_documento=0)) == ''


class TestElCPKEsUnHechoPorVehiculo:

    def test_se_pinta_por_placa_con_su_numero(self, tmp_path):
        html = _pintar(tmp_path, _plata(cpk_mes=[
            {'placa': 'TGZ653', 'cpk': '840.00', 'marca': 'declarada',
             'pesos': '840000.00', 'km': 1000}]))
        assert 'TGZ653' in html
        assert '840' in html

    def test_dos_vehiculos_salen_los_dos_y_sin_promediarse(self, tmp_path):
        """El canon §3: el CPK **no compara vehículos**. Un promedio de flota
        mediría la composición del parque, no la operación — y sería el número
        que alguien usaría para decir que un camión es caro."""
        html = _pintar(tmp_path, _plata(cpk_mes=[
            {'placa': 'TGZ653', 'cpk': '840.00', 'marca': 'declarada',
             'pesos': '840000.00', 'km': 1000},
            {'placa': 'UPQ606', 'cpk': '1200.00', 'marca': 'declarada',
             'pesos': '600000.00', 'km': 500}]))
        assert 'TGZ653' in html and 'UPQ606' in html
        assert 'promedio' not in html.lower()
        assert 'comparan entre sí' in html

    def test_sin_dato_se_dice_con_palabras_no_como_cero_pesos(self, tmp_path):
        """**Nunca `$0`.** Un CPK de cero se lee como un vehículo gratis, y un
        vehículo gratis no se investiga."""
        html = _pintar(tmp_path, _plata(cpk_mes=[
            {'placa': 'THP696', 'cpk': 'sin_dato', 'marca': 'sin_dato',
             'pesos': '0.00', 'km': 0}]))
        assert 'THP696: sin dato' in html
        assert '$0' not in html

    def test_una_lista_vacia_no_pinta_el_renglon(self, tmp_path):
        """`[]` es «todavía no hay un peso registrado», no un CPK de cero."""
        assert _pintar(tmp_path, _plata(cpk_mes=[])) == ''


# ══════════════════════════════════════════════════════════════════════════
# 2 — El expediente del vehículo
#
# Arnés propio: lo que hay que leer no es lo que la función devuelve —no
# devuelve nada— sino el HTML que dejó en `#flota-recibo`.
# ══════════════════════════════════════════════════════════════════════════

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const FLOTAJS = process.argv[2];
const GUION = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

function elemento(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', style: {}, disabled: false,
    dataset: {},
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
  get: async (ruta) => {
    for (const [trozo, payload] of Object.entries(GUION.rutas)) {
      if (String(ruta).includes(trozo)) return payload;
    }
    throw new Error('ruta no sembrada: ' + ruta);
  },
  horaColombia: (x) => String(x),
  alerta: () => {},
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
  // `FLOTA_PLACA` se declara con `let`, así que NO es una propiedad del objeto
  // global del contexto: asignarla desde fuera crea otra variable y la pantalla
  // sigue viendo `null`. Se asigna corriendo un script en el mismo contexto,
  // que sí alcanza el binding léxico.
  vm.runInContext('FLOTA_PLACA = ' + JSON.stringify(GUION.placa) + ';', ctx);
  await ctx[GUION.fn]();
  process.stdout.write(JSON.stringify({
    html: doc.getElementById('flota-recibo').innerHTML || '',
  }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _expediente(tmp_path, payload) -> str:
    """Corre `flotaRenderGastos()` del `flota.js` real y devuelve lo pintado."""
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'fn': 'flotaRenderGastos', 'placa': 'TGZ653',
                             'rutas': {'/flota/gastos/': payload}}),
                 encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(FLOTA_JS), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'la pantalla reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)['html']


def _payload(**extra):
    base = {
        'placa': 'TGZ653', 'desde': '2026-03-01', 'hasta': '2026-03-31',
        'gastos': [], 'cpk': '840.00', 'cpk_marca': 'declarada',
        'cpk_motivo': None, 'lecturas_en_ventana': 4,
        'pesos_imputados': '840000.00', 'km_recorridos': 1000,
        'rendimiento_km_galon': '24.00',
        'capacidad_tanque_galones': '15.00',
        'categorias': ['combustible', 'mantenimiento', 'soat', 'otro'],
        'categorias_con_periodo': ['soat', 'rtm', 'impuesto_vehicular', 'seguro'],
        'categorias_de_campo': ['combustible', 'llanta', 'mantenimiento',
                                'repuesto'],
        'origenes_costo': ['tarjeta_convenio', 'credito_proveedor',
                           'efectivo_conductor', 'sin_dato'],
        'estados_tanque': ['lleno', 'parcial', 'sin_dato'],
    }
    base.update(extra)
    return base


class TestElCPKSeVeConSusInsumos:

    def test_el_numero_sale_con_la_division_que_lo_produjo(self, tmp_path):
        """Un CPK suelto no se puede auditar, y éste va a la pantalla de quien
        decide: tiene que poder rehacer la división."""
        html = _expediente(tmp_path, _payload())
        assert '840' in html
        assert '840.000' in html or '840000' in html      # los pesos
        assert '1.000 km' in html or '1000 km' in html    # los kilómetros
        assert 'declarada' in html

    def test_sin_dato_por_falta_de_gastos_lo_dice_con_el_motivo(self, tmp_path):
        html = _expediente(tmp_path, _payload(
            cpk='sin_dato', km_recorridos=800, lecturas_en_ventana=4,
            cpk_motivo='ningún gasto registrado contra este vehículo. '
                       'No es que sea gratis: es que nadie ha cargado una factura.'))
        assert 'sin dato' in html
        assert 'nadie ha cargado una factura' in html

    def test_sin_dato_por_falta_de_kilometros_da_OTRO_motivo(self, tmp_path):
        """Los `sin_dato` del canon no son el mismo, y quien lo lee tiene que
        saber qué le falta: registrar un gasto, registrar un odómetro, o
        verificar el que ya se registró."""
        html = _expediente(tmp_path, _payload(
            cpk='sin_dato', km_recorridos=0, lecturas_en_ventana=1,
            cpk_motivo='menos de dos lecturas de odómetro dentro del mes: no '
                       'hay tramo que dividir. Se resuelve registrando kilometraje.'))
        assert 'registrando kilometraje' in html
        assert 'factura' not in html.split('Registrar un gasto')[0]

    def test_la_pantalla_NO_re_deriva_el_motivo_del_CPK(self, tmp_path):
        """La otra dirección, y es la que atrapa el defecto real.

        Hasta el 2026-09-04 esta pantalla adivinaba el motivo con
        `km_recorridos > 0` y solo distinguía dos de los cuatro casos — **y los
        confundía**: un vehículo con dos lecturas dudosas tiene km > 0, así que
        la pantalla decía «no hay ningún gasto registrado» sobre un vehículo que
        sí tenía gastos, y mandaba a cargar una factura ya cargada.

        Con los MISMOS kilómetros y dos motivos distintos, el texto tiene que
        cambiar. Si alguien reintroduce la derivación en el navegador, los dos
        renders salen iguales y esto se pone rojo.
        """
        km = 800
        uno = _expediente(tmp_path, _payload(
            cpk='sin_dato', km_recorridos=km, lecturas_en_ventana=2,
            cpk_motivo='los dos extremos del tramo son dudosos: ninguno se '
                       'verificó contra su foto. Se resuelve verificando kilometrajes.'))
        otro = _expediente(tmp_path, _payload(
            cpk='sin_dato', km_recorridos=km, lecturas_en_ventana=4,
            cpk_motivo='ningún gasto registrado contra este vehículo. '
                       'No es que sea gratis: es que nadie ha cargado una factura.'))
        assert 'verificando kilometrajes' in uno
        assert 'nadie ha cargado una factura' in otro
        assert uno != otro, (
            'con el mismo `km_recorridos` y distinto `cpk_motivo` la pantalla '
            'pintó lo mismo: está derivando el motivo en el navegador otra vez')

    def test_el_CPK_con_cifra_declara_cuantas_lecturas_lo_sostienen(self, tmp_path):
        """Regla 13 a nivel de fila. Un CPK sobre dos lecturas y uno sobre
        veinte no se leen igual, y desde el número no se distinguen."""
        html = _expediente(tmp_path, _payload(lecturas_en_ventana=7))
        assert '7 lectura(s)' in html

    def test_dice_que_NO_se_compara_con_otro_vehiculo(self, tmp_path):
        html = _expediente(tmp_path, _payload())
        assert 'No se compara con otro vehículo' in html

    def test_el_rendimiento_sin_ventanas_explica_el_tanque_lleno(self, tmp_path):
        """El campo del que depende que la medición exista, explicado donde se
        lee y no en un instructivo aparte."""
        html = _expediente(tmp_path, _payload(rendimiento_km_galon='sin_dato'))
        assert 'tanque lleno' in html


class TestElFormularioNoTraeNadaMarcado:

    def test_el_estado_del_tanque_arranca_vacio(self, tmp_path):
        """**Regla 1 del módulo aplicada al único campo que decide si una
        medición existe.** Un `lleno` preseleccionado no produce un error:
        produce una ventana basura que infla o hunde el rendimiento."""
        html = _expediente(tmp_path, _payload())
        assert '<option value="" selected>— elegí una —</option>' in html
        assert '<option value="lleno" selected' not in html

    def test_las_tres_opciones_de_tanque_estan(self, tmp_path):
        html = _expediente(tmp_path, _payload())
        for opcion in ('lleno', 'parcial', 'sin_dato'):
            assert f'<option value="{opcion}">' in html

    def test_las_categorias_salen_del_SERVIDOR(self, tmp_path):
        """Si el JS llevara su propia lista, el día que se agregue una
        categoría periodificable el formulario no pediría el período y el gasto
        entero caería sobre un día (regla 0)."""
        html = _expediente(tmp_path, _payload(
            categorias=['combustible', 'peritaje_inventado']))
        assert 'peritaje_inventado' in html

    def test_el_formulario_sella_su_placa(self, tmp_path):
        html = _expediente(tmp_path, _payload())
        assert 'data-placa="TGZ653"' in html

    def test_sin_capacidad_en_la_ficha_lo_avisa_ANTES_de_registrar(self, tmp_path):
        """Que nada vaya a poder revisar el registro se dice donde se registra,
        no solo en el tablero que se abre el martes."""
        html = _expediente(tmp_path, _payload(capacidad_tanque_galones=None))
        assert 'no dice cuántos galones caben' in html

    def test_con_capacidad_se_muestra_el_numero(self, tmp_path):
        html = _expediente(tmp_path, _payload())
        assert '15.00 galones' in html


class TestLaFilaDeTanqueoDiceLoQuePasa:

    def _con_tanqueo(self, excede):
        return _payload(gastos=[{
            'id': 1, 'categoria': 'combustible', 'fecha': '2026-03-05',
            'valor': '168000.00', 'proveedor': 'Terpel',
            'documento_numero': 'FAC-1', 'centro_op': '003',
            'origen_costo': 'tarjeta_convenio', 'descripcion': None,
            'periodo_desde': '2026-03-05', 'periodo_hasta': '2026-03-05',
            'cubre_periodo': False, 'lectura_id': 1, 'km': 100000,
            'registrado_por_usuario_id': 1,
            'tanqueo': {'galones': '22.000', 'tanque': 'lleno',
                        'estacion': 'Terpel Neiva', 'precio_galon': '7636.36',
                        'excede_capacidad': excede},
        }])

    def test_el_exceso_se_ve_en_la_fila_y_no_acusa(self, tmp_path):
        html = _expediente(tmp_path, self._con_tanqueo(True))
        assert 'no pueden ser los dos' in html
        assert 'mirar cuál' in html
        for palabra in ('robo', 'hurto', 'culpable'):
            assert palabra not in html.lower()

    def test_sin_capacidad_la_fila_dice_que_NO_se_reviso(self, tmp_path):
        """`sin_dato` no se pinta como un tanqueo normal: `False` significaría
        «se revisó y está bien»."""
        html = _expediente(tmp_path, self._con_tanqueo('sin_dato'))
        assert 'no se pudo' in html.lower()
        assert 'No es que esté bien' in html

    def test_un_tanqueo_normal_no_pinta_ningun_aviso(self, tmp_path):
        """La otra dirección: sin esto, un aviso incondicional pasaría los dos
        de arriba y la pantalla marcaría todos los tanqueos."""
        html = _expediente(tmp_path, self._con_tanqueo(False))
        assert 'no pueden ser los dos' not in html
        assert 'No es que esté bien' not in html

    def test_un_gasto_sin_documento_se_marca_en_la_fila(self, tmp_path):
        p = self._con_tanqueo(False)
        p['gastos'][0]['documento_numero'] = None
        html = _expediente(tmp_path, p)
        assert 'sin documento' in html
        assert 'causación' in html

    def test_el_precio_del_galon_se_ve_formateado_y_no_en_notacion_cientifica(
            self, tmp_path):
        """`1.400E+4` no es un precio que alguien lea. El defecto ya ocurrió en
        este módulo con `Carlos Pérez · undefined`."""
        p = self._con_tanqueo(False)
        p['gastos'][0]['tanqueo']['precio_galon'] = '14000.00'
        html = _expediente(tmp_path, p)
        assert '$14.000' in html
        assert 'E+' not in html
