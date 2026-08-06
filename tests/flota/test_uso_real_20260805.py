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
        # 1 definición + los SEIS formularios que escriben contra una placa:
        # recibo de escritorio, recibo del conductor, entrega, ficha, odómetro y
        # documentos. La primera pasada selló solo los tres que mandan fotos, y
        # el que quedó afuera —la ficha— guardó en el vehículo equivocado esa
        # misma tarde.
        assert js.count('flotaPlacaDelFormulario(') == 7

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


# ══════════════════════════════════════════════════════════════════════════
# SEGUNDA RONDA — lo que Yesid encontró después del primer arreglo
# ══════════════════════════════════════════════════════════════════════════

class TestUnConductorPuedeSoltarSuPropioVehiculo:
    """El gesto más común del día estaba bloqueado, y el mensaje era absurdo.

    Víctor tenía el UPQ606. Al apretar «Entregar UPQ606» recibía:
    *«El UPQ606 lo tiene Victor desde 04/08 a las 01:54. Si lo vas a recibir
    vos, Victor tiene que cerrar su turno primero.»* — dicho al propio Víctor.

    La causa: el adaptador decidía comparando el custodio ENTRANTE con el
    vigente. Al entregar a una sede el custodio entrante no es un conductor, así
    que la comparación daba falso y caía en el rechazo pensado para *«no le
    quites el vehículo a otro»*. El guard que protege a un conductor de otro
    estaba impidiendo que el dueño lo soltara.

    La pregunta correcta no es «¿a quién va?» sino «¿quién lo tiene ahora?».
    """

    def _veredicto(self, es_el_custodio_actual):
        from datetime import datetime

        from flota.dominio.custodia import QuienPide, puede_recibir
        from flota.dominio.valores import Custodia

        c = Custodia(vehiculo_id=1, custodio_tipo='conductor',
                     inicio_ts=datetime(2026, 8, 4, 1, 54),
                     registrado_por_usuario_id=1, km_inicio=100,
                     custodio_conductor_id=9)
        return puede_recibir(c, QuienPide.CONDUCTOR,
                             nombre_custodio_actual='Víctor', placa='UPQ606',
                             es_el_custodio_actual=es_el_custodio_actual)

    def test_el_custodio_actual_puede_cerrar_su_turno(self):
        v = self._veredicto(True)
        assert v.puede is True
        assert v.requiere_forzado is False, (
            'cerrar el turno propio no es un forzado: no hay nada que forzar')

    def test_otro_conductor_sigue_sin_poder_quitarselo(self):
        """El guard no se aflojó: se le corrigió la pregunta."""
        v = self._veredicto(False)
        assert v.puede is False
        assert 'Víctor' in v.mensaje

    def test_el_adaptador_resuelve_por_USUARIO_no_por_conductor(self):
        """Quien pide se identifica con su cuenta; `Conductor.usuario_id` los une.

        Comparar ids de conductor era justamente el error: al entregar a una
        sede no hay conductor entrante con el que comparar.
        """
        from pathlib import Path

        fuente = (Path(__file__).resolve().parents[2] / 'flota' / 'adaptadores'
                  / 'traspaso.py').read_text(encoding='utf-8')
        i = fuente.index('es_el_custodio_actual = (')
        cuerpo = fuente[i:i + 400]
        assert 'custodio_conductor.usuario_id' in cuerpo
        assert 'registrado_por_usuario_id' in cuerpo


class TestElVisorExisteDondeSeUsa:
    """«No da opciones para el cómo estaba» — Yesid, 2:09 PM.

    Lo que veía era mi propio mensaje de error: *«la pantalla no tiene dónde
    mostrar la foto — falta #flota-visor»*. El div estaba en el recibo de
    escritorio y en documentos, y faltaba en las DOS pantallas del conductor —
    que son las únicas donde el botón «cómo estaba» existe.

    El botón no estaba roto: estaba en una pantalla sin dónde pintar.
    """

    def test_las_cuatro_pantallas_tienen_visor(self):
        js = _js()
        assert js.count('id="flota-visor"') == 4, (
            'una pantalla que ofrece «cómo estaba» sin visor produce un botón '
            'que responde con un error interno')

    def test_el_error_sigue_siendo_ruidoso(self):
        """No se quitó el mensaje: se quitó la causa. Si mañana aparece una
        quinta pantalla sin visor, tiene que gritar igual."""
        js = _js()
        assert 'falta #flota-visor' in js


