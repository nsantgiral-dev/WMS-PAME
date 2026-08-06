"""
Los cinco endpoints de §4, construidos con su consumidor (§3).

Lo que se prueba acá no es que devuelvan 200: es que **la frontera HTTP no
afloje ninguna de las políticas del dominio.** Un endpoint que acepta lo que el
dominio rechaza es un rodeo alrededor de todo lo demás — y como devuelve 201,
nadie se entera.
"""
from datetime import datetime

from app.utils.fecha import dia_operativo

import pytest

from flota.adaptadores.modelos import Custodia, LecturaOdometro

_H = 'Authorization'


@pytest.fixture
def flota_mundo(db):
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='EPX100', tipo='NHR', activo=True)
    alm = Almacen(codigo='EP-SEDE', nombre='Sede endpoints')
    db.session.add_all([veh, alm])
    db.session.flush()
    con = Conductor(nombre='Conductor EP', cedula='EP-1', activo=True)
    db.session.add(con)
    db.session.commit()
    return {'placa': veh.placa, 'veh': veh.id, 'alm': alm.id, 'con': con.id}


def _auth(token):
    return {_H: f'Bearer {token}'}


class TestSesionObligatoria:
    """`registrado_por_usuario_id` sale del token, nunca del cuerpo.

    Quién dice que entregó el turno no lo elige quien manda el JSON.
    """

    def test_los_cinco_exigen_sesion(self, client, flota_mundo):
        p = flota_mundo['placa']
        assert client.get(f'/flota/custodia/activa/{p}').status_code == 401
        assert client.post('/flota/custodia/traspaso', json={}).status_code == 401
        assert client.post('/flota/odometro', json={}).status_code == 401
        assert client.get(f'/flota/vehiculo/{p}/ficha').status_code == 401
        assert client.put(f'/flota/vehiculo/{p}/ficha', json={}).status_code == 401


class TestCustodiaActiva:

    def test_una_placa_que_no_existe_es_404_y_no_una_respuesta_vacia(
            self, client, jwt_token_admin, flota_mundo):
        """Un vehículo que nadie dio de alta se dice, no se rodea."""
        r = client.get('/flota/custodia/activa/NOEXISTE', headers=_auth(jwt_token_admin))
        assert r.status_code == 404

    def test_sin_lecturas_el_odometro_viaja_como_palabra_no_como_cero(
            self, client, jwt_token_admin, flota_mundo):
        """0 km es 'no ha rodado'. `sin_dato` es 'no sabemos'."""
        r = client.get(f"/flota/custodia/activa/{flota_mundo['placa']}",
                       headers=_auth(jwt_token_admin))
        cuerpo = r.get_json()
        assert cuerpo['odometro_actual'] == 'sin_dato'
        assert cuerpo['odometro_actual'] != 0
        assert cuerpo['custodia'] is None


