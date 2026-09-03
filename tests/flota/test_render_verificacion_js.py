"""
La cola de verificación, EJECUTADA — no leída.

Un aserto de substring sobre `flota.js` pasa con la pantalla entera
desconectada: ya pasó en este repo con un `|| true` que anuló el pintado y
sobrevivió a la suite. Acá el `flota.js` real corre en Node y se mira **lo que
quedó pintado**.

Los dos arneses se importan en vez de copiarse por cuarta vez (regla 0):
`test_render_salud_js` devuelve lo que la función retorna —sirve para el bloque
del tablero— y `test_render_gastos_js` lee `#flota-recibo` —sirve para la
pantalla, que no devuelve nada—. Si alguien los cambia, esto se rompe y se
entera, que es lo que hay que querer.

## Lo que se verifica, y por qué ninguna es cosmética

· **La fila sin foto lo dice.** Las 26 lecturas de producción no tienen foto del
  tablero: confirmarlas es la palabra de quien confirma, no la de una evidencia.
  Esconderlo convertiría 26 verificaciones en 26 firmas sobre nada.
· **El bloque desaparece cuando la cola está vacía.** La disciplina de todos los
  bloques de esta pantalla: un tablero que siempre muestra algo se deja de mirar.
· **El motivo se pinta.** Sin él, quien abre la cola ve un número marcado y
  ninguna razón para desconfiar — y a la tercera vez deja de abrirla.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.flota.test_render_gastos_js import HARNESS as HARNESS_PINTADO
from tests.flota.test_render_salud_js import HARNESS as HARNESS_RETORNO
from tests.flota.test_render_salud_js import _pintar, _sano

RAIZ = Path(__file__).resolve().parents[2]
FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'
_RUTA = '/flota/odometro/dudosas'


def _correr(tmp_path, harness, guion) -> str:
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(harness, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps(guion), encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(FLOTA_JS), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'la pantalla reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)['html']


def _bloque(tmp_path, cola) -> str:
    """El aviso del tablero: `flotaBloqueDudosas()` y lo que devuelve."""
    return _correr(tmp_path, HARNESS_RETORNO,
                   {'fn': 'flotaBloqueDudosas', 'rutas': {_RUTA: cola}})


def _pantalla(tmp_path, cola) -> str:
    """La cola completa: `flotaRenderVerificacion()` y lo que dejó pintado."""
    return _correr(tmp_path, HARNESS_PINTADO,
                   {'fn': 'flotaRenderVerificacion', 'placa': '',
                    'rutas': {_RUTA: cola}})


def _fila(**extra):
    base = {'lectura_id': 7, 'vehiculo_id': 1, 'placa': 'THP696',
            'valor_km': 16697948, 'ts': '2026-08-18T05:00:00Z',
            'origen': 'entrega', 'tiene_foto': False, 'foto_id': None,
            'motivo': 'sin foto del tablero: el número no se puede cotejar '
                      'contra el vehículo'}
    base.update(extra)
    return base


def _cola(*filas):
    return {'pendientes': list(filas), 'total': len(filas)}


class TestElBloqueDelTableroNoGritaSinMotivo:

    def test_una_cola_vacia_no_ocupa_espacio(self, tmp_path):
        assert _bloque(tmp_path, _cola()) == ''

    def test_con_pendientes_se_ve_el_numero_y_el_botón(self, tmp_path):
        html = _bloque(tmp_path, _cola(_fila(), _fila(lectura_id=8)))
        assert '2 kilometraje(s) sin verificar' in html
        assert 'flotaAbrirVerificacion()' in html

    def test_dice_que_el_CPK_no_se_publica_en_vez_de_sonar_a_tramite(
            self, tmp_path):
        """«Pendiente de verificación» se lee como papeleo. Lo que de verdad
        pasa es que el costo por kilómetro de ese vehículo no existe."""
        html = _bloque(tmp_path, _cola(_fila()))
        assert 'se publica' in html
        assert 'sin dato' in html

    def test_un_servidor_que_no_responde_no_tumba_el_tablero(self, tmp_path):
        """El bloque vive en la pantalla del administrador, arriba de todo."""
        assert _correr(tmp_path, HARNESS_RETORNO,
                       {'fn': 'flotaBloqueDudosas', 'rutas': {}}) == ''


class TestLaColaMuestraLoQueHaceFaltaParaDecidir:

    def test_el_numero_grande_y_la_placa_al_lado(self, tmp_path):
        html = _pantalla(tmp_path, _cola(_fila()))
        assert '16.697.948 km' in html, (
            f'el kilometraje no salió formateado y legible: {html[:300]}')
        assert 'THP696' in html

    def test_el_motivo_se_pinta(self, tmp_path):
        """Con un motivo que **no** sea el de la foto, a propósito.

        Con `motivo='sin foto del tablero…'` este test pasaba aunque la fila no
        pintara el motivo: la misma frase la aporta el aviso de «sin foto». Un
        guard verde por otra razón, destapado por la mutación que borró el
        renglón del motivo. El salto de ×301 es el del THP696 y solo puede venir
        del campo `motivo`.
        """
        html = _pantalla(tmp_path, _cola(_fila(
            tiene_foto=True, foto_id=42,
            motivo='salto de ×301 respecto de la lectura anterior '
                   '(55349 → 16697948)')))
        assert 'salto de ×301' in html

    def test_sin_foto_lo_dice_en_vez_de_ofrecer_un_boton_que_no_lleva_a_nada(
            self, tmp_path):
        """**El caso de las 26.** Confirmar sin foto es la palabra de quien
        confirma: quien lo hace tiene derecho a saber qué está firmando."""
        html = _pantalla(tmp_path, _cola(_fila(tiene_foto=False)))
        assert 'es tu palabra' in html
        assert 'flotaVerFoto(' not in html

    def test_con_foto_ofrece_verla(self, tmp_path):
        """La otra dirección: si nunca pintara el botón, la cola no cumpliría
        lo único que se le pidió —la foto grande, el número al lado—."""
        html = _pantalla(tmp_path, _cola(_fila(tiene_foto=True, foto_id=42)))
        assert 'flotaVerFoto(42' in html
        assert 'es tu palabra' not in html

    def test_las_dos_salidas_estan(self, tmp_path):
        html = _pantalla(tmp_path, _cola(_fila()))
        assert 'flotaConfirmarKm(7)' in html
        assert 'flotaCorregirKm(' in html

    def test_hay_donde_pintar_la_foto(self, tmp_path):
        """Sin `#flota-visor` el botón contesta con un error interno — es lo
        que Yesid vio el 2026-08-05."""
        html = _pantalla(tmp_path, _cola(_fila(tiene_foto=True, foto_id=42)))
        assert 'id="flota-visor"' in html

    def test_una_cola_vacia_explica_cuando_entra_una_lectura(self, tmp_path):
        html = _pantalla(tmp_path, _cola())
        assert 'No hay kilometrajes en duda' in html
        assert 'foto del tablero' in html


class TestElBloqueDeSaludPublicaLaDeudaYElTrabajo:
    """Los dos campos nuevos del health, en el tablero. Un campo que nadie mira
    es el defecto que este módulo lleva la semana arreglando."""

    def test_las_dudosas_pendientes_se_ven(self, tmp_path):
        html = _pintar(tmp_path, _sano(lecturas_dudosas_pendientes=26))
        assert '26 kilometraje(s) esperan verificación' in html
        assert 'no calculable' in html

    def test_las_verificadas_del_mes_tambien(self, tmp_path):
        html = _pintar(tmp_path, _sano(lecturas_verificadas_30d=4))
        assert '4 kilometraje(s) verificados este mes' in html

    def test_van_en_lineas_SEPARADAS(self, tmp_path):
        """Sumadas —o convertidas en un porcentaje— «cero verificadas sobre
        cero dudosas» (flota sana) se ve igual que «cero sobre veinte» (cola
        que nadie abre), y esos son los dos estados que hay que distinguir."""
        html = _pintar(tmp_path, _sano(lecturas_dudosas_pendientes=20,
                                       lecturas_verificadas_30d=3))
        assert '20 kilometraje(s) esperan verificación' in html
        assert '3 kilometraje(s) verificados este mes' in html

    def test_una_flota_sin_dudosas_ni_verificaciones_no_ocupa_espacio(
            self, tmp_path):
        assert _pintar(tmp_path, _sano(lecturas_dudosas_pendientes=0,
                                       lecturas_verificadas_30d=0)) == ''
