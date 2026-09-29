"""La cola del conductor no pierde nada — el lado del servidor (T2, 2026-09-27).

## La clase

*«Un registro que el conductor hizo sin señal se pierde o se deforma al
llegar tarde.»* Cuatro formas, todas medidas en la auditoría del 2026-09-27:

1. **El rechazo borraba** la operación del teléfono y el servidor no guardaba
   nada: control de flota no se enteraba de un tanqueo pagado que no entró.
   → el rechazo se anota (`flota_rechazo_cola`) y la bandeja lo muestra.
2. **La inspección del lunes que llegó el martes** se juzgaba con la lista del
   martes y daba 409 por los ítems semanales. → se juzga con el día en que se
   hizo (`dominio.cola.dia_de_la_operacion`), y lo que no tocaba se descarta y
   se declara.
3. **El recibo repetido** (la tarjeta volvía a pedir «Recibir» tras
   sincronizar) cerraba el turno propio y abría otro. → no-op declarado. Lo
   prueba `test_cola_del_conductor.py`.
4. **La jornada** le ponía confianza alta a la hora de un recibo que llegó por
   la cola horas después. → baja, con la hora del teléfono a la vista.

## Lo que NO se afirma

Que el reloj del teléfono diga la verdad. Se le cree dentro de una ventana
(`TOLERANCIA_RELOJ_ADELANTADO`, `MAX_DIAS_EN_EL_TELEFONO`) y fuera de ella se
vuelve al día del servidor, declarado.
"""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.utils.fecha import dia_operativo_de
from flota.dominio.cola import (MAX_DIAS_EN_EL_TELEFONO, TOLERANCIA_RELOJ_ADELANTADO,
                                dia_de_la_operacion)

RAIZ = Path(__file__).resolve().parents[2]


def _auth(t):
    return {'Authorization': f'Bearer {t}'}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · El día de la operación (dominio, puro)
# ═══════════════════════════════════════════════════════════════════════════

class TestElDiaDeLaOperacion:
    AHORA = datetime(2026, 9, 22, 15, 0)          # martes 10:00 en Bogotá

    def test_sin_hora_del_telefono_es_el_dia_del_servidor(self):
        d = dia_de_la_operacion(None, self.AHORA, dia_operativo_de)
        assert d.dia == date(2026, 9, 22) and d.fuente == 'servidor'

    def test_lo_que_se_hizo_ayer_se_juzga_con_ayer(self):
        d = dia_de_la_operacion(datetime(2026, 9, 21, 11, 0), self.AHORA,
                                dia_operativo_de)
        assert d.dia == date(2026, 9, 21) and d.fuente == 'telefono'
        assert '2026-09-21' in d.motivo

    def test_las_nueve_de_la_noche_de_bogota_es_ese_dia(self):
        """21:00 del lunes en Bogotá = 02:00 UTC del martes: el día es LUNES."""
        d = dia_de_la_operacion(datetime(2026, 9, 22, 2, 0), self.AHORA,
                                dia_operativo_de)
        assert d.dia == date(2026, 9, 21)

    def test_un_reloj_adelantado_no_decide(self):
        d = dia_de_la_operacion(self.AHORA + TOLERANCIA_RELOJ_ADELANTADO
                                + timedelta(minutes=1), self.AHORA, dia_operativo_de)
        assert d.fuente == 'servidor' and 'adelantado' in d.motivo

    def test_dentro_de_la_tolerancia_se_le_cree(self):
        d = dia_de_la_operacion(self.AHORA + timedelta(minutes=5), self.AHORA,
                                dia_operativo_de)
        assert d.fuente == 'telefono'

    def test_mas_de_una_semana_atras_no_decide(self):
        d = dia_de_la_operacion(
            self.AHORA - timedelta(days=MAX_DIAS_EN_EL_TELEFONO, minutes=1),
            self.AHORA, dia_operativo_de)
        assert d.fuente == 'servidor'


