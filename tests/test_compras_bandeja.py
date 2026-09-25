"""
Compras — la bandeja del comprador (2026-09-25).

Los mundos se arman con kardex + el reconstructor REAL, `stock_siesa`, el
espejo de OCs y los servicios reales; las respuestas esperadas se calculan
aparte con la aritmética del motor escrita a mano.

Casos: urgente · esta semana · próximas · ya pedido cubre · bloqueado · sin
kardex · sin costo · empaque/MOQ · proveedor por OC · sin existencias conocidas.
Más: permisos por rol, las pestañas que no revientan en vacío, el contenedor
que no finge una propuesta, la temporada con su fecha límite, y el trinquete
«la bandeja no calcula» (AST) con sus meta-tests.
"""
import ast
import math
import pathlib
from datetime import date, timedelta

import pytest

from app.utils.fecha import dia_operativo

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SERVICIO = RAIZ / 'app' / 'services' / 'compras_bandeja.py'


def _hoy():
    return dia_operativo()


# ─────────────────────────────────────────────────────────────────────────────
# El mundo
# ─────────────────────────────────────────────────────────────────────────────

def _producto(db, ref, precio=None, origen=None):
    from app.models.producto import Producto
    p = Producto(codigo=ref, nombre=f'Producto {ref}', codigo_siesa=ref, origen=origen,
                 precio_compra=precio or 0, activo=True)
    db.session.add(p)
    db.session.flush()
    return p


def _diaria(db, ref, q=10, dias=359):
    from app.services.kardex_service import KardexMovimiento
    for d in range(1, dias + 1):
        db.session.add(KardexMovimiento(
            fecha=_hoy() - timedelta(days=d), tipo_docto='X', bodega='NB1',
            referencia=ref, concepto=501, naturaleza=2, cantidad=q, costo_promedio=0))


def _stock(db, ref, existencia, comprometido=0):
    from app.models.stock_siesa import StockSiesa
    db.session.add(StockSiesa(bodega='NB1', codigo_siesa=ref, existencia=existencia,
                              comprometido=comprometido, salida_sin_conf=0))


_ROWID = [1000]


def _oc(db, ref, *, abierta, proveedor='P1', factor=1, pedida_base=60, entrada_base=None,
        pendiente=None, precio=None, fecha_entrega=None, consec=1, dias_atras=40):
    from app.models.compras_fuentes import OcLineaSiesa
    _ROWID[0] += 1
    db.session.add(OcLineaSiesa(
        rowid_linea=_ROWID[0], co='003', tipo_docto='OC', consec_docto=consec,
        fecha_oc=_hoy() - timedelta(days=dias_atras), estado_oc=1 if abierta else 3,
        proveedor_codigo=proveedor, proveedor_nombre=f'PROVEEDOR {proveedor}',
        proveedor_nit=f'900{proveedor}', referencia=ref, bodega='NB1', moneda='COP',
        unidad_medida='CJ' if factor > 1 else 'UND', factor=factor,
        cant_pedida=pedida_base / factor, cant_pedida_base=pedida_base,
        cant_entrada_base=entrada_base if entrada_base is not None else (0 if abierta else pedida_base),
        pendiente_base=pendiente if pendiente is not None else (pedida_base if abierta else 0),
        precio_unitario=precio, fecha_entrega=fecha_entrega, abierta=abierta))