class TestTraspaso:

    def _payload(self, m, km=100_000, **kw):
        base = {'placa': m['placa'], 'km': km, 'custodio_tipo': 'conductor',
                'custodio_conductor_id': m['con']}
        base.update(kw)
        return base

    def test_el_primer_turno_es_linea_base(self, client, jwt_token_admin, flota_mundo):
        r = client.post('/flota/custodia/traspaso', json=self._payload(flota_mundo),
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201
        assert r.get_json()['linea_base'] is True

    def test_sin_km_no_pasa(self, client, jwt_token_admin, flota_mundo):
        """Regla 3: sin odómetro no se persiste ningún evento de flota."""
        p = self._payload(flota_mundo)
        del p['km']
        r = client.post('/flota/custodia/traspaso', json=p, headers=_auth(jwt_token_admin))
        assert r.status_code == 400
        assert 'km' in r.get_json()['error']

    def test_un_odometro_que_decrece_es_409_y_no_deja_rastro(
            self, client, jwt_token_admin, flota_mundo):
        """409, no 400: no es sintaxis del cliente, es que el mundo no lo admite."""
        client.post('/flota/custodia/traspaso', json=self._payload(flota_mundo, km=100_000),
                    headers=_auth(jwt_token_admin))
        r = client.post('/flota/custodia/traspaso', json=self._payload(flota_mundo, km=99_000),
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 409
        assert Custodia.query.count() == 1
        assert LecturaOdometro.query.count() == 1

    def test_un_custodio_invalido_no_cierra_el_turno_anterior(
            self, client, jwt_token_admin, flota_mundo):
        """La frontera HTTP no puede dejar al vehículo sin responsable."""
        client.post('/flota/custodia/traspaso', json=self._payload(flota_mundo),
                    headers=_auth(jwt_token_admin))
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': flota_mundo['placa'], 'km': 100_100,
                              'custodio_tipo': 'conductor'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 409
        assert Custodia.query.filter(Custodia.fin_ts.is_(None)).count() == 1

    def test_sede_sin_fila_entra_declarada(self, client, jwt_token_admin, flota_mundo):
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': flota_mundo['placa'], 'km': 100_000,
                              'custodio_tipo': 'sede', 'custodio_estado': 'pendiente_sede'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201
        assert r.get_json()['custodio_estado'] == 'pendiente_sede'


class TestOdometro:

    def test_una_correccion_sin_motivo_es_409(self, client, jwt_token_admin, flota_mundo):
        client.post('/flota/odometro',
                    json={'placa': flota_mundo['placa'], 'valor_km': 100_000,
                          'origen': 'entrega'}, headers=_auth(jwt_token_admin))
        r = client.post('/flota/odometro',
                        json={'placa': flota_mundo['placa'], 'valor_km': 99_000,
                              'origen': 'correccion'}, headers=_auth(jwt_token_admin))
        assert r.status_code == 409

    def test_una_correccion_con_motivo_si_entra(self, client, jwt_token_admin, flota_mundo):
        client.post('/flota/odometro',
                    json={'placa': flota_mundo['placa'], 'valor_km': 100_000,
                          'origen': 'entrega'}, headers=_auth(jwt_token_admin))
        r = client.post('/flota/odometro',
                        json={'placa': flota_mundo['placa'], 'valor_km': 99_000,
                              'origen': 'correccion',
                              'motivo_correccion': 'digitación: sobraba un cero'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201

    def test_un_origen_inventado_es_400(self, client, jwt_token_admin, flota_mundo):
        r = client.post('/flota/odometro',
                        json={'placa': flota_mundo['placa'], 'valor_km': 1,
                              'origen': 'lo_que_sea'}, headers=_auth(jwt_token_admin))
        assert r.status_code == 400


class TestFicha:

    def test_sin_ficha_devuelve_existe_false_y_no_una_ficha_vacia(
            self, client, jwt_token_admin, flota_mundo):
        """La pantalla tiene que distinguir 'sin levantar' de 'levantada a medias'."""
        cuerpo = client.get(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                            headers=_auth(jwt_token_admin)).get_json()
        assert cuerpo['existe'] is False
        assert cuerpo['completa'] is None      # no False: no hay ficha que juzgar

    def test_una_ficha_nueva_nace_incompleta(self, client, jwt_token_admin, flota_mundo):
        """Todo en `sin_dato` hasta que alguien la llene. Sin defaults alegres."""
        r = client.put(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                       json={'posiciones_llanta': 4, 'km_inicial': 100_000},
                       headers=_auth(jwt_token_admin))
        assert r.status_code == 201
        cuerpo = r.get_json()
        assert cuerpo['completa'] is False
        assert 'transmision_final' in cuerpo['atributos_sin_dato']

    def test_un_dato_de_seguridad_sin_procedencia_lo_rechaza_la_base(
            self, client, jwt_token_admin, flota_mundo):
        """El endpoint no revalida: la política vive en un solo lugar."""
        client.put(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                   json={'posiciones_llanta': 4, 'km_inicial': 100_000},
                   headers=_auth(jwt_token_admin))
        r = client.put(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                       json={'distribucion': 'correa'},
                       headers=_auth(jwt_token_admin))
        assert r.status_code == 409
        assert 'detalle' in r.get_json()

    def test_con_procedencia_si_guarda(self, client, jwt_token_admin, flota_mundo):
        client.put(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                   json={'posiciones_llanta': 4, 'km_inicial': 100_000},
                   headers=_auth(jwt_token_admin))
        r = client.put(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                       json={'distribucion': 'correa',
                             'distribucion_fuente': 'concesionario',
                             'distribucion_verificado_ts': datetime(2026, 8, 1).isoformat()},
                       headers=_auth(jwt_token_admin))
        assert r.status_code == 200
        assert r.get_json()['ficha']['distribucion'] == 'correa'

    def test_una_fecha_mal_escrita_es_400_y_no_un_NULL_silencioso(
            self, client, jwt_token_admin, flota_mundo):
        """Una fecha de verificación guardada como NULL diría que nunca se verificó."""
        client.put(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                   json={'posiciones_llanta': 4, 'km_inicial': 100_000},
                   headers=_auth(jwt_token_admin))
        r = client.put(f"/flota/vehiculo/{flota_mundo['placa']}/ficha",
                       json={'frenos_verificado_ts': '01/08/2026'},
                       headers=_auth(jwt_token_admin))
        assert r.status_code == 400


class TestDocumentos:
    """`no_encontrado` es una afirmación, no un campo vacío.

    Si Yesid verifica los cinco SOAT y uno no aparece, eso tiene que quedar
    escrito como hallazgo. Registrarlo como ausencia de dato lo vuelve
    indistinguible de "todavía no lo hemos mirado", y esas dos cosas exigen
    acciones opuestas: buscar el papel, o sacar el camión de ruta.
    """

    def _url(self, m):
        return f"/flota/vehiculo/{m['placa']}/documentos"

    def test_lo_no_verificado_se_declara_aparte(self, client, jwt_token_admin, flota_mundo):
        cuerpo = client.get(self._url(flota_mundo),
                            headers=_auth(jwt_token_admin)).get_json()
        assert set(cuerpo['sin_verificar']) == {
            'soat', 'rtm', 'poliza_rc', 'tarjeta_propiedad'}

    def test_un_soat_vigente_se_guarda_con_sus_dias(self, client, jwt_token_admin, flota_mundo):
        from datetime import date, timedelta
        r = client.post(self._url(flota_mundo), json={
            'tipo': 'soat', 'numero': 'S-1', 'entidad': 'Aseguradora',
            'fecha_expedicion': (dia_operativo() - timedelta(days=100)).isoformat(),
            'fecha_vencimiento': (dia_operativo() + timedelta(days=20)).isoformat(),
        }, headers=_auth(jwt_token_admin))
        assert r.status_code == 201
        assert r.get_json()['dias_para_vencer'] == 20
        assert r.get_json()['vencido'] is False

    def test_no_encontrado_entra_sin_fechas_y_se_cuenta_aparte(
            self, client, jwt_token_admin, flota_mundo):
        from flota.adaptadores.medicion import MedidorSQL
        medidor = MedidorSQL()
        antes_no, antes_venc = medidor.documentos_no_encontrados(), medidor.documentos_vencidos()

        r = client.post(self._url(flota_mundo),
                        json={'tipo': 'soat', 'estado': 'no_encontrado'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 201
        cuerpo = r.get_json()
        assert cuerpo['estado'] == 'no_encontrado'
        assert cuerpo['fecha_vencimiento'] is None

        # Cuenta en su propio contador y NO se cuela en vencidos: son dos
        # afirmaciones distintas y la segunda es la más grave.
        assert medidor.documentos_no_encontrados() == antes_no + 1
        assert medidor.documentos_vencidos() == antes_venc

    def test_las_fechas_de_un_no_encontrado_se_descartan(
            self, client, jwt_token_admin, flota_mundo):
        """Si el papel no apareció, no hay de dónde salieron esas fechas."""
        from datetime import date
        r = client.post(self._url(flota_mundo), json={
            'tipo': 'rtm', 'estado': 'no_encontrado',
            'fecha_vencimiento': dia_operativo().isoformat(),
        }, headers=_auth(jwt_token_admin))
        assert r.get_json()['fecha_vencimiento'] is None

    def test_un_vigente_sin_fechas_lo_rechaza_la_base(
            self, client, jwt_token_admin, flota_mundo):
        r = client.post(self._url(flota_mundo),
                        json={'tipo': 'soat', 'numero': 'S-2', 'entidad': 'X'},
                        headers=_auth(jwt_token_admin))
        assert r.status_code == 409

    def test_guardar_dos_veces_el_mismo_tipo_reemplaza_no_acumula(
            self, client, jwt_token_admin, flota_mundo):
        from datetime import date, timedelta
        from flota.adaptadores.modelos import DocumentoVehiculo
        for dias in (10, 400):
            client.post(self._url(flota_mundo), json={
                'tipo': 'soat', 'numero': f'S-{dias}', 'entidad': 'Aseguradora',
                'fecha_expedicion': dia_operativo().isoformat(),
                'fecha_vencimiento': (dia_operativo() + timedelta(days=dias)).isoformat(),
            }, headers=_auth(jwt_token_admin))
        assert DocumentoVehiculo.query.filter_by(tipo='soat').count() == 1

    def test_una_fecha_mal_escrita_es_400(self, client, jwt_token_admin, flota_mundo):
        r = client.post(self._url(flota_mundo), json={
            'tipo': 'soat', 'numero': 'S-3', 'entidad': 'X',
            'fecha_vencimiento': '01/08/2026',
        }, headers=_auth(jwt_token_admin))
        assert r.status_code == 400

    def test_los_cierres_forzados_salen_con_nombre(self, client, jwt_token_admin, flota_mundo):
        """Con nombre porque hay que saber A QUIÉN avisarle.

        Una lista de custodias forzadas sin decir quién tenía el vehículo no
        sirve para nada: el punto entero es que el custodio anterior se entere.
        """
        from flota.adaptadores import traspaso
        from flota.dominio.valores import CustodioTipo, QuienPide

        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_000,
            registrado_por_usuario_id=1, custodio_tipo=CustodioTipo.CONDUCTOR,
            custodio_conductor_id=flota_mundo['con'])
        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_100,
            registrado_por_usuario_id=1, custodio_tipo=CustodioTipo.SEDE,
            custodio_sede_id=flota_mundo['alm'], quien_pide=QuienPide.ADMIN_ZONA,
            motivo_forzado='se fue sin cerrar y el camión salía a las 6')

        cuerpo = client.get('/flota/custodia/cierres-forzados',
                            headers=_auth(jwt_token_admin)).get_json()
        assert len(cuerpo['cierres']) == 1
        c = cuerpo['cierres'][0]
        assert c['placa'] == flota_mundo['placa']
        assert c['lo_tenia'] == 'Conductor EP'      # a quién avisarle
        assert 'sin cerrar' in c['motivo']

    def test_un_cierre_normal_no_aparece_en_la_lista(self, client, jwt_token_admin, flota_mundo):
        from flota.adaptadores import traspaso
        from flota.dominio.valores import CustodioTipo

        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_000,
            registrado_por_usuario_id=1, custodio_tipo=CustodioTipo.CONDUCTOR,
            custodio_conductor_id=flota_mundo['con'])
        cuerpo = client.get('/flota/custodia/cierres-forzados',
                            headers=_auth(jwt_token_admin)).get_json()
        assert cuerpo['cierres'] == []


class TestVistaDelConductor:
    """El conductor ve solo lo suyo, y la identidad sale del token.

    Si el admin registra por el conductor, el conductor no está reportando nada:
    la app deja de ser su respaldo y pasa a ser un registro sobre él hecho por
    otro. Eso rompe el argumento central del sistema.
    """

    @pytest.fixture
    def conductor_con_cuenta(self, db, flota_mundo):
        from app.services.ruta_service import RutaService
        conductor, usuario = RutaService.crear_cuenta_para_conductor(
            flota_mundo['con'], 'ep@test.com', 'clave123')
        return conductor, usuario

    def _token(self, app, usuario):
        from flask_jwt_extended import create_access_token
        with app.app_context():
            return create_access_token(identity=str(usuario.id))

    def test_sin_ficha_vinculada_lo_dice_en_vez_de_romperse(
            self, client, jwt_token_admin, flota_mundo):
        """Un admin no es conductor: no tiene turno, y eso se explica."""
        r = client.get('/flota/conductor/mi-turno', headers=_auth(jwt_token_admin))
        assert r.status_code == 404
        assert 'vinculado' in r.get_json()['error']

    def test_sin_custodia_ni_ruta_elige_de_la_lista(
            self, app, client, db, flota_mundo, conductor_con_cuenta):
        _, usuario = conductor_con_cuenta
        cuerpo = client.get('/flota/conductor/mi-turno',
                            headers=_auth(self._token(app, usuario))).get_json()
        assert cuerpo['origen'] == 'eleccion'
        assert cuerpo['requiere_confirmacion'] is True
        assert any(c['placa'] == flota_mundo['placa'] for c in cuerpo['candidatos'])

    def test_con_custodia_activa_es_su_vehiculo_y_no_se_pregunta(
            self, app, client, db, flota_mundo, conductor_con_cuenta):
        from flota.adaptadores import traspaso
        from flota.dominio.valores import CustodioTipo

        conductor, usuario = conductor_con_cuenta
        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_000,
            registrado_por_usuario_id=usuario.id,
            custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=conductor.id)

        cuerpo = client.get('/flota/conductor/mi-turno',
                            headers=_auth(self._token(app, usuario))).get_json()
        assert cuerpo['origen'] == 'custodia'
        assert cuerpo['requiere_confirmacion'] is False
        assert cuerpo['placa'] == flota_mundo['placa']
        assert cuerpo['tiene_turno_abierto'] is True

    def test_un_vehiculo_ocupado_dice_quien_lo_tiene(
            self, app, client, db, flota_mundo, conductor_con_cuenta):
        """El dato que convierte un 409 en una conversación."""
        from app.models.conductor import Conductor
        from app.services.ruta_service import RutaService
        from flota.adaptadores import traspaso
        from flota.dominio.valores import CustodioTipo

        otro = Conductor(nombre='Víctor', cedula='EP-2', activo=True)
        db.session.add(otro)
        db.session.commit()
        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_000, registrado_por_usuario_id=1,
            custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=otro.id)

        _, usuario = conductor_con_cuenta
        cuerpo = client.get('/flota/conductor/mi-turno',
                            headers=_auth(self._token(app, usuario))).get_json()
        ocupado = [c for c in cuerpo['candidatos'] if c['ocupado_por']]
        assert ocupado and ocupado[0]['ocupado_por'] == 'Víctor'

    def test_un_conductor_no_puede_quitarle_el_turno_por_HTTP(
            self, app, client, db, flota_mundo, conductor_con_cuenta):
        """El rol decide `quien_pide`, no el cuerpo del request.

        Si viniera en el JSON, un conductor mandaría `admin_zona` y la regla
        entera se saltaría con un campo.
        """
        from app.models.conductor import Conductor
        from flota.adaptadores import traspaso
        from flota.dominio.valores import CustodioTipo

        otro = Conductor(nombre='Víctor', cedula='EP-3', activo=True)
        db.session.add(otro)
        db.session.commit()
        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_000, registrado_por_usuario_id=1,
            custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=otro.id)

        conductor, usuario = conductor_con_cuenta
        r = client.post('/flota/custodia/traspaso', json={
            'placa': flota_mundo['placa'], 'km': 100_100,
            'custodio_tipo': 'conductor', 'custodio_conductor_id': conductor.id,
            'motivo_forzado': 'me lo llevo igual',   # ← ignorado: no es admin
        }, headers=_auth(self._token(app, usuario)))
        assert r.status_code == 409
        assert 'Víctor' in r.get_json()['error']
        # Dice cómo, no solo que no. Ver el test hermano en test_traspaso_t1.
        assert 'Entregar turno' in r.get_json()['error']

    def test_mis_reportes_muestra_si_le_cerraron_el_turno(
            self, app, client, db, flota_mundo, conductor_con_cuenta):
        """Que se entere por su app, no por un tercero tres días después."""
        from flota.adaptadores import traspaso
        from flota.dominio.valores import CustodioTipo, QuienPide

        conductor, usuario = conductor_con_cuenta
        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_000, registrado_por_usuario_id=usuario.id,
            custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=conductor.id)
        # El admin tiene que ser OTRO usuario, no un `1` mágico.
        #
        # Desde el 2026-08-05 «quien pide ES el custodio actual» significa algo:
        # cerrar el turno propio no es un cierre forzado. Con `usuario_id=1`
        # coincidiendo por casualidad con la cuenta del conductor, este test
        # afirmaba un forzado que no ocurría.
        from app.models.usuario import Usuario
        admin = Usuario(email='admin-forzado@x.com', nombre='Admin',
                        rol='admin', activo=True)
        admin.set_password('x')
        db.session.add(admin)
        db.session.commit()

        traspaso.traspasar(
            vehiculo_id=flota_mundo['veh'], km=100_100,
            registrado_por_usuario_id=admin.id,
            custodio_tipo=CustodioTipo.SEDE, custodio_sede_id=flota_mundo['alm'],
            quien_pide=QuienPide.ADMIN_ZONA, motivo_forzado='se fue sin cerrar')

        cuerpo = client.get('/flota/conductor/mis-reportes',
                            headers=_auth(self._token(app, usuario))).get_json()
        forzado = [t for t in cuerpo['turnos'] if t['cerrado_a_la_fuerza']]
        assert forzado and 'sin cerrar' in forzado[0]['motivo_del_cierre_forzado']
