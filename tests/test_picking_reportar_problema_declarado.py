"""«Reportar problema» registra lo que el picker DECLARA, nunca un 0 por omisión.

**El caso** (auditoría «operación diaria por rol», 2026-09-25): los cuatro
botones de `picking.js` mandaban `cantidad_encontrada: 0`, y las dos rutas
hacían `int(data.get('cantidad_encontrada', 0))`. Un picker con 3 de 5
escaneadas que reportaba «Agotado» quedaba como «encontró 0»: las 3 que tenía
en la mano no se descontaban del hueco, las 5 quedaban bloqueadas y la tarea
conservaba `cantidad_recogida = 3`.

**La clase**: *un número que declara una persona se rellena con 0 cuando no
viene*. Cero es un dato («no había nada»), no la ausencia de uno (Regla 0).

Tres capas:

1. El servidor valida con UNA función (`cantidad_encontrada_declarada`): sin
   cantidad → 400 (salvo UBICACION_VACIA, que la declara), fuera de rango → 400.
2. La pantalla la PIDE, ejecutada en Node con `util.js` real.
3. Trinquete de clase: ningún `.get('<…cant…>', 0)` nuevo en `app/routes`
   (AST), con inventario que solo encoge y meta-tests.
"""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
RUTAS = RAIZ / 'app' / 'routes'


# ═════════════════════════════════════════════════════════════════════════════
# 1 · El servidor
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tarea(db, almacen, producto, ub_picking, inv_picking, usuario):
    from app.services.picking_service import PickingService
    t = PickingService.crear_tareas(
        producto_id=producto.id, cantidad=5, almacen_id=almacen.id,
        referencia_documento='PD9001', tipo_documento='PEDIDO')[0]
    PickingService.iniciar_picking(t.id, usuario.id)
    return t


def _reportar(client, jwt_token, tarea_id, **cuerpo):
    return client.post('/api/mobile/reportar-problema',
                       json={'tarea_id': tarea_id, 'tipo': 'PICKING', **cuerpo},
                       headers={'Authorization': f'Bearer {jwt_token}'})


class TestElServidorNoInventaElCero:

    def test_faltante_sin_cantidad_se_rechaza_y_no_bloquea(self, client, jwt_token, tarea, db):
        from app.models.picking import TareaPicking
        r = _reportar(client, jwt_token, tarea.id, motivo='FALTANTE')
        assert r.status_code == 400, r.get_json()
        assert 'cantidad' in r.get_json()['error'].lower()
        assert db.session.get(TareaPicking, tarea.id).estado != 'BLOQUEADO'

    @pytest.mark.parametrize('motivo', ['MERCANCIA_AVERIADA', 'PRODUCTO_INCORRECTO'])
    def test_los_otros_motivos_tambien_piden_la_cantidad(self, client, jwt_token, tarea, motivo):
        assert _reportar(client, jwt_token, tarea.id, motivo=motivo).status_code == 400

    def test_el_faltante_declarado_se_registra_tal_cual(self, client, jwt_token, tarea, db):
        from app.models.picking import TareaPicking
        r = _reportar(client, jwt_token, tarea.id, motivo='FALTANTE', cantidad_encontrada=3)
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert (d['cantidad_encontrada'], d['cantidad_faltante']) == (3, 2)
        t = db.session.get(TareaPicking, tarea.id)
        assert (t.estado, t.cantidad_recogida) == ('BLOQUEADO', 3)

    def test_un_cero_escrito_es_un_dato_y_pisa_los_escaneos(self, client, jwt_token, tarea, db):
        """La tarea tenía 3 escaneadas; el picker declara 0. La recogida queda
        en 0 — antes conservaba las 3 sin haberlas descontado del hueco."""
        from app.models.picking import TareaPicking
        t = db.session.get(TareaPicking, tarea.id)
        t.cantidad_recogida = 3
        db.session.commit()
        r = _reportar(client, jwt_token, tarea.id, motivo='FALTANTE', cantidad_encontrada=0)
        assert r.status_code == 200, r.get_json()
        assert db.session.get(TareaPicking, tarea.id).cantidad_recogida == 0

    def test_ubicacion_vacia_declara_cero_por_si_misma(self, client, jwt_token, tarea):
        r = _reportar(client, jwt_token, tarea.id, motivo='UBICACION_VACIA')
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['cantidad_encontrada'] == 0

    def test_ubicacion_vacia_con_unidades_se_contradice(self, client, jwt_token, tarea):
        r = _reportar(client, jwt_token, tarea.id, motivo='UBICACION_VACIA', cantidad_encontrada=2)
        assert r.status_code == 400

    @pytest.mark.parametrize('valor', [-1, 6, 'tres', True, 2.5, ''])
    def test_fuera_de_rango_o_ilegible(self, client, jwt_token, tarea, valor):
        r = _reportar(client, jwt_token, tarea.id, motivo='FALTANTE', cantidad_encontrada=valor)
        assert r.status_code == 400, (valor, r.get_json())

    def test_sin_motivo_no_se_asume_ubicacion_vacia(self, client, jwt_token, tarea):
        """Otra forma del mismo defecto: `data.get('motivo', 'UBICACION_VACIA')`
        convertía «no dijo por qué» en «no había nada»."""
        assert _reportar(client, jwt_token, tarea.id).status_code == 400

    def test_la_ruta_gemela_aplica_la_misma_politica(self, client, jwt_token, tarea):
        r = client.post(f'/api/picking/{tarea.id}/reportar-problema',
                        json={'motivo': 'FALTANTE'},
                        headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# 2 · La pantalla, ejecutada
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const G = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const traza = { posts: [], alertas: [], modales: [] };
const nodo = () => ({ style: {}, remove() {}, value: G.obs || '' });
const ctx = {
  console,
  document: { getElementById: () => nodo(), querySelector: () => null, querySelectorAll: () => [],
              addEventListener() {}, createElement: () => nodo(), body: { appendChild() {} } },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {} },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const a of ['util.js', 'picking.js']) vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf-8'), ctx);
