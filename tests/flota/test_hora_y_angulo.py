"""
Dos datos que el sistema tenía y no decía: de qué zona es una hora, y de qué
parte del vehículo es una foto.

Los dos se reportaron el 2026-08-03 usando la app de verdad, y los dos son de la
misma familia — no falta el dato, falta lo que el dato significa:

  · Un recibo hecho a las 15:00 se mostraba como 20:00. La hora estaba bien
    guardada; lo que faltaba era decir que era UTC. `new Date()` la leía como
    hora local del teléfono y corría el reloj cinco horas.
  · Ocho fotos por turno se guardaban sin ángulo. El orden tampoco las
    identificaba: el frontend filtra las faltantes antes de enviar, así que con
    `frontal` sin tomar, la primera es `trasera` y todo queda corrido.

Un número que parece correcto y está mal es peor que un hueco. El hueco se ve.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flota.api._tiempo import iso_utc
from flota.dominio.valores import (
    ANGULOS_FIJOS,
    ANGULOS_FOTO,
    MAX_POSICIONES_LLANTA,
    POSICIONES_LLANTA_FALLBACK,
    angulos_de_custodia,
    posiciones_llanta,
)

_PWA = Path(__file__).resolve().parents[2] / 'app' / 'static' / 'pwa'
_H = 'Authorization'
# Un timestamp con parte horaria. Las fechas sueltas (Date) no llevan zona.
_ISO_CON_HORA = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}')


def _auth(t):
    return {_H: f'Bearer {t}'}


@pytest.fixture
def mundo(db):
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='HZA900', tipo='Turbo', activo=True)
    alm = Almacen(codigo='HZ-SEDE', nombre='Sede hora')
    db.session.add_all([veh, alm])
    db.session.flush()
    con = Conductor(nombre='Conductor HZ', cedula='HZ-1', activo=True)
    db.session.add(con)
    db.session.commit()
    return {'placa': veh.placa, 'veh': veh.id, 'con': con.id}


# ── La hora ──────────────────────────────────────────────────────────────────

class TestIsoUtc:

    def test_un_naive_sale_declarando_utc(self):
        s = iso_utc(datetime(2026, 8, 3, 20, 4, 12))
        assert s.endswith('+00:00'), f'sin zona: {s}'

    def test_none_sigue_siendo_none(self):
        """Una custodia sin cerrar no tiene `fin_ts`. Eso es un hecho."""
        assert iso_utc(None) is None

    def test_un_aware_se_respeta(self):
        s = iso_utc(datetime(2026, 8, 3, 15, 4, 12, tzinfo=timezone.utc))
        assert s.endswith('+00:00')

    def test_las_15_de_colombia_son_las_20_utc(self):
        """El caso exacto del reporte, al revés: la conversión que la pantalla
        tiene que hacer para volver a mostrar 15:00."""
        from datetime import timedelta

        utc = datetime(2026, 8, 3, 20, 4, tzinfo=timezone.utc)
        assert (utc - timedelta(hours=5)).hour == 15


class TestNingunaHoraSaleSinZona:
    """La propiedad, no un endpoint puntual.

    Recorre la respuesta entera: cualquier string con forma de timestamp tiene
    que traer zona. Así un endpoint nuevo queda cubierto sin que nadie se
    acuerde de agregarlo acá.
    """

    def _sin_zona(self, obj, ruta=''):
        malos = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                malos += self._sin_zona(v, f'{ruta}.{k}')
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                malos += self._sin_zona(v, f'{ruta}[{i}]')
        elif isinstance(obj, str) and _ISO_CON_HORA.match(obj):
            if not (obj.endswith('Z') or re.search(r'[+-]\d{2}:\d{2}$', obj)):
                malos.append(f'{ruta} = {obj}')
        return malos

    def test_custodia_activa(self, client, jwt_token_admin, mundo):
        client.post('/flota/custodia/traspaso',
                    json={'placa': mundo['placa'], 'km': 100,
                          'custodio_tipo': 'conductor',
                          'custodio_conductor_id': mundo['con']},
                    headers=_auth(jwt_token_admin))
        r = client.get(f"/flota/custodia/activa/{mundo['placa']}",
                       headers=_auth(jwt_token_admin))
        malos = self._sin_zona(r.get_json())
        assert not malos, (
            '\nTimestamps sin zona — JavaScript los lee como hora local:\n'
            + '\n'.join(f'  · {m}' for m in malos))

    def test_cierres_forzados(self, client, jwt_token_admin, mundo):
        r = client.get('/flota/custodia/cierres-forzados',
                       headers=_auth(jwt_token_admin))
        assert not self._sin_zona(r.get_json())

    def test_encontro_al_menos_un_timestamp(self, client, jwt_token_admin, mundo):
        """Si la respuesta no trae ninguno, el test de arriba pasa vacío."""
        client.post('/flota/custodia/traspaso',
                    json={'placa': mundo['placa'], 'km': 100,
                          'custodio_tipo': 'conductor',
                          'custodio_conductor_id': mundo['con']},
                    headers=_auth(jwt_token_admin))
        r = client.get(f"/flota/custodia/activa/{mundo['placa']}",
                       headers=_auth(jwt_token_admin))
        assert 'inicio_ts' in str(r.get_json())


class TestLaPantallaConvierte:
    """TRINQUETE — cortar el string no convierte nada.

    `ts.slice(0, 16)` mostraba UTC crudo y parecía correcto: tenía la forma de
    una hora. Ese es el punto — un formato válido con el número equivocado.
    """

    def test_existe_el_helper_y_usa_la_zona_de_colombia(self):
        app_js = (_PWA / 'app.js').read_text(encoding='utf-8')
        assert 'function horaColombia' in app_js
        i = app_js.index('function horaColombia')
        assert "America/Bogota" in app_js[i:i + 700]

    @staticmethod
    def _es_comentario(linea):
        """Un comentario que CITA el patrón no lo ejecuta.

        Se filtra en la detección y no con una lista de exenciones: el docstring
        de `horaColombia` explica qué reemplaza, y un guard que castiga a su
        propia documentación es un guard que alguien va a apagar.
        """
        t = linea.strip()
        return t.startswith('*') or t.startswith('//') or t.startswith('/*')

    def test_ningun_modulo_corta_un_timestamp_a_mano(self):
        culpables = []
        for js in sorted(_PWA.glob('*.js')):
            for n, l in enumerate(js.read_text(encoding='utf-8').split('\n'), 1):
                if self._es_comentario(l):
                    continue
                if re.search(r"\.slice\(0,\s*16\).*replace\('T'", l):
                    culpables.append(f'{js.name}:{n}')
        assert not culpables, (
            '\nCortan el string en vez de convertir la zona — muestran UTC:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nUsar horaColombia(iso) — app.js.')

    def test_el_filtro_de_comentarios_no_apaga_el_guard(self):
        """Saltear comentarios no puede saltear el código.

        Es la falla que no se ve: verde y ya no mira nada.
        """
        assert self._es_comentario(" * Lo que reemplaza: ts.slice(0, 16)")
        assert self._es_comentario("// ts.slice(0, 16).replace('T', ' ')")
        assert not self._es_comentario("  const x = ts.slice(0, 16).replace('T', ' ');")


# ── El ángulo ────────────────────────────────────────────────────────────────

class TestVocabularioDeAngulos:

    def test_los_fijos_no_incluyen_llantas_en_plural(self):
        """`llantas` era una sola foto para 4 o 6 ruedas: no ubicaba nada."""
        assert 'llantas' not in ANGULOS_FIJOS
        assert 'llantas' not in ANGULOS_FOTO

    def test_un_camion_pide_mas_fotos_que_un_furgon(self):
        assert len(angulos_de_custodia(6)) > len(angulos_de_custodia(4))

    def test_todo_angulo_generado_esta_en_el_vocabulario(self):
        """Si no, el CHECK de la tabla rechaza una foto legítima."""
        for n in range(1, MAX_POSICIONES_LLANTA + 1):
            for a in angulos_de_custodia(n):
                assert a in ANGULOS_FOTO, f'{a} no está en el CHECK'

    def test_fuera_de_rango_levanta(self):
        with pytest.raises(ValueError):
            angulos_de_custodia(0)
        with pytest.raises(ValueError):
            angulos_de_custodia(MAX_POSICIONES_LLANTA + 1)


class TestDeDondeSaleElNumeroDeLlantas:
    """Tres fuentes con tres niveles de confianza, y se dicen distinto."""

    def test_la_ficha_manda(self):
        assert posiciones_llanta(6, 'Van') == (6, 'ficha')

    def test_sin_ficha_se_deduce_del_tipo(self):
        assert posiciones_llanta(None, 'Turbo') == (6, 'tipo')
        assert posiciones_llanta(None, 'Van') == (4, 'tipo')

    def test_tolera_tildes_y_mayusculas(self):
        assert posiciones_llanta(None, 'CAMIÓN') == (6, 'tipo')

    def test_un_tipo_desconocido_no_levanta_pero_se_declara(self):
        """Dejar al conductor sin formulario a las 5 a.m. es peor que pedirle
        cuatro fotos y decirle que el número es un supuesto."""
        n, fuente = posiciones_llanta(None, 'Nave espacial')
        assert n == POSICIONES_LLANTA_FALLBACK
        assert fuente == 'fallback'


class TestElAnguloLlegaHastaLaFila:

    def _traspaso(self, client, token, placa, con_id, angulos):
        from flota.adaptadores.almacen_fotos import guardar_foto  # noqa: F401

        fotos = [{'clase': 'evidencia_estado', 'angulo': a,
                  'data_url': _DATA_URL, 'ancho': 800, 'alto': 600}
                 for a in angulos]
        return client.post('/flota/custodia/traspaso',
                           json={'placa': placa, 'km': 500,
                                 'custodio_tipo': 'conductor',
                                 'custodio_conductor_id': con_id,
                                 'fotos_inicio': fotos},
                           headers=_auth(token))

    def test_cada_foto_queda_con_su_angulo(self, client, jwt_token_admin, mundo,
                                           tmp_path, monkeypatch):
        from flota.adaptadores.modelos import Foto

        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        r = self._traspaso(client, jwt_token_admin, mundo['placa'], mundo['con'],
                           ['frontal', 'llanta_1', 'llanta_2'])
        assert r.status_code == 201, r.get_json()
        guardados = {f.angulo for f in Foto.query.all()}
        assert {'frontal', 'llanta_1', 'llanta_2'} <= guardados

    def test_el_orden_ya_no_es_lo_que_identifica(self, client, jwt_token_admin,
                                                 mundo, tmp_path, monkeypatch):
        """El bug de fondo: faltando `frontal`, la primera foto era `trasera`.

        Con ángulo explícito, saltarse una no corre las demás.
        """
        from flota.adaptadores.modelos import Foto

        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        self._traspaso(client, jwt_token_admin, mundo['placa'], mundo['con'],
                       ['trasera', 'llanta_2'])
        primera = Foto.query.order_by(Foto.id).first()
        assert primera.angulo == 'trasera', 'el orden volvió a decidir el ángulo'

    def test_un_angulo_inventado_no_entra(self, client, jwt_token_admin, mundo,
                                          tmp_path, monkeypatch):
        """El CHECK de la tabla. El cliente no define el vocabulario."""
        from sqlalchemy.exc import IntegrityError

        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        with pytest.raises(IntegrityError):
            self._traspaso(client, jwt_token_admin, mundo['placa'], mundo['con'],
                           ['debajo_del_asiento'])


class TestElSelectorDeAngulosNoEstaQuemado:
    """TRINQUETE — la pantalla arma los ángulos con lo que manda el servidor."""

    def test_flota_js_no_tiene_llantas_en_plural(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert "'llantas'" not in js, (
            'Quedó el ángulo único `llantas`: una foto para 4 o 6 ruedas no '
            'ubica un flanco herido en ninguna.')

    def test_los_angulos_vienen_de_la_respuesta(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert 'FLOTA_ESTADO.angulos' in js, (
            'La pantalla no lee los ángulos del servidor: volvería a pedir '
            'los mismos para todos los vehículos.')


class TestElVisorExisteYSeEnciende:
    """TRINQUETE — el almacén dejó de ser de solo escritura.

    El único enlace "ver foto" del PWA era un `<a href target=_blank>` contra un
    endpoint con `@jwt_required()`. Una pestaña nueva no manda headers: devolvía
    401 siempre. Nunca funcionó, y como nadie lo abrió, nadie lo supo.
    """

    def test_ninguna_foto_se_abre_con_un_ancla(self):
        for js in sorted(_PWA.glob('*.js')):
            texto = js.read_text(encoding='utf-8')
            assert 'href="${API}/flota/foto/' not in texto, (
                f'{js.name}: abre la foto en una pestaña sin header de '
                f'autorización — 401 garantizado.')

    def test_el_visor_manda_el_token(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        i = js.index('async function flotaVerFoto')
        cuerpo = js[i:i + 900]
        assert 'Bearer' in cuerpo, 'el visor no manda el token'

    def test_tiene_quien_lo_llame(self):
        """Una función sin caller es el patrón que ya costó cuatro veces."""
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert js.count('flotaVerFoto(') >= 2, 'definida y nunca invocada'
        assert 'flotaVerFotosDeCustodia(' in js

    def test_el_contenedor_existe_donde_se_usa(self):
        """`flotaVerFoto` escribe en `#flota-visor`. Si la pantalla que tiene el
        botón no lo declara, el botón no hace nada — que es exactamente cómo
        sobrevivió el enlace roto."""
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert js.count('id="flota-visor"') >= 2, (
            'Hay pantallas con botón de ver foto y sin contenedor donde '
            'mostrarla.')


class TestLaPlacaSeTieneQueVer:
    """TRINQUETE — el resguardo existía y era invisible.

    El modal se diseñó con la placa en un encabezado pegajoso para que nadie
    registrara un odómetro en el camión equivocado. Pero `#banner-modo` está en
    `z-index: 9999` y el modal en `900`: el banner pintaba encima y tapaba
    exactamente eso. En las capturas del 2026-08-03 se lee "Recibo de turno" y
    la placa no aparece — el usuario no podía saber sobre qué vehículo estaba
    trabajando, que es el escenario entero que el modal existía para impedir.
    """

    def test_el_modal_se_corre_debajo_del_banner(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert 'flotaBajarModalDebajoDelBanner' in js
        i = js.index('function flotaBajarModalDebajoDelBanner')
        assert "banner-modo" in js[i:i + 600], (
            'el ajuste no consulta el banner: si el banner cambia de alto, '
            'la placa se vuelve a tapar')

    def test_se_llama_al_abrir_y_no_solo_se_define(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert js.count('flotaBajarModalDebajoDelBanner') >= 2, (
            'definida y nunca invocada — el patrón función-sin-caller sobre el '
            'arreglo de un resguardo invisible')

    def test_el_modal_no_le_gana_el_z_index_al_banner(self):
        """Subirle el z-index al modal taparía el aviso de DATOS DE PRUEBA.

        Los dos avisos importan y dicen cosas distintas: uno qué camión, el otro
        si lo que se está cargando cuenta como real.
        """
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        html = (_PWA / 'index.html').read_text(encoding='utf-8')
        z_modal = int(re.search(r'flota-modal[\s\S]{0,200}?z-index:(\d+)', js).group(1))
        z_banner = int(re.search(r'banner-modo[\s\S]{0,200}?z-index:(\d+)', html).group(1))
        assert z_modal < z_banner, (
            'El modal tapa el banner de modo: alguien puede cargar datos de '
            'prueba creyendo que son reales.')

    def test_la_placa_esta_tambien_en_el_boton_de_confirmar(self):
        """Lo último que se mira antes del gesto irreversible."""
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        i = js.index('id="flota-guardar"')
        assert 'FLOTA_PLACA' in js[i:i + 300]


class TestNingunSelectorCssDefinidoDosVeces:
    """TRINQUETE — `.flota-modal-cabeza` estaba escrito dos veces, idéntico.

    Dos copias idénticas hoy son dos copias distintas en tres meses: alguien
    ajusta el padding en la primera, la segunda gana por orden de cascada, y el
    cambio "no hace nada" sin que se entienda por qué. Es el mismo patrón que ya
    costó 25× en el kardex, aplicado a estilos.
    """

    def test_sin_duplicados(self):
        html = (_PWA / 'index.html').read_text(encoding='utf-8')
        selectores = re.findall(r'^\s{4}(\.[a-z][a-z0-9_-]*)\s*\{', html, re.M)
        repetidos = sorted({s for s in selectores if selectores.count(s) > 1})
        assert not repetidos, (
            '\nSelectores CSS definidos más de una vez:\n'
            + '\n'.join(f'  · {s}' for s in repetidos)
            + '\n\nDos copias idénticas hoy divergen mañana.')

    def test_encontro_selectores(self):
        """Si el regex deja de encontrarlos, el test de arriba pasa vacío."""
        html = (_PWA / 'index.html').read_text(encoding='utf-8')
        assert len(re.findall(r'^\s{4}(\.[a-z][a-z0-9_-]*)\s*\{', html, re.M)) > 20


class TestListadoDeFotosDeUnaCustodia:

    def test_lista_los_angulos_que_faltan_tambien(self, client, jwt_token_admin,
                                                  mundo, tmp_path, monkeypatch):
        """Un hueco que no se muestra es un hueco que nadie va a llenar."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': mundo['placa'], 'km': 500,
                              'custodio_tipo': 'conductor',
                              'custodio_conductor_id': mundo['con'],
                              'fotos_inicio': [{'clase': 'evidencia_estado',
                                                'angulo': 'frontal',
                                                'data_url': _DATA_URL,
                                                'ancho': 800, 'alto': 600}]},
                        headers=_auth(jwt_token_admin))
        cid = r.get_json()['custodia_id']
        d = client.get(f'/flota/custodia/{cid}/fotos',
                       headers=_auth(jwt_token_admin)).get_json()
        # Turbo → 6 posiciones → 7 fijos + 6 llantas
        assert len(d['angulos_esperados']) == 13
        assert len(d['fotos']) == 1
        assert d['posiciones_llanta_fuente'] == 'tipo'

    def test_devuelve_la_clase_para_identificar_el_tablero(
            self, client, jwt_token_admin, mundo, tmp_path, monkeypatch):
        """Las fotos anteriores a la migración no tienen ángulo — pero sí clase.

        En un recibo hay exactamente una `foto_dato`: el tablero. Eso identifica
        la foto del odómetro **sin adivinar por el orden**, que es lo único que
        permite verificar si los seis dígitos se leen en las que ya se tomaron.
        """
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': mundo['placa'], 'km': 700,
                              'custodio_tipo': 'conductor',
                              'custodio_conductor_id': mundo['con'],
                              'fotos_inicio': [
                                  {'clase': 'evidencia_estado', 'data_url': _DATA_URL,
                                   'ancho': 800, 'alto': 600},
                                  {'clase': 'foto_dato', 'data_url': _DATA_URL,
                                   'ancho': 1600, 'alto': 1200},
                              ]},
                        headers=_auth(jwt_token_admin))
        cid = r.get_json()['custodia_id']
        d = client.get(f'/flota/custodia/{cid}/fotos',
                       headers=_auth(jwt_token_admin)).get_json()
        sin_angulo = [f for f in d['fotos'] if not f['angulo']]
        assert len(sin_angulo) == 2
        datos = [f for f in sin_angulo if f['clase'] == 'foto_dato']
        assert len(datos) == 1, 'la clase no identifica el tablero'

    def test_la_pantalla_usa_la_clase_y_no_el_orden(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        i = js.index('async function flotaVerFotosDeCustodia')
        cuerpo = js[i:i + 2600]
        assert "clase === 'foto_dato'" in cuerpo, (
            'la pantalla no distingue el tablero por su clase — quien busque el '
            'odómetro tiene que abrir las nueve')

    def test_una_custodia_que_no_existe_es_404(self, client, jwt_token_admin, mundo):
        r = client.get('/flota/custodia/999999/fotos', headers=_auth(jwt_token_admin))
        assert r.status_code == 404


# JPEG mínimo real — bytes de verdad, no un placeholder.
_JPEG_B64 = (
    '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof'
    'Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh'
    'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR'
    'CAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA'
    'AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK'
    'FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG'
    'h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl'
    '5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+iiigD//Z'
)
_DATA_URL = f'data:image/jpeg;base64,{_JPEG_B64}'


class TestLaReferenciaNoCuestaEsperar:
    """El "cómo estaba" no puede comerse el presupuesto de la entrega.

    Cada referencia era un viaje a la red del patio: cuatro toques, cuatro
    esperas. A cinco segundos cada uno son veinte sobre cuarenta — la mitad de
    la entrega gastada en mirar en vez de registrar.

    Se precargan mientras el conductor teclea el odómetro, que son treinta
    segundos en los que la red no hace nada.
    """

    def _js(self):
        return (_PWA / 'flota.js').read_text(encoding='utf-8')

    def test_la_precarga_existe_y_se_dispara(self):
        js = self._js()
        assert js.count('flotaPrecargarReferencias') >= 2, 'definida y sin caller'

    def test_no_bloquea_el_formulario(self):
        """Con `await`, el conductor esperaría las cuatro fotos antes de poder
        escribir el kilometraje — justo al revés de lo que se busca."""
        js = self._js()
        assert 'await flotaPrecargarReferencias' not in js

    def test_el_visor_usa_la_cache_antes_de_ir_a_la_red(self):
        js = self._js()
        i = js.index('async function flotaVerFoto')
        cuerpo = js[i:i + 700]
        assert 'FLOTA_REF_CACHE[fotoId]' in cuerpo, (
            'el visor no consulta la caché: precargar no sirve de nada')

    def test_un_solo_lugar_dibuja_la_foto(self):
        """El atajo de caché y el camino de red comparten el pintado.

        Dos copias del mismo HTML divergen — el mismo fallback en dos sitios ya
        costó 25× en este repo.
        """
        js = self._js()
        assert js.count('function flotaPintarFoto') == 1
        assert js.count('flotaPintarFoto(') >= 3, 'no lo usan los dos caminos'

    def test_si_la_precarga_falla_el_boton_sigue_sirviendo(self):
        """Sin señal, "cómo estaba" tiene que caer al fetch de siempre, no
        quedarse mudo: la referencia es una ayuda, no un requisito."""
        js = self._js()
        i = js.index('function flotaPrecargarReferencias')
        assert '.catch(' in js[i:i + 700]