# ═══════════════════════════════════════════════════════════════════════════
# El mundo
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mundo(db, app, almacen, tmp_path, monkeypatch):
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import catalogo
    from tests.flota._turno import dar_turno

    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    veh = Vehiculo(placa='CNP100', tipo='camion', activo=True)
    cond = Usuario(nombre='Yesid Cola', email='cnp_cond@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    otro = Usuario(nombre='Otro Cola', email='cnp_otro@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control Cola', email='cnp_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, cond, otro, flota])
    db.session.commit()
    catalogo.sembrar(db)
    dar_turno(db, cond.id, veh.placa, km=1000, nombre='Yesid Cola')
    with app.app_context():
        return {
            'placa': veh.placa, 'vehiculo_id': veh.id, 'u_cond': cond.id,
            't_cond': create_access_token(identity=str(cond.id)),
            't_otro': create_access_token(identity=str(otro.id)),
            't_flota': create_access_token(identity=str(flota.id)),
        }


def _dano(client, mundo, km, clave=None, token=None):
    # `km < 1000` es «un daño que el servidor rechaza». Hasta el 2026-09-29 lo
    # rechazaba el km que retrocede; desde VAL-COLA-1 ese, por la cola, entra en
    # duda: el rechazo sale ahora de una gravedad que no existe (409 igual).
    cuerpo = {'placa': mundo['placa'],
              'criticidad': 'rarisima' if km < 1000 else 'menor',
              'descripcion': 'rayón', 'km': km}
    if clave:
        cuerpo['clave_idempotencia'] = clave
        cuerpo['ts_dispositivo'] = '2026-09-27T11:00:00Z'
    return client.post('/flota/hallazgos', json=cuerpo,
                       headers=_auth(token or mundo['t_cond']))


def _rechazo(clave):
    from flota.adaptadores.modelos import RechazoCola
    return RechazoCola.query.filter_by(clave=clave).one_or_none()


# ═══════════════════════════════════════════════════════════════════════════
# 2 · El rechazo se anota, y se resuelve solo cuando entra
# ═══════════════════════════════════════════════════════════════════════════

