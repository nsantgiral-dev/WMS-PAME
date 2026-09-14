"""
E2E de punta a punta del módulo de flota — entrega de turnos (custodia) y
asignación de vehículo a conductor, para la matriz de escenarios que el
usuario pidió cubrir (2026-09-04): "seguimiento a la entrega de turnos y
asignaciones entre los conductores".

No toca Siesa: `custodia`/`turno`/`traspaso` son dominio puro de flota —
ninguno de los tres importa `connekta` ni nada de `app.services.*_siesa*`
(lo verifica `TestTrinqueteFronteraDominio` en `test_trinquetes_flota.py`).
Por eso este archivo corre contra los endpoints reales
(`client.post`/`client.get`) sin ningún doble de Siesa.

## Qué recorre cada escenario

Los once escenarios siguen la vida real de una placa en un solo hilo
narrativo, no once turnos sueltos: FLT100 nace con Ana, pasa a Beto por
entrega directa (turno propio), Eva se lo lleva a la fuerza cuando Beto no
cierra, Eva lo entrega a la sede, y Diego lo recibe limpio al día
siguiente. En paralelo, FLT200 cubre la cascada de asignación
(custodia → ruta programada → elección libre) y FLT300 cubre el bloqueo de
"un conductor, un vehículo a la vez".

## El reporte

Igual que `tests/flujo/test_e2e_ciclo_completo_liquidacion.py`: cada
escenario se registra con `_reportar()` — cómo entró y cómo terminó. Por
defecto no escribe nada a disco; si `E2E_REPORTE_DIR` está definida, al
cerrar la sesión de pytest se vuelca `e2e_flota_turnos.md`/`.json` ahí.
"""
import json
import os
from datetime import date, datetime, timezone

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

_REPORTE = []
_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


def _reportar(escenario, entrada, salida):
    _REPORTE.append({'escenario': escenario, 'entrada': entrada, 'salida': salida})


def _render_markdown(reporte):
    hoy = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    out = [
        '# Reporte E2E — Flota: entrega de turnos y asignación de conductores',
        '',
        f'Generado: {hoy}. {len(reporte)} escenarios, corridos de punta a '
        'punta contra los endpoints reales de `flota/` (sin Siesa — el '
        'dominio de custodia/turno no lo toca).',
        '',
    ]
    for i, r in enumerate(reporte, 1):
        out.append(f'## {i}. {r["escenario"]}')
        out.append('')
        out.append('**Cómo inició:**')
        out.append('')
        for k, v in r['entrada'].items():
            out.append(f'- `{k}`: {v}')
        out.append('')
        out.append('**Cómo terminó:**')
        out.append('')
        for k, v in r['salida'].items():
            out.append(f'- `{k}`: {v}')
        out.append('')
    return '\n'.join(out)


@pytest.fixture(scope='session', autouse=True)
def _volcar_reporte():
    yield
    destino = os.environ.get('E2E_REPORTE_DIR')
    if not destino or not _REPORTE:
        return
    os.makedirs(destino, exist_ok=True)
    with open(os.path.join(destino, 'e2e_flota_turnos.json'), 'w', encoding='utf-8') as f:
        json.dump(_REPORTE, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(destino, 'e2e_flota_turnos.md'), 'w', encoding='utf-8') as f:
        f.write(_render_markdown(_REPORTE))


# ── Actores y maestros ───────────────────────────────────────────────────

def _conductor(db, app, nombre, cedula, email):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario

    u = Usuario(nombre=nombre, email=email,
                password_hash=generate_password_hash('e2e12345'),
                rol='conductor', activo=True)
    db.session.add(u)
    db.session.flush()
    c = Conductor(nombre=nombre, cedula=cedula, usuario_id=u.id,
                  activo=True, disponible=True)
    db.session.add(c)
    db.session.commit()
    with app.app_context():
        token = create_access_token(identity=str(u.id))
    return {'usuario_id': u.id, 'conductor_id': c.id, 'token': token, 'nombre': nombre}


def _vehiculo(db, placa, tipo='NHR'):
    from app.models.vehiculo import Vehiculo
    v = Vehiculo(placa=placa, tipo=tipo, activo=True)
    db.session.add(v)
    db.session.commit()
    return v


