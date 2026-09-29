"""Validación crítica de flota T2+T3 («la cola no pierde nada», «el odómetro no
se envenena»), 2026-09-29. Cada P1 que la validación encontró, como xfail
estricto. Cerrados los tres el 2026-09-29 (n1-flota-cola-qa): las marcas se
quitaron y los tests quedan como guardia.

VAL-COLA-1 · Un km MENOR que el último, en el recibo, lo bloquea el teléfono:
    no se encola, no llega al servidor, no queda en la bandeja. El conductor
    que recibe un camión cuyo km anterior alguien tecleó de más (+1.000, que no
    es ×10 y entró como `cuenta`) no puede recibirlo, pierde las doce fotos si
    cierra, y control de flota no se entera. La salida natural —teclear un
    número ≥ al malo— envenena la serie sin marca.

VAL-COLA-2 · El derecho del conductor se juzga con la custodia de AHORA, no la
    del momento en que hizo el registro: un tanqueo (o un daño) hecho sin señal
    durante su turno, que sincroniza después de que otro recibió el camión (o
    de un cierre forzado), da 403 `sin_derecho` para siempre. «Reintentar» no
    lo arregla nunca; la plata solo entra si control de flota la reconstruye
    del teléfono.

VAL-COLA-3 · Sin espacio en el teléfono la cola se CUELGA en vez de decirlo:
    `_condDB.set` (rutas.js) resuelve con `oncomplete` y rechaza con `onerror`,
    pero un `QuotaExceededError` al confirmar la transacción de IndexedDB solo
    dispara `abort`. La promesa no se resuelve nunca: `flotaColaRegistrar` no
    devuelve (el botón queda en «Guardando…»), el candado de la cola queda
    tomado y nada más sale hasta recargar. La rama «perdido: no cierre esta
    pantalla» nunca se alcanza.
"""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'


def _auth(t):
    return {'Authorization': f'Bearer {t}'}


# ═══════════════════════════════════════════════════════════════════════════
# VAL-COLA-1 · el km menor del recibo no puede quedar sin rastro
# ═══════════════════════════════════════════════════════════════════════════

class TestUnKmMenorEnElReciboNoQuedaSinRastro:

    def test_el_conductor_puede_mandar_el_km_que_marca_el_tablero(self, tmp_path):
        from tests.flota.test_mi_camion_hoy_js import _correr

        ts = (datetime.utcnow() - timedelta(hours=12)).isoformat() + 'Z'
        # El anterior tecleó 13.500 (el tablero marcaba 12.500): +1.000 no es
        # ×10, entró como `cuenta`. El que recibe ve 12.520 y lo confirma.
        semilla = ('FLOTA_COND = ' + json.dumps({
            'placa': 'THP696',
            'km_plausible': {'km': 13500, 'ts': ts, 'km_dia': 400}}) + ';'
            'confirm = () => true;')
        assert _correr(tmp_path, "flotaKmCreible('THP696', 12520, 'cf-error')",
                       semilla=semilla) is True


# ═══════════════════════════════════════════════════════════════════════════
# VAL-COLA-2 · el derecho se juzga con el turno del momento del registro
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def relevo(db, app, almacen, tmp_path, monkeypatch):
    """A tiene el camión; B lo recibe después. A tenía un tanqueo y un daño en
    la cola, hechos durante SU turno."""
    from flask_jwt_extended import create_access_token

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import catalogo
    from tests.flota._turno import dar_turno

    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    veh = Vehiculo(placa='VRL100', tipo='camion', activo=True)
    a = Usuario(nombre='Ana Relevo', email='vrl_a@test.com',
                password_hash=generate_password_hash('x'), rol='conductor',
                almacen_id=almacen.id, activo=True)
    b = Usuario(nombre='Beto Relevo', email='vrl_b@test.com',
                password_hash=generate_password_hash('x'), rol='conductor',
                almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, a, b])
    db.session.commit()
    catalogo.sembrar(db)
    from app.models.conductor import Conductor
    from flota.adaptadores import traspaso
    from flota.dominio.valores import CustodioTipo, QuienPide

    # Ana recibió hace dos horas (el fixture original la hacía recibir AHORA,
    # y «hace 30 min» caía ANTES de su turno: el escenario no era el que el
    # docstring describe). 2026-09-29, al cerrar VAL-COLA-2.
    dar_turno(db, a.id, veh.placa, km=1000, nombre='Ana Relevo',
              ts=datetime.utcnow() - timedelta(hours=2))
    hecho_por_a = datetime.utcnow() - timedelta(minutes=30)   # durante su turno
    # Ana se fue sin entregar (su teléfono sin señal, con la cola llena): el
    # admin de zona fuerza el cierre y Beto recibe el camión.
    cb = Conductor(nombre='Beto Relevo', cedula='VRL-B', activo=True, usuario_id=b.id)
    db.session.add(cb)
    db.session.commit()
    traspaso.traspasar(
        vehiculo_id=veh.id, km=1100, registrado_por_usuario_id=b.id,
        custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=cb.id,
        quien_pide=QuienPide.ADMIN_ZONA,
        motivo_forzado='Ana se fue sin entregar el camión')
    with app.app_context():
        return {'placa': veh.placa, 'hecho': hecho_por_a.isoformat() + 'Z',
                't_a': create_access_token(identity=str(a.id))}


