"""
Qué orígenes puede tener una lectura suelta, y por qué no todos los del enum.

`OrigenLectura` nombra los seis gestos que **existen en el modelo**.
`ORIGENES_LECTURA_SUELTA` declara cuáles de esos gestos **son este endpoint**.
No son la misma lista y confundirlas deja tres agujeros distintos:

  · `entrega` — la escribe el traspaso atómico, que además cierra la custodia
    anterior. Una lectura suelta declarada `entrega` inventa un cambio de turno
    que nunca pasó, y queda indistinguible de las reales en el histórico.
  · `preoperacional` / `ot` — no tienen fuente todavía. La lectura apuntaría a
    un padre imposible, y el día que existan habrá filas viejas que no se
    pueden reconciliar con ninguna inspección ni ninguna orden.

El desplegable ofrecía los cinco (2026-08-03). Esconderlos en el JS habría
dejado la API aceptándolos igual: el arreglo es la lista en el dominio, y este
archivo verifica que la pantalla y la frontera lean **esa misma lista**.
"""
import re
from pathlib import Path

import pytest

from flota.dominio.valores import (
    MOTIVO_ORIGEN_NO_SUELTO,
    ORIGENES_LECTURA_SUELTA,
    OrigenLectura,
)

_PWA = Path(__file__).resolve().parents[2] / 'app' / 'static' / 'pwa'
_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


@pytest.fixture
def vehiculo(db):
    from app.models.vehiculo import Vehiculo

    v = Vehiculo(placa='ORG100', tipo='NHR', activo=True)
    db.session.add(v)
    db.session.commit()
    return v.placa


class TestLaListaDelDominio:

    def test_todo_origen_suelto_existe_en_el_enum(self):
        for o in ORIGENES_LECTURA_SUELTA:
            assert isinstance(o, OrigenLectura)

    def test_es_un_subconjunto_estricto(self):
        """Si algún día son iguales, esta lista dejó de decidir algo."""
        assert set(ORIGENES_LECTURA_SUELTA) < set(OrigenLectura)

    @pytest.mark.parametrize('prohibido', ['entrega', 'preoperacional', 'ot'])
    def test_los_tres_que_no_van(self, prohibido):
        assert OrigenLectura(prohibido) not in ORIGENES_LECTURA_SUELTA

    def test_todo_origen_excluido_tiene_su_motivo_escrito(self):
        """El mapa de motivos es total sobre el complemento — y la frontera lo
        indexa directo confiando en eso.

        Agregar un origen al enum sin decidir de qué lado cae dejaría un
        KeyError en un 400: el usuario vería un 500 en vez del motivo.
        """
        excluidos = set(OrigenLectura) - set(ORIGENES_LECTURA_SUELTA)
        assert set(MOTIVO_ORIGEN_NO_SUELTO) == excluidos
        for motivo in MOTIVO_ORIGEN_NO_SUELTO.values():
            assert len(motivo) > 20, 'un motivo de una palabra no explica nada'


