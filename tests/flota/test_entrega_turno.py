"""
Entregar el turno: el gesto que no existía.

Hasta el 2026-08-03 el traspaso era siempre cerrar+abrir, y el botón que decía
"Entregar turno" llamaba a la misma función que "Recibir turno" — solo cambiaba
el texto. Un conductor que terminaba a las 6 p.m. no tenía a quién entregarle:
su única salida era volver a recibirse el vehículo a sí mismo. Eso produjo
**nueve custodias de cero kilómetros en el THP696 dentro del mismo minuto**.

Tres cosas se prueban acá, y las tres salieron de esa tarde:

  1. **Ubicación ≠ custodia.** Dónde está el vehículo y quién responde por él
     son hechos independientes. Mezclarlos descarga de responsabilidad a la
     única persona que tiene el camión.
  2. **Idempotencia.** Un timeout no significa que falló (regla 9). El traspaso
     tiene ese mismo perfil y no tenía ninguna defensa.
  3. **Asimetría de fotos.** Recibir es exhaustivo porque protege a quien asume;
     entregar es rápido porque cierra el reloj.
"""
import re
from pathlib import Path

import pytest

from flota.dominio.valores import (
    ANGULOS_ENTREGA,
    ANGULOS_FIJOS,
    CustodioTipo,
    Ubicacion,
    custodio_de_ubicacion,
)

_PWA = Path(__file__).resolve().parents[2] / 'app' / 'static' / 'pwa'
_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


@pytest.fixture
def mundo(db):
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='ENT100', tipo='Turbo', activo=True)
    alm = Almacen(codigo='ENT-SEDE', nombre='Patio')
    db.session.add_all([veh, alm])
    db.session.flush()
    con = Conductor(nombre='Conductor ENT', cedula='ENT-1', activo=True)
    db.session.add(con)
    db.session.commit()
    return {'placa': veh.placa, 'veh': veh.id, 'alm': alm.id, 'con': con.id}


class TestUbicacionNoEsCustodia:
    """El error de diseño que se corrigió antes de construirlo.

    La primera versión decía "campo dónde queda el vehículo: sede / taller /
    fuera de sede" **y** "la custodia pasa a custodio_tipo = sede", en la misma
    frase. Funciona para el patio. Rompe el único caso con riesgo.
    """

    def test_en_sede_responde_la_sede(self):
        assert custodio_de_ubicacion(Ubicacion.SEDE) is CustodioTipo.SEDE

    def test_en_taller_tambien(self):
        """Tanda 1 no tiene tabla de talleres: responde la sede que lo envió."""
        assert custodio_de_ubicacion(Ubicacion.TALLER) is CustodioTipo.SEDE

    def test_fuera_de_sede_sigue_respondiendo_el_conductor(self):
        """EL caso. Si esto devuelve `sede`, se acaba de descargar de
        responsabilidad a la única persona que tiene el vehículo."""
        assert custodio_de_ubicacion(Ubicacion.FUERA_DE_SEDE) is CustodioTipo.CONDUCTOR


class TestLaBaseImpideLaCombinacionImposible:
    """No alcanza con que el adaptador lo valide.

    Es la clase de regla que un refactor borra sin notarlo, y su consecuencia
    aparece meses después en una discusión sobre quién paga un golpe.
    """

    def test_fuera_de_sede_con_custodia_de_sede_no_entra(self, db, mundo):
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO flota_custodia "
                "(vehiculo_id, custodio_tipo, custodio_sede_id, custodio_estado, "
                " registrado_por_usuario_id, inicio_ts, km_inicio, ubicacion, "
                " ubicacion_motivo, linea_base, cierre_forzado) "
                f"VALUES ({mundo['veh']}, 'sede', {mundo['alm']}, 'resuelto', "
                f"1, '2026-08-03 12:00:00', 100, 'fuera_de_sede', 'x', 0, 0)"))
            db.session.commit()
        db.session.rollback()

    def test_fuera_de_sede_sin_motivo_no_entra(self, db, mundo):
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO flota_custodia "
                "(vehiculo_id, custodio_tipo, custodio_conductor_id, custodio_estado, "
                " registrado_por_usuario_id, inicio_ts, km_inicio, ubicacion, "
                " linea_base, cierre_forzado) "
                f"VALUES ({mundo['veh']}, 'conductor', {mundo['con']}, 'resuelto', "
                f"1, '2026-08-03 12:00:00', 100, 'fuera_de_sede', 0, 0)"))
            db.session.commit()
        db.session.rollback()

    def test_una_ubicacion_inventada_no_entra(self, db, mundo):
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO flota_custodia "
                "(vehiculo_id, custodio_tipo, custodio_sede_id, custodio_estado, "
                " registrado_por_usuario_id, inicio_ts, km_inicio, ubicacion, "
                " linea_base, cierre_forzado) "
                f"VALUES ({mundo['veh']}, 'sede', {mundo['alm']}, 'resuelto', "
                f"1, '2026-08-03 12:00:00', 100, 'en_la_casa_de_mi_tia', 0, 0)"))
            db.session.commit()
        db.session.rollback()


