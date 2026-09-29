"""Los tres P1 de la validación de T2/T3 (2026-09-29), y los P2 que se cerraron.

VAL-COLA-1 · Un km MENOR que el último, por la cola del conductor, no traba el
    recibo: entra en duda (`serie='contradice'`, o `tardia` si se hizo antes de
    la lectura que lo supera), no mueve el tope, queda pendiente de
    verificación, y el escritorio puede anular CUALQUIER lectura. Verificar una
    lectura que dejaría la serie decreciente se rechaza (también el salto).
VAL-COLA-2 · Lo que llega por la cola se autoriza —y se atribuye— con el turno
    EN EL INSTANTE en que se hizo, si la hora del teléfono es creíble.
VAL-COLA-3 · Sin espacio, IndexedDB aborta al confirmar: la cola lo dice y no
    se cuelga (`tests/flota/test_val_flota_cola_20260929.py` + la cola de rutas).
P2 · Un rechazo que control de flota CERRÓ no se ejecuta otra vez; una ventana
    lleno-a-lleno con un tanqueo sacado por su km no se mide.
"""
from datetime import date, datetime, timedelta

import pytest

from tests.flota.test_cola_del_conductor import _auth, _recibo, mundo  # noqa: F401
from tests.flota.test_val_flota_cola_20260929 import (  # noqa: F401
    _IDB_LLENO as _IDB_LLENO_SRC, _cond_db_real, _correr_disco_lleno, relevo)


def _correr_con_idb(tmp_path, idb, expr):
    """Como `_correr_disco_lleno`, con otro doble de IndexedDB."""
    import json
    import shutil
    import subprocess

    from tests.flota.test_mi_camion_hoy_js import HARNESS, PWA

    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'semilla': idb + '_condDB = ' + _cond_db_real() + ';',
                             'expr': expr, 'respuestas': [], 'get': {},
                             'sin_almacen': True}), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)['salida']


def _lectura(vehiculo_id, km, ts, autor, origen='cierre_dia', anula=None, motivo=None):
    from app.extensions import db
    from flota.adaptadores.modelos import LecturaOdometro

    f = LecturaOdometro(vehiculo_id=vehiculo_id, valor_km=km, ts=ts, origen=origen,
                        autor_usuario_id=autor, anula_lectura_id=anula,
                        motivo_correccion=motivo)
    db.session.add(f)
    db.session.commit()
    return f


def _sede_con(mundo, km):
    """El camión quedó en la sede con `km` (el conductor anterior tecleó de más)."""
    from app.models.almacen import Almacen
    from flota.adaptadores import traspaso
    from flota.dominio.valores import CustodioTipo, QuienPide

    alm = Almacen.query.first()
    return traspaso.traspasar(
        vehiculo_id=mundo['vehiculo_id'], km=km, registrado_por_usuario_id=1,
        custodio_tipo=CustodioTipo.SEDE, custodio_sede_id=alm.id,
        quien_pide=QuienPide.ADMIN_ZONA,
        ts=datetime.utcnow() - timedelta(hours=10))


def _lecturas(vehiculo_id):
    from flota.adaptadores.modelos import LecturaOdometro

    return LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id).order_by(
        LecturaOdometro.id).all()


def _actual(vehiculo_id):
    from flota.dominio import odometro as dom

    return dom.odometro_actual([l.a_dominio() for l in _lecturas(vehiculo_id)])


# ═══════════════════════════════════════════════════════════════════════════
# VAL-COLA-1
# ═══════════════════════════════════════════════════════════════════════════