class TestLaFichaNoDiceGuardadaSobreOtroVehiculo:
    """«Sale ficha guardada y no almacena» — en tres vehículos.

    Dos causas distintas se sumaban y las dos daban el mismo síntoma:

    1. El service worker cacheaba `/flota/*` (ver `tests/test_red_pwa.py`): la
       ficha se guardaba bien y el GET siguiente devolvía la respuesta vieja.
    2. `flotaGuardarFicha` armaba la URL con `FLOTA_PLACA` — la misma global
       mutable que cruzó las fotos. La ficha se guardaba en OTRO vehículo.

    La segunda es la que da miedo: no perdía el dato, lo ponía en el expediente
    de otro camión y decía «guardada ✓».
    """

    def test_ningun_destino_de_escritura_sale_de_la_global(self):
        js = _js()
        malas = [l.strip() for l in js.split('\n')
                 if 'FLOTA_PLACA' in l and ('/ficha' in l or '/documentos' in l)]
        assert not malas, f'volvió a construir la URL con la global: {malas}'

    def test_los_seis_formularios_sellan_su_placa(self):
        js = _js()
        assert js.count('data-placa=') == 6, (
            'recibo escritorio, recibo conductor, entrega, ficha, odómetro y '
            'documentos: los seis escriben contra una placa')

    def test_el_aviso_de_guardado_dice_sobre_QUE_vehiculo(self):
        """Un «guardada ✓» sin placa no se puede desmentir mirando la pantalla."""
        js = _js()
        i = js.index("'Ficha guardada y completa ✓'")
        assert "+ ' · ' + placa" in js[i:i + 400]


class TestElDiaCompletoDeUnConductor:
    """Recibir y ENTREGAR, de punta a punta, por HTTP.

    Este test no existía y por eso el bloqueo llegó a producción: había tests de
    recibir y tests del dominio de entregar, pero ninguno que hiciera el día
    entero contra los endpoints. La entrega a sede —el gesto de las 6 p.m., el
    más frecuente de todos— nunca se había ejercido completa.

    Es la lección de la tanda 1 aplicada a los tests, no al producto: una cosa
    construida y verificada de verdad vale más que cinco probadas por partes.
    """

    @pytest.fixture
    def mundo(self, app, db):
        from flask_jwt_extended import create_access_token

        from app.models.almacen import Almacen
        from app.models.conductor import Conductor
        from app.models.usuario import Usuario
        from app.models.vehiculo import Vehiculo

        veh = Vehiculo(placa='DIA100', tipo='NHR', activo=True)
        alm = Almacen(codigo='DIA-SEDE', nombre='Sede del día')
        u = Usuario(email='victor-dia@x.com', nombre='Víctor',
                    rol='conductor', activo=True)
        u.set_password('x')
        db.session.add_all([veh, alm, u])
        db.session.flush()
        c = Conductor(nombre='Víctor', cedula='DIA-1', activo=True, usuario_id=u.id)
        db.session.add(c)
        db.session.commit()
        with app.app_context():
            token = create_access_token(identity=str(u.id))
        return {'placa': veh.placa, 'alm': alm.id, 'con': c.id, 'token': token}

    def _post(self, client, mundo, payload):
        return client.post('/flota/custodia/traspaso', json=payload,
                           headers={'Authorization': f"Bearer {mundo['token']}"})

    def test_recibe_y_despues_ENTREGA_a_la_sede(self, client, mundo):
        recibo = self._post(client, mundo, {
            'placa': mundo['placa'], 'km': 100_000,
            'custodio_tipo': 'conductor', 'custodio_conductor_id': mundo['con'],
        })
        assert recibo.status_code in (200, 201), recibo.get_json()

        entrega = self._post(client, mundo, {
            'placa': mundo['placa'], 'km': 100_180,
            'custodio_tipo': 'sede', 'custodio_sede_id': mundo['alm'],
            'ubicacion': 'sede',
        })
        assert entrega.status_code in (200, 201), (
            f'un conductor no pudo entregar su propio vehículo: '
            f'{entrega.get_json()}')

    def test_y_puede_dejarlo_FUERA_de_sede_con_motivo(self, client, mundo):
        self._post(client, mundo, {
            'placa': mundo['placa'], 'km': 100_000,
            'custodio_tipo': 'conductor', 'custodio_conductor_id': mundo['con'],
        })
        r = self._post(client, mundo, {
            'placa': mundo['placa'], 'km': 100_200,
            'custodio_tipo': 'conductor', 'custodio_conductor_id': mundo['con'],
            'ubicacion': 'fuera_de_sede',
            'ubicacion_motivo': 'se quedó sin batería en Pitalito',
        })
        assert r.status_code in (200, 201), r.get_json()

    def test_pero_OTRO_conductor_sigue_sin_poder_quitarselo(self, app, client, db, mundo):
        """El guard no se aflojó."""
        from flask_jwt_extended import create_access_token

        from app.models.conductor import Conductor
        from app.models.usuario import Usuario

        self._post(client, mundo, {
            'placa': mundo['placa'], 'km': 100_000,
            'custodio_tipo': 'conductor', 'custodio_conductor_id': mundo['con'],
        })

        otro_u = Usuario(email='otro-dia@x.com', nombre='Otro',
                         rol='conductor', activo=True)
        otro_u.set_password('x')
        db.session.add(otro_u)
        db.session.flush()
        otro_c = Conductor(nombre='Otro', cedula='DIA-2', activo=True,
                           usuario_id=otro_u.id)
        db.session.add(otro_c)
        db.session.commit()
        with app.app_context():
            token = create_access_token(identity=str(otro_u.id))

        r = client.post('/flota/custodia/traspaso', json={
            'placa': mundo['placa'], 'km': 100_050,
            'custodio_tipo': 'conductor', 'custodio_conductor_id': otro_c.id,
        }, headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 409
        assert 'Víctor' in r.get_json()['error']