def _sede(db, codigo, nombre):
    from app.models.almacen import Almacen
    a = Almacen(codigo=codigo, nombre=nombre)
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def elenco(db, app):
    """Cuatro conductores, tres vehículos, una sede — todo el reparto de la
    narrativa en un solo fixture para que cada test lo reciba armado."""
    return {
        'ana': _conductor(db, app, 'Ana E2E', 'E2E-ANA', 'ana.e2e@test.com'),
        'beto': _conductor(db, app, 'Beto E2E', 'E2E-BETO', 'beto.e2e@test.com'),
        'carla': _conductor(db, app, 'Carla E2E', 'E2E-CARLA', 'carla.e2e@test.com'),
        'diego': _conductor(db, app, 'Diego E2E', 'E2E-DIEGO', 'diego.e2e@test.com'),
        'eva': _conductor(db, app, 'Eva E2E', 'E2E-EVA', 'eva.e2e@test.com'),
        'flt100': _vehiculo(db, 'FLT100'),
        'flt200': _vehiculo(db, 'FLT200'),
        'flt300': _vehiculo(db, 'FLT300'),
        'sede1': _sede(db, 'FLT-SEDE', 'Patio principal'),
    }


class TestFlujoDeTurnosYAsignaciones:
    """Un solo hilo narrativo, en orden: cada test depende del estado que
    dejó el anterior (misma `db` de función — cada método usa `elenco`,
    pero los métodos corren en el orden declarado dentro de la clase y
    pytest no la reordena)."""

    def test_01_recibo_inicial_es_linea_base(self, client, jwt_token_admin, elenco):
        """Ana recibe FLT100 por primera vez — nadie lo tenía antes."""
        r = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['ana']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        assert r.status_code == 201, r.get_json()
        body = r.get_json()
        assert body['linea_base'] is True

        _reportar(
            'Recibo inicial (línea base) — Ana recibe FLT100',
            {'conductor': 'Ana E2E', 'placa': 'FLT100', 'km': 1000,
             'quien_registra': 'admin'},
            {'status': r.status_code, 'custodia_id': body['custodia_id'],
             'linea_base': body['linea_base'],
             'nota': 'primera custodia del vehículo — daños preexistentes sin responsable'},
        )

    def test_02_ana_entrega_su_propio_turno_a_beto(self, client, jwt_token_admin, elenco):
        """Ana (con SU token) entrega FLT100 a Beto — turno propio, sin forzado."""
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['ana']['conductor_id'],
        }, headers=_auth(jwt_token_admin))

        r = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1180, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['beto']['conductor_id'],
        }, headers=_auth(elenco['ana']['token']))
        assert r.status_code == 201, r.get_json()
        body = r.get_json()

        from flota.adaptadores.modelos import Custodia
        anterior = Custodia.query.filter_by(
            vehiculo_id=elenco['flt100'].id,
            custodio_conductor_id=elenco['ana']['conductor_id']).one()
        assert anterior.fin_ts is not None
        assert anterior.cierre_forzado is False

        _reportar(
            'Entrega directa entre conductores — Ana entrega FLT100 a Beto',
            {'entrega': 'Ana E2E', 'recibe': 'Beto E2E', 'placa': 'FLT100',
             'km_cierre': 1180, 'quien_registra': 'Ana (su propio token)'},
            {'status': r.status_code, 'custodia_nueva_id': body['custodia_id'],
             'custodia_anterior_cerrada': anterior.fin_ts is not None,
             'cierre_forzado': anterior.cierre_forzado,
             'nota': 'cerrar el turno propio siempre se puede, sin fotos ni motivo'},
        )

    def test_03_mi_turno_con_custodia_activa_no_pide_confirmacion(
            self, client, jwt_token_admin, elenco):
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['ana']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1180, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['beto']['conductor_id'],
        }, headers=_auth(elenco['ana']['token']))

        r = client.get('/flota/conductor/mi-turno', headers=_auth(elenco['beto']['token']))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['origen'] == 'custodia'
        assert body['placa'] == 'FLT100'
        assert body['requiere_confirmacion'] is False

        _reportar(
            'Mi turno — Beto ya tiene custodia activa',
            {'conductor': 'Beto E2E', 'consulta': 'GET /flota/conductor/mi-turno'},
            {'status': r.status_code, 'origen': body['origen'],
             'placa': body['placa'],
             'requiere_confirmacion': body['requiere_confirmacion'],
             'nota': 'segundo paso de la cascada: ya lo tiene, no se pregunta'},
        )

    def test_04_mi_turno_por_ruta_programada_pide_confirmacion(
            self, client, jwt_token_admin, elenco, db):
        from app.utils.fecha import dia_operativo
        from app.models.ruta_despacho import RutaDespacho

        ruta = RutaDespacho(conductor_id=elenco['carla']['conductor_id'],
                             vehiculo_id=elenco['flt200'].id,
                             tipo_ruta='Urbana', fecha_programada=dia_operativo(),
                             estado='PROGRAMADO')
        db.session.add(ruta)
        db.session.commit()

        r = client.get('/flota/conductor/mi-turno', headers=_auth(elenco['carla']['token']))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['origen'] == 'ruta'
        assert body['vehiculo_id'] == elenco['flt200'].id
        assert body['requiere_confirmacion'] is True

        _reportar(
            'Mi turno — Carla sin custodia pero con ruta programada hoy',
            {'conductor': 'Carla E2E', 'ruta_id': ruta.id, 'vehiculo_sugerido': 'FLT200',
             'consulta': 'GET /flota/conductor/mi-turno'},
            {'status': r.status_code, 'origen': body['origen'],
             'vehiculo_id': body['vehiculo_id'],
             'requiere_confirmacion': body['requiere_confirmacion'],
             'nota': 'la ruta es sugerencia, exige confirmar la placa antes de arrancar'},
        )

    def test_05_mi_turno_sin_nada_cae_a_eleccion_libre(self, client, elenco):
        r = client.get('/flota/conductor/mi-turno', headers=_auth(elenco['diego']['token']))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['origen'] == 'eleccion'
        assert body['requiere_confirmacion'] is True
        assert len(body['candidatos']) >= 3

        _reportar(
            'Mi turno — Diego sin custodia ni ruta',
            {'conductor': 'Diego E2E', 'consulta': 'GET /flota/conductor/mi-turno'},
            {'status': r.status_code, 'origen': body['origen'],
             'n_candidatos': len(body['candidatos']),
             'nota': 'tercer escalón de la cascada: elige de la lista completa'},
        )

    def test_06_otro_conductor_no_puede_tomar_vehiculo_ajeno(
            self, client, jwt_token_admin, elenco):
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['ana']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1180, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['beto']['conductor_id'],
        }, headers=_auth(elenco['ana']['token']))

        r = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1200, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['eva']['conductor_id'],
        }, headers=_auth(elenco['eva']['token']))
        assert r.status_code == 409, r.get_json()
        mensaje = r.get_json()['error']
        assert 'Beto' in mensaje

        _reportar(
            'Bloqueo — Eva intenta tomar el FLT100 que tiene Beto, sin admin',
            {'conductor_que_pide': 'Eva E2E', 'placa': 'FLT100',
             'quien_lo_tiene': 'Beto E2E', 'quien_registra': 'Eva (su propio token)'},
            {'status': r.status_code, 'mensaje': mensaje,
             'nota': 'la conversación es entre Eva y Beto, no un admin resolviendo por atajo'},
        )

    def test_07_admin_fuerza_el_cierre_cuando_beto_no_entrego(
            self, client, jwt_token_admin, elenco):
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['ana']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1180, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['beto']['conductor_id'],
        }, headers=_auth(elenco['ana']['token']))

        # Sin motivo: bloqueado incluso para el admin.
        sin_motivo = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1300, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['eva']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        assert sin_motivo.status_code == 409, sin_motivo.get_json()

        con_motivo = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1300, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['eva']['conductor_id'],
            'motivo_forzado': 'Beto no devolvió el vehículo ni respondió, el camión '
                               'tiene que salir con Eva hoy',
        }, headers=_auth(jwt_token_admin))
        assert con_motivo.status_code == 201, con_motivo.get_json()

        r = client.get('/flota/custodia/cierres-forzados', headers=_auth(jwt_token_admin))
        assert r.status_code == 200
        cierres = [c for c in r.get_json()['cierres'] if c['placa'] == 'FLT100']
        assert len(cierres) == 1
        assert cierres[0]['lo_tenia'] == 'Beto E2E'

        _reportar(
            'Cierre forzado — admin le pasa el FLT100 a Eva sin que Beto cerrara',
            {'placa': 'FLT100', 'custodio_anterior': 'Beto E2E', 'custodio_nuevo': 'Eva E2E',
             'primer_intento': 'sin motivo_forzado (rechazado)',
             'segundo_intento': 'con motivo_forzado'},
            {'status_sin_motivo': sin_motivo.status_code,
             'status_con_motivo': con_motivo.status_code,
             'visible_en_cierres_forzados': True,
             'lo_tenia': cierres[0]['lo_tenia'],
             'nota': 'sin fotos de cierre — el próximo turno arranca sin comparación'},
        )

    def test_08_un_conductor_no_puede_tener_dos_vehiculos(
            self, client, jwt_token_admin, elenco):
        # Eva ya tiene FLT100 (mismo camino de los escenarios 1-2-7).
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['ana']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1180, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['eva']['conductor_id'],
        }, headers=_auth(elenco['ana']['token']))

        r = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT300', 'km': 50, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['eva']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        assert r.status_code == 409, r.get_json()
        mensaje = r.get_json()['error']
        assert 'FLT100' in mensaje

        _reportar(
            'Bloqueo — Eva intenta recibir un segundo vehículo sin entregar el primero',
            {'conductor': 'Eva E2E', 'ya_tiene': 'FLT100', 'intenta_recibir': 'FLT300'},
            {'status': r.status_code, 'mensaje': mensaje,
             'nota': 'un conductor responde por un solo camión a la vez'},
        )

    def test_09_entrega_a_sede_deja_el_vehiculo_libre_de_friccion(
            self, client, jwt_token_admin, elenco):
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['ana']['conductor_id'],
        }, headers=_auth(jwt_token_admin))
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1180, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['eva']['conductor_id'],
        }, headers=_auth(elenco['ana']['token']))

        entrega = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1250, 'custodio_tipo': 'sede',
            'custodio_sede_id': elenco['sede1'].id, 'ubicacion': 'sede',
        }, headers=_auth(elenco['eva']['token']))
        assert entrega.status_code == 201, entrega.get_json()

        activa = client.get('/flota/custodia/activa/FLT100', headers=_auth(jwt_token_admin))
        assert activa.get_json()['custodia']['custodio_tipo'] == 'sede'

        # Diego lo recibe al día siguiente SIN admin ni forzado — la sede no
        # es una persona que haya que convencer de soltarlo.
        recibo = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT100', 'km': 1250, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['diego']['conductor_id'],
        }, headers=_auth(elenco['diego']['token']))
        assert recibo.status_code == 201, recibo.get_json()

        _reportar(
            'Entrega a sede (fin de turno) — Eva entrega FLT100 en el patio',
            {'conductor': 'Eva E2E', 'placa': 'FLT100', 'sede': 'Patio principal',
             'siguiente': 'Diego lo recibe al día siguiente'},
            {'status_entrega': entrega.status_code,
             'custodio_tipo_tras_entrega': activa.get_json()['custodia']['custodio_tipo'],
             'status_recibo_diego': recibo.status_code,
             'nota': 'recibirlo desde la sede no exige forzado — ya quedó bien entregado'},
        )

    def test_10_vehiculo_fuera_de_sede_queda_visible_con_motivo(
            self, client, jwt_token_admin, elenco):
        r = client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT200', 'km': 300, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['carla']['conductor_id'],
            'ubicacion': 'fuera_de_sede',
            'ubicacion_motivo': 'ruta larga a Pitalito, duerme allá',
        }, headers=_auth(jwt_token_admin))
        assert r.status_code == 201, r.get_json()

        fuera = client.get('/flota/custodia/fuera-de-sede', headers=_auth(jwt_token_admin))
        assert fuera.status_code == 200
        filas = [f for f in fuera.get_json()['fuera_de_sede'] if f['placa'] == 'FLT200']
        assert len(filas) == 1
        assert filas[0]['responde'] == 'Carla E2E'
        assert 'Pitalito' in filas[0]['motivo']

        _reportar(
            'Vehículo fuera de sede — Carla pernocta con el FLT200',
            {'conductor': 'Carla E2E', 'placa': 'FLT200',
             'motivo': 'ruta larga a Pitalito, duerme allá'},
            {'status': r.status_code, 'visible_en_fuera_de_sede': True,
             'responde': filas[0]['responde'],
             'nota': 'tiene que verse el lunes, no descubrirse cuando aparezca un golpe'},
        )

    def test_11_admin_consulta_que_vehiculo_tiene_un_conductor(
            self, client, jwt_token_admin, elenco):
        client.post('/flota/custodia/traspaso', json={
            'placa': 'FLT300', 'km': 10, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': elenco['diego']['conductor_id'],
        }, headers=_auth(jwt_token_admin))

        r = client.get(f'/flota/custodia/vehiculo-de-conductor/{elenco["diego"]["conductor_id"]}',
                       headers=_auth(jwt_token_admin))
        assert r.status_code == 200
        body = r.get_json()
        assert body['placa'] == 'FLT300'

        sin_vehiculo = client.get(
            f'/flota/custodia/vehiculo-de-conductor/{elenco["carla"]["conductor_id"]}',
            headers=_auth(jwt_token_admin))
        assert sin_vehiculo.get_json()['vehiculo_id'] is None

        _reportar(
            'Asignación — admin arma "Programar viaje" y consulta el vehículo de Diego',
            {'conductor': 'Diego E2E', 'consulta': 'GET /flota/custodia/vehiculo-de-conductor/<id>',
             'control': 'Carla E2E (sin custodia vigente)'},
            {'status': r.status_code, 'placa_encontrada': body['placa'],
             'control_sin_vehiculo': sin_vehiculo.get_json()['vehiculo_id'] is None,
             'nota': 'es la contraparte de mi-turno, pero para que un admin elija por otro'},
        )
