"""Lo que el servidor dice y la pantalla escuchaba: los tres casos.

## El patrón, medido

Un barrido por AST el 2026-09-03 contó **11 de 95 claves** que la API de flota
devuelve y ningún JS lee. Tres eran de la misma familia y las tres le costaban
algo a una persona concreta:

| clave | quién la necesitaba | qué veía en cambio |
|---|---|---|
| `estado_vehiculo` *(no existía)* | el conductor a las 5 a.m. | su placa y su kilometraje |
| `confianza` / `motivo_dudosa` | quien registra un odómetro | «Lectura registrada ✓» en verde |
| `tu_rol` / `roles_permitidos` | quien recibe un 403 | «Sin permiso» a secas |

Es la firma del módulo entero: **se mide, se publica, y no hay lector.**

## Qué NO hace este cambio

**No bloquea nada.** El estado informa; el camión sale igual. Es la secuencia
obligatoria —medir, corregir, imponer— y la regla 1: dejar un camión en el patio
por una pantalla es cómo la operación desmonta el sistema en 48 horas. Lo que
cierra es la frase «no sabía».
"""
from datetime import date, datetime, timedelta

import pytest

_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


@pytest.fixture
def mundo(db, almacen):
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.traspaso import traspasar
    from flota.dominio.valores import CustodioTipo

    v = Vehiculo(placa='CVE100', tipo='NHR', activo=True)
    u = Usuario(nombre='Turno A', email='cve@t.co', rol='conductor',
                almacen_id=almacen.id, activo=True,
                password_hash=generate_password_hash('x'))
    db.session.add_all([v, u])
    db.session.flush()
    c = Conductor(nombre='Turno A', cedula='CVE-1', activo=True, usuario_id=u.id)
    db.session.add(c)
    db.session.commit()

    traspasar(vehiculo_id=v.id, km=1000, registrado_por_usuario_id=u.id,
              custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=c.id)
    return {'veh': v.id, 'placa': v.placa, 'usr': u.id, 'cond': c.id, 'db': db,
            't': create_access_token(identity=str(u.id))}


def _turno(client, mundo):
    r = client.get('/flota/conductor/mi-turno', headers=_auth(mundo['t']))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


class TestElConductorVeElEstadoDeSuCamion:
    def test_un_camion_sano_no_le_grita(self, client, mundo):
        """La otra dirección primero: si el bloque apareciera siempre, se dejaría
        de leer — la lección de los 639 avisos conocidos."""
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgos_abiertos'] == 0
        assert e['hallazgos_vencidos'] == 0
        assert e['hallazgo_peor'] is None
        assert e['documentos_vencidos'] == []

    def test_ve_el_dano_que_el_mismo_reporto(self, client, mundo):
        """**El caso.** Hasta hoy reportaba un daño y al día siguiente su
        pantalla no lo mencionaba."""
        from flota.adaptadores import hallazgos

        hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='bloqueante',
                           descripcion='Freno de servicio sin presión', km=1200,
                           reportado_por_usuario_id=mundo['usr'])
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgos_abiertos'] == 1
        assert e['hallazgo_peor']['criticidad'] == 'bloqueante'
        assert 'Freno' in e['hallazgo_peor']['descripcion']

    def test_el_peor_es_el_bloqueante_y_no_el_primero_que_salga(
            self, client, mundo):
        """Un contador dice cuántos; el conductor necesita saber CUÁL mirar."""
        from flota.adaptadores import hallazgos

        hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='menor',
                           descripcion='rayón', km=1100,
                           reportado_por_usuario_id=mundo['usr'])
        hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='bloqueante',
                           descripcion='dirección con juego', km=1200,
                           reportado_por_usuario_id=mundo['usr'])
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgo_peor']['criticidad'] == 'bloqueante'

    def test_un_dano_vencido_se_cuenta_aparte(self, client, mundo):
        from flota.adaptadores import hallazgos
        from flota.adaptadores.modelos import Hallazgo

        h = hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='mayor',
                               descripcion='fuga', km=1100,
                               reportado_por_usuario_id=mundo['usr'])
        Hallazgo.query.get(h.id).fecha_limite = datetime.utcnow() - timedelta(days=2)
        mundo['db'].session.commit()
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgos_vencidos'] == 1
        assert e['hallazgo_peor']['vencido'] is True

    def test_ve_un_documento_vencido(self, client, mundo):
        """Una tecnomecánica vencida es un camión que no debería circular, y el
        que lo maneja no se enteraba."""
        from flota.adaptadores.modelos import DocumentoVehiculo

        mundo['db'].session.add(DocumentoVehiculo(
            vehiculo_id=mundo['veh'], tipo='rtm', numero='X-1',
            entidad='CDA Neiva', fecha_expedicion=date(2024, 11, 11),
            fecha_vencimiento=date(2025, 11, 11), estado='vigente'))
        mundo['db'].session.commit()
        e = _turno(client, mundo)['estado_vehiculo']
        assert [d['tipo'] for d in e['documentos_vencidos']] == ['rtm']

    def test_sin_vehiculo_el_estado_es_None_y_no_un_cero(self, client, db, almacen):
        """Regla 4: «no hay a qué mirarle el estado» no es «está todo bien»."""
        from flask_jwt_extended import create_access_token
        from werkzeug.security import generate_password_hash

        from app.models.conductor import Conductor
        from app.models.usuario import Usuario

        u = Usuario(nombre='Sin camión', email='sinv@t.co', rol='conductor',
                    almacen_id=almacen.id, activo=True,
                    password_hash=generate_password_hash('x'))
        db.session.add(u)
        db.session.flush()
        db.session.add(Conductor(nombre='Sin camión', cedula='SV-1',
                                 activo=True, usuario_id=u.id))
        db.session.commit()
        r = client.get('/flota/conductor/mi-turno',
                       headers=_auth(create_access_token(identity=str(u.id))))
        assert r.get_json()['estado_vehiculo'] is None


