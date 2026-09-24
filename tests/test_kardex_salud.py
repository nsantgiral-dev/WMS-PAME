"""
¿Funciona el kardex? — lo que el dueño no podía saber mirando la pantalla.

La pregunta llegó así: «el kardex no sé si está funcionando». Y no había cómo
contestarla desde la aplicación. Cuatro defectos, medidos el 2026-09-24 contra
Siesa QA (solo lectura) y contra el código:

1. **La prueba de completitud por conteo nunca corrió.** El endpoint dinámico
   declara `total_registros` / `total_páginas` (con tilde y eñe, medido en
   vivo); la descarga buscaba seis nombres adivinados y ninguno era el real.
   `total_declarado_por_siesa` salía `None` siempre, y el test que la cuidaba
   solo pedía que la clave existiera.
2. **El diagnóstico de orden comparaba posiciones.** Dentro de una página las
   filas no vienen ordenadas (medido: 4902, 4906, 4910… y en el pedido
   siguiente 4901, 4905…), así que «0 de 100 en la misma posición» salía aunque
   la página trajera las mismas filas. Y metía `LineaRegistro` —la posición,
   no la fila— en la identidad.
3. **Un registro abierto por un proceso muerto bloqueaba todo para siempre.**
   `/descargar` decía «ya en curso» y `/reconstruir` devolvía 409 sin fin.
4. **Nadie podía ver si el kardex estaba al día**, y la pantalla Modelos decía
   «✓ Kardex completo» sobre un kardex VACÍO (cero conceptos sin clasificar es
   lo que el vacío cumple por construcción). Y el resultado de «Reconstruir»
   leía `d.dias` / `d.referencias`, claves que el servidor nunca mandó.
"""
import ast
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.conftest import hoy_operativo as _hoy

_RAIZ = Path(__file__).resolve().parents[1]
_PWA = _RAIZ / 'app' / 'static' / 'pwa'


def _fila(ref, lr, consec, dia='2026-09-01', bodega='NB1', cant=1.0,
          concepto=501, nat='Salida', nro=1):
    """Una fila con la forma que devuelve la consulta dinámica."""
    return {
        'LineaRegistro': lr, 'f120_referencia': ref, 'f150_id': bodega,
        'f350_fecha': f'{dia}T00:00:00', 'f350_id_tipo_docto': 'FEW',
        'f350_consec_docto': str(consec), 'f470_nro_registro': nro,
        'f470_id_concepto': concepto, 'f470_ind_naturaleza': nat,
        'f470_cant_base': cant, 'f470_costo_prom_uni': 100.0,
    }


def _sobre(filas, pagina, total_paginas, total_registros, tam=100):
    """El sobre REAL del endpoint dinámico, medido el 2026-09-24."""
    return {'codigo': 0, 'mensaje': 'Transacción Exitosa', 'detalle': {
        'tamaño_página': tam, 'página_actual': pagina,
        'total_páginas': total_paginas, 'total_registros': total_registros,
        'Datos': filas}}


@pytest.fixture
def hoy_operativo():
    return _hoy()


@pytest.fixture
def siesa_falso(monkeypatch):
    """Reemplaza `_get` en la CLASE (no en la instancia: ver CLAUDE.md, el
    sombreado de instancia sobrevive al teardown) y registra lo pedido."""
    from app.services.connekta_gateway import ConnektaGateway
    monkeypatch.setenv('KARDEX_PAGE_DELAY_S', '0')
    estado = {'paginas': {}, 'pedidas': []}

    def _get(self, nombre, params_extra=None, timeout=30, url=None):
        m = re.search(r'numPag=(\d+)', (params_extra or {}).get('paginacion', ''))
        n = int(m.group(1)) if m else 1
        estado['pedidas'].append(n)
        resp = estado['paginas'].get(n)
        if callable(resp):
            return resp()
        return resp or {'codigo': 0, 'detalle': {'Datos': []}}

    monkeypatch.setattr(ConnektaGateway, '_get', _get)
    return estado


# ══════════════════════════════════════════════════════════════════════════
# 1. La prueba por conteo
# ══════════════════════════════════════════════════════════════════════════