def _mundo(db):
    """Siete SKU nacionales que venden 10 u/día (359 de los últimos 360 días:
    d ≈ 9,97, σ_d chica). Con LT 5 ± 2 (default), z(95%) = 1,645 y ciclo 7:
      punto de pedido ≈ 50 + 1,645 × 20 ≈ 83 · nivel objetivo ≈ 120 + 33 ≈ 152
    Las respuestas exactas salen de `_rop_a_mano` / `_objetivo_a_mano`.
    """
    for ref in ('URG', 'SEM', 'PROX', 'PEDIDO', 'BLOQ', 'SINCOSTO', 'EMP'):
        _producto(db, ref, precio=0 if ref == 'SINCOSTO' else 1500)
        _diaria(db, ref)
    _stock(db, 'URG', 30)          # 30 < 10 × 5: se agota antes de que llegue
    _stock(db, 'SEM', 70)          # bajo 83, sobre 50
    _stock(db, 'PROX', 150)        # sobre 83, cruza en (150 − 83)/10 = 6,7 días
    _stock(db, 'PEDIDO', 30)       # + 200 ya pedidos → 230: no hace falta
    _stock(db, 'BLOQ', 30)
    _stock(db, 'SINCOSTO', 70)
    _stock(db, 'EMP', 30)
    _oc(db, 'PEDIDO', abierta=True, pedida_base=200, proveedor='P2',
        fecha_entrega=_hoy() - timedelta(days=3))
    # Última compra de EMP: caja de 12 a $12.000 la caja → $1.000/u, proveedor P1.
    _oc(db, 'EMP', abierta=False, factor=12, pedida_base=60, precio=12000, proveedor='P1')
    from app.models.producto import Producto
    from app.models.producto_bloqueado import ProductoBloqueado
    p = Producto.query.filter_by(codigo_siesa='BLOQ').first()
    db.session.add(ProductoBloqueado(producto_id=p.id, motivo='VELOCITY_CERO', activo=True))
    db.session.commit()
    from app.services.kardex_service import KardexService
    KardexService.reconstruir_stock_diario()


def _z():
    from app.services.kardex_service import _norm_ppf
    return _norm_ppf(0.95)


def _demanda(ref):
    """d y σ_d del motor (la demanda descensurada tiene su propio test); lo
    que se verifica acá es lo que la bandeja hace con ellas."""
    from app.services.kardex_service import KardexService
    dem = KardexService.demanda_descensurada(ventana_meses=12, nivel='red')[ref]
    return dem['d_avg'], dem['sigma_d']


def _rop_a_mano(ref, lt=5, slt=2):
    d, sd = _demanda(ref)
    return d * lt + _z() * math.sqrt(lt * sd ** 2 + d ** 2 * slt ** 2)


def _objetivo_a_mano(ref, lt=5, slt=2, ciclo=7):
    d, sd = _demanda(ref)
    return d * (lt + ciclo) + _z() * math.sqrt((lt + ciclo) * sd ** 2 + d ** 2 * slt ** 2)


def _lineas(r):
    return {l['referencia']: l for p in r['proveedores'] for l in p['lineas']}


@pytest.fixture
def mundo(app, db, monkeypatch):
    monkeypatch.delenv('ROP_CICLO_NACIONAL_DIAS', raising=False)
    monkeypatch.delenv('ROP_LT_NACIONAL_DIAS', raising=False)
    monkeypatch.delenv('ROP_SIGMA_LT_NACIONAL', raising=False)
    _mundo(db)
    from app.services import compras_bandeja
    return compras_bandeja.bandeja()


# ─────────────────────────────────────────────────────────────────────────────
# Los casos
# ─────────────────────────────────────────────────────────────────────────────

