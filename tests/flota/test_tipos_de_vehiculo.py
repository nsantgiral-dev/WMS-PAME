"""
Que el desplegable de tipos y el dominio no se separen.

`Vehiculo.tipo` es texto libre en el backend (`ruta_service.crear_vehiculo`), y
flota lo usa para inferir **cuántas fotos de llanta pedir** cuando el vehículo
todavía no tiene ficha técnica (`POSICIONES_LLANTA_POR_TIPO`). Un valor del
formulario que no esté en ese mapa cae al fallback de 4.

El fallback no es un bug —`posiciones_llanta()` devuelve de dónde salió el
número y la pantalla lo muestra distinto, que es lo correcto: una inferencia no
puede parecer un dato—. Lo que sí sería un bug es que el formulario **ofrezca**
un tipo que el dominio no conoce: ahí el sistema estaría induciendo el caso
degradado en vez de encontrárselo.

Lo destapó agregar «Camioneta» (2026-08-10): el dominio ya la tenía desde
siempre, el desplegable no la ofrecía, y BDT261 no se podía registrar bien.
"""
import re
from pathlib import Path

from flota.dominio.valores import POSICIONES_LLANTA_POR_TIPO, _normalizar

_INDEX = Path(__file__).resolve().parents[2] / 'app' / 'static' / 'pwa' / 'index.html'


def _opciones_del_desplegable():
    """Los `value=` del `<select id="veh-form-tipo">`, sin el vacío.

    Se recorta al bloque del select en vez de barrer el HTML entero: hay otros
    desplegables con `<option>` y contarlos todos haría fallar el test por
    razones que no tienen que ver con flota.
    """
    html = _INDEX.read_text(encoding='utf-8')
    i = html.find('id="veh-form-tipo"')
    assert i != -1, 'no está el select de tipo de vehículo — ¿lo renombraron?'
    fin = html.find('</select>', i)
    assert fin != -1
    return [v for v in re.findall(r'<option value="([^"]*)"', html[i:fin]) if v]


class TestElFormularioNoOfreceTiposQueElDominioIgnora:

    def test_toda_opcion_esta_en_el_mapa_de_llantas(self):
        faltan = [o for o in _opciones_del_desplegable()
                  if _normalizar(o) not in POSICIONES_LLANTA_POR_TIPO]
        assert not faltan, (
            f'\nTipos ofrecidos en el formulario que el dominio no conoce: {faltan}\n'
            f'Cada vehículo registrado así va a pedir 4 fotos de llanta por '
            f'fallback hasta que alguien le levante la ficha.\n'
            f'Agregalos a POSICIONES_LLANTA_POR_TIPO en flota/dominio/valores.py '
            f'con las posiciones reales, o sacalos del desplegable.')

    def test_camioneta_esta_ofrecida(self):
        """La que faltaba. BDT261 no se podía registrar con su tipo real."""
        assert 'Camioneta' in _opciones_del_desplegable()

    def test_el_detector_ve_las_opciones(self):
        """Si el recorte del select dejara de encontrar `<option>`, el test de
        arriba pasaría vacío para siempre."""
        assert len(_opciones_del_desplegable()) >= 6

    def test_la_normalizacion_tolera_tildes_y_mayusculas(self):
        """`Camión` del formulario tiene que encontrar `camion` del mapa —
        si no, el tipo más común del parque cae al fallback."""
        assert _normalizar('Camión') in POSICIONES_LLANTA_POR_TIPO
        assert _normalizar('CAMIONETA') in POSICIONES_LLANTA_POR_TIPO
