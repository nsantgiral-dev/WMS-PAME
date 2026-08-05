"""
Lo que Yesid encontró en una mañana con el THP696 y el UPQ606.

Seis cosas, y ninguna la iba a encontrar un test ni una revisión de código: el
formulario funcionaba, los endpoints respondían y la suite estaba en verde. Las
encontró una persona parada al lado de un camión.

Por orden de lo que costaba:

1. **Las fotos de un vehículo se guardaban en otro.** Evidencia con hash y GPS
   atada a la placa equivocada, con la placa correcta en el rótulo del botón.
2. **Salir del modal descartaba el trabajo sin avisar** — y el mensaje de error
   lo mandaba a apretar ese botón.
3. **La lista de sedes no cargaba** y sin ella no se podía entregar el turno.
4. **La tarjeta de propiedad exigía una fecha de vencimiento que no existe.**
5. **La foto del tablero se pedía dos veces.**
6. **Los laterales y las llantas no tenían convención**, así que `lateral_izq`
   de una persona podía ser el `lateral_der` de otra.
"""
import os
import re
from pathlib import Path

import pytest

_JS = Path(__file__).resolve().parents[2] / 'app' / 'static' / 'pwa' / 'flota.js'


def _js():
    return _JS.read_text(encoding='utf-8')


# ══════════════════════════════════════════════════════════════════════════
# 1 — La evidencia no puede terminar en otro vehículo
# ══════════════════════════════════════════════════════════════════════════

class TestLaPlacaViajaConElFormulario:
    """`FLOTA_PLACA` es una global mutable que tres funciones cambian.

    El rótulo del botón se escribía al DIBUJAR y `payload.placa` se leía al
    APRETARLO. Entre esos dos momentos, abrir la ficha, el odómetro o los
    documentos de otro vehículo cambiaba la global sin redibujar el recibo — y
    las fotos se iban al otro expediente con el rótulo correcto en pantalla.

    El arreglo no es limpiar el estado en esas tres funciones: eso tapa el
    síntoma y deja viva la clase. El formulario lleva su placa adentro y al
    enviar se comprueba que sigan coincidiendo.
    """

    def test_los_tres_botones_sellan_su_placa(self):
        js = _js()
        # Recibo de escritorio, recibo del conductor y entrega.
        assert js.count('data-placa="${FLOTA_PLACA}"') == 3, (
            'algún formulario que manda fotos volvió a depender solo de la global')

    def test_ningun_envio_lee_la_global_como_placa(self):
        """TRINQUETE — `placa: FLOTA_PLACA` es literalmente el bug."""
        js = _js()
        malas = [l.strip() for l in js.split('\n') if re.search(r'placa:\s*FLOTA_PLACA', l)]
        assert not malas, (
            'un envío volvió a tomar la placa de la global mutable en vez del '
            f'formulario: {malas}')

    def test_hay_UNA_funcion_que_resuelve_la_placa(self):
        """Regla 0 — una política, una función.

        Tres formularios con la misma comprobación escrita tres veces divergen,
        y acá divergir significa que uno de los tres vuelve a guardar en el
        vehículo equivocado.
        """
        js = _js()
        assert js.count('function flotaPlacaDelFormulario') == 1
        assert js.count('flotaPlacaDelFormulario(') == 4   # 1 definición + 3 usos

    def test_la_comprobacion_compara_las_dos_fuentes(self):
        js = _js()
        i = js.index('function flotaPlacaDelFormulario')
        cuerpo = js[i:i + 1200]
        assert 'placa !== FLOTA_PLACA' in cuerpo, (
            'sellar la placa sin comparar no sirve: es la comparación la que '
            'convierte un error silencioso en uno que se ve')
        assert 'return null' in cuerpo

    def test_el_mensaje_nombra_los_dos_vehiculos(self):
        """Quien lo lee tiene que saber qué pasó, no solo que algo pasó."""
        js = _js()
        i = js.index('function flotaPlacaDelFormulario')
        cuerpo = js[i:i + 1200]
        assert '${placa}' in cuerpo and '${FLOTA_PLACA}' in cuerpo


# ══════════════════════════════════════════════════════════════════════════
# 2 — Salir no descarta quince minutos de trabajo en silencio
# ══════════════════════════════════════════════════════════════════════════

class TestSalirAvisa:

    def test_el_boton_no_se_llama_Cerrar(self):
        """El rechazo de traspaso pide «cerrar el turno» y este era el único
        botón con esa palabra. Yesid lo apretó buscando cumplir la instrucción."""
        js = _js()
        i = js.index('onclick="flotaCerrarModal()"')
        assert '>Cerrar<' not in js[i:i + 80]

    def test_pregunta_antes_de_descartar_fotos(self):
        js = _js()
        i = js.index('function flotaCerrarModal')
        cuerpo = js[i:i + 800]
        assert 'confirm(' in cuerpo
        assert 'flotaTrabajoSinGuardar()' in cuerpo

    def test_cuenta_el_tablero_ademas_de_la_grilla(self):
        js = _js()
        i = js.index('function flotaTrabajoSinGuardar')
        cuerpo = js[i:i + 400]
        assert 'FLOTA_FOTOS' in cuerpo and 'FLOTA_FOTO_TABLERO' in cuerpo