class TestLaBandeja:

    def test_urgente_se_agota_antes_de_que_llegue(self, mundo):
        l = _lineas(mundo)['URG']
        assert l['urgencia'] == 'URGENTE'
        assert l['pedir_unidades'] == round(_objetivo_a_mano('URG')) - 30
        assert l['porque']['disponible'] == 30 and l['porque']['posicion'] == 30
        assert l['alcanza_dias'] < l['entrega_dias'] == 5

    def test_esta_semana_bajo_punto_de_pedido(self, mundo):
        l = _lineas(mundo)['SEM']
        assert l['urgencia'] == 'ESTA_SEMANA'
        assert l['porque']['punto_de_pedido'] == round(_rop_a_mano('SEM'))
        assert 70 < l['porque']['punto_de_pedido'] and 70 >= 5 * _demanda('SEM')[0]
        assert l['pedir_unidades'] == round(_objetivo_a_mano('SEM')) - 70

    def test_proximas_cruza_en_la_semana(self, mundo):
        l = _lineas(mundo)['PROX']
        assert l['urgencia'] == 'PROXIMAS'
        d, _sd = _demanda('PROX')
        assert l['dias_hasta_punto_de_pedido'] == round((150 - _rop_a_mano('PROX')) / d, 1)
        assert 0 < l['dias_hasta_punto_de_pedido'] <= 7

    def test_lo_ya_pedido_cubre_y_no_se_pide_otra_vez(self, mundo):
        assert 'PEDIDO' not in _lineas(mundo)

    def test_bloqueado_no_se_propone_y_se_declara(self, mundo):
        assert 'BLOQ' not in _lineas(mundo)
        assert [b['referencia'] for b in mundo['excluidos']['bloqueados']] == ['BLOQ']

    def test_sin_costo_no_vale_cero(self, mundo):
        l = _lineas(mundo)['SINCOSTO']
        assert l['valor_cop'] is None and l['precio']['unitario_cop'] is None
        assert l['precio']['fuente'] == 'sin precio conocido'
        grupo = next(p for p in mundo['proveedores'] if any(
            x['referencia'] == 'SINCOSTO' for x in p['lineas']))
        assert grupo['subtotal_es_cota_inferior'] is True
        assert mundo['resumen']['valor_es_cota_inferior'] is True

    def test_empaque_redondea_a_caja_de_la_ultima_oc(self, mundo):
        l = _lineas(mundo)['EMP']
        falta = round(_objetivo_a_mano('EMP')) - 30
        assert l['pedir_unidades'] % 12 == 0
        assert l['pedir_empaques'] == math.ceil(falta / 12)
        assert l['porque']['redondeo_empaque'] == l['pedir_unidades'] - falta
        assert l['empaque']['unidad'] == 'CJ'
        assert l['precio']['unitario_cop'] == 1000.0 and l['precio']['fuente'] == 'última orden de compra'
        assert l['valor_cop'] == l['pedir_unidades'] * 1000

    def test_proveedor_por_su_ultima_oc(self, mundo):
        p1 = next(p for p in mundo['proveedores'] if p['codigo'] == 'P1')
        assert [l['referencia'] for l in p1['lineas']] == ['EMP']
        assert p1['nit'] == '900P1' and p1['nombre'] == 'PROVEEDOR P1'
        sin = next(p for p in mundo['proveedores'] if p['codigo'] is None)
        assert sin['conocido'] is False
        assert mundo['resumen']['sin_proveedor'] == len(sin['lineas'])

    def test_urgentes_primero_dentro_del_proveedor(self, mundo):
        sin = next(p for p in mundo['proveedores'] if p['codigo'] is None)
        orden = [l['urgencia'] for l in sin['lineas']]
        assert orden == sorted(orden, key={'URGENTE': 0, 'ESTA_SEMANA': 1, 'PROXIMAS': 2}.get)

    def test_la_fecha_de_entrega_sugerida_es_hoy_mas_el_lead_time(self, mundo):
        l = _lineas(mundo)['URG']
        assert l['fecha_entrega_sugerida'] == (_hoy() + timedelta(days=5)).isoformat()

    def test_destino_es_el_cdi_con_su_co(self, mundo):
        assert mundo['destino'] == {'bodega': 'NB1', 'co': '003'}

    def test_sin_existencias_conocidas_no_se_propone(self, app, db, monkeypatch):
        monkeypatch.delenv('ROP_CICLO_NACIONAL_DIAS', raising=False)
        _producto(db, 'FANTASMA', precio=100)
        _diaria(db, 'FANTASMA')
        db.session.commit()
        from app.services.kardex_service import KardexService
        KardexService.reconstruir_stock_diario()
        from app.services import compras_bandeja
        r = compras_bandeja.bandeja()
        assert 'FANTASMA' not in _lineas(r)
        assert r['excluidos']['sin_existencias_conocidas'] == [{'referencia': 'FANTASMA'}]


class TestSinKardexNoInventa:

    def test_dice_que_falta_y_como_encenderlo(self, app, db):
        from app.services import compras_bandeja
        r = compras_bandeja.bandeja()
        assert r['estado'] == 'SIN_KARDEX' and r['proveedores'] == []
        textos = ' '.join(f"{f['titulo']} {f['que_hacer']}" for f in r['falta'])
        assert 'KARDEX_AUTO' in textos and 'vacío' in textos


