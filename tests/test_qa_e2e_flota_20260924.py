"""Defectos encontrados por el recorrido e2e por rol de QA 960d2f1c (2026-09-24).

Cada test REPRODUCE un defecto observado en el recorrido HTTP con JWT real contra
la app local (SQLite desechable, Connekta en simulación) y en el render en Node
con `util.js` real. Están marcados `xfail(strict=True)`: el día que alguien
arregle el defecto, el test pasa, el xfail estricto se pone rojo y obliga a
quitar la marca — así la reproducción no queda como decoración.

No se tocó código de producción.

| Test | Defecto | Severidad |
|---|---|---|
| `TestListaDeParadasConEntregadoSinPago` | La lista de paradas del conductor revienta (TypeError) si una parada quedó ENTREGADO_SIN_PAGO | P1 |
| `TestFORZARDeAdvertenciasNoEsCierreForzado` | Jornada y bitácora leen el FORZAR de «despachar con advertencias» como «ruta cerrada a la fuerza» | P2 |
| `TestBandejaFinDelDiaNormal` | Ruta entregada + camión devuelto a la sede = señal «la ruta no coincide con el turno» y pendiente «turno de la sede sin fotos de inicio» | P2 |
| `TestAnguloInvalidoNoEs500` | Un ángulo de foto desconocido en el traspaso da 500 (CHECK) en vez de 400; la cola lo reintenta para siempre | P3 |
| `TestRechazoNoGuardaFormaDePago` | «No pagó y se quedó» guarda la forma de pago vieja del select (CREDITO) y el desglose la lista como parada CRÉDITO | P3 |
| `TestAdvertenciaSinCodigoCrudo` | «La inspección de hoy salió «no_apto»» — código crudo en el texto que ve el muelle | P3 |
"""
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

_JPEG = ('/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof'
         'Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh'
         'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR'
         'CAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA'
         'AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK'
         'FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG'
         'h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl'
         '5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+iiigD//Z')
_DATA_URL = f'data:image/jpeg;base64,{_JPEG}'


def _auth(t):
    return {'Authorization': f'Bearer {t}'}


def _foto(clase, angulo):
    return {'clase': clase, 'angulo': angulo, 'data_url': _DATA_URL,
            'ancho': 1, 'alto': 1, 'mime': 'image/jpeg'}


# ═════════════════════════════════════════════════════════════════════════
# Mundo mínimo: sede, vehículo, conductor con cuenta, control de flota, admin
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mundo(db, app, almacen, tmp_path, monkeypatch):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import catalogo

    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    s = uuid.uuid4().hex[:6]
    veh = Vehiculo(placa=f'QA{s[:4].upper()}', tipo='NHR', activo=True)

    def usuario(rol, nombre):
        u = Usuario(nombre=nombre, email=f'{rol}_{s}@qa.test', rol=rol,
                    almacen_id=almacen.id, activo=True)
        u.set_password('x')
        return u

    u_cond = usuario('conductor', 'Ana QA')
    u_flota = usuario('control_flota', 'Yesid QA')
    u_admin = usuario('admin', 'Laura QA')
    db.session.add_all([veh, u_cond, u_flota, u_admin])
    db.session.flush()
    c = Conductor(nombre='Ana QA', cedula=f'QA-{s}', usuario_id=u_cond.id,
                  activo=True, disponible=True)
    db.session.add(c)
    db.session.commit()
    catalogo.sembrar(db)
    db.session.commit()
    with app.app_context():
        return SimpleNamespace(
            veh=veh, placa=veh.placa, vehiculo_id=veh.id, conductor=c,
            conductor_id=c.id, u_cond=u_cond, u_admin=u_admin, almacen=almacen,
            t_cond=create_access_token(identity=str(u_cond.id)),
            t_flota=create_access_token(identity=str(u_flota.id)),
            t_admin=create_access_token(identity=str(u_admin.id)))


def _ruta_con_parada(db, m, estado='EN_CARGUE', bulto_estado='CARGADO', cond='C02'):
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    from app.utils.fecha import dia_operativo
    s = uuid.uuid4().hex[:6]
    ruta = RutaDespacho(conductor_id=m.conductor_id, vehiculo_id=m.vehiculo_id,
                        tipo_ruta='Urbana', estado=estado, fecha_programada=dia_operativo(),
                        fecha_cierre=datetime.utcnow() - timedelta(minutes=30))
    db.session.add(ruta)
    db.session.flush()
    t = TareaPacking(codigo=f'PK-QA-{s}', estado='DESPACHADO', almacen_id=m.almacen.id,
                     tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1,
                     numero_pedido_siesa=f'PD-QA-{s}', cliente='Droguería QA',
                     municipio='Neiva', cond_pago=cond, valor_factura=90000,
                     siesa_triggered=True, tipo_documento='PEDIDO')
    db.session.add(t)
    db.session.flush()
    for i in (1, 2):
        db.session.add(Bulto(tarea_id=t.id, ruta_despacho_id=ruta.id,
                             codigo_barras=f'B-QA-{uuid.uuid4().hex[:8]}', tipo='CAJA',
                             numero=i, total=2, estado=bulto_estado))
    db.session.commit()
    return ruta, t