class TestElRechazoDiceComoSeDestraba:
    """«Tiene que cerrar su turno primero» → «¿cómo se hace?»."""

    def _mensaje(self):
        from flota.dominio.custodia import QuienPide, puede_recibir
        from flota.dominio.valores import Custodia

        from datetime import datetime
        c = Custodia(vehiculo_id=1, custodio_tipo='conductor',
                     inicio_ts=datetime(2026, 8, 1),
                     registrado_por_usuario_id=1, km_inicio=100,
                     custodio_conductor_id=9)
        v = puede_recibir(c, QuienPide.CONDUCTOR, nombre_custodio_actual='Víctor',
                          placa='THP696')
        return v.mensaje

    def test_nombra_el_gesto_y_la_cuenta(self):
        m = self._mensaje()
        assert 'Entregar turno' in m
        assert 'SU usuario' in m

    def test_dice_la_salida_cuando_la_persona_no_esta(self):
        """Sin esto, un conductor que se fue deja el camión trabado."""
        assert 'admin de zona' in self._mensaje()


# ══════════════════════════════════════════════════════════════════════════
# 3 — La lista de sedes
# ══════════════════════════════════════════════════════════════════════════

class TestLaListaDeSedesNoPasaPorUnRedirect:
    """`/api/almacenes` (sin barra) responde 308.

    Detrás del proxy de Railway, sin `ProxyFix`, ese `Location` salía como
    `http://` — contenido mixto desde una página HTTPS, bloqueado por el
    navegador. El desplegable quedaba en «no se pudo cargar la lista» y no se
    podía entregar el turno.
    """

    def test_el_cliente_pide_con_barra_final(self):
        js = _js()
        assert "get('/api/almacenes/')" in js
        assert "get('/api/almacenes')" not in js, (
            'sin la barra vuelve el 308, y con él la dependencia de que el '
            'redirect se construya bien')

    def test_el_endpoint_sin_barra_sigue_redirigiendo(self, client):
        """No se arregló quitando el redirect: se arregló no dependiendo de él.
        Si Flask dejara de redirigir, este test avisa que el mundo cambió."""
        assert client.get('/api/almacenes').status_code == 308

    def test_el_redirect_respeta_el_esquema_del_proxy(self, client):
        """La causa de fondo. Sin ProxyFix el `Location` sale en `http://`."""
        r = client.get('/api/almacenes',
                       headers={'X-Forwarded-Proto': 'https',
                                'Host': 'wms.up.railway.app'})
        assert r.headers['Location'].startswith('https://'), (
            'el redirect volvió a salir en http: desde una página HTTPS el '
            'navegador lo bloquea como contenido mixto y la llamada muere en '
            'el catch del cliente')

    def test_proxyfix_esta_puesto(self):
        fuente = (Path(__file__).resolve().parents[2] / 'app'
                  / '__init__.py').read_text(encoding='utf-8')
        assert 'ProxyFix(' in fuente
        assert 'x_proto=1' in fuente


# ══════════════════════════════════════════════════════════════════════════
# 4 — La tarjeta de propiedad no vence
# ══════════════════════════════════════════════════════════════════════════

class TestDocumentosQueNoVencen:

    def test_la_politica_vive_en_el_dominio(self):
        from flota.dominio.valores import exige_vencimiento

        assert exige_vencimiento('soat') is True
        assert exige_vencimiento('rtm') is True
        assert exige_vencimiento('poliza_rc') is True
        assert exige_vencimiento('tarjeta_propiedad') is False

    def test_un_tipo_desconocido_revienta(self):
        """Sin `.get(tipo, True)`: agregar un tipo y olvidarlo tiene que doler."""
        from flota.dominio.valores import exige_vencimiento

        with pytest.raises(ValueError, match='desconocido'):
            exige_vencimiento('licencia_transito')

    def test_el_vocabulario_de_la_tabla_sale_del_dominio(self):
        from flota.adaptadores.modelos import TIPO_DOCUMENTO
        from flota.dominio.valores import TIPOS_DOCUMENTO

        assert TIPO_DOCUMENTO == TIPOS_DOCUMENTO