class TestElCicloEsUnSupuestoDeclarado:

    def test_configurado_cambia_la_cantidad(self, app, db, monkeypatch):
        monkeypatch.setenv('ROP_CICLO_NACIONAL_DIAS', '14')
        _mundo(db)
        from app.services import compras_bandeja
        r = compras_bandeja.bandeja()
        assert r['ciclo']['dias'] == 14 and r['ciclo']['fuente'] == 'CONFIGURADO'
        assert _lineas(r)['URG']['pedir_unidades'] == round(_objetivo_a_mano('URG', ciclo=14)) - 30

    def test_ilegible_cae_al_default_y_lo_dice(self, monkeypatch):
        from app.services.armador_service import ciclo_pedido_nacional
        monkeypatch.setenv('ROP_CICLO_NACIONAL_DIAS', 'semanal')
        c = ciclo_pedido_nacional()
        assert c['dias'] == 7 and c['fuente'] == 'DEFAULT_SUPUESTO' and 'semanal' in c['nota']


# ─────────────────────────────────────────────────────────────────────────────
# Las otras pestañas
# ─────────────────────────────────────────────────────────────────────────────

class TestContenedorNoFingePropuesta:

    def test_sin_origen_lo_dice_en_grande(self, app, db):
        _producto(db, 'X1')
        db.session.commit()
        from app.services import compras_bandeja
        r = compras_bandeja.contenedor()
        assert r['estado'] == 'SIN_ORIGEN' and r['skus_sin_origen'] == 1
        assert 'barras' not in r and 'ventana_llegada' not in r and 'proveedores' not in r

    def test_sin_fichas_lo_dice(self, app, db):
        _producto(db, 'CH1', origen='CHINA')
        _diaria(db, 'CH1')
        _stock(db, 'CH1', 10)
        db.session.commit()
        from app.services.kardex_service import KardexService
        KardexService.reconstruir_stock_diario()
        from app.services import compras_bandeja
        r = compras_bandeja.contenedor()
        assert r['estado'] == 'SIN_FICHAS' and r['sin_ficha'] == 1
        assert 'barras' not in r

    def test_con_fichas_agrupa_por_proveedor_con_nombre(self, app, db):
        from app.models.importacion import FichaImportacion
        p = _producto(db, 'CH2', origen='CHINA')
        db.session.add(FichaImportacion(producto_id=p.id, unidades_por_caja=100,
                                        cbm_por_caja=0.1, peso_kg_por_caja=10, moq_cajas=1,
                                        proveedor_china='NINGBO X', costo_fob_usd=0.5,
                                        fuente='VERIFICADA'))
        _diaria(db, 'CH2')
        _stock(db, 'CH2', 10)
        db.session.commit()
        from app.services.kardex_service import KardexService
        KardexService.reconstruir_stock_diario()
        from app.services import compras_bandeja
        r = compras_bandeja.contenedor()
        assert r['estado'] == 'PROPUESTA'
        g = r['proveedores'][0]
        assert g['proveedor'] == 'NINGBO X'
        linea = g['lineas'][0]
        assert linea['nombre'] == 'Producto CH2' and linea['unidades'] == linea['cajas'] * 100
        assert g['fob_usd'] == round(linea['cajas'] * 100 * 0.5, 2)


class TestTemporadaFechaLimite:

    def test_la_temporada_que_viene_por_fecha(self):
        from app.services.temporada_service import proxima_temporada
        t = proxima_temporada(date(2026, 9, 25))
        assert t['etiqueta'] == '2026-27' and t['inicio'] == date(2026, 12, 1)
        assert t['en_curso'] is False
        t2 = proxima_temporada(date(2027, 1, 10))
        assert t2['etiqueta'] == '2026-27' and t2['en_curso'] is True
        assert proxima_temporada(date(2027, 3, 2))['etiqueta'] == '2027-28'

    def test_fecha_limite_es_inicio_menos_lead_time_y_sigma(self, app, db):
        from app.services.temporada_service import fechas_limite_pedido
        r = fechas_limite_pedido(date(2026, 9, 25))
        assert r['por_origen']['CHINA']['fecha_limite'] == (
            date(2026, 12, 1) - timedelta(days=105 + 15)).isoformat()
        assert r['por_origen']['NACIONAL']['fecha_limite'] == (
            date(2026, 12, 1) - timedelta(days=5 + 2)).isoformat()
        assert r['por_origen']['CHINA']['vencida'] is True

    def test_conciliacion_una_cifra(self):
        from app.services.temporada_service import conciliar_con_contenedor
        r = conciliar_con_contenedor([{'referencia': 'A', 'pedir': 900}, {'referencia': 'B', 'pedir': 5}],
                                     [{'referencia': 'A', 'unidades': 600}])
        assert r == {'A': {'cifra': 900, 'fuente': 'TEMPORADA', 'en_contenedor_unidades': 600,
                           'ajuste_contenedor_unidades': 300}}

    def test_temporada_vacia_no_revienta(self, app, db):
        from app.services import compras_bandeja
        r = compras_bandeja.temporada()
        assert r['estado'] == 'SIN_DATOS' and r['filas'] == []