class TestLosTotalesQueSiesaDeclara:

    def test_lee_el_sobre_real_con_tilde_y_enie(self):
        from app.services.kardex_service import totales_declarados
        assert totales_declarados(_sobre([], 1, 7121, 35604)) == (35604, 7121)

    def test_lo_que_no_viene_es_None_no_cero(self):
        from app.services.kardex_service import totales_declarados
        assert totales_declarados({'detalle': {'Datos': []}}) == (None, None)
        assert totales_declarados({'detalle': 'No autorizado'}) == (None, None)

    def test_la_descarga_reporta_lo_declarado(self, app, db, siesa_falso):
        """Antes: `None` siempre, con Siesa declarándolo en cada respuesta."""
        from app.services.kardex_service import KardexService
        filas = [_fila(f'R{i}', i, i) for i in range(1, 4)]
        siesa_falso['paginas'][1] = _sobre(filas, 1, 1, 3)
        r = KardexService.descargar_kardex('20260101', '20261231')
        assert r['total_declarado_por_siesa'] == 3
        assert r['paginas_declaradas_por_siesa'] == 1


class TestCompletaSoloSiElConteoCuadra:

    def test_enumeracion_limpia_es_COMPLETA(self, app, db, siesa_falso):
        from app.services.kardex_service import KardexService
        p1 = [_fila(f'R{i}', i, i) for i in range(1, 101)]
        p2 = [_fila(f'R{i}', i, i) for i in range(101, 151)]
        siesa_falso['paginas'][1] = _sobre(p1, 1, 2, 150)
        siesa_falso['paginas'][2] = _sobre(p2, 2, 2, 150)
        r = KardexService.descargar_kardex('20260101', '20261231')
        assert r['estado'] == 'COMPLETA', r['detalle_estado']
        assert r['ok'] is True
        assert r['faltan_por_conteo'] == 0

    def test_orden_no_determinista_NO_es_COMPLETA(self, app, db, siesa_falso):
        """Lo medido en QA: cada página trae sus 100 filas —la suma cuadra— pero
        la segunda repite filas de la primera y otras no llegan nunca."""
        from app.services.kardex_service import KardexService
        p1 = [_fila(f'R{i}', i, i) for i in range(1, 101)]
        p2 = [_fila(f'R{i}', i + 100, i) for i in range(51, 101)]   # repetidas
        siesa_falso['paginas'][1] = _sobre(p1, 1, 2, 150)
        siesa_falso['paginas'][2] = _sobre(p2, 2, 2, 150)
        r = KardexService.descargar_kardex('20260101', '20261231')
        assert r['filas_recibidas'] == 150
        assert r['movimientos_distintos_recibidos'] == 100
        assert r['estado'] == 'CONTEO_NO_CUADRA'
        assert r['ok'] is False
        assert r['faltan_por_conteo'] == 50
        assert r['reanudar_desde'] is not None

    def test_el_fin_lo_declara_siesa_no_una_pagina_corta(self, app, db, siesa_falso):
        """Una página corta en el medio terminaba la descarga como COMPLETA."""
        from app.services.kardex_service import KardexService
        siesa_falso['paginas'][1] = _sobre([_fila(f'A{i}', i, i) for i in range(1, 41)], 1, 3, 120, tam=40)
        siesa_falso['paginas'][2] = _sobre([_fila(f'B{i}', i, i) for i in range(1, 41)], 2, 3, 120, tam=40)
        siesa_falso['paginas'][3] = _sobre([_fila(f'C{i}', i, i) for i in range(1, 41)], 3, 3, 120, tam=40)
        r = KardexService.descargar_kardex('20260101', '20261231')
        assert siesa_falso['pedidas'] == [1, 2, 3]
        assert r['estado'] == 'COMPLETA'
        assert r['total_descargados'] == 120

    def test_una_pagina_vacia_antes_del_final_es_un_hueco(self, app, db, siesa_falso):
        from app.services.kardex_service import KardexService
        siesa_falso['paginas'][1] = _sobre([_fila(f'R{i}', i, i) for i in range(1, 101)], 1, 3, 250)
        siesa_falso['paginas'][2] = _sobre([], 2, 3, 250)
        r = KardexService.descargar_kardex('20260101', '20261231')
        assert r['estado'] == 'PAGINA_VACIA_ANTES_DEL_FINAL'
        assert r['ok'] is False

    def test_sin_totales_declarados_sigue_el_criterio_viejo(self, app, db, siesa_falso):
        """Una consulta que no declare nada no queda imposible de completar."""
        from app.services.kardex_service import KardexService
        siesa_falso['paginas'][1] = {'codigo': 0, 'detalle': {
            'Datos': [_fila(f'R{i}', i, i) for i in range(1, 11)]}}
        r = KardexService.descargar_kardex('20260101', '20261231')
        assert r['estado'] == 'COMPLETA'
        assert r['total_declarado_por_siesa'] is None

    def test_cuenta_las_filas_sin_clave_natural(self, app, db, siesa_falso):
        """Sin consecutivo ni línea, dos ventas iguales del mismo día colapsan."""
        from app.services.kardex_service import KardexService
        filas = [_fila('R1', 1, ''), _fila('R1', 2, '')]
        for f in filas:
            f['f470_nro_registro'] = None
        siesa_falso['paginas'][1] = _sobre(filas, 1, 1, 2)
        r = KardexService.descargar_kardex('20260101', '20261231')
        assert r['filas_sin_clave_natural'] == 2
        assert r['estado'] == 'CONTEO_NO_CUADRA', (
            'Siesa declara 2 y el kardex guarda 1: está incompleto')