class TestLaFronteraHTTP:
    """La pantalla es una sugerencia; el endpoint es la política.

    Un cliente que mande el JSON a mano no ve el desplegable.
    """

    @pytest.mark.parametrize('prohibido', ['entrega', 'preoperacional', 'ot'])
    def test_un_origen_sin_fuente_no_entra(
            self, client, jwt_token_admin, vehiculo, prohibido):
        r = client.post('/flota/odometro',
                        json={'placa': vehiculo, 'valor_km': 1000,
                              'origen': prohibido},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 400, (
            f'origen {prohibido!r} entró: la lectura queda apuntando a un '
            f'padre que no existe'
        )

    def test_el_error_dice_por_que_y_no_solo_que_no(
            self, client, jwt_token_admin, vehiculo):
        """Quien lo reciba tiene que saber qué hacer en su lugar."""
        r = client.post('/flota/odometro',
                        json={'placa': vehiculo, 'valor_km': 1000, 'origen': 'ot'},
                        headers=_auth(jwt_token_admin))
        cuerpo = r.get_json()
        assert 'tanqueo' in cuerpo['error']       # los que sí puede usar
        assert cuerpo['motivo']                   # y la razón del que eligió

    def test_una_entrega_falsa_no_se_cuela_por_aca(
            self, client, jwt_token_admin, vehiculo):
        """El caso más caro de los tres: no falta una tabla, sobra un hecho.

        `entrega` sí tiene fuente — el traspaso. Aceptarla acá no deja una
        referencia colgada: deja un cambio de turno que nadie hizo, con la
        misma forma que los verdaderos.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        antes = LecturaOdometro.query.count()
        r = client.post('/flota/odometro',
                        json={'placa': vehiculo, 'valor_km': 1000,
                              'origen': 'entrega'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 400
        assert LecturaOdometro.query.count() == antes, 'quedó escrita igual'

    @pytest.mark.parametrize('valido', ['tanqueo', 'cierre_dia'])
    def test_los_que_sí_van_siguen_pasando(
            self, client, jwt_token_admin, vehiculo, valido):
        """El guard nuevo no puede haber cerrado la puerta principal."""
        r = client.post('/flota/odometro',
                        json={'placa': vehiculo, 'valor_km': 1000,
                              'origen': valido},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201, r.get_json()

    def test_la_correccion_sigue_exigiendo_motivo(
            self, client, jwt_token_admin, vehiculo):
        """Habilitarla en el selector no relajó su regla."""
        client.post('/flota/odometro',
                    json={'placa': vehiculo, 'valor_km': 5000, 'origen': 'tanqueo'},
                    headers=_auth(jwt_token_admin))
        r = client.post('/flota/odometro',
                        json={'placa': vehiculo, 'valor_km': 4000,
                              'origen': 'correccion'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 409


class TestElSelectorNoPuedeDivergirDelDominio:
    """TRINQUETE — la pantalla se valida contra la tupla, no contra una copia.

    Es el mismo patrón que `test_toda_opcion_del_selector_es_un_rol_valido`:
    dos listas de lo mismo en dos archivos divergen, y el usuario se entera
    eligiendo una opción que el backend rechaza.
    """

    def _opciones_del_selector(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        i = js.index('<select id="od-origen"')
        bloque = js[i:js.index('</select>', i)]
        return set(re.findall(r'<option value="([a-z_]+)"', bloque))

    def test_el_selector_ofrece_exactamente_los_habilitados(self):
        esperado = {o.value for o in ORIGENES_LECTURA_SUELTA}
        assert self._opciones_del_selector() == esperado, (
            '\nEl desplegable de origen y ORIGENES_LECTURA_SUELTA no coinciden.\n'
            'Habilitar un origen se hace en el dominio; la pantalla lo sigue.'
        )

    def test_encontro_opciones_de_verdad(self):
        """Si el regex deja de encontrarlas, el test de arriba pasa vacío.

        Un guard que compara dos conjuntos vacíos da verde igual que uno que
        compara bien — y son cosas distintas.
        """
        assert len(self._opciones_del_selector()) >= 3


class TestNadieRenderizaLaCedulaAMano:
    """TRINQUETE — `Carlos Pérez · undefined` (2026-08-03).

    `listar_conductores` borra cédula, teléfono y email salvo para admin y jefe
    de almacén: es el guard de datos personales, no un campo perdido. Cualquier
    pantalla que interpole `c.cedula` directo escribe `undefined` para los demás
    roles — pasó en `flota.js` **y** en `rutas.js`, con la misma forma.

    Por eso la política vive en `identidadConductor()` y este guard prohíbe la
    otra vía: el mismo fallback en dos sitios diverge, y ya costó 25× una vez.
    """

    @staticmethod
    def _fuera_del_helper(lineas):
        """Rinde (n, línea) salteando el cuerpo de `identidadConductor`.

        El helper interpola la cédula porque **él es la política**. Se lo
        detecta por su cuerpo, no por número de línea ni por una lista de
        exenciones: un guard que se mantiene a mano se desactualiza y termina
        apagado.
        """
        dentro = False
        for n, linea in enumerate(lineas, 1):
            if linea.startswith('function identidadConductor'):
                dentro = True
            elif dentro and linea.startswith('}'):
                dentro = False
                continue
            if not dentro:
                yield n, linea

    def test_ningun_modulo_interpola_cedula_directo(self):
        culpables = []
        for js in sorted(_PWA.glob('*.js')):
            lineas = js.read_text(encoding='utf-8').split('\n')
            for n, linea in self._fuera_del_helper(lineas):
                if re.search(r'\$\{[^}]*\.cedula[^}]*\}', linea):
                    culpables.append(f'{js.name}:{n}')
        assert not culpables, (
            '\nInterpolan la cédula directo y escriben "undefined" para todo rol '
            'que no sea admin o jefe de almacén:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nUsar identidadConductor(c, lista) — app.js.'
        )

    def test_la_exclusion_no_apaga_el_guard(self):
        """Saltear el helper no puede saltear todo lo que viene después.

        Es la falla que no se ve: el guard sigue en verde y ya no mira nada.
        """
        lineas = ['function identidadConductor(c, todos) {',
                  '  if (c.cedula) return `CC ${c.cedula}`;',
                  '}',
                  'el.innerHTML = `CC ${c.cedula}`;']
        fuera = list(self._fuera_del_helper(lineas))
        assert any('.cedula' in l for _, l in fuera), 'la exclusión se comió todo'
        assert not any('if (c.cedula)' in l for _, l in fuera)

    def test_el_helper_existe_y_contempla_el_homonimo(self):
        """Sin cédula, dos conductores con el mismo nombre son un solo renglón.

        En un desplegable donde se elige quién responde por un vehículo, eso no
        es cosmético: la custodia queda a nombre de la persona equivocada.
        """
        app_js = (_PWA / 'app.js').read_text(encoding='utf-8')
        assert 'function identidadConductor' in app_js
        i = app_js.index('function identidadConductor')
        cuerpo = app_js[i:i + 400]
        assert 'homonimos' in cuerpo, 'el helper no distingue homónimos'