class TestHabilitaDespachoLlegaAQuienDecide:
    """`habilita_despacho` se calculaba, se publicaba, y su único lector era un
    mensaje que desaparecía. Ahora le llega al conductor."""

    def test_sin_inspeccion_lo_dice_sin_afirmar_que_esta_bien(self, client, mundo):
        i = _turno(client, mundo)['estado_vehiculo']['inspeccion_de_hoy']
        assert i['hecha'] is False
        assert i['habilita_despacho'] is None, (
            'un `False` diría que la inspección salió mal; lo que pasa es que '
            'no se hizo, y son cosas distintas (regla 4)')

    def test_una_inspeccion_no_apta_viaja_como_tal(self, client, mundo):
        """Con el adaptador REAL y no con una fila a mano.

        `flota_inspeccion` tiene seis CHECK que ligan veredicto, ítems sin dato
        y bloqueantes; una fila construida a mano o los adivina o los viola —y
        si los adivina bien, prueba que la base acepta esa fila, no que el
        sistema la produzca. Es la regla del arnés de `tests/flujo/`: cada etapa
        se avanza llamando al servicio que la operación llama.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores import catalogo, inspecciones

        catalogo.sembrar(mundo['db'])
        vehiculo = Vehiculo.query.get(mundo['veh'])
        items = inspecciones.items_del_dia_de(vehiculo, __import__(
            'app.utils.fecha', fromlist=['dia_operativo']).dia_operativo())
        bloqueante = next(i for i in items if i.criticidad == 'bloqueante')

        inspecciones.registrar(
            vehiculo_id=mundo['veh'], tipo_vehiculo_obj=vehiculo, km=1500,
            inspeccionada_por_usuario_id=mundo['usr'],
            respuestas=[{'item_id': i.id,
                         'respuesta': 'no_apto' if i.id == bloqueante.id else 'optimo'}
                        for i in items],
            segundos_llenado=180)

        i = _turno(client, mundo)['estado_vehiculo']['inspeccion_de_hoy']
        assert i['hecha'] is True
        assert i['veredicto'] == 'no_apto'
        assert i['habilita_despacho'] is False, (
            'un bloqueante en no_apto tiene que llegarle al conductor: el '
            'veredicto se calculaba y su único lector era un toast')

    def test_una_inspeccion_apta_SI_habilita(self, client, mundo):
        """La otra dirección: si `habilita_despacho` fuera siempre False, el
        campo no distinguiría nada y el conductor aprendería a ignorarlo."""
        from app.models.vehiculo import Vehiculo
        from app.utils.fecha import dia_operativo
        from flota.adaptadores import catalogo, inspecciones

        catalogo.sembrar(mundo['db'])
        vehiculo = Vehiculo.query.get(mundo['veh'])
        items = inspecciones.items_del_dia_de(vehiculo, dia_operativo())
        inspecciones.registrar(
            vehiculo_id=mundo['veh'], tipo_vehiculo_obj=vehiculo, km=1500,
            inspeccionada_por_usuario_id=mundo['usr'],
            respuestas=[{'item_id': i.id, 'respuesta': 'optimo'} for i in items],
            segundos_llenado=180)

        i = _turno(client, mundo)['estado_vehiculo']['inspeccion_de_hoy']
        assert i['veredicto'] == 'apto'
        assert i['habilita_despacho'] is True


class TestElErrorDicePorQue:
    def test_un_403_de_flota_trae_el_rol_que_haria_falta(self, client, mundo):
        """El docstring de `exige` promete que el 403 diga qué hace falta.
        Lo dice — y hasta hoy la pantalla lo tiraba."""
        r = client.post('/flota/gastos', json={}, headers=_auth(mundo['t']))
        assert r.status_code == 403
        d = r.get_json()
        assert d['tu_rol'] == 'conductor'
        assert d['roles_permitidos']