class TestLoQueSeHizoEnElTurnoEntraAunqueLlegueDespues:

    def test_el_tanqueo_de_a_entra(self, client, relevo):
        cuerpo = dict(placa=relevo['placa'], fecha=datetime.utcnow().date().isoformat(),
                      valor='168000', galones='12', tanque='lleno',
                      estacion='Terpel Garzón', km=1050, proveedor='Terpel',
                      origen_costo='tarjeta_convenio',
                      clave_idempotencia='val-relevo-tq',
                      ts_dispositivo=relevo['hecho'])
        r = client.post('/flota/tanqueos', json=cuerpo, headers=_auth(relevo['t_a']))
        assert r.status_code == 201, r.get_json()

    def test_el_dano_de_a_entra(self, client, relevo):
        cuerpo = {'placa': relevo['placa'], 'criticidad': 'menor',
                  'descripcion': 'frenos largos', 'km': 1050,
                  'clave_idempotencia': 'val-relevo-dano',
                  'ts_dispositivo': relevo['hecho']}
        r = client.post('/flota/hallazgos', json=cuerpo, headers=_auth(relevo['t_a']))
        assert r.status_code == 201, r.get_json()


# ═══════════════════════════════════════════════════════════════════════════
# VAL-COLA-3 · sin espacio, la cola dice «no quedó guardado»; no se cuelga
# ═══════════════════════════════════════════════════════════════════════════

#: IndexedDB con el disco lleno, como lo hace el navegador: el `put` sale bien
#: y la transacción ABORTA al confirmar (QuotaExceededError). Sin evento
#: `error` en la transacción: la especificación lo dice («abort … due to an
#: error while committing … does not always result in any error events»).
_IDB_LLENO = r"""
const __ALMACEN_IDB = {};
indexedDB = { open() {
  const req = {};
  const db = {
    objectStoreNames: { contains: () => true }, createObjectStore() {},
    transaction() {
      const tx = { oncomplete: null, onerror: null, onabort: null, error: null,
        objectStore() { return {
          get(k) { const r = {}; setTimeout(() => {
            r.result = __ALMACEN_IDB[k]; if (r.onsuccess) r.onsuccess(); }, 0); return r; },
          put(v) { const r = {}; setTimeout(() => {
            if (r.onsuccess) r.onsuccess();
            setTimeout(() => { tx.error = { name: 'QuotaExceededError' };
                               if (tx.onabort) tx.onabort({ target: tx }); }, 0);
          }, 0); return r; },
        }; } };
      return tx;
    } };
  setTimeout(() => { req.result = db; if (req.onsuccess) req.onsuccess({ target: req }); }, 0);
  return req;
} };
"""


def _cond_db_real():
    """El `_condDB` de rutas.js, tal cual (la IIFE entera)."""
    src = (PWA / 'rutas.js').read_text(encoding='utf-8')
    m = re.search(r'const _condDB = (\(\(\) => \{.*?\n\}\)\(\));', src, re.S)
    assert m, 'no se encontró la IIFE de _condDB en rutas.js'
    return m.group(1)


def _correr_disco_lleno(tmp_path, expr):
    from tests.flota.test_mi_camion_hoy_js import HARNESS

    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    semilla = _IDB_LLENO + '_condDB = ' + _cond_db_real() + ';'
    g.write_text(json.dumps({'semilla': semilla, 'expr': expr, 'respuestas': [],
                             'get': {}, 'sin_almacen': True}), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)['salida']


_CARRERA = ("Promise.race([{p}, new Promise(r => setTimeout(() => r('colgado'), {ms}))])")


class TestSinEspacioLaColaNoSeCuelga:

    def test_el_arnes_mide_lo_que_dice(self, tmp_path):
        """Sanidad del doble: la lectura funciona, así que lo que cuelga es la
        escritura y no el arnés."""
        assert _correr_disco_lleno(tmp_path, _CARRERA.format(
            p="_condDB.get('x').then(v => v === null ? 'leido' : 'raro')", ms=500)) == 'leido'

    def test_guardar_sin_espacio_se_rechaza(self, tmp_path):
        assert _correr_disco_lleno(tmp_path, _CARRERA.format(
            p="_condDB.set('flota_cola', [1]).then(() => 'guardado', () => 'rechazado')",
            ms=500)) == 'rechazado'

    def test_el_registro_dice_que_no_quedo_guardado(self, tmp_path):
        r = _correr_disco_lleno(tmp_path, _CARRERA.format(
            p="flotaColaRegistrar('tanqueo', 'tanqueo', 'THP696', {km: 1}).then(x => x.estado)",
            ms=3000))
        assert r == 'perdido'
