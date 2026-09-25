"""
Compras — la pantalla del comprador, pintada en Node con `util.js` REAL y
contra respuestas REALES del servidor (los mundos se arman con los servicios
de `tests/test_compras_bandeja.py`, y lo que viaja es lo que devuelve
`compras_bandeja`, serializado como lo serializa Flask).

Qué se exige de cada pestaña (Bandeja, franja, Contenedor con y sin datos,
Temporada, Lo pedido):
  · sin códigos crudos (`ESTA_SEMANA`, `SIN_COSTO`, `DEFAULT_CONSERVADOR`…);
  · sin «undefined», «NaN» ni «null» en el texto;
  · sin hex en estilos y nada por debajo de 12 px;
  · cada número con su contexto (unidades, días, $ o US$);
  · el dato escrito por una persona, escapado;
  · en los `onclick`, solo posiciones o palabras fijas;
  · la bandeja en tarjetas (sin `<table>`): se lee en el teléfono.
Y el borrador de OC, el enlace desde Analítica, y la lista paralela de
temporada solo editable por quien el servidor deja escribir.
"""
import json
import pathlib
import re
import shutil
import subprocess
from datetime import date, timedelta

import pytest

from tests.test_compras_bandeja import _diaria, _mundo, _oc, _producto, _stock
from tests.test_analitica_recorrido import mundo  # noqa: F401 (fixture)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
MALO = '<img src=x onerror=alert(1)>'

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0]; const r = JSON.parse(fs.readFileSync(args[1], 'utf8'));
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '', dataset: {}, open: false,
  scrollIntoView() {} });
const llamadas = [];
const ctx = { console, document: { getElementById: el, querySelector: (s) => r.nav || null },
  get: (url) => { llamadas.push(url); return Promise.resolve((r.respuestas || {})[url.split('?')[0]] || {}); },
  post: () => Promise.resolve({}), alerta: () => {}, OPERARIO: r.operario || null, setTimeout,
  navigator: { clipboard: { writeText: () => Promise.resolve() } } };