# ══════════════════════════════════════════════════════════════════════════
# 2. El diagnóstico de orden compara conjuntos
# ══════════════════════════════════════════════════════════════════════════

class TestElDiagnosticoComparaConjuntosNoPosiciones:

    def _correr(self, monkeypatch, siesa_falso, paginas_en_orden):
        import time as _t
        monkeypatch.setattr(_t, 'sleep', lambda *_: None)
        cola = list(paginas_en_orden)
        siesa_falso['paginas'][50] = lambda: _sobre(cola.pop(0), 50, 99, 9900)
        siesa_falso['paginas'][51] = _sobre([_fila(f'Z{i}', 5000 + i, 9000 + i) for i in range(100)], 51, 99, 9900)
        from app.services.kardex_service import KardexService
        return KardexService.probar_estabilidad_paginacion(pagina=50, espera_s=1)

    def test_mismas_filas_en_otro_orden_es_estable(self, monkeypatch, siesa_falso):
        """Medido: dentro de una página las filas llegan intercaladas. El
        diagnóstico viejo daba «0/100 en la misma posición» → NO_DETERMINISTA."""
        base = [_fila(f'R{i}', 4900 + i, i) for i in range(1, 101)]
        r = self._correr(monkeypatch, siesa_falso, [base, base[::-1], base[1::2] + base[::2]])
        assert r['iguales_tras_5s'] == 100
        assert r['causa_probable'].startswith('NINGUNA'), r
        assert r['se_puede_paginar'] is True

    def test_LineaRegistro_no_es_parte_de_la_identidad(self, monkeypatch, siesa_falso):
        """Es la POSICIÓN: la misma fila con otro número sigue siendo la misma."""
        base = [_fila(f'R{i}', 4900 + i, i) for i in range(1, 101)]
        renumerada = [dict(f, LineaRegistro=5000 - f['LineaRegistro']) for f in base]
        r = self._correr(monkeypatch, siesa_falso, [base, renumerada, renumerada])
        assert r['iguales_tras_5s'] == 100

    def test_otras_filas_es_orden_no_determinista(self, monkeypatch, siesa_falso):
        a = [_fila(f'R{i}', 4900 + i, i) for i in range(1, 101)]
        b = [_fila(f'S{i}', 4900 + i, 500 + i) for i in range(1, 101)]
        r = self._correr(monkeypatch, siesa_falso, [a, b, b])
        assert r['causa_probable'] == 'ORDEN_NO_DETERMINISTA'
        assert r['se_puede_paginar'] is False


# ══════════════════════════════════════════════════════════════════════════
# 3. Un registro abierto por un proceso muerto no es una descarga en curso
# ══════════════════════════════════════════════════════════════════════════

def _abrir_registro(db, hace):
    from app.models.registro_sync import RegistroSync
    r = RegistroSync(tipo='kardex', inicio=datetime.utcnow() - hace)
    db.session.add(r)
    db.session.commit()
    return r