class TestLoPedido:

    def test_atrasadas_primero_y_sin_500(self, app, db):
        _producto(db, 'OC1')
        _oc(db, 'OC1', abierta=True, pedida_base=40, precio=500,
            fecha_entrega=_hoy() - timedelta(days=4), proveedor='P9')
        db.session.commit()
        from app.services import compras_bandeja
        r = compras_bandeja.lo_pedido()
        assert r['errores'] == []
        prov = r['ocs']['proveedores'][0]
        assert prov['codigo'] == 'P9' and prov['atrasadas'] == 1
        oc = prov['ocs'][0]
        assert oc['atrasada'] is True and oc['dias_atraso'] == 4
        assert oc['lineas'][0]['nombre'] == 'Producto OC1'
        assert oc['valor_pendiente'] == 40 * 500
        assert r['deriva'] is not None

    def test_una_seccion_que_falla_se_declara(self, app, db, monkeypatch):
        from app.services import compras_fuentes, compras_bandeja

        def _revienta():
            raise RuntimeError('espejo roto')
        monkeypatch.setattr(compras_fuentes, 'ocs_abiertas', _revienta)
        r = compras_bandeja.lo_pedido()
        assert r['ocs'] is None and 'espejo roto' in r['errores'][0]


class TestConfianza:

    def test_sin_kardex_dice_no_decidir(self, app, db):
        from app.services import compras_bandeja
        r = compras_bandeja.confianza()
        k = next(x for x in r['renglones'] if x['clave'] == 'kardex')
        assert k['nivel'] == 'mal' and 'no decidir' in k['titulo']
        assert r['decidir'] is False
        o = next(x for x in r['renglones'] if x['clave'] == 'origen')
        assert o['nivel'] == 'mal'


# ─────────────────────────────────────────────────────────────────────────────
# Permisos por rol
# ─────────────────────────────────────────────────────────────────────────────

_URLS = ['/api/compras/bandeja', '/api/compras/bandeja/confianza',
         '/api/compras/bandeja/contenedor', '/api/compras/bandeja/temporada',
         '/api/compras/bandeja/lo-pedido', '/api/compras/bandeja/sku?referencia=X',
         '/api/kardex/temporada/pedido', '/api/kardex/temporada/juicios?temporada=2026-27']


def _cab(app, db, almacen, rol):
    from flask_jwt_extended import create_access_token
    from app.models.usuario import Usuario
    u = Usuario(email=f'{rol}@bandeja.test', nombre=rol, rol=rol, activo=True,
                almacen_id=almacen.id)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


class TestPermisos:

    @pytest.mark.parametrize('rol', ['compras', 'admin', 'jefe_almacen', 'gerente'])
    def test_compras_entra(self, app, db, client, almacen, rol):
        cab = _cab(app, db, almacen, rol)
        for url in _URLS:
            assert client.get(url, headers=cab).status_code == 200, url

    @pytest.mark.parametrize('rol', ['conductor', 'operario', 'supervisor', 'tienda'])
    def test_los_demas_no(self, app, db, client, almacen, rol):
        cab = _cab(app, db, almacen, rol)
        for url in _URLS:
            assert client.get(url, headers=cab).status_code == 403, url

    def test_escribir_juicios_sigue_en_admin_y_jefe(self, app, db, client, almacen):
        cab = _cab(app, db, almacen, 'compras')
        r = client.post('/api/kardex/temporada/juicios', headers=cab,
                        json={'referencia': 'X', 'cantidad_juicio': 3})
        assert r.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete: la bandeja NO calcula