class TestLaFronteraNoDejaElegirLaCombinacion:

    def test_fuera_de_sede_con_tipo_sede_es_400_y_explica(
            self, client, jwt_token_admin, mundo):
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': mundo['placa'], 'km': 100,
                              'custodio_tipo': 'sede',
                              'custodio_sede_id': mundo['alm'],
                              'ubicacion': 'fuera_de_sede',
                              'ubicacion_motivo': 'quedó en la casa'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 400
        assert 'conductor' in r.get_json()['error']
        assert r.get_json()['motivo']

    def test_entregar_a_sede_deja_la_custodia_en_la_sede(
            self, client, jwt_token_admin, mundo):
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': mundo['placa'], 'km': 100,
                              'custodio_tipo': 'sede',
                              'custodio_sede_id': mundo['alm'],
                              'ubicacion': 'sede'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201, r.get_json()

    def test_fuera_de_sede_bien_formado_pasa(self, client, jwt_token_admin, mundo):
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': mundo['placa'], 'km': 100,
                              'custodio_tipo': 'conductor',
                              'custodio_conductor_id': mundo['con'],
                              'ubicacion': 'fuera_de_sede',
                              'ubicacion_motivo': 'ruta larga, duerme en Pitalito'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201, r.get_json()


class TestIdempotencia:
    """Regla 9 acá: un timeout no significa que falló.

    Deshabilitar el botón evita el doble toque desde esa pantalla. No evita dos
    pestañas, un reintento del navegador, ni una red que responde tarde.
    """

    def _cuerpo(self, mundo):
        return {'placa': mundo['placa'], 'km': 5000,
                'custodio_tipo': 'conductor',
                'custodio_conductor_id': mundo['con']}

    def test_dos_posts_identicos_dan_una_sola_custodia(
            self, client, jwt_token_admin, mundo):
        from flota.adaptadores.modelos import Custodia

        a = client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                        headers=_auth(jwt_token_admin))
        b = client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                        headers=_auth(jwt_token_admin))
        assert a.status_code == b.status_code == 201
        assert a.get_json()['custodia_id'] == b.get_json()['custodia_id']
        assert Custodia.query.count() == 1

    def test_nueve_toques_no_hacen_nueve_custodias(
            self, client, jwt_token_admin, mundo):
        """El caso literal del THP696."""
        from flota.adaptadores.modelos import Custodia

        for _ in range(9):
            client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                        headers=_auth(jwt_token_admin))
        assert Custodia.query.count() == 1

    def test_devuelve_201_y_no_un_error(self, client, jwt_token_admin, mundo):
        """Un rojo en la cara del conductor lo hace tocar de nuevo.

        Para quien llama, el resultado es el que pidió: hay una custodia abierta
        con esos datos. Levantar convertiría un reintento inocente en un
        problema.
        """
        client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                    headers=_auth(jwt_token_admin))
        r = client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201

    def test_un_traspaso_con_otro_kilometraje_si_es_otro_hecho(
            self, client, jwt_token_admin, mundo):
        """La ventana no puede tragarse un turno legítimo.

        Si el odómetro cambió, el camión rodó: son dos hechos distintos aunque
        pasen seguidos.
        """
        from flota.adaptadores.modelos import Custodia

        client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                    headers=_auth(jwt_token_admin))
        otro = dict(self._cuerpo(mundo), km=5040)
        client.post('/flota/custodia/traspaso', json=otro,
                    headers=_auth(jwt_token_admin))
        assert Custodia.query.count() == 2

    def test_un_traspaso_a_otra_persona_si_es_otro_hecho(
            self, client, jwt_token_admin, mundo, db):
        from app.models.conductor import Conductor
        from flota.adaptadores.modelos import Custodia

        otro_con = Conductor(nombre='Segundo', cedula='ENT-2', activo=True)
        db.session.add(otro_con)
        db.session.commit()

        client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                    headers=_auth(jwt_token_admin))
        # Cerrar el turno de otro exige motivo escrito — la ventana de
        # idempotencia no puede saltarse esa regla ni al revés.
        r = client.post('/flota/custodia/traspaso',
                        json=dict(self._cuerpo(mundo),
                                  custodio_conductor_id=otro_con.id,
                                  motivo_forzado='el primero se fue sin cerrar'),
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201, r.get_json()
        assert Custodia.query.count() == 2

    def test_sin_motivo_el_relevo_ajeno_sigue_bloqueado(
            self, client, jwt_token_admin, mundo, db):
        """La idempotencia no puede haber aflojado la regla del cierre forzado."""
        from app.models.conductor import Conductor
        from flota.adaptadores.modelos import Custodia

        otro_con = Conductor(nombre='Tercero', cedula='ENT-3', activo=True)
        db.session.add(otro_con)
        db.session.commit()

        client.post('/flota/custodia/traspaso', json=self._cuerpo(mundo),
                    headers=_auth(jwt_token_admin))
        r = client.post('/flota/custodia/traspaso',
                        json=dict(self._cuerpo(mundo),
                                  custodio_conductor_id=otro_con.id),
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 409
        assert Custodia.query.count() == 1


class TestAsimetriaDeFotos:
    """Recibir protege a quien asume. Entregar cierra el reloj."""

    def test_entregar_pide_cuatro_y_recibir_trece(self):
        assert len(ANGULOS_ENTREGA) == 4
        assert len(ANGULOS_FIJOS) == 7

    def test_las_cuatro_de_entrega_existen_en_las_de_recibo(self):
        """Si no fueran las mismas, no habría con qué comparar — y comparar es
        lo único que hace que la entrega sirva para atribuir un daño."""
        for a in ANGULOS_ENTREGA:
            assert a in ANGULOS_FIJOS

    def test_son_las_cuatro_caras_del_vehiculo(self):
        """Las que detectan un golpe nuevo. Un daño en el cajón o un espejo roto
        no se ven acá: costo aceptado a cambio de que el registro se haga."""
        assert set(ANGULOS_ENTREGA) == {
            'frontal', 'trasera', 'lateral_izq', 'lateral_der'}

    def test_no_incluye_el_tablero_como_evidencia_de_estado(self):
        """El tablero va aparte, como foto_dato: es la fuente del odómetro, no
        una foto de cómo está el camión."""
        assert 'tablero' not in ANGULOS_ENTREGA


class TestElBotonDiceLoQueHace:
    """TRINQUETE — "Entregar turno" ejecutaba un recibo.

    Un solo `onclick` y el texto cambiando. No fallaba: engañaba con confianza,
    y la evidencia de que engañó son nueve filas en la base.
    """

    def test_entregar_y_recibir_llaman_a_funciones_distintas(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        # El patrón exacto que había: un ternario de TEXTO sobre un solo handler.
        malo = re.search(
            r"onclick=\"flotaCondAbrirRecibo\(\)\"[^>]*>\s*\$\{[^}]*"
            r"tiene_turno_abierto\s*\?\s*'Entregar", js)
        assert not malo, (
            'El botón vuelve a decir "Entregar" y ejecutar el recibo: un solo '
            'handler con el texto cambiando.')

    def test_existe_un_flujo_de_entrega_propio(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert 'flotaCondAbrirEntrega' in js, 'no hay gesto de entrega'
        assert js.count('flotaCondAbrirEntrega') >= 2, 'definido y sin caller'


class TestElDesplegableDeSedeNoPuedeSalirVacio:
    """Tres bugs encadenados, y el visible era el tercero (2026-08-03).

    El `<select>` de "¿qué sede?" salía **sin una sola opción**. Debajo había:

      1. `/api/almacenes` devuelve una LISTA, no `{almacenes: [...]}`. El código
         hacía `d.almacenes || []` → `undefined || []` → vacío. Nunca funcionó,
         ni en la entrega ni en el modal de escritorio.
      2. El conductor no tenía permiso: el guard era `_es_personal_almacen() or
         _es_control_flota()`.
      3. El `catch` se tragaba el fallo y dejaba el `<select>` vacío —
         indistinguible de "no hay sedes".

    Y el guard de permisos no atrapó el (2) porque `/api/almacenes` sin barra
    final devuelve **308**, no 403: medía el redirect, no el destino.
    """

    def test_el_endpoint_devuelve_una_lista(self, client, jwt_token_admin):
        """Si algún día devuelve un objeto, el frontend tiene que enterarse
        acá y no con un desplegable vacío en el patio."""
        r = client.get('/api/almacenes/', headers=_auth(jwt_token_admin))
        assert r.status_code == 200
        assert isinstance(r.get_json(), list), (
            'cambió el contrato: revisá flotaLlenarSedes()')

    def test_un_conductor_puede_leer_las_sedes(self, client, db, app):
        """Sin esto, quien entrega el turno no puede decir dónde queda el
        vehículo — y la custodia queda `pendiente_sede` sin razón."""
        from flask_jwt_extended import create_access_token

        from app.models.usuario import Usuario

        u = Usuario(email='cond-sede@x.com', nombre='C', rol='conductor', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            t = create_access_token(identity=str(u.id))
        r = client.get('/api/almacenes/', headers=_auth(t))
        assert r.status_code == 200, r.get_json()

    def test_tienda_sigue_sin_poder(self, client, db, app):
        """Se agregó `conductor`, no se abrió la puerta.

        Nota sobre `_es_personal_almacen()`: es una LISTA NEGRA —todos menos
        conductor y tienda— con nombre de lista blanca. Un operario, compras o
        gerencia ya podían listar almacenes antes de este cambio. Queda anotado
        como deuda: el nombre promete una cosa y el cuerpo hace otra. Acá se
        verifica lo único que ese helper sí excluye.
        """
        from flask_jwt_extended import create_access_token

        from app.models.usuario import Usuario

        u = Usuario(email='tienda-sede@x.com', nombre='T', rol='tienda', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            t = create_access_token(identity=str(u.id))
        assert client.get('/api/almacenes/', headers=_auth(t)).status_code == 403

    def test_el_conductor_no_puede_crear_almacenes(self, client, db, app):
        """Leer los maestros no es administrarlos."""
        from flask_jwt_extended import create_access_token

        from app.models.usuario import Usuario

        u = Usuario(email='cond-crea@x.com', nombre='C', rol='conductor', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            t = create_access_token(identity=str(u.id))
        r = client.post('/api/almacenes/', json={'codigo': 'XX1', 'nombre': 'X'},
                        headers=_auth(t))
        assert r.status_code == 403

    def test_una_sola_funcion_llena_los_dos_desplegables(self):
        """El mismo desempaquetado en dos sitios diverge — ya costó 25×."""
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        assert js.count('function flotaLlenarSedes') == 1
        assert js.count("flotaLlenarSedes('") == 2, (
            'la entrega y el modal de escritorio tienen que usar la misma')

    def test_tolera_las_dos_formas_de_respuesta(self):
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        i = js.index('async function flotaLlenarSedes')
        assert 'Array.isArray(d)' in js[i:i + 900], (
            'si el contrato cambia, la pantalla se vacía en silencio otra vez')

    def test_si_falla_lo_dice_en_vez_de_quedar_vacio(self):
        """Un desplegable vacío sin explicación es la regla 5 rota en la cara
        del usuario: parece que no hay sedes."""
        js = (_PWA / 'flota.js').read_text(encoding='utf-8')
        i = js.index('async function flotaLlenarSedes')
        cuerpo = js[i:i + 1400]
        assert 'alerta(' in cuerpo, 'el fallo se sigue tragando'
        assert 'no se pudo cargar' in cuerpo