class TestLaDescargaMuertaNoBloqueaParaSiempre:

    def test_recien_abierta_esta_en_curso(self, app, db):
        from app.services.kardex_service import estado_descarga
        _abrir_registro(db, timedelta(minutes=3))
        e = estado_descarga()
        assert e['en_curso'] is True and e['interrumpida'] is False

    def test_abierta_mas_alla_del_techo_esta_interrumpida(self, app, db):
        from app.services.kardex_service import (
            KARDEX_TOPE_MINUTOS, MARGEN_INTERRUMPIDA_MIN, estado_descarga)
        _abrir_registro(db, timedelta(minutes=KARDEX_TOPE_MINUTOS + MARGEN_INTERRUMPIDA_MIN + 1))
        e = estado_descarga()
        assert e['en_curso'] is False
        assert e['interrumpida'] is True
        assert e['resultado']['ok'] is False
        assert e['resultado']['estado'] == 'INTERRUMPIDA'

    def test_ninguna_corrida_puede_pasar_el_techo(self):
        """Del techo depende poder declarar muerta una corrida."""
        from app.services.kardex_service import KARDEX_TOPE_MINUTOS, tope_minutos
        assert tope_minutos(10_000) == KARDEX_TOPE_MINUTOS
        assert tope_minutos(0) >= 1
        assert tope_minutos(None) == 25

    def test_descargar_arranca_sobre_una_interrumpida(self, app, db, client,
                                                      jwt_token_admin, monkeypatch):
        import threading
        arrancados = []
        monkeypatch.setattr(threading, 'Thread',
                            lambda target, daemon: type('T', (), {'start': lambda s: arrancados.append(1)})())
        _abrir_registro(db, timedelta(hours=5))
        r = client.post('/api/kardex/descargar', json={},
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        assert 'ya en curso' not in r.get_json()['mensaje']
        assert arrancados == [1]

    def test_descargar_no_arranca_dos_a_la_vez(self, app, db, client,
                                                jwt_token_admin, monkeypatch):
        import threading
        arrancados = []
        monkeypatch.setattr(threading, 'Thread',
                            lambda target, daemon: type('T', (), {'start': lambda s: arrancados.append(1)})())
        _abrir_registro(db, timedelta(minutes=2))
        r = client.post('/api/kardex/descargar', json={},
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert 'ya en curso' in r.get_json()['mensaje']
        assert arrancados == []

    def test_reconstruir_rechaza_por_calidad_no_por_espera(self, app, db, client, jwt_token_admin):
        """409 «esperá» sobre algo que no se arregla esperando escondía el override."""
        _abrir_registro(db, timedelta(hours=5))
        r = client.post('/api/kardex/reconstruir', json={},
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 409
        d = r.get_json()
        assert d.get('estado_descarga') == 'INTERRUMPIDA'
        assert 'override' in d

    def test_el_estado_la_declara(self, app, db, client, jwt_token_admin):
        _abrir_registro(db, timedelta(hours=5))
        d = client.get('/api/kardex/descargar/estado',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'}).get_json()
        assert d['en_curso'] is False and d['interrumpida'] is True
        assert d['resultado']['estado'] == 'INTERRUMPIDA'


# ══════════════════════════════════════════════════════════════════════════
# 4. La salud, servida
# ══════════════════════════════════════════════════════════════════════════

def _mov(db, ref='R1', fecha=None, concepto=501, bodega='NB1'):
    from app.services.kardex_service import KardexMovimiento
    db.session.add(KardexMovimiento(
        referencia=ref, bodega=bodega, fecha=fecha or date.today(),
        concepto=concepto, naturaleza=2, cantidad=1, tipo_docto='FEW'))
    db.session.commit()


def _descarga_completa(db):
    import json
    from app.models.registro_sync import RegistroSync
    db.session.add(RegistroSync(
        tipo='kardex', inicio=datetime.utcnow() - timedelta(minutes=30),
        fin=datetime.utcnow(), ok=True,
        resultado=json.dumps({'ok': True, 'estado': 'COMPLETA', 'total_descargados': 1})))
    db.session.commit()


def _stock_diario(db, hasta, ref='R1'):
    from app.services.kardex_service import StockDiario
    db.session.add(StockDiario(referencia=ref, bodega='NB1', fecha=hasta,
                               stock_cierre=5, tuvo_stock=True))
    db.session.commit()


class TestLaSaludDelKardex:

    def test_vacio_NO_es_confiable(self, app, db):
        """El caso que la pantalla Modelos pintaba «✓ Kardex completo»."""
        from app.services.kardex_service import salud_kardex
        s = salud_kardex()
        assert s['veredicto'] == 'SIN_DATOS'
        assert s['confiable'] is False

    def test_al_dia_de_verdad(self, app, db, hoy_operativo):
        from app.services.kardex_service import salud_kardex
        _mov(db, fecha=hoy_operativo)
        _descarga_completa(db)
        _stock_diario(db, hoy_operativo)
        s = salud_kardex()
        assert s['problemas'] == []
        assert s['veredicto'] == 'AL_DIA' and s['confiable'] is True
        assert s['movimientos']['dias_desde_ultimo'] == 0

    def test_viejo_es_DESACTUALIZADO(self, app, db, hoy_operativo):
        from app.services.kardex_service import dias_frescura, salud_kardex
        viejo = hoy_operativo - timedelta(days=dias_frescura() + 1)
        _mov(db, fecha=viejo)
        _descarga_completa(db)
        _stock_diario(db, viejo)
        s = salud_kardex()
        assert s['veredicto'] == 'DESACTUALIZADO'
        assert s['confiable'] is False

    def test_justo_en_la_tolerancia_no_es_viejo(self, app, db, hoy_operativo):
        from app.services.kardex_service import dias_frescura, salud_kardex
        borde = hoy_operativo - timedelta(days=dias_frescura())
        _mov(db, fecha=borde)
        _descarga_completa(db)
        _stock_diario(db, borde)
        assert salud_kardex()['confiable'] is True

    def test_stock_diario_detras_del_kardex(self, app, db, hoy_operativo):
        from app.services.kardex_service import salud_kardex
        _mov(db, fecha=hoy_operativo)
        _descarga_completa(db)
        _stock_diario(db, hoy_operativo - timedelta(days=3))
        codigos = [p['codigo'] for p in salud_kardex()['problemas']]
        assert codigos == ['STOCK_DIARIO_ATRASADO']

    def test_movimientos_sin_descarga_registrada(self, app, db, hoy_operativo):
        """Regla 0: sin constancia de cómo terminó, no se afirma completo."""
        from app.services.kardex_service import salud_kardex
        _mov(db, fecha=hoy_operativo)
        _stock_diario(db, hoy_operativo)
        assert salud_kardex()['veredicto'] == 'SIN_DESCARGA_REGISTRADA'

    def test_ultima_descarga_parcial(self, app, db, hoy_operativo):
        import json
        from app.models.registro_sync import RegistroSync
        from app.services.kardex_service import salud_kardex
        _mov(db, fecha=hoy_operativo)
        _stock_diario(db, hoy_operativo)
        db.session.add(RegistroSync(
            tipo='kardex', inicio=datetime.utcnow(), fin=datetime.utcnow(), ok=True,
            resultado=json.dumps({'ok': False, 'estado': 'TIMEOUT_PARCIAL'})))
        db.session.commit()
        s = salud_kardex()
        assert s['veredicto'] == 'ULTIMA_DESCARGA_INCOMPLETA'
        assert 'TIMEOUT_PARCIAL' in s['problemas'][0]['titulo']

    def test_concepto_sin_clasificar(self, app, db, hoy_operativo):
        from app.services.kardex_service import salud_kardex
        _mov(db, fecha=hoy_operativo, concepto=999)
        _descarga_completa(db)
        _stock_diario(db, hoy_operativo)
        assert salud_kardex()['veredicto'] == 'CONCEPTOS_SIN_CLASIFICAR'

    def test_descarga_interrumpida(self, app, db, hoy_operativo):
        from app.services.kardex_service import salud_kardex
        _mov(db, fecha=hoy_operativo)
        _stock_diario(db, hoy_operativo)
        _abrir_registro(db, timedelta(hours=5))
        assert salud_kardex()['veredicto'] == 'DESCARGA_INTERRUMPIDA'

    def test_declara_que_nada_lo_actualiza_solo(self, app, db):
        from app.services.kardex_service import salud_kardex
        assert salud_kardex()['actualizacion_automatica'] is False

    def test_endpoint_para_gestion_y_no_para_operario(self, app, db, client,
                                                       jwt_token_admin, jwt_token):
        ok = client.get('/api/kardex/salud', headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert ok.status_code == 200 and ok.get_json()['veredicto'] == 'SIN_DATOS'
        no = client.get('/api/kardex/salud', headers={'Authorization': f'Bearer {jwt_token}'})
        assert no.status_code == 403


class TestNadieDescargaElKardexSolo:
    """`actualizacion_automatica: False` es una afirmación sobre el código.

    Si alguien agenda la descarga, la salud tiene que dejar de decirlo. Por AST
    sobre `app/` y `flota/`: el único que llama `descargar_kardex` es la ruta.
    """

    def _llamadores(self):
        hallados = []
        for base in ('app', 'flota'):
            for py in (_RAIZ / base).rglob('*.py'):
                arbol = ast.parse(py.read_text(encoding='utf-8'))
                for n in ast.walk(arbol):
                    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr == 'descargar_kardex'):
                        hallados.append(str(py.relative_to(_RAIZ)))
        return hallados

    def test_solo_la_ruta_la_dispara(self):
        assert set(self._llamadores()) == {'app/routes/kardex.py'}, (
            'algo nuevo descarga el kardex: actualizar `actualizacion_automatica` '
            'y `nota_actualizacion` en salud_kardex')

    def test_el_escaner_ve_la_llamada_que_existe(self):
        """Piso: un escáner roto devolvería cero y el test de arriba fallaría
        por la razón equivocada — o pasaría con un conjunto vacío si alguien
        lo relaja."""
        assert len(self._llamadores()) >= 1


# ══════════════════════════════════════════════════════════════════════════
# 5. La pantalla lee lo que el servidor manda
# ══════════════════════════════════════════════════════════════════════════

def _sin_comentarios(js):
    """Quita los comentarios `//` y `/* */`: el escáner mide lo que el código
    LEE, y un comentario que explica el defecto viejo nombra la clave vieja."""
    js = re.sub(r'/\*.*?\*/', '', js, flags=re.S)
    return re.sub(r'(?m)^\s*//.*$|\s//[^\n`\'"]*$', '', js)


def _cuerpo(js, firma):
    i = js.index(firma)
    resto = js[i + len(firma):]
    cortes = [c for c in (resto.find('\nasync function '), resto.find('\nfunction '))
              if c != -1]
    return js[i:i + len(firma) + (min(cortes) if cortes else len(resto))]


class TestLaPantallaLeeElContratoReal:

    def test_reconstruir_lee_claves_que_el_servidor_manda(self, app, db, client,
                                                          jwt_token_admin, hoy_operativo):
        """Cada `d.X` de `kardexReconstruir` existe en alguna respuesta real
        del endpoint (200 forzado o 409). `d.dias` y `d.referencias` no
        existían en ninguna: el aviso salía sin números."""
        _mov(db, fecha=hoy_operativo)
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        rechazo = client.post('/api/kardex/reconstruir', json={}, headers=h).get_json()
        exito = client.post('/api/kardex/reconstruir', json={'forzar': True}, headers=h).get_json()
        claves = set(rechazo) | set(exito)
        js = (_PWA / 'kardex.js').read_text(encoding='utf-8')
        leidas = set(re.findall(r'\bd\.(\w+)',
                                _sin_comentarios(_cuerpo(js, 'async function kardexReconstruir('))))
        assert leidas, 'el escáner no encontró ninguna lectura: está roto'
        assert leidas <= claves, f'la pantalla lee claves que no existen: {leidas - claves}'

    def test_la_salud_se_pinta_con_claves_que_existen(self, app, db, client, jwt_token_admin):
        s = client.get('/api/kardex/salud',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'}).get_json()
        js = _sin_comentarios(_cuerpo((_PWA / 'kardex.js').read_text(encoding='utf-8'),
                                      'function _kardexSaludHtml('))
        objetos = {'s': s, 'm': s['movimientos'], 'sd': s['stock_diario'],
                   'ud': s['ultima_descarga']}
        for var, obj in objetos.items():
            leidas = set(re.findall(r'\b' + var + r'\.(\w+)', js)) - {'_error'}
            assert leidas, f'no se leyó nada de {var}: el escáner está roto'
            assert leidas <= set(obj), f'{var} lee claves inexistentes: {leidas - set(obj)}'

    def test_el_panel_pide_la_salud_al_servidor(self):
        js = _cuerpo((_PWA / 'kardex.js').read_text(encoding='utf-8'),
                     'async function kardexCargarPanel(')
        assert "/api/kardex/salud" in js
        assert '_kardexSaludHtml' in js

    def test_el_color_lo_decide_el_servidor(self):
        """El JS no recalcula el veredicto: pinta `confiable`."""
        js = _cuerpo((_PWA / 'kardex.js').read_text(encoding='utf-8'), 'function _kardexSaludHtml(')
        assert 's.confiable === true' in js
        assert 'dias_desde_ultimo >' not in js and 'umbral_dias >' not in js

    def test_el_semaforo_de_modelos_usa_la_salud_y_no_la_compuerta(self):
        """«✓ Kardex completo» salía sobre un kardex vacío."""
        js = _cuerpo((_PWA / 'compras_ia.js').read_text(encoding='utf-8'),
                     'async function modelosSemaforoKardex(')
        assert "get('/api/kardex/salud')" in js
        assert '/api/kardex/reconciliar' not in js
        assert 'd.confiable === true' in js
        sin_comentarios = re.sub(r'//[^\n]*', '', js)
        assert 'Kardex completo' not in sin_comentarios