vm.createContext(ctx);
for (const f of ['util.js', 'compras_ia.js', 'temporada.js', 'compras_bandeja.js', 'analitica_portada.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx, { filename: f });
(async () => {
  const salida = {};
  for (const [nombre, expr] of Object.entries(r.pintar || {})) {
    salida[nombre] = await vm.runInContext(expr, ctx);
  }
  salida.els = Object.fromEntries(Object.entries(els).map(([k, v]) =>
    [k, { innerHTML: v.innerHTML, style: v.style, open: v.open }]));
  salida.llamadas = llamadas;
  console.log(JSON.stringify(salida));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _node(tmp_path, **r):
    if not shutil.which('node'):
        pytest.skip('sin node')
    f = tmp_path / 'entrada.json'
    f.write_text(json.dumps(r, default=str), encoding='utf-8')
    p = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), str(f)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _json(x):
    """Lo que viaja por HTTP: fechas como texto, como las serializa Flask."""
    return json.loads(json.dumps(x, default=str))


def _texto(html):
    t = re.sub(r'<[^>]+>', ' ', html)
    for a, b in (('&lt;', '<'), ('&gt;', '>'), ('&amp;', '&'), ('&#39;', "'"), ('&quot;', '"')):
        t = t.replace(a, b)
    return re.sub(r'\s+', ' ', t).strip()


_CODIGO_CRUDO = re.compile(r'\b[A-Z][A-Z0-9]+_[A-Z0-9_]+\b')


def _limpio(html, permitido=()):
    """Las reglas comunes de toda pestaña."""
    texto = _texto(html)
    for palabra in ('undefined', 'NaN', 'null', '[object Object]'):
        assert palabra not in texto, f'«{palabra}» en pantalla: …{texto[max(0, texto.find(palabra) - 80):][:160]}'
    crudos = [c for c in _CODIGO_CRUDO.findall(texto) if c not in permitido]
    assert not crudos, f'códigos crudos en pantalla: {crudos}'
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b', ' '.join(re.findall(r'style="([^"]*)"', html))), \
        'hex en un estilo'
    chicas = [int(x) for x in re.findall(r'font-size\s*:\s*(\d+)px', html) if int(x) < 12]
    assert not chicas, f'letra de {chicas} px'
    malos = [c for c in re.findall(r'onclick="([^"]*)"', html)
             if not re.fullmatch(r"(cmp\w+|comp\w+)\((\d+|'[A-Za-z_]*')?\)(;return false;)?", c)]
    assert not malos, f'onclick con datos: {malos}'
    return texto


def _bandeja(db, monkeypatch, malo=False):
    monkeypatch.delenv('ROP_CICLO_NACIONAL_DIAS', raising=False)
    _mundo(db)
    if malo:
        from app.models.producto import Producto
        p = Producto.query.filter_by(codigo_siesa='URG').first()
        p.nombre = MALO
        db.session.commit()
    from app.services import compras_bandeja
    return _json(compras_bandeja.bandeja())


# ─────────────────────────────────────────────────────────────────────────────
# 🛒 Bandeja
# ─────────────────────────────────────────────────────────────────────────────

class TestBandejaPintada:

    def test_con_datos(self, app, db, monkeypatch, tmp_path):
        d = _bandeja(db, monkeypatch, malo=True)
        out = _node(tmp_path, pintar={'html': f'cmpBandejaHtml({json.dumps(d)}, null)'})
        html = out['html']
        texto = _limpio(html)
        assert '<img' not in html and '&lt;img' in html, 'el nombre escrito por una persona se escapa'
        assert '<table' not in html, 'la bandeja va en tarjetas: se lee en el teléfono'
        # Cada número con su contexto.
        assert re.search(r'Pedir \d', texto) and 'Alcanza' in texto and 'llega en 5 días' in texto
        assert re.search(r'\$\d{1,3}(\.\d{3})* c/u', texto), 'precio con su signo y c/u'
        assert 'Sin precio conocido' in texto, 'el SKU sin costo lo dice, no pinta $0'
        assert 'Urgente' in texto and 'Esta semana' in texto and 'Próximas' in texto
        assert 'PROVEEDOR P1' in texto and 'NIT 900P1' in texto
        assert 'Sin proveedor conocido' in texto
        assert 'Bloqueados para recompra' in texto and 'BLOQ' in texto
        assert 'CJ de 12' in texto, 'el empaque de la última OC'
        assert 'al menos' in texto, 'el subtotal con líneas sin precio es una cota'
        assert 'Copiar OC' in html and 'Exportar CSV' in html

    def test_el_porque_pone_la_aritmetica_en_palabras(self, app, db, monkeypatch, tmp_path):
        d = _bandeja(db, monkeypatch)
        texto = _texto(_node(tmp_path, pintar={'h': f'cmpBandejaHtml({json.dumps(d)}, null)'})['h'])
        for frase in ('vendidos sin despachar', 'disponibles de verdad', 'Ya pedido',
                      'Punto de pedido', 'reserva de seguridad', 'Cantidad a tener',
                      'se piden', 'de más para completar el empaque', 'el 95 % de los días'):
            assert frase in texto, frase

    def test_filtrar_por_urgencia(self, app, db, monkeypatch, tmp_path):
        d = _bandeja(db, monkeypatch)
        html = _node(tmp_path, pintar={'h': f"cmpBandejaHtml({json.dumps(d)}, 'URGENTE')"})['h']
        tarjetas = re.findall(r'id="cmp-l-\d+-\d+"', html)
        urgentes = sum(1 for p in d['proveedores'] for l in p['lineas'] if l['urgencia'] == 'URGENTE')
        assert len(tarjetas) == urgentes > 0

    def test_sin_kardex_no_inventa(self, app, db, tmp_path):
        from app.services import compras_bandeja
        d = _json(compras_bandeja.bandeja())
        html = _node(tmp_path, pintar={'h': f'cmpBandejaHtml({json.dumps(d)}, null)'})['h']
        texto = _limpio(html, permitido=('KARDEX_AUTO', 'HEAVY_SCHEDULERS'))
        assert 'no puede proponer' in texto and 'no inventa' in texto
        assert 'Pedir' not in texto


class TestBorradorDeOc:

    def test_filas_y_csv(self, app, db, monkeypatch, tmp_path):
        d = _bandeja(db, monkeypatch)
        i = next(k for k, p in enumerate(d['proveedores']) if p['codigo'] == 'P1')
        out = _node(tmp_path, pintar={
            'oc': f'cmpOcFilas({json.dumps(d)}, {i})',
            'csv': f'(() => {{ const o = cmpOcFilas({json.dumps(d)}, {i}); return cmpCsv(o.encabezado, o.filas); }})()',
            'texto': f'cmpOcTexto(cmpOcFilas({json.dumps(d)}, {i}))'})
        fila = out['oc']['filas'][0]
        linea = d['proveedores'][i]['lineas'][0]
        assert fila[0] == '900P1' and fila[2] == '003' and fila[3] == 'NB1'
        assert fila[4] == 'EMP' and fila[6] == 'CJ'
        assert fila[7] == linea['pedir_empaques'] and fila[8] == linea['pedir_unidades']
        assert fila[9] == 1000.0 and fila[11] == linea['fecha_entrega_sugerida']
        lineas_csv = out['csv'].split('\n')
        assert lineas_csv[0].startswith('nit_proveedor;proveedor;co;bodega;referencia')
        assert 'por confirmar con el consultor' in out['texto']

    def test_el_csv_escapa_separador_y_comillas(self, tmp_path):
        out = _node(tmp_path, pintar={'c': """cmpCsv(['a','b'], [['x;y', 'di "hola"']])"""})
        assert out['c'].split('\n')[1] == '"x;y";"di ""hola"""'


# ─────────────────────────────────────────────────────────────────────────────
# Franja de confianza
# ─────────────────────────────────────────────────────────────────────────────

class TestFranja:

    def test_sin_datos_dice_no_decidir(self, app, db, tmp_path):
        from app.services import compras_bandeja
        d = _json(compras_bandeja.confianza())
        html = _node(tmp_path, pintar={'h': f'cmpFranjaHtml({json.dumps(d)})'})['h']
        texto = _limpio(html, permitido=('ROP_LT_NACIONAL_DIAS', 'ROP_CICLO_NACIONAL_DIAS',
                                         'COMPRAS_OC_SYNC', 'HEAVY_SCHEDULERS'))
        assert 'No decidir con estos números' in texto
        assert 'no se sabe qué se trae de China' in texto

    def test_con_datos_nombra_las_sedes(self, app, db, monkeypatch, tmp_path):
        _bandeja(db, monkeypatch)
        from app.services import compras_bandeja
        d = _json(compras_bandeja.confianza())
        texto = _texto(_node(tmp_path, pintar={'h': f'cmpFranjaHtml({json.dumps(d)})'})['h'])
        assert 'NB1' in texto and 'Existencias por sede' in texto


# ─────────────────────────────────────────────────────────────────────────────
# 🚢 Contenedor
# ─────────────────────────────────────────────────────────────────────────────

def _china(db, ficha=True, nombre='COLORES X12'):
    from app.models.importacion import FichaImportacion
    p = _producto(db, 'CHX', precio=5200, origen='CHINA')
    p.nombre = nombre
    if ficha:
        db.session.add(FichaImportacion(producto_id=p.id, unidades_por_caja=144, cbm_por_caja=0.06,
                                        peso_kg_por_caja=14, moq_cajas=2,
                                        proveedor_china='NINGBO QUIROND', costo_fob_usd=0.45,
                                        fuente='VERIFICADA'))
    _diaria(db, 'CHX', q=12)
    _stock(db, 'CHX', 100)
    db.session.commit()
    from app.services.kardex_service import KardexService
    KardexService.reconstruir_stock_diario()


class TestContenedorPintado:

    def test_sin_origen_no_pinta_propuesta(self, app, db, tmp_path):
        _producto(db, 'X1')
        db.session.commit()
        from app.services import compras_bandeja
        d = _json(compras_bandeja.contenedor())
        html = _node(tmp_path, pintar={'h': f'cmpContenedorHtml({json.dumps(d)})'})['h']
        texto = _limpio(html)
        assert '1 productos sin origen' in texto and 'Ir a 🧾 Fuentes' in texto
        assert 'm³' not in texto and 'Llegaría' not in texto, 'sin barras ni ETA: no hay propuesta'

    def test_sin_fichas_lo_dice(self, app, db, tmp_path):
        _china(db, ficha=False)
        from app.services import compras_bandeja
        d = _json(compras_bandeja.contenedor())
        texto = _limpio(_node(tmp_path, pintar={'h': f'cmpContenedorHtml({json.dumps(d)})'})['h'])
        assert 'no tienen ficha verificada' in texto and 'Llegaría' not in texto

    def test_con_propuesta(self, app, db, tmp_path):
        _china(db, nombre=MALO)
        from app.services import compras_bandeja
        d = _json(compras_bandeja.contenedor())
        assert d['estado'] == 'PROPUESTA'
        html = _node(tmp_path, pintar={'h': f'cmpContenedorHtml({json.dumps(d)})'})['h']
        texto = _limpio(html)
        assert '&lt;img' in html and '<img' not in html
        assert 'NINGBO QUIROND' in texto and 'US$ ' in texto
        assert re.search(r'US\$ \d+,\d{2}', texto), 'dólares con dos decimales y su signo'
        assert 'cajas de 144' in texto and 'Exportar packing list' in texto
        # Lo que tarda es el lead time de China, no su variación.
        lt = d['temporada']['por_origen']['CHINA']
        assert f"tarda unos {int(lt['lt_dias'])} días ± {int(lt['sigma_lt'])} días" in texto
        assert 'Llega con la temporada escolar ya empezada' in texto or d['alerta_temporada'] is None

    def test_packing_list(self, app, db, tmp_path):
        _china(db)
        from app.services import compras_bandeja
        d = _json(compras_bandeja.contenedor())
        filas = _node(tmp_path, pintar={'f': f'cmpPackingFilas({json.dumps(d)})'})['f']
        linea = d['proveedores'][0]['lineas'][0]
        assert filas[0][:5] == ['NINGBO QUIROND', 'CHX', 'COLORES X12', linea['cajas'], linea['unidades']]


# ─────────────────────────────────────────────────────────────────────────────
# 🎒 Temporada y 📦 Lo pedido
# ─────────────────────────────────────────────────────────────────────────────

def _temporada_mundo(db):
    from app.services.kardex_service import KardexMovimiento, KardexService
    from app.utils.fecha import dia_operativo
    hoy = dia_operativo()
    p = _producto(db, 'ESC', precio=4000)
    ultima = hoy.year - 1 if hoy.month >= 3 else hoy.year - 2
    for anio, q in ((ultima - 2, 18), (ultima - 1, 20), (ultima, 22)):
        for j in range(30):
            db.session.add(KardexMovimiento(fecha=date(anio, 12, 1) + timedelta(days=3 * j),
                                            tipo_docto='X', bodega='NB1', referencia='ESC',
                                            concepto=501, naturaleza=2, cantidad=q, costo_promedio=0))
    _stock(db, 'ESC', 300, comprometido=20)
    db.session.commit()
    KardexService.reconstruir_stock_diario()
    return p


class TestTemporadaPintada:

    def test_con_datos(self, app, db, tmp_path):
        _temporada_mundo(db)
        from app.services import compras_bandeja
        d = _json(compras_bandeja.temporada())
        assert d['estado'] == 'OK'
        html = _node(tmp_path, pintar={'h': f'cmpTemporadaHtml({json.dumps(d)})'})['h']
        texto = _limpio(html)
        for palabra in ('Tener', 'Hay', 'Viene', 'Pedir', 'Nacional', 'China', 'Fecha límite'):
            assert palabra in texto, palabra
        assert 'El export del 1 de agosto' not in texto and '7 de agosto' not in texto

    def test_vacia(self, app, db, tmp_path):
        from app.services import compras_bandeja
        d = _json(compras_bandeja.temporada())
        texto = _limpio(_node(tmp_path, pintar={'h': f'cmpTemporadaHtml({json.dumps(d)})'})['h'])
        assert 'Todavía no hay pedido de temporada' in texto

    @pytest.mark.parametrize('rol,edita', [('compras', False), ('jefe_almacen', True), ('admin', True)])
    def test_la_lista_paralela_la_escribe_quien_el_servidor_deja(self, app, db, tmp_path, rol, edita):
        _temporada_mundo(db)
        from app.services.temporada_service import TemporadaService
        d = _json(TemporadaService.preparar_pedido_temporada())
        out = _node(tmp_path, operario={'rol': rol},
                    pintar={'h': f"(() => {{ _TEMP_DATA = {json.dumps(d)}; _tempRender(document.getElementById('t'), _TEMP_DATA); return document.getElementById('t').innerHTML; }})()"})
        html = out['h']
        assert ('<input type="number"' in html) is edita
        assert ('la registra el comité' in html) is (not edita)
        assert "temporadaSetParalela('" not in html, 'el onchange lleva la posición, no la referencia'

    def test_los_roles_del_js_son_los_del_servidor(self):
        from app.routes._auth_helpers import Roles
        js = (PWA / 'temporada.js').read_text(encoding='utf-8')
        roles = re.search(r'TEMP_ROLES_REGISTRAN_JUICIO = \[([^\]]*)\]', js).group(1)
        assert sorted(re.findall(r"'(\w+)'", roles)) == sorted(Roles.ALMACEN)


class TestLoPedidoPintado:

    def test_con_datos(self, app, db, tmp_path):
        _producto(db, 'OC1')
        from app.models.producto import Producto
        Producto.query.filter_by(codigo_siesa='OC1').first().nombre = MALO
        from app.utils.fecha import dia_operativo
        _oc(db, 'OC1', abierta=True, pedida_base=40, precio=500,
            fecha_entrega=dia_operativo() - timedelta(days=4), proveedor='P9')
        db.session.commit()
        from app.services import compras_bandeja
        d = _json(compras_bandeja.lo_pedido())
        html = _node(tmp_path, pintar={'h': f'cmpLoPedidoHtml({json.dumps(d)})'})['h']
        texto = _limpio(html)
        assert '&lt;img' in html and '<img' not in html
        assert 'atrasada 4 días' in texto and 'falta $20.000' in texto
        assert 'PROVEEDOR P9' in texto and 'NIT 900P9' in texto
        assert 'respetan los acuerdos' in texto


# ─────────────────────────────────────────────────────────────────────────────
# Desde Analítica: «Venta perdida por agotados» → la fila de la Bandeja
# ─────────────────────────────────────────────────────────────────────────────

class TestEnlaceDesdeAnalitica:

    def test_abre_la_fila_de_la_referencia(self, app, db, monkeypatch, tmp_path):
        d = _bandeja(db, monkeypatch)
        pos = next((i, j) for i, p in enumerate(d['proveedores'])
                   for j, l in enumerate(p['lineas']) if l['referencia'] == 'URG')
        out = _node(tmp_path, respuestas={'/api/compras/bandeja': d},
                    pintar={'r': "(async () => { comprasIrABandeja('URG'); await cmpCargarBandeja(); return 1; })()"})
        fila = out['els'][f'cmp-l-{pos[0]}-{pos[1]}']
        assert fila['style'].get('boxShadow'), 'la fila queda marcada'
        assert out['els'][f'cmp-pq-{pos[0]}-{pos[1]}']['open'] is True, 'con su porqué abierto'

    def test_si_no_esta_dice_por_que(self, app, db, monkeypatch, tmp_path):
        d = _bandeja(db, monkeypatch)
        out = _node(tmp_path, respuestas={
            '/api/compras/bandeja': d,
            '/api/compras/bandeja/sku': {'referencia': 'ZZ', 'motivo': 'SOBRE_PUNTO_DE_PEDIDO',
                                         'dias_hasta_punto_de_pedido': 12.4,
                                         'texto': 'Hoy no está bajo su punto de pedido.'}},
            pintar={'r': "(async () => { comprasIrABandeja('ZZ'); await cmpCargarBandeja(); await new Promise(r => setTimeout(r, 10)); return 1; })()"})
        aviso = _texto(out['els']['cmp-aviso-ir']['innerHTML'])
        assert 'ZZ no está en la bandeja' in aviso and 'unos 12 días' in aviso

    def test_el_boton_solo_para_quien_ve_compras(self, tmp_path):
        vis = _node(tmp_path, nav={'style': {'display': ''}},
                    pintar={'b': "anPortBotonAccion({tipo: 'ver_bandeja', referencia: 'X'})"})['b']
        oculto = _node(tmp_path, nav={'style': {'display': 'none'}},
                       pintar={'b': "anPortBotonAccion({tipo: 'ver_bandeja', referencia: 'X'})"})['b']
        assert vis == 'Ver en la Bandeja de compras ›' and oculto == ''

    def test_la_portada_trae_los_productos_con_su_referencia(self, db, mundo):
        from tests.test_analitica_fugas import _agotado
        from app.services import analitica_portada
        from app.models.producto import Producto
        t = _agotado(db, mundo['almacen'], mundo['oper'], precio=12000, categoria='CUADERNOS')
        ref = db.session.get(Producto, t.producto_id).codigo_siesa
        assert ref, 'el mundo tiene referencia de Siesa'
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        m = analitica_portada.Medicion(hoy - timedelta(days=29), hoy, None)
        pq = analitica_portada._por_que_venta_perdida(m)
        acciones = [a for a in pq['que_hacer'] if a['tipo'] == 'ver_bandeja']
        assert acciones and acciones[0]['referencia'] == ref