# ═════════════════════════════════════════════════════════════════════════
# P1 · La lista de paradas del conductor revienta con ENTREGADO_SIN_PAGO
# ═════════════════════════════════════════════════════════════════════════

_ARNES_RUTAS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const G = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const nodos = new Map();
function nodo(id) {
  return { id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
           classList: { add() {}, remove() {}, contains() { return false; } },
           addEventListener() {}, appendChild() {}, focus() {} };
}
const ctx = {
  console, document: {
    getElementById: (id) => { if (!nodos.has(id)) nodos.set(id, nodo(id)); return nodos.get(id); },
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
    createElement: () => nodo(null), body: nodo('body') },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  alerta() {}, TOKEN: 'x', API: '', confirm: () => true,
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const a of ['util.js', 'rutas.js']) {
  vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf-8'), ctx, { filename: a });
}
vm.runInContext(`_COND_RUTA_ACTIVA = { id: 1 }; _COND_PARADAS = ${JSON.stringify(G.paradas)};`, ctx);
vm.runInContext(`_condRenderParadas(${JSON.stringify(G.d)})`, ctx);
process.stdout.write(JSON.stringify({ html: nodos.get('cond-contenido').innerHTML }));
"""


def _parada(estado):
    return {'tarea_id': 1, 'cliente': 'Droguería Pasteur', 'municipio': 'Neiva',
            'numero_pedido': 'PD2005', 'bultos': [{'id': 1}, {'id': 2}],
            'recaudo': {'estado_entrega': estado, 'forma_pago': None,
                        'monto_cobrado': 0, 'observaciones': 'se quedó con las cajas'}}


def _pintar_lista(tmp_path, estado):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'a.mjs'
    h.write_text(_ARNES_RUTAS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'paradas': [_parada(estado)],
                             'd': {'facturas_gestionadas': 1}}), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f'la lista de paradas reventó:\n{p.stderr[-600:]}'
    return json.loads(p.stdout)['html']


class TestListaDeParadasConEntregadoSinPago:
    """Reproducción (e2e): Ana confirma PD2005 como «No pagó y se quedó» (200,
    el servidor lo traduce a ENTREGADO_SIN_PAGO). `condVolverAParadas(true)` →
    `condAbrirParadas` → `_condRenderParadas` hace `EST_C[est]` con un mapa que
    solo tiene ENTREGADO/PARCIAL/RECHAZADO → `c` undefined →
    `TypeError: Cannot read properties of undefined (reading 'badgeBg')` en
    rutas.js:2013. La pantalla queda en «Cargando paradas...» y el conductor ya
    no ve el botón «Cerrar Ruta» de esa ruta (vive en esa misma lista)."""

    def test_control_la_lista_con_una_entrega_normal_pinta(self, tmp_path):
        assert 'Droguería Pasteur' in _pintar_lista(tmp_path, 'ENTREGADO')

    def test_la_lista_pinta_con_entregado_sin_pago(self, tmp_path):
        html = _pintar_lista(tmp_path, 'ENTREGADO_SIN_PAGO')
        assert 'Droguería Pasteur' in html


# ═════════════════════════════════════════════════════════════════════════
# P2 · El FORZAR de «despachar con advertencias» se lee como cierre forzado
# ═════════════════════════════════════════════════════════════════════════

class TestFORZARDeAdvertenciasNoEsCierreForzado:
    """Reproducción (e2e): el jefe despacha R3 con advertencias de flota
    (motivo → `_reconocer_advertencias_flota` escribe FORZAR sobre la
    RutaDespacho). Carla cierra la ruta normal (`/entregar`). La jornada de
    Carla muestra «Ruta cerrada a la fuerza por la oficina»
    (`jornada_conductor.py:461-465` junta todo FORZAR de RutaDespacho en
    `forzadas`) y la bitácora dice «Jorge forzó el cierre de la ruta RUTA-3»
    (`analitica_salud.VERBOS['FORZAR']`). Nadie forzó ningún cierre."""

    def _despachar_con_motivo_y_entregar(self, db, m):
        from app.services.ruta_service import RutaService
        ruta, _t = _ruta_con_parada(db, m, estado='EN_CARGUE')
        # El vehículo no tiene turno ni inspección: hay advertencias de flota.
        RutaService.cerrar_ruta(ruta.id, motivo_advertencias='sale igual, lo recibe en la vía',
                                usuario_id=m.u_admin.id)
        RutaService.entregar_ruta(ruta.id, {'bultos': []}, m.u_cond.id)
        return ruta

    def test_la_jornada_no_dice_cierre_forzado(self, client, db, mundo):
        from app.utils.fecha import dia_operativo
        self._despachar_con_motivo_y_entregar(db, mundo)
        r = client.get(f'/api/jornada?dia={dia_operativo().isoformat()}'
                       f'&conductor_id={mundo.conductor_id}', headers=_auth(mundo.t_flota))
        assert r.status_code == 200, r.get_json()
        tipos = [e.get('tipo') for e in r.get_json()['eventos']]
        assert 'cierre_ruta' in tipos and 'cierre_ruta_forzado' not in tipos, tipos

    def test_la_bitacora_no_dice_que_forzo_el_cierre(self, client, db, mundo):
        self._despachar_con_motivo_y_entregar(db, mundo)
        r = client.get('/api/analitica/bitacora?accion=FORZAR&entidad=RutaDespacho',
                       headers=_auth(mundo.t_admin))
        assert r.status_code == 200, r.get_json()
        frases = [a['frase'] for a in r.get_json()['acciones']]
        assert frases and not any('forzó el cierre' in f for f in frases), frases


# ═════════════════════════════════════════════════════════════════════════
# P2 · La bandeja convierte el fin de un día normal en señal y pendiente
# ═════════════════════════════════════════════════════════════════════════

class TestBandejaFinDelDiaNormal:
    """Reproducción (e2e): Ana recibe AAA111 con sus 11+1 fotos, hace su ruta,
    la cierra (ENTREGADA) y entrega el camión a la sede con las 4 fotos + tablero
    que pide el PWA (`FLOTA_ANGULOS_ENTREGA`). Es el día perfecto. La bandeja
    del encargado muestra:

    · señal `turno_de_la_ruta` «La ruta de hoy de AAA111 ya salió (entregada) y
      el vehículo sigue en custodia de la sede» — `dominio/senales.py:268`
      trata ENTREGADA como «en la calle» y compara contra la custodia ACTUAL;
      propone «Hacer el traspaso en la app», que ya se hizo;
    · pendiente `turno_sin_fotos` «turno de la sede … sin fotos de inicio (0 de
      11)» — `medicion.custodias_por_vehiculo` exige fotos de inicio también a
      la custodia de la SEDE, que nadie fotografía (las fotos las firmó el
      conductor como fotos de fin). Se repite 7 días (`VENTANA_RECIENTE_DIAS`)
      por cada noche en sede. El mundo de `test_bandeja.py` no lo ve porque le
      siembra `fotos=exigidas` a la custodia de sede (línea 223).
    """

    def _dia_normal(self, client, db, m):
        from tests.flota._turno import dar_turno
        from app.models.ruta_despacho import RutaDespacho
        dar_turno(db, m.u_cond.id, m.placa, km=1000)
        ruta, _t = _ruta_con_parada(db, m, estado='ENTREGADA', bulto_estado='ENTREGADO')
        RutaDespacho.query.get(ruta.id).fecha_entregada = datetime.utcnow()
        db.session.commit()
        fotos = [_foto('evidencia_estado', a)
                 for a in ('frontal', 'trasera', 'lateral_izq', 'lateral_der')]
        fotos.append(_foto('foto_dato', 'tablero'))
        r = client.post('/flota/custodia/traspaso', headers=_auth(m.t_cond), json={
            'placa': m.placa, 'km': 1080, 'ubicacion': 'sede', 'custodio_tipo': 'sede',
            'custodio_sede_id': m.almacen.id, 'fotos_fin': fotos})
        assert r.status_code == 201, r.get_json()
        b = client.get('/flota/bandeja', headers=_auth(m.t_flota))
        assert b.status_code == 200, b.get_json()
        return b.get_json()

    def test_sin_senal_de_turno_de_la_ruta(self, client, db, mundo):
        b = self._dia_normal(client, db, mundo)
        senales = [s for s in b['senales']
                   if s['clase'] == 'turno_de_la_ruta' and s['placa'] == mundo.placa]
        assert not senales, [s['texto'] for s in senales]

    def test_sin_pendiente_de_turno_de_sede_sin_fotos(self, client, db, mundo):
        b = self._dia_normal(client, db, mundo)
        pend = [p for p in b['pendientes']
                if p['clase'] == 'turno_sin_fotos' and p['placa'] == mundo.placa
                and 'la sede' in p['texto']]
        assert not pend, [p['texto'] for p in pend]


# ═════════════════════════════════════════════════════════════════════════
# P3 · Un ángulo de foto desconocido es 500
# ═════════════════════════════════════════════════════════════════════════

class TestAnguloInvalidoNoEs500:
    """Reproducción (e2e): entrega del turno con `angulo: 'lateral_izquierda'` →
    `sqlite3.IntegrityError: CHECK constraint failed: ck_flota_angulo` en
    `flota/adaptadores/traspaso.py:301` → 500 «Error interno del servidor».
    La cola del conductor (`flotaColaEnviarUna`) trata todo 5xx como
    «reintentar»: un ítem así no sale nunca y bloquea los que vienen detrás."""

    @pytest.mark.xfail(strict=True, reason='P3 QA 2026-09-24: ángulo inválido → 500 (CHECK) '
                       'en vez de 400')
    def test_angulo_desconocido_es_400(self, client, db, mundo):
        from tests.flota._turno import dar_turno
        dar_turno(db, mundo.u_cond.id, mundo.placa, km=1000)
        r = client.post('/flota/custodia/traspaso', headers=_auth(mundo.t_cond), json={
            'placa': mundo.placa, 'km': 1010, 'ubicacion': 'sede', 'custodio_tipo': 'sede',
            'custodio_sede_id': mundo.almacen.id,
            'fotos_fin': [_foto('evidencia_estado', 'lateral_izquierda')]})
        assert r.status_code == 400, (r.status_code, r.get_json())


# ═════════════════════════════════════════════════════════════════════════
# P3 · «No pagó y se quedó» guarda la forma de pago del select
# ═════════════════════════════════════════════════════════════════════════

class TestRechazoNoGuardaFormaDePago:
    """Reproducción (e2e): PD2005 (C02) confirmada RECHAZADO/NO_PAGO_SE_QUEDO con
    el select trayendo CREDITO → 200, `recaudo.forma_pago = 'CREDITO'`. La
    liquidación la pinta «$0 · CREDITO», `/liquidacion/desglose` la cuenta en
    `matriz` como «CREDITO | ENTREGADO_SIN_PAGO» y la lista en
    `paradas_credito` («Una parada marcada CRÉDITO nunca dispara recibo de
    caja») — sobre una factura de contado contraentrega."""

    @pytest.mark.xfail(strict=True, reason='P3 QA 2026-09-24: RECHAZADO/ENTREGADO_SIN_PAGO '
                       'conserva la forma de pago vieja del select')
    def test_no_pago_se_quedo_no_queda_como_credito(self, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        ruta, t = _ruta_con_parada(db, mundo, estado='EN_TRANSITO')
        RutaService.confirmar_parada(ruta.id, t.id, mundo.u_cond.id, {
            'version_formulario': 3, 'estado_entrega': 'RECHAZADO',
            'motivo_rechazo': 'NO_PAGO_SE_QUEDO', 'forma_pago': 'CREDITO',
            'observaciones': 'paga el viernes', 'monto_cobrado': 0,
            'foto_entrega': _DATA_URL, 'geo': {'fuente': 'sin_dato', 'motivo': 'sin_senal'}})
        rec = RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=t.id).one()
        assert rec.estado_entrega == 'ENTREGADO_SIN_PAGO'
        assert rec.forma_pago is None, rec.forma_pago


# ═════════════════════════════════════════════════════════════════════════
# P3 · Código crudo en la advertencia de flota del muelle
# ═════════════════════════════════════════════════════════════════════════

class TestAdvertenciaSinCodigoCrudo:
    """Reproducción (e2e): iniciar el cargue de R3 (CCC333, inspección con un
    bloqueante no apto) → 409 con «La inspección de hoy salió «no_apto»»
    (`senales_ruta.py:502`). El texto viaja tal cual al modal del muelle."""

    @pytest.mark.xfail(strict=True, reason='P3 QA 2026-09-24: «no_apto» crudo en el texto de '
                       'la advertencia')
    def test_el_texto_no_lleva_el_codigo(self, db, mundo, monkeypatch):
        from flota.adaptadores import inspecciones
        from app.services import senales_ruta
        ruta, _t = _ruta_con_parada(db, mundo)
        monkeypatch.setattr(inspecciones, 'del_dia',
                            lambda vid, dia: [SimpleNamespace(veredicto='no_apto')])
        adv = senales_ruta.advertencias_de_flota(ruta)
        textos = [a['texto'] for a in adv if a['clave'] == 'inspeccion_no_apta']
        assert textos, adv
        assert not any('_' in t for t in textos), textos