# ─────────────────────────────────────────────────────────────────────────────

_ARITMETICA = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
               ast.MatMult)
_FUNCIONES_QUE_CALCULAN = {'sum', 'max', 'min', 'abs', 'round', 'pow', 'divmod'}
_MODULOS_QUE_CALCULAN = {'math', 'statistics', 'numpy', 'decimal'}
#: Las tablas crudas que tienen dueño: leerlas acá sería calcular por fuera.
_MODELOS_CRUDOS = {'StockSiesa', 'StockDiario', 'KardexMovimiento', 'OcLineaSiesa',
                   'ItemEnTransito', 'AcuerdoMarco', 'PrecioProveedor',
                   'FichaImportacion', 'ProductoBloqueado', 'Producto', 'Contenedor',
                   'RecepcionMercancia'}


def escanear_calculos(src: str) -> list:
    """[(linea, qué)] — toda forma de calcular en un módulo que solo compone.

    Ve: `a − b`, `x −= n`, `max(0, a − b)`, `-x`, `sum(…)`, `round(…)`,
    `math.ceil(…)`, `import math`, `from x import StockSiesa`, `.query`,
    `db.session`, `func.sum`. No ve: comparaciones, `len`, f-strings, texto
    (docstrings y comentarios no son nodos de operación)."""
    hallados = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.BinOp) and isinstance(n.op, _ARITMETICA):
            hallados.append((n.lineno, f'operador {type(n.op).__name__}'))
        elif isinstance(n, ast.AugAssign) and isinstance(n.op, _ARITMETICA):
            hallados.append((n.lineno, f'asignación {type(n.op).__name__}='))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            hallados.append((n.lineno, 'signo'))
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in _FUNCIONES_QUE_CALCULAN:
                hallados.append((n.lineno, f'{f.id}()'))
            if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id in _MODULOS_QUE_CALCULAN | {'func'}):
                hallados.append((n.lineno, f'{f.value.id}.{f.attr}()'))
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split('.')[0] in _MODULOS_QUE_CALCULAN:
                    hallados.append((n.lineno, f'import {a.name}'))
        elif isinstance(n, ast.ImportFrom):
            if (n.module or '').split('.')[0] in _MODULOS_QUE_CALCULAN:
                hallados.append((n.lineno, f'from {n.module}'))
            for a in n.names:
                if a.name in _MODELOS_CRUDOS:
                    hallados.append((n.lineno, f'lee la tabla {a.name}'))
        elif isinstance(n, ast.Attribute):
            if n.attr == 'query' or (n.attr == 'session' and isinstance(n.value, ast.Name)
                                     and n.value.id == 'db'):
                hallados.append((n.lineno, f'consulta .{n.attr}'))
    return hallados


class TestLaBandejaNoCalcula:
    """Una fórmula nueva va en su dueño (armador, compras_fuentes, costo,
    temporada), con su test. La bandeja lee. Inventario de excepciones: vacío,
    y así se queda."""

    def test_cero_calculos(self):
        hallados = escanear_calculos(SERVICIO.read_text(encoding='utf-8'))
        assert hallados == [], (
            'compras_bandeja calcula:\n' + '\n'.join(f'  · línea {l}: {q}' for l, q in hallados)
            + '\n\nLlevá la cuenta a su dueño (ver el docstring del módulo) y leéla acá.')

    def test_piso_el_escaner_recorre_el_modulo(self):
        arbol = ast.parse(SERVICIO.read_text(encoding='utf-8'))
        funciones = [n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)]
        assert len(funciones) >= 8
        assert sum(1 for _ in ast.walk(arbol)) >= 1500
        # Y compone de verdad: nombra a sus dueños.
        src = SERVICIO.read_text(encoding='utf-8')
        for dueno in ('rop_dual', 'pedido_en_empaques', 'empaque_de_compra', 'proveedor_habitual',
                      'resolver_costos', 'valorizar', 'sumar_valores', 'verificar_oc',
                      'salud_kardex', 'fuente_stock', 'armar_contenedor',
                      'preparar_pedido_temporada', 'fechas_limite_pedido', 'detectar_deriva'):
            assert dueno in src, dueno

    @pytest.mark.parametrize('src', [
        'def f(a, b):\n    return a - b\n',
        'def f(x, n):\n    x -= n\n    return x\n',
        'def f(a, b):\n    return max(0, a - b)\n',
        'def f(x):\n    return -x\n',
        'def f(xs):\n    return sum(xs)\n',
        'def f(x):\n    return round(x)\n',
        'import math\ndef f(x):\n    return math.ceil(x)\n',
        'def f(u, c):\n    return u * c\n',
        'def f(a):\n    return a / 12\n',
        'from app.models.stock_siesa import StockSiesa\n',
        'def f():\n    return Producto.query.all()\n',
        'def f():\n    return db.session.query(1)\n',
        'def f():\n    return func.sum(1)\n',
        "def f(a, b):\n    return f'{a}' + b\n",
    ])
    def test_el_escaner_ve_la_forma(self, src):
        assert escanear_calculos(src), src

    @pytest.mark.parametrize('src', [
        'def f(a, b):\n    """a - b, max(0, x) y sum(xs) en el docstring."""\n    return a < b\n',
        'def f(xs):\n    # total = sum(xs) - 1\n    return len(xs)\n',
        "def f(a):\n    return f'hay {a} − vendido'\n",
        'def f(xs):\n    return sorted(xs, key=lambda x: (x[0], x[1]), reverse=True)\n',
        "def f(d):\n    return d.get('x') is not None and d['y'] <= 7\n",
    ])
    def test_no_marca_lo_sano(self, src):
        assert escanear_calculos(src) == [], src