class TestElRechazoSeAnota:

    def test_un_409_de_la_cola_queda_anotado_y_el_registro_no(self, client, mundo):
        from flota.adaptadores.modelos import Hallazgo
        r = _dano(client, mundo, km=500, clave='k-rech-1')     # retrocede
        assert r.status_code == 409, r.get_json()
        fila = _rechazo('k-rech-1')
        assert fila is not None, 'el rechazo no quedó anotado: control de flota no se entera'
        assert fila.estado == 'abierto' and fila.intentos == 1
        assert fila.operacion == 'hallazgo' and fila.placa == mundo['placa']
        assert fila.status_http == 409 and 'criticidad' in fila.mensaje
        assert fila.ts_dispositivo is not None
        assert Hallazgo.query.count() == 0, 'el registro rechazado dejó filas'

    def test_el_reintento_que_vuelve_a_fallar_suma_intentos(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-rech-2')
        _dano(client, mundo, km=500, clave='k-rech-2')
        assert _rechazo('k-rech-2').intentos == 2

    def test_cuando_la_misma_clave_entra_queda_resuelto(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-rech-3')
        r = _dano(client, mundo, km=1200, clave='k-rech-3')
        assert r.status_code == 201, r.get_json()
        assert _rechazo('k-rech-3').estado == 'resuelto'

    def test_sin_clave_no_se_anota_nada(self, client, mundo):
        """El escritorio no manda clave: ve el 409 en su pantalla."""
        from flota.adaptadores.modelos import RechazoCola
        assert _dano(client, mundo, km=500).status_code == 409
        assert RechazoCola.query.count() == 0

    def test_un_rechazo_por_rol_no_llega_a_anotarse(self, client, mundo):
        """`@exige` corta antes de la cola: no es un registro que se perdió,
        es alguien sin permiso."""
        from flota.adaptadores.modelos import RechazoCola
        from app.models.usuario import Usuario
        from app.extensions import db
        u = Usuario(nombre='Tienda', email='cnp_tienda@test.com',
                    password_hash=generate_password_hash('x'), rol='tienda',
                    activo=True)
        db.session.add(u)
        db.session.commit()
        r = _dano(client, mundo, km=2000, clave='k-rol',
                  token=create_access_token(identity=str(u.id)))
        assert r.status_code == 403
        assert RechazoCola.query.count() == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3 · El conductor avisa o descarta; control de flota cierra
# ═══════════════════════════════════════════════════════════════════════════

class TestElConductorDecideSobreLoSuyo:

    def test_pedir_ayuda_lo_marca(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-ayuda')
        r = client.post('/flota/conductor/rechazos/k-ayuda', json={'accion': 'ayuda'},
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 200, r.get_json()
        assert 'bandeja' in r.get_json()['mensaje']
        assert _rechazo('k-ayuda').pidio_ayuda_ts is not None

    def test_descartar_lo_marca_sin_borrarlo(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-desc')
        r = client.post('/flota/conductor/rechazos/k-desc', json={'accion': 'descartar'},
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 200
        assert _rechazo('k-desc').estado == 'descartado'

    def test_un_rechazo_que_el_servidor_no_conocia_se_crea_con_lo_del_telefono(
            self, client, mundo):
        r = client.post('/flota/conductor/rechazos/k-viejo', json={
            'accion': 'ayuda', 'operacion': 'tanqueo', 'placa': mundo['placa'],
            'mensaje': 'No se pudo registrar el tanqueo'}, headers=_auth(mundo['t_cond']))
        assert r.status_code == 200, r.get_json()
        fila = _rechazo('k-viejo')
        assert fila.operacion == 'tanqueo' and fila.vehiculo_id == mundo['vehiculo_id']

    def test_sin_decir_que_era_no_se_inventa(self, client, mundo):
        r = client.post('/flota/conductor/rechazos/k-nada', json={'accion': 'ayuda'},
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 400

    def test_una_accion_desconocida_es_400(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-acc')
        r = client.post('/flota/conductor/rechazos/k-acc', json={'accion': 'borrar'},
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 400

    def test_el_de_otro_conductor_no_se_toca(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-ajeno')
        r = client.post('/flota/conductor/rechazos/k-ajeno', json={'accion': 'descartar'},
                        headers=_auth(mundo['t_otro']))
        assert r.status_code == 403 and r.get_json()['motivo'] == 'sin_derecho'
        assert _rechazo('k-ajeno').estado == 'abierto'


class TestControlDeFlotaCierraConMotivo:

    def _cerrar(self, client, mundo, token, motivo):
        fila = _rechazo('k-cerrar')
        return client.post(f'/flota/rechazos/{fila.id}/cerrar', json={'motivo': motivo},
                           headers=_auth(token))

    def test_el_conductor_no_cierra(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-cerrar')
        assert self._cerrar(client, mundo, mundo['t_cond'], 'x').status_code == 403

    def test_sin_motivo_no(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-cerrar')
        assert self._cerrar(client, mundo, mundo['t_flota'], '  ').status_code == 400
        assert _rechazo('k-cerrar').estado == 'abierto'

    def test_con_motivo_queda_cerrado_con_su_autor(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-cerrar')
        r = self._cerrar(client, mundo, mundo['t_flota'], 'se registró desde la oficina')
        assert r.status_code == 200, r.get_json()
        fila = _rechazo('k-cerrar')
        assert fila.estado == 'cerrado' and fila.cerrado_por_usuario_id is not None

    def test_lo_que_ya_entro_no_se_cierra(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-cerrar')
        _dano(client, mundo, km=1300, clave='k-cerrar')
        assert self._cerrar(client, mundo, mundo['t_flota'], 'x').status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# 4 · La bandeja lo muestra
# ═══════════════════════════════════════════════════════════════════════════

class TestLaBandejaLoMuestra:

    def _pendientes(self):
        from flota.adaptadores.bandeja import armar_bandeja
        return [p for p in armar_bandeja()['pendientes']
                if p['clase'] == 'registro_rechazado']

    def test_un_rechazo_abierto_es_un_pendiente_con_su_placa_y_su_motivo(
            self, client, mundo):
        _dano(client, mundo, km=500, clave='k-b1')
        (p,) = self._pendientes()
        assert p['placa'] == mundo['placa']
        assert 'Yesid Cola' in p['texto'] and 'criticidad' in p['texto']
        assert p['accion'] == {'tipo': 'rechazo', 'rechazo_id': _rechazo('k-b1').id}

    def test_el_que_pidio_ayuda_va_primero(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-b2')
        _dano(client, mundo, km=400, clave='k-b3')
        client.post('/flota/conductor/rechazos/k-b3', json={'accion': 'ayuda'},
                    headers=_auth(mundo['t_cond']))
        ps = self._pendientes()
        assert ps[0]['rechazo_id'] == _rechazo('k-b3').id and ps[0]['pidio_ayuda']
        assert 'PIDIÓ AYUDA' in ps[0]['detalle']

    def test_el_descartado_se_sigue_viendo_y_se_dice(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-b4')
        client.post('/flota/conductor/rechazos/k-b4', json={'accion': 'descartar'},
                    headers=_auth(mundo['t_cond']))
        (p,) = self._pendientes()
        assert p['descartado'] and 'descartó' in p['detalle']

    def test_lo_resuelto_y_lo_cerrado_salen(self, client, mundo):
        _dano(client, mundo, km=500, clave='k-b5')
        _dano(client, mundo, km=1300, clave='k-b5')
        assert self._pendientes() == []


# ═══════════════════════════════════════════════════════════════════════════
# 5 · La inspección se juzga con el día en que se hizo
# ═══════════════════════════════════════════════════════════════════════════

LUNES, MARTES = date(2026, 9, 21), date(2026, 9, 22)
MARTES_10AM_UTC = datetime(2026, 9, 22, 15, 0)
LUNES_6AM_TEL = '2026-09-21T11:00:00Z'           # 06:00 Bogotá


@pytest.fixture
def martes(monkeypatch):
    """El servidor cree que es martes 10:00 en Bogotá."""
    from flota.api import inspecciones as api
    monkeypatch.setattr(api, '_ahora', lambda: MARTES_10AM_UTC)


def _lista(mundo, dia):
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import inspecciones as ad
    return ad.items_del_dia_de(Vehiculo.query.get(mundo['vehiculo_id']), dia)


def _inspeccion(client, mundo, items, **extra):
    cuerpo = {'placa': mundo['placa'], 'km': 10_000, 'segundos_llenado': 95,
              'respuestas': [{'item_id': i.id, 'respuesta': 'optimo'} for i in items]}
    cuerpo.update(extra)
    return client.post('/flota/inspeccion', json=cuerpo, headers=_auth(mundo['t_cond']))


class TestLaInspeccionDelLunesQueLlegaElMartes:

    def test_precondicion_el_lunes_trae_items_que_el_martes_no(self, mundo):
        """Sin esto, los tests de abajo pasarían por una lista sin semanales."""
        assert {i.id for i in _lista(mundo, LUNES)} > {i.id for i in _lista(mundo, MARTES)}

    def test_por_la_cola_entra_con_la_lista_del_lunes(self, client, mundo, martes):
        from flota.adaptadores.modelos import Inspeccion
        r = _inspeccion(client, mundo, _lista(mundo, LUNES),
                        clave_idempotencia='k-insp-lunes', ts_dispositivo=LUNES_6AM_TEL)
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d['dia'] == LUNES.isoformat() and d['dia_fuente'] == 'telefono'
        assert d['items_descartados'] == []
        assert d['veredicto'] == 'apto', 'las respuestas del lunes no contaron'
        assert Inspeccion.query.one().dia == LUNES, (
            'la inspección del lunes quedó como del martes: contaría como '
            '«inspección de hoy» de un día en que nadie inspeccionó')

    def test_desde_el_escritorio_sigue_siendo_el_dia_del_servidor(
            self, client, mundo, martes):
        r = _inspeccion(client, mundo, _lista(mundo, LUNES))
        assert r.status_code == 409, r.get_json()

    def test_lo_que_no_tocaba_se_descarta_y_se_dice(self, client, mundo, martes):
        """La lista del lunes contestada el martes (el teléfono no bajó la
        nueva): los semanales se descartan y se declaran; nada se rechaza."""
        lunes = _lista(mundo, LUNES)
        semanales = {i.id for i in lunes} - {i.id for i in _lista(mundo, MARTES)}
        r = _inspeccion(client, mundo, lunes, clave_idempotencia='k-insp-martes',
                        ts_dispositivo='2026-09-22T12:00:00Z')
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d['dia'] == MARTES.isoformat()
        assert {x['item_id'] for x in d['items_descartados']} == semanales
        assert 'no se guard' in d['aviso']

    def test_con_el_reloj_adelantado_se_usa_el_dia_del_servidor(
            self, client, mundo, martes):
        r = _inspeccion(client, mundo, _lista(mundo, MARTES),
                        clave_idempotencia='k-insp-adelantado',
                        ts_dispositivo='2026-09-24T12:00:00Z')
        assert r.status_code == 201, r.get_json()
        assert r.get_json()['dia'] == MARTES.isoformat()
        assert r.get_json()['dia_fuente'] == 'servidor'


# ═══════════════════════════════════════════════════════════════════════════
# 6 · mi-turno dice de qué día es
# ═══════════════════════════════════════════════════════════════════════════

class TestMiTurnoDiceDeQueDiaEs:

    def test_trae_el_dia(self, client, mundo):
        from app.utils.fecha import dia_operativo
        r = client.get('/flota/conductor/mi-turno', headers=_auth(mundo['t_cond']))
        assert r.status_code == 200
        assert r.get_json()['dia'] == dia_operativo().isoformat()


# ═══════════════════════════════════════════════════════════════════════════
# 7 · La jornada no le cree a la hora de sincronizar
# ═══════════════════════════════════════════════════════════════════════════

class _MundoFalso:
    def __init__(self, cola):
        from app.services.jornada_conductor import UMBRALES_POR_DEFECTO
        self.cola_flota = cola
        self.U = dict(UMBRALES_POR_DEFECTO)


class TestLaJornadaYLaCola:

    def _hora(self, cola, entidad_id=7):
        from app.services.jornada_conductor import _hora_de_un_gesto_de_flota
        return _hora_de_un_gesto_de_flota(_MundoFalso(cola), 'traspaso', entidad_id)

    def test_lo_registrado_en_linea_es_alta(self):
        assert self._hora({})[0] == 'alta'

    def test_por_la_cola_horas_despues_es_baja_y_muestra_la_del_telefono(self):
        conf, motivo, tel = self._hora({('traspaso', 7): (
            datetime(2026, 9, 22, 16, 0), datetime(2026, 9, 22, 10, 30))})
        assert conf == 'baja' and 'sincronizar' in motivo
        assert tel == '05:30'

    def test_por_la_cola_al_instante_sigue_alta(self):
        conf, _m, _t = self._hora({('traspaso', 7): (
            datetime(2026, 9, 22, 10, 31), datetime(2026, 9, 22, 10, 30))})
        assert conf == 'alta'

    def test_por_la_cola_sin_hora_del_telefono_es_media(self):
        assert self._hora({('traspaso', 7): (datetime(2026, 9, 22, 16, 0), None)})[0] == 'media'


#: Los eventos de flota que pueden llegar por la cola del teléfono.
_GESTOS_DE_FLOTA = {'recibo_turno', 'entrega_turno', 'preoperacional', 'tanqueo'}


def _eventos_con_hora_en_linea(fuente: str):
    """`Evento('<gesto de flota>', ..., *_EN_LINEA, ...)`: la hora afirmada como
    en línea sin preguntar si llegó por la cola. Solo llamadas reales (AST)."""
    malas = []
    for n in ast.walk(ast.parse(fuente)):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == 'Evento' and n.args):
            continue
        tipo = n.args[0]
        if not (isinstance(tipo, ast.Constant) and tipo.value in _GESTOS_DE_FLOTA):
            continue
        if any(isinstance(a, ast.Starred) and isinstance(a.value, ast.Name)
               and a.value.id == '_EN_LINEA' for a in n.args):
            malas.append(tipo.value)
    return malas


class TestNingunGestoDeFlotaSeAfirmaEnLinea:
    """TRINQUETE — la clase: *«un gesto que puede llegar por la cola declara su
    hora como en línea»*. Ningún evento de flota usa `_EN_LINEA` directo."""

    def _fuente(self):
        return (RAIZ / 'app' / 'services' / 'jornada_conductor.py').read_text(
            encoding='utf-8')

    def test_ninguno(self):
        assert _eventos_con_hora_en_linea(self._fuente()) == []

    def test_piso_los_cuatro_gestos_existen(self):
        fuente = self._fuente()
        vistos = {n.args[0].value for n in ast.walk(ast.parse(fuente))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == 'Evento' and n.args
                  and isinstance(n.args[0], ast.Constant)}
        assert _GESTOS_DE_FLOTA <= vistos, 'el escáner ya no ve los eventos de flota'

    def test_el_detector_ve_la_forma_y_no_marca_lo_sano(self):
        malo = "Evento('tanqueo', ts, 'servidor', *_EN_LINEA, propio=True)"
        sano = ("Evento('cargue', ts, 'servidor', *_EN_LINEA)\n"
                "Evento('tanqueo', ts, 'servidor', conf, motivo)\n"
                "# Evento('recibo_turno', ts, 'servidor', *_EN_LINEA)\n"
                "x = \"Evento('preoperacional', t, 's', *_EN_LINEA)\"")
        assert _eventos_con_hora_en_linea(malo) == ['tanqueo']
        assert _eventos_con_hora_en_linea(sano) == []


# ═══════════════════════════════════════════════════════════════════════════
# 8 · El adaptador `flota.adaptadores.rechazos_cola`, directo
# ═══════════════════════════════════════════════════════════════════════════

class TestElAdaptadorDeRechazos:

    def test_una_accion_fuera_del_vocabulario_levanta(self, mundo):
        from flota.adaptadores import rechazos_cola
        with pytest.raises(rechazos_cola.RechazoInvalido):
            rechazos_cola.marcar(usuario_id=mundo['u_cond'], clave='k', accion='borrar',
                                 operacion='tanqueo', datos={}, mensaje=None,
                                 ts_dispositivo=None)

    def test_cerrar_sin_motivo_levanta(self, client, mundo):
        from flota.adaptadores import rechazos_cola
        _dano(client, mundo, km=500, clave='k-ad')
        with pytest.raises(rechazos_cola.RechazoInvalido):
            rechazos_cola.cerrar(rechazo_id=_rechazo('k-ad').id,
                                 usuario_id=mundo['u_cond'], motivo='   ')

    def test_descartar_queda_en_la_bitacora(self, client, mundo):
        from app.models.bitacora import BitacoraAccion
        _dano(client, mundo, km=500, clave='k-bit')
        client.post('/flota/conductor/rechazos/k-bit', json={'accion': 'descartar'},
                    headers=_auth(mundo['t_cond']))
        b = BitacoraAccion.query.filter_by(accion='DESCARTAR', entidad='RechazoCola').one()
        assert b.usuario_id == mundo['u_cond'] and b.despues['estado'] == 'descartado'

    def test_una_anotacion_que_falla_no_rompe_el_rechazo(self, client, mundo, monkeypatch):
        """El 409 tiene que llegar al teléfono aunque la anotación reviente:
        el teléfono igual conserva la operación."""
        from flota.adaptadores import rechazos_cola
        monkeypatch.setattr(rechazos_cola, 'texto_del_rechazo',
                            lambda *a: (_ for _ in ()).throw(RuntimeError('x')))
        r = _dano(client, mundo, km=500, clave='k-falla')
        assert r.status_code == 409