class TestElReciboConKmMenorEntraEnDuda:

    def test_por_la_cola_entra_contradice_sin_mover_el_tope(self, client, mundo):
        from flota.adaptadores.modelos import Custodia

        sede = _sede_con(mundo, 13500)
        r = client.post('/flota/custodia/traspaso',
                        json=_recibo(mundo, 'k-menor', km=12520,
                                     ts_dispositivo=datetime.utcnow().isoformat() + 'Z'),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 201, r.get_json()
        ultima = _lecturas(mundo['vehiculo_id'])[-1]
        assert (ultima.valor_km, ultima.serie, ultima.confianza) == (
            12520, 'contradice', 'dudosa')
        assert 'control de flota' in ultima.motivo_dudosa
        assert _actual(mundo['vehiculo_id']) == 13500
        # El turno de la sede no queda con un cierre inventado.
        assert Custodia.query.get(sede.id).km_fin is None

    def test_sin_la_cola_sigue_siendo_409(self, client, mundo):
        _sede_con(mundo, 13500)
        r = client.post('/flota/custodia/traspaso', json=_recibo(mundo, km=12520),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 409

    def test_queda_pendiente_de_verificacion(self, client, mundo):
        from flota.adaptadores import verificacion

        _sede_con(mundo, 13500)
        client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-pend', km=12520),
                    headers=_auth(mundo['t_cond']))
        assert 12520 in [l.valor_km for l, _ in verificacion.pendientes()]


class TestVerificarNoDejaLaSerieDecreciente:

    def test_la_que_contradice_se_verifica_despues_de_anular_la_mala(
            self, client, mundo):
        from flota.adaptadores import verificacion

        _sede_con(mundo, 13500)
        client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-v', km=12520),
                    headers=_auth(mundo['t_cond']))
        mala = next(l for l in _lecturas(mundo['vehiculo_id']) if l.valor_km == 13500)
        nueva = _lecturas(mundo['vehiculo_id'])[-1]
        with pytest.raises(verificacion.VerificacionInvalida, match='decreciente'):
            verificacion.verificar(lectura_id=nueva.id, usuario_id=1)
        # El escritorio anula la mala (no es la última: antes no se podía).
        d = client.get(f"/flota/custodia/activa/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert mala.id in [l['id'] for l in d['lecturas_anulables']]
        r = client.post('/flota/odometro', json={
            'placa': mundo['placa'], 'valor_km': 12530, 'origen': 'correccion',
            'motivo_correccion': 'el anterior tecleó 13.500', 'anula_lectura_id': mala.id},
            headers=_auth(mundo['t_flota']))
        assert r.status_code == 201, r.get_json()
        verificacion.verificar(lectura_id=nueva.id, usuario_id=1)
        d = client.get(f"/flota/custodia/activa/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert mala.id not in [l['id'] for l in d['lecturas_anulables']]

    def test_un_salto_con_lecturas_menores_despues_no_se_verifica(self, mundo):
        from flota.adaptadores import verificacion

        t0 = datetime.utcnow() - timedelta(days=3)
        _lectura(mundo['vehiculo_id'], 12500, t0, 1)
        salto = _lectura(mundo['vehiculo_id'], 125000, t0 + timedelta(days=1), 1)
        assert salto.serie == 'salto'
        _lectura(mundo['vehiculo_id'], 12600, t0 + timedelta(days=2), 1)
        with pytest.raises(verificacion.VerificacionInvalida, match='decreciente'):
            verificacion.verificar(lectura_id=salto.id, usuario_id=1)


class TestLaInspeccionPorLaColaTampocoSeTraba:

    def test_con_km_menor_entra_contradice(self, client, mundo):
        """Recibo, daño y entrega no son las únicas puertas: la inspección que
        llega por la cola con un km menor tampoco se pierde."""
        from tests.flota.test_endpoints_inspeccion import _cuerpo, _items

        from app.extensions import db
        from flota.adaptadores import catalogo
        catalogo.sembrar(db)
        r = client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-ins-rec', km=1000),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 201, r.get_json()
        _lectura(mundo['vehiculo_id'], 1500, datetime.utcnow(), 1)
        items = _items(client, mundo)['items']
        cuerpo = _cuerpo(items, km=1200, clave_idempotencia='k-ins-menor',
                         ts_dispositivo=datetime.utcnow().isoformat() + 'Z')
        cuerpo['placa'] = mundo['placa']
        r = client.post('/flota/inspeccion', json=cuerpo, headers=_auth(mundo['t_cond']))
        assert r.status_code == 201, r.get_json()
        ultima = _lecturas(mundo['vehiculo_id'])[-1]
        assert (ultima.valor_km, ultima.serie) == (1200, 'contradice')
        assert _actual(mundo['vehiculo_id']) == 1500


# ═══════════════════════════════════════════════════════════════════════════
# VAL-COLA-2
# ═══════════════════════════════════════════════════════════════════════════

class TestElTurnoDeEntonces:

    def test_el_dano_queda_bajo_el_turno_de_quien_lo_vio(self, client, relevo):
        from app.models.conductor import Conductor
        from flota.adaptadores.modelos import Custodia, Hallazgo

        r = client.post('/flota/hallazgos', json={
            'placa': relevo['placa'], 'criticidad': 'menor', 'descripcion': 'frenos',
            'km': 1050, 'clave_idempotencia': 'k-ana-dano',
            'ts_dispositivo': relevo['hecho']}, headers=_auth(relevo['t_a']))
        assert r.status_code == 201, r.get_json()
        h = Hallazgo.query.filter_by(descripcion='frenos').one()
        ana = Conductor.query.filter_by(nombre='Ana Relevo').one()
        assert Custodia.query.get(h.custodia_id).custodio_conductor_id == ana.id
        from flota.adaptadores.modelos import LecturaOdometro
        lec = LecturaOdometro.query.get(h.lectura_id)
        assert lec.serie == 'tardia'     # se hizo antes del 1.100 de Beto

    def test_sin_hora_creible_sigue_siendo_403(self, client, relevo):
        vieja = (datetime.utcnow() - timedelta(days=9)).isoformat() + 'Z'
        r = client.post('/flota/hallazgos', json={
            'placa': relevo['placa'], 'criticidad': 'menor', 'descripcion': 'x',
            'km': 1050, 'clave_idempotencia': 'k-ana-vieja',
            'ts_dispositivo': vieja}, headers=_auth(relevo['t_a']))
        assert r.status_code == 403

    def test_la_hora_creible_tiene_sus_dos_bordes(self):
        """La MISMA ventana que decide el día: ni adelantada más de 10 min ni
        de más de 7 días atrás. Fuera de ella, manda el presente."""
        from flota.dominio.cola import instante_creible

        ahora = datetime(2026, 9, 29, 12)
        assert instante_creible(None, ahora) is None
        assert instante_creible(ahora - timedelta(days=6), ahora) == ahora - timedelta(days=6)
        assert instante_creible(ahora - timedelta(days=8), ahora) is None
        assert instante_creible(ahora + timedelta(minutes=5), ahora) is not None
        assert instante_creible(ahora + timedelta(minutes=30), ahora) is None

    def test_sin_la_cola_tampoco(self, client, relevo):
        r = client.post('/flota/hallazgos', json={
            'placa': relevo['placa'], 'criticidad': 'menor', 'descripcion': 'x',
            'km': 1150, 'ts_dispositivo': relevo['hecho']},
            headers=_auth(relevo['t_a']))
        assert r.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# P2 · lo que control de flota cerró no se registra dos veces
# ═══════════════════════════════════════════════════════════════════════════

class TestElCierreDeFlotaResuelveLaClave:

    def test_el_reintento_despues_del_cierre_no_duplica(self, client, mundo):
        from flota.adaptadores.modelos import Hallazgo, RechazoCola

        client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-b', km=1000),
                    headers=_auth(mundo['t_cond']))
        dano = {'placa': mundo['placa'], 'descripcion': 'rayón', 'km': 1100,
                'clave_idempotencia': 'k-cerrado'}
        r = client.post('/flota/hallazgos', json=dict(dano, criticidad='rarisima'),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code in (400, 409)
        fila = RechazoCola.query.filter_by(clave='k-cerrado').one()
        r = client.post(f'/flota/rechazos/{fila.id}/cerrar',
                        json={'motivo': 'lo registré a mano desde el escritorio'},
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 200, r.get_json()
        r = client.post('/flota/hallazgos', json=dict(dano, criticidad='menor'),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['ya_resuelto_por_flota'] is True
        assert 'a mano' in r.get_json()['mensaje']
        assert Hallazgo.query.count() == 0


# ═══════════════════════════════════════════════════════════════════════════
# P2 · la ventana con un tanqueo sacado por su km no se mide
# ═══════════════════════════════════════════════════════════════════════════

class TestLosGalonesDelQueSeSacaNoSePierden:

    def test_tres_llenos_sin_el_del_medio_no_dan_el_doble(self, mundo):
        from flota.adaptadores import gastos
        from flota.dominio import costos

        d0 = date(2026, 9, 1)
        tqs = []
        for i, km in enumerate((1000, 1400, 1800)):
            tqs.append(gastos.registrar_tanqueo(
                vehiculo_id=mundo['vehiculo_id'], fecha=d0 + timedelta(days=10 * i),
                valor='168000', galones='12', tanque='lleno', estacion='T', km=km,
                proveedor='T', origen_costo='tarjeta_convenio',
                registrado_por_usuario_id=1,
                ts=datetime(2026, 9, 1, 10) + timedelta(days=10 * i)))
        medio = tqs[1].gasto.lectura
        _lectura(mundo['vehiculo_id'], 1850, datetime(2026, 9, 25, 10), 1,
                 origen='correccion', anula=medio.id, motivo='tecleado mal')
        lista = gastos.tanqueos_de(mundo['vehiculo_id'])
        assert [t['km'] for t in lista] == [1000, 1800]
        assert costos.ventanas_lleno_a_lleno(lista) == [], (
            'sin los 12 galones del medio, 800 km / 12 galones es el doble')


# ═══════════════════════════════════════════════════════════════════════════
# Las pantallas (Node, util.js real)
# ═══════════════════════════════════════════════════════════════════════════

class TestLasPantallas:

    def test_el_escritorio_elige_cualquier_lectura(self, tmp_path):
        from tests.flota.test_mi_camion_hoy_js import _correr

        lecturas = [{'id': 9, 'valor_km': 13500, 'ts': '2026-09-28T10:00:00Z',
                     'origen': 'tanqueo', 'confianza': 'dudosa', 'serie': 'tardia'},
                    {'id': 7, 'valor_km': 13400, 'ts': '2026-09-27T10:00:00Z',
                     'origen': 'entrega', 'confianza': 'declarada', 'serie': 'cuenta'}]
        semilla = ("FLOTA_PLACA = 'THP696';"
                   "document.getElementById('od-origen').value = 'correccion';")
        salida = _correr(
            tmp_path,
            "(async () => { await flotaOrigenCambio();"
            " const de_entrada = FLOTA_OD_ANULA.id;"
            " document.getElementById('od-anula-sel').value = '1';"
            " flotaOdElegirAnulada();"
            " return [de_entrada, FLOTA_OD_ANULA.id,"
            "         document.getElementById('od-anula-sel').innerHTML]; })()",
            semilla=semilla,
            get={'/flota/custodia/activa/THP696': {'lecturas_anulables': lecturas}})
        assert salida[0] == 9 and salida[1] == 7
        assert 'value="1"' in salida[2] and '13.400 km' in salida[2]

    def test_la_cola_de_rutas_no_se_cree_guardada_sin_espacio(self, tmp_path):
        assert _correr_disco_lleno(tmp_path, (
            "Promise.race([_condDB.enqueue({a: 1}).then(() => 'guardado', "
            "() => 'rechazado'), new Promise(r => setTimeout(() => r('colgado'), 500))])"
        )) == 'rechazado'

    def test_un_guardado_que_nunca_termina_no_cuelga_la_cola(self, tmp_path):
        """Ni `complete` ni `abort`: la cola corta por tiempo (los relojes de
        diez segundos o más corren cien veces más rápido en el arnés)."""
        colgado = (_IDB_LLENO_SRC.replace(
            "if (r.onsuccess) r.onsuccess();\n            setTimeout(() => { tx.error = { name: 'QuotaExceededError' };\n"
            "                               if (tx.onabort) tx.onabort({ target: tx }); }, 0);",
            "if (r.onsuccess) r.onsuccess();"))
        assert colgado != _IDB_LLENO_SRC
        r = _correr_con_idb(tmp_path, colgado, (
            "(() => { const st = setTimeout; globalThis.setTimeout = (f, ms, ...a) =>"
            " st(f, ms >= 10000 ? ms / 100 : ms, ...a);"
            " return Promise.race([flotaColaRegistrar('tanqueo', 'tanqueo', 'THP696', {km: 1})"
            ".then(x => x.estado), new Promise(r => st(() => r('colgado'), 3000))]); })()"))
        assert r == 'perdido'

    def test_lo_resuelto_por_flota_sale_con_su_mensaje(self, tmp_path):
        from tests.flota.test_mi_camion_hoy_js import _correr

        s = _correr(tmp_path,
                    "flotaColaEnviarUna({tipo: 'tanqueo', cuerpo: {km: 1}})",
                    respuestas=[{'status': 200, 'json': {
                        'ya_resuelto_por_flota': True,
                        'mensaje': 'Control de flota ya lo resolvió: a mano.'}}])
        assert s['estado'] == 'hecho'
        assert s['mensaje'] == 'Control de flota ya lo resolvió: a mano.'

    def test_sin_espacio_el_conductor_lee_que_hacer(self, tmp_path):
        r = _correr_disco_lleno(tmp_path, (
            "Promise.race([flotaColaRegistrar('tanqueo', 'tanqueo', 'THP696', {km: 1})"
            ".then(x => x.mensaje), new Promise(r => setTimeout(() => r('colgado'), 3000))])"))
        assert 'No quedó guardado' in r and 'Libere espacio' in r
