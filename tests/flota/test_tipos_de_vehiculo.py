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

from flota.dominio.valores import POSICIONES_LLANTA_POR_TIPO, normalizar_tipo

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
                  if normalizar_tipo(o) not in POSICIONES_LLANTA_POR_TIPO]
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
        assert normalizar_tipo('Camión') in POSICIONES_LLANTA_POR_TIPO
        assert normalizar_tipo('CAMIONETA') in POSICIONES_LLANTA_POR_TIPO


# ═══════════════════════════════════════════════════════════════════════════
# El backend valida contra el mismo catálogo (QA e2e 2026-09-24)
# ═══════════════════════════════════════════════════════════════════════════
#
# `Vehiculo.tipo` era texto libre también en el BACKEND: un POST a mano con
# «Tractomula» daba de alta un vehículo que cae al fallback de llantas. El
# desplegable es una lista; la API tiene que ser la misma lista.

class TestElBackendValidaElTipo:

    def test_el_catalogo_es_el_del_desplegable(self):
        from flota.dominio.valores import TIPOS_VEHICULO
        assert list(TIPOS_VEHICULO) == _opciones_del_desplegable()

    def _admin(self, db):
        from flask_jwt_extended import create_access_token
        from app.models.usuario import Usuario
        u = Usuario(email='tipos@veh.test', nombre='Admin', rol='admin', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}

    def test_un_tipo_fuera_del_catalogo_es_400_claro(self, client, db):
        r = client.post('/api/rutas/vehiculos', json={'placa': 'TIP001', 'tipo': 'Tractomula'},
                        headers=self._admin(db))
        assert r.status_code == 400, r.get_json()
        assert 'Tractomula' in r.get_json()['error'] and 'NHR' in r.get_json()['error']

    def test_se_guarda_el_nombre_canonico(self, client, db):
        r = client.post('/api/rutas/vehiculos', json={'placa': 'TIP002', 'tipo': 'camion'},
                        headers=self._admin(db))
        assert r.status_code == 201, r.get_json()
        assert r.get_json()['vehiculo']['tipo'] == 'Camión'

    def test_cambiar_a_un_tipo_desconocido_es_400(self, client, db):
        from app.models.vehiculo import Vehiculo
        h = self._admin(db)
        v = Vehiculo(placa='TIP003', tipo='NHR', activo=True)
        db.session.add(v)
        db.session.commit()
        r = client.put(f'/api/rutas/vehiculos/{v.id}', json={'tipo': 'Bus'}, headers=h)
        assert r.status_code == 400, r.get_json()

    def test_un_vehiculo_viejo_fuera_del_catalogo_se_sigue_editando(self, client, db):
        """Validar solo cuando el tipo CAMBIA: un tipo viejo no traba lo demás."""
        from app.models.vehiculo import Vehiculo
        h = self._admin(db)
        v = Vehiculo(placa='TIP004', tipo='sencillo', activo=True)
        db.session.add(v)
        db.session.commit()
        r = client.put(f'/api/rutas/vehiculos/{v.id}',
                       json={'tipo': 'sencillo', 'capacidad_kg': 3000}, headers=h)
        assert r.status_code == 200, r.get_json()