class TestLoQueSoloAdministracionPuedeHacer:
    """E2E 2026-09-25: la bandeja le pedía al rol compras encender variables de
    Railway. A quien no es admin se le dice «pídale a administración…»."""

    def test_quien_no_es_admin_no_lee_variables(self):
        from app.services.compras_bandeja import para_quien_mira
        r = {'falta': [{'titulo': 'x', 'que_hacer': 'En Railway: KARDEX_AUTO=true',
                        'que_hacer_otros': 'Pídale a administración que la encienda.'}],
             'confianza': {'renglones': [{'que_hacer': 'Cargar fichas.'}]}}
        otros = para_quien_mira(r, es_admin=False)
        assert otros['falta'][0]['que_hacer'].startswith('Pídale a administración')
        assert 'que_hacer_otros' not in otros['falta'][0]
        assert otros['confianza']['renglones'][0]['que_hacer'] == 'Cargar fichas.'
        admin = para_quien_mira(r, es_admin=True)
        assert 'KARDEX_AUTO' in admin['falta'][0]['que_hacer']

    def test_toda_instruccion_de_variables_trae_su_version_para_otros(self):
        """Por AST: en compras_bandeja, un `que_hacer` que nombra una variable
        del servidor (MAYÚSCULAS_CON_GUIONES) va con su `que_hacer_otros`."""
        import ast
        import re
        from pathlib import Path
        f = Path(__file__).resolve().parents[1] / 'app' / 'services' / 'compras_bandeja.py'
        variable = re.compile(r'\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b')
        faltan = []
        for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
            if not isinstance(n, ast.Dict):
                continue
            claves = [k.value for k in n.keys if isinstance(k, ast.Constant)]
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == 'que_hacer':
                    textos = [c.value for c in ast.walk(v) if isinstance(c, ast.Constant)
                              and isinstance(c.value, str)]
                    if any(variable.search(t) for t in textos) and 'que_hacer_otros' not in claves:
                        faltan.append(n.lineno)
        assert not faltan, f'instrucciones de administración sin versión para otros: líneas {faltan}'

    def test_la_ruta_la_aplica_por_rol(self, app, client, db, almacen):
        from flask_jwt_extended import create_access_token
        from app.models.usuario import Usuario
        u = Usuario(nombre='Comp', email='comp-rol@t.co', rol='compras', password_hash='x',
                    almacen_id=almacen.id, activo=True)
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            tok = create_access_token(identity=str(u.id))
        d = client.get('/api/compras/bandeja/confianza', headers={'Authorization': f'Bearer {tok}'}).get_json()
        texto = str(d)
        assert 'que_hacer_otros' not in texto
        assert 'KARDEX_AUTO' not in texto and 'COMPRAS_OC_SYNC' not in texto