Object.assign(ctx, {
  __G: G, __traza: traza,
});
vm.runInContext(`
  TAREA_ACTUAL = __G.tarea;
  post = async (url, body) => { __traza.posts.push({ url, body }); return { ok: true }; };
  alerta = (m, t) => __traza.alertas.push([m, t]);
  pedirTarea = () => {};
  _modalCantidad = async (titulo, msg, opts) => { __traza.modales.push({ titulo, opts }); return __G.respuesta; };
`, ctx);
await vm.runInContext(`confirmarProblema(41, __G.motivo)`, ctx);
process.stdout.write(JSON.stringify(traza));
"""


def _pantalla(tmp_path, motivo, tarea, respuesta=None):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'h.mjs'
    h.write_text(_ARNES, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'motivo': motivo, 'tarea': tarea, 'respuesta': respuesta}),
                 encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


class TestLaPantallaPideLaCantidad:

    def test_agotado_sin_escaneos_pregunta_y_manda_lo_declarado(self, tmp_path):
        t = _pantalla(tmp_path, 'FALTANTE', {'id': 41, 'tipo': 'PICKING', 'cantidad_escaneada': 0,
                                             'cantidad_requerida': 5}, respuesta=2)
        assert len(t['modales']) == 1, 'no preguntó cuántas encontró'
        assert t['modales'][0]['opts']['min'] == 0 and t['modales'][0]['opts']['max'] == 5
        assert t['posts'][0]['body']['cantidad_encontrada'] == 2

    def test_cancelar_no_manda_nada(self, tmp_path):
        t = _pantalla(tmp_path, 'FALTANTE', {'id': 41, 'cantidad_escaneada': 0,
                                             'cantidad_requerida': 5}, respuesta=None)
        assert t['posts'] == [], 'cancelar la pregunta mandó el reporte igual'

    def test_con_escaneos_propone_lo_escaneado(self, tmp_path):
        t = _pantalla(tmp_path, 'MERCANCIA_AVERIADA', {'id': 41, 'cantidad_escaneada': 3,
                                                       'cantidad_requerida': 5}, respuesta=3)
        assert t['modales'][0]['opts']['valorInicial'] == 3
        assert t['posts'][0]['body']['cantidad_encontrada'] == 3

    def test_ubicacion_vacia_sin_escaneos_no_pregunta(self, tmp_path):
        t = _pantalla(tmp_path, 'UBICACION_VACIA', {'id': 41, 'cantidad_escaneada': 0,
                                                    'cantidad_requerida': 5})
        assert t['modales'] == []
        assert t['posts'][0]['body']['cantidad_encontrada'] == 0

    def test_ubicacion_vacia_con_escaneos_no_se_manda(self, tmp_path):
        t = _pantalla(tmp_path, 'UBICACION_VACIA', {'id': 41, 'cantidad_escaneada': 2,
                                                    'cantidad_requerida': 5})
        assert t['posts'] == []
        assert t['alertas'] and t['alertas'][0][1] == 'error'

    def test_ningun_boton_lleva_la_cantidad_en_el_onclick(self):
        fuente = (PWA / 'picking.js').read_text(encoding='utf-8')
        assert "'FALTANTE',0)" not in fuente and "'UBICACION_VACIA',0)" not in fuente


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Trinquete de clase — un `.get('<cant…>', 0)` en la frontera
# ═════════════════════════════════════════════════════════════════════════════

#: Los que quedan, con su porqué. **Solo encoge.** Una entrada nueva es una
#: decisión: ¿de verdad «no vino» significa cero en ese campo?
CEROS_POR_OMISION_DECLARADOS = {
    ('bloqueo_recompra.py', 'cantidad_autorizada'):
        'autorización de recompra: 0 = «sin tope» en el servicio; no mueve inventario',
    ('mobile.py', 'cantidad_recogida'):
        '`/faltante-info` es solo un aviso al admin DESPUÉS de confirmar; la '
        'cantidad real ya la escribió `/confirmar`',
    ('mobile.py', 'cantidad_solicitada'):
        'mismo aviso informativo de `/faltante-info`',
    ('picking.py', 'cantidad_hallada'):
        'auditoría del admin: el servicio solo la usa con resultado ENCONTRADO '
        'y la exige ahí — pendiente pasarla a la misma política',
    ('recepcion.py', 'cantidad_averiada'):
        'recepción: «no declaró averías» es 0 en el formulario de escaneo',
    # ('rutas.py', 'cantidad_devuelta') y ('rutas.py', 'cantidad_pedida') salieron
    # al integrar: vivían en `/liquidar-completo`, que el frente de liquidación
    # borró (2026-09-25).
    ('siesa.py', 'f421_cant_pedida'): 'fila de Siesa, no una declaración de persona',
    ('siesa.py', 'f421_cant_entrada'): 'fila de Siesa, no una declaración de persona',
    ('traslados.py', 'cantidad'): 'arma el payload de Siesa desde un ítem ya validado',
}


def ceros_por_omision(fuente: str):
    """`[(clave, línea)]` de cada `X.get('<…cant…>', 0)` del fuente, por AST.
    Un comentario o docstring no es código y no cuenta."""
    salida = []
    for n in ast.walk(ast.parse(fuente)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get' and len(n.args) == 2):
            k, d = n.args
            if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and 'cant' in k.value.lower()
                    and isinstance(d, ast.Constant) and not isinstance(d.value, bool)
                    and d.value in (0, 0.0, '0')):
                salida.append((k.value, n.lineno))
    return salida


def _encontrados():
    out = {}
    for f in sorted(RUTAS.rglob('*.py')):
        for clave, linea in ceros_por_omision(f.read_text(encoding='utf-8')):
            out.setdefault((f.name, clave), []).append(linea)
    return out


class TestTrinqueteCeroPorOmision:

    def test_no_crece(self):
        nuevos = set(_encontrados()) - set(CEROS_POR_OMISION_DECLARADOS)
        assert not nuevos, (
            f'Un número que declara una persona se rellena con 0 cuando no viene: '
            f'{sorted(nuevos)}. Cero es un dato, no la ausencia de uno (Regla 0). '
            f'Si de verdad «no vino» significa 0 ahí, declarálo con su porqué.')

    def test_solo_encoge(self):
        viejos = set(CEROS_POR_OMISION_DECLARADOS) - set(_encontrados())
        assert not viejos, f'Ya no existen, sacalos del inventario: {sorted(viejos)}'

    def test_cada_entrada_dice_por_que(self):
        for k, motivo in CEROS_POR_OMISION_DECLARADOS.items():
            assert isinstance(motivo, str) and len(motivo) > 20, k

    def test_el_caso_del_defecto_ya_no_esta(self):
        encontrados = _encontrados()
        assert ('mobile.py', 'cantidad_encontrada') not in encontrados
        assert ('picking.py', 'cantidad_encontrada') not in encontrados

    # ── meta: el detector muerde, no marca lo sano, y no se apaga callado ──

    def test_detecta_las_dos_escrituras(self):
        src = ("x = int(data.get('cantidad_encontrada', 0))\n"
               "y = data.get('cant_real', 0.0)\n")
        assert sorted(c for c, _ in ceros_por_omision(src)) == ['cant_real', 'cantidad_encontrada']

    def test_no_marca_lo_sano(self):
        src = ('"""data.get(\'cantidad\', 0) en un docstring"""\n'
               "# data.get('cantidad', 0)\n"
               "a = data.get('cantidad')\n"
               "b = data.get('cantidad', None)\n"
               "c = data.get('motivo', 0)\n"
               "d = data.get('cantidad', False)\n")
        assert ceros_por_omision(src) == []

    def test_piso(self):
        assert sum(len(v) for v in _encontrados().values()) >= 8, (
            'el escáner devolvió casi nada: ¿se rompió?')