class TestElEndpointNoGuardaFechasInventadas:

    def _url(self, m):
        return f"/flota/vehiculo/{m['placa']}/documentos"

    @pytest.fixture
    def mundo(self, db):
        from app.models.vehiculo import Vehiculo

        v = Vehiculo(placa='YES100', tipo='NHR', activo=True)
        db.session.add(v)
        db.session.commit()
        return {'placa': v.placa}

    def _guardar(self, client, token, mundo, **extra):
        cuerpo = {'tipo': 'tarjeta_propiedad', 'numero': '128899933',
                  'entidad': 'Papelería Medellín',
                  'fecha_expedicion': '2026-08-04'}
        cuerpo.update(extra)
        return client.post(self._url(mundo), json=cuerpo,
                           headers={'Authorization': f'Bearer {token}'})

    def test_la_tarjeta_entra_sin_vencimiento(self, client, jwt_token_admin, mundo):
        r = self._guardar(client, jwt_token_admin, mundo)
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d['fecha_vencimiento'] is None
        assert d['vence'] is False
        assert d['dias_para_vencer'] is None

    def test_una_fecha_mandada_por_el_cliente_se_DESCARTA(
            self, client, jwt_token_admin, mundo):
        """La fecha inventada no puede entrar por otra puerta. Si entrara, el
        aviso de renovación la perseguiría como si fuera real."""
        r = self._guardar(client, jwt_token_admin, mundo,
                          fecha_vencimiento='2045-08-20')
        assert r.status_code == 201
        assert r.get_json()['fecha_vencimiento'] is None

    def test_el_soat_sigue_exigiendo_vencimiento(self, client, jwt_token_admin, mundo):
        """El invariante no se aflojó para todos."""
        from sqlalchemy.exc import IntegrityError

        r = client.post(self._url(mundo), json={
            'tipo': 'soat', 'numero': 'S-1', 'entidad': 'Mundial',
            'fecha_expedicion': '2026-01-01',
        }, headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 409

    def test_un_tipo_inventado_da_400_y_no_500(self, client, jwt_token_admin, mundo):
        r = client.post(self._url(mundo), json={
            'tipo': 'licencia_transito', 'numero': 'X', 'entidad': 'Y',
            'fecha_expedicion': '2026-01-01', 'fecha_vencimiento': '2027-01-01',
        }, headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 400

    def test_la_pantalla_no_pide_la_fecha_que_no_existe(self):
        js = _js()
        assert 'FLOTA_TIPOS_SIN_VENCIMIENTO' in js
        assert 'function flotaDocTipoCambio' in js
        assert 'no vence' in js


# ══════════════════════════════════════════════════════════════════════════
# 5 — La foto del tablero, una sola vez
# ══════════════════════════════════════════════════════════════════════════

class TestElTableroSePideUnaVez:
    """Estaba en su propio campo Y en la grilla, y se mandaban las dos."""

    def test_la_grilla_lo_excluye(self):
        from flota.dominio.valores import (ANGULOS_FIJOS, ANGULOS_GRILLA,
                                           ANGULO_TABLERO)

        assert ANGULO_TABLERO in ANGULOS_FIJOS, (
            'el ángulo tablero SIGUE existiendo: lo que cambia es quién lo pide')
        assert ANGULO_TABLERO not in ANGULOS_GRILLA

    def test_el_cliente_filtra_en_un_solo_lugar(self):
        js = _js()
        assert js.count('function flotaAngulosDeGrilla') == 1
        # Tres formularios + el payload: el filtro no se escribe en cada sitio.
        assert js.count('flotaAngulosDeGrilla(') >= 5

    def test_ningun_render_recorre_los_angulos_crudos(self):
        """TRINQUETE — si alguno vuelve a iterar `FLOTA_ANGULOS` para dibujar
        botones, el tablero reaparece dos veces."""
        js = _js()
        malas = [l.strip() for l in js.split('\n')
                 if re.search(r'FLOTA_ANGULOS\.(forEach|map)\(', l)]
        assert not malas, f'la grilla volvió a dibujarse sin filtrar: {malas}'


# ══════════════════════════════════════════════════════════════════════════
# 6 — La convención que hace que las fotos signifiquen algo
# ══════════════════════════════════════════════════════════════════════════

class TestLaConvencionEstaEnLaPantalla:
    """«Cada persona puede tomar diferentes puntos de referencia» — Yesid.

    Sin convención, `lateral_izq` de uno es el `lateral_der` del otro y
    `llanta_3` no ubica ninguna rueda. La evidencia se toma para poder decir
    CUÁL rueda tenía el flanco herido; sin esto no lo dice.
    """

    def test_dice_desde_donde_se_miran_los_laterales(self):
        js = _js()
        i = js.index('function flotaConvencionFotos')
        cuerpo = js[i:js.index('}', js.index('return `', i))]
        assert 'de frente' in cuerpo

    def test_define_la_llanta_1_y_el_sentido(self):
        js = _js()
        i = js.index('function flotaConvencionFotos')
        cuerpo = js[i:i + 1600]
        assert 'delantera derecha' in cuerpo
        assert 'antihorario' in cuerpo

    def test_aparece_en_los_tres_formularios_que_piden_fotos(self):
        js = _js()
        assert js.count('${flotaConvencionFotos()}') == 3, (
            'un formulario que pide fotos sin la convención produce fotos que no '
            'se pueden comparar con las de los otros')
