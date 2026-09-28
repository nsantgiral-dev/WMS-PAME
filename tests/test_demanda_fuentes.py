"""
De dónde sale la demanda de compras — la cascada de fuentes (2026-09-27).

La clase: *la demanda de compras depende de una sola fuente que no responde, y
cuando no responde nadie lo dice* (la bandeja no proponía nada con el kardex
en 401). Ahora `demanda_fuentes.fuente_de_demanda` elige UNA fuente, la
declara, y quien decide lee de ella. Estos tests:

- la lectura de la venta diaria que Siesa suma: completa, paginación
  inestable (una fila que llega dos veces con otro contenido), filas que faltan
  y se buscan otra vez, filas que no aparecen nunca (se guarda hasta el hueco),
  página que falla (se guarda lo completo), cero filas (no es «no hubo
  ventas»), anulada después (queda en cero, no se borra);
- la cascada: nada → NINGUNA; Siesa > kardex > facturas desde pedido; un
  kardex viejo (el de producción, enero de 2024) no tapa la de pedidos; un
  hueco corta la cobertura; atraso → no apta;
- la demanda que sale de Siesa es la misma cuenta que la del kardex (el neteo
  de devoluciones D6 incluido), y la bandeja propone sin kardex;
- las decisiones que exigen la caja de las tiendas (contenedor, bloqueo) no
  se toman con la fuente parcial;
- la pantalla (Node, `util.js` real);
- el trinquete de «una fuente de demanda», con meta-tests y piso.
"""
import ast
import json
import pathlib
import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal

import pytest

from app.utils.fecha import dia_operativo

RAIZ = pathlib.Path(__file__).resolve().parent.parent
PWA = RAIZ / 'app' / 'static' / 'pwa'
MALO = '<img src=x onerror=alert(1)>'


def _hoy():
    return dia_operativo()


# ─────────────────────────────────────────────────────────────────────────────
# Una Siesa falsa con la forma REAL del endpoint dinámico
# ─────────────────────────────────────────────────────────────────────────────

class SiesaVentasFalsa:
    """Responde la consulta de venta diaria con el sobre real (`Datos`,
    `total_registros`, `total_páginas`) y el SQL de `demanda_fuentes`: orden
    DESCENDENTE por fecha, `orden` y `total_filas` en cada fila."""
    url_get_dinamico = 'https://siesa.falsa/dinamico'

    def __init__(self, filas, desde, hasta, *, faltan_una_vez=(), faltan_siempre=(),
                 revienta_en=None, duplica_en=None, fila_ilegible_en=None):
        orden = sorted(filas, key=lambda f: (-f[0].toordinal(), f[1], f[2]))
        self.rows = [{
            'orden': i + 1, 'total_filas': len(orden),
            'ventana_desde': desde.isoformat() + 'T00:00:00',
            'ventana_hasta': hasta.isoformat() + 'T00:00:00',
            'fecha': f[0].isoformat() + 'T00:00:00', 'bodega': f[1], 'referencia': f[2],
            'vendido': f[3], 'devuelto': f[4], 'lineas': 1,
        } for i, f in enumerate(orden)]
        self.faltan_una_vez = set(faltan_una_vez)
        self.faltan_siempre = set(faltan_siempre)
        self.revienta_en = revienta_en
        self.duplica_en = duplica_en
        self.fila_ilegible_en = fila_ilegible_en
        self.pedidas = []

    def _get(self, nombre, params, url=None, timeout=None):
        assert url == self.url_get_dinamico, 'la consulta es DINÁMICA'
        pag = int(params['paginacion'].split('|')[0].split('=')[1])
        self.pedidas.append(pag)
        if self.revienta_en == pag:
            raise Exception('Connekta no respondió — reintente')
        trozo = [dict(r) for r in self.rows[(pag - 1) * 100:pag * 100]]
        if self.duplica_en == pag and trozo:
            falsa = dict(self.rows[0])
            falsa['orden'] = trozo[0]['orden']
            trozo[0] = falsa
        if self.fila_ilegible_en == pag and trozo:
            trozo[0]['fecha'] = 'no es fecha'
        primera_vez = self.pedidas.count(pag) == 1
        trozo = [r for r in trozo
                 if r['orden'] not in self.faltan_siempre
                 and not (primera_vez and r['orden'] in self.faltan_una_vez)]
        return {'codigo': 0, 'detalle': {'Datos': trozo, 'total_registros': len(self.rows),
                                         'total_páginas': (len(self.rows) + 99) // 100}}


def _filas_60_dias(refs=('A', 'B', 'C', 'D', 'E'), dias=60, hoy=None):
    """5 SKU × 2 bodegas que venden 1 u cada día de 60 (salvo el día 30, en que
    no se vende nada: tiene que quedar CUBIERTO igual). Hoy también vende: esa
    fila NO se guarda (el día no está cerrado)."""
    hoy = hoy or _hoy()
    filas = []
    for d in range(0, dias + 1):
        if d == 30:
            continue
        for ref in refs:
            for bod in ('NB1', 'NS1'):
                filas.append((hoy - timedelta(days=d), bod, ref, 1, 0))
    return filas


def _leer(gw, **kw):
    from app.services import demanda_fuentes as dfu
    return dfu.descargar_ventas_dia(gateway=gw, pausa_s=0, **kw)


def _cubiertos():
    from app.models.demanda_siesa import DemandaDiaCubierto
    return sorted(c.fecha for c in DemandaDiaCubierto.query.all())


def _filas_db():
    from app.models.demanda_siesa import DemandaDiaSiesa
    return {(f.fecha, f.bodega, f.referencia): f for f in DemandaDiaSiesa.query.all()}


# ═════════════════════════════════════════════════════════════════════════════
# La lectura
# ═════════════════════════════════════════════════════════════════════════════

class TestLaLecturaDeLaVentaDiaria:

    def test_completa_guarda_la_ventana_cerrada_incluidos_los_dias_sin_venta(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa(_filas_60_dias(), hoy - timedelta(days=60), hoy)
        r = _leer(gw)
        assert r['completa'] is True, r
        assert r['filas_declaradas'] == len(gw.rows) == r['filas_leidas']
        cub = _cubiertos()
        assert cub[0] == hoy - timedelta(days=60)
        assert cub[-1] == hoy - timedelta(days=1), 'hoy no está cerrado: no se cubre'
        assert hoy - timedelta(days=30) in cub, 'un día sin venta leído es un cero verdadero'
        filas = _filas_db()
        assert not any(k[0] == hoy for k in filas), 'la venta de hoy no se guarda'
        assert filas[(hoy - timedelta(days=1), 'NB1', 'A')].vendido == 1

    def test_fila_que_llega_dos_veces_con_otro_contenido_no_guarda_nada(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa(_filas_60_dias(), hoy - timedelta(days=60), hoy, duplica_en=2)
        r = _leer(gw)
        assert r['completa'] is False and 'no es estable' in r['motivo']
        assert _cubiertos() == [] and _filas_db() == {}

    def test_la_fila_que_falta_se_busca_otra_vez_en_su_pagina(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa(_filas_60_dias(), hoy - timedelta(days=60), hoy,
                              faltan_una_vez={150, 151})
        r = _leer(gw)
        assert r['completa'] is True, r
        assert gw.pedidas.count(2) == 2, 'la página 2 se pidió otra vez'

    def test_la_que_no_aparece_nunca_deja_guardado_solo_lo_anterior_al_hueco(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa(_filas_60_dias(), hoy - timedelta(days=60), hoy,
                              faltan_siempre={300})
        r = _leer(gw)
        assert r['completa'] is False and r['faltantes'] == 1
        dia_hueco = gw.rows[299]['fecha'][:10]
        cub = _cubiertos()
        assert cub, 'lo más reciente sí quedó completo'
        assert cub[0].isoformat() > dia_hueco, 'nada del día del hueco ni anterior'
        assert cub[-1] == hoy - timedelta(days=1)

    def test_una_pagina_que_falla_guarda_lo_completo_y_lo_dice(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa(_filas_60_dias(), hoy - timedelta(days=60), hoy, revienta_en=4)
        r = _leer(gw)
        assert r['completa'] is False and 'página 4' in r['motivo']
        cub = _cubiertos()
        assert cub and cub[-1] == hoy - timedelta(days=1)
        assert cub[0] > hoy - timedelta(days=60)

    def test_cero_filas_no_es_que_no_hubo_ventas(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa([], hoy - timedelta(days=60), hoy)
        r = _leer(gw)
        assert r['completa'] is False and 'no se sabe' in r['motivo']
        assert _cubiertos() == []

    def test_fila_ilegible_no_se_salta_en_silencio(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa(_filas_60_dias(), hoy - timedelta(days=60), hoy,
                              fila_ilegible_en=1)
        r = _leer(gw)
        assert r['completa'] is False and r['ilegibles'] >= 1
        assert _cubiertos() == []

    def test_la_venta_anulada_despues_queda_en_cero_sin_borrar(self, app, db):
        hoy = _hoy()
        filas = _filas_60_dias()
        _leer(SiesaVentasFalsa(filas, hoy - timedelta(days=60), hoy))
        ayer = hoy - timedelta(days=1)
        sin = [f for f in filas if not (f[0] == ayer and f[2] == 'A' and f[1] == 'NB1')]
        _leer(SiesaVentasFalsa(sin, hoy - timedelta(days=60), hoy))
        f = _filas_db()[(ayer, 'NB1', 'A')]
        assert f.vendido == 0, 'Siesa la dejó de reportar en un día leído completo'

    def test_la_consulta_va_por_el_endpoint_dinamico_de_a_100(self, app, db):
        hoy = _hoy()
        gw = SiesaVentasFalsa(_filas_60_dias(), hoy - timedelta(days=60), hoy)
        pedidos = []
        real = gw._get

        def _espia(nombre, params, url=None, timeout=None):
            pedidos.append((nombre, params))
            return real(nombre, params, url=url, timeout=timeout)
        gw._get = _espia
        _leer(gw)
        assert all('tamPag=100' in p['paginacion'] for _n, p in pedidos)
        assert all('parametros' not in p for _n, p in pedidos), \
            'medido en producción: una dinámica con parámetros da 400'
        assert pedidos[0][0] == 'papeleriamedellin_WMS_Ventas_Dia'


class TestDiasCompletos:
    """La función pura que decide qué días de una lectura parcial valen."""

    def _por_orden(self, fechas):
        hoy = _hoy()
        return {i + 1: {'fecha': f, 'ventana_desde': hoy - timedelta(days=90),
                        'ventana_hasta': hoy} for i, f in enumerate(fechas)}

    def test_si_la_siguiente_es_del_mismo_dia_ese_dia_no_esta_entero(self):
        from app.services.demanda_fuentes import dias_completos
        hoy = _hoy()
        d = [hoy - timedelta(days=1)] * 2 + [hoy - timedelta(days=5)] * 3
        po = self._por_orden(d)
        del po[5]
        t = dias_completos(po, 6, hoy - timedelta(days=1))
        assert t == (hoy - timedelta(days=4), hoy - timedelta(days=1))

    def test_si_la_siguiente_es_de_un_dia_anterior_los_del_medio_son_ceros(self):
        from app.services.demanda_fuentes import dias_completos
        hoy = _hoy()
        d = [hoy - timedelta(days=1), hoy - timedelta(days=2), hoy - timedelta(days=9)]
        po = self._por_orden(d)
        t = dias_completos(po, 10, hoy - timedelta(days=1))
        assert t == (hoy - timedelta(days=8), hoy - timedelta(days=1))

    def test_sin_la_primera_fila_nada(self):
        from app.services.demanda_fuentes import dias_completos
        po = self._por_orden([_hoy()] * 3)
        del po[1]
        assert dias_completos(po, 3, _hoy()) is None


# ═════════════════════════════════════════════════════════════════════════════
# La cascada
# ═════════════════════════════════════════════════════════════════════════════

def _kardex(db, ref, dias_atras, cant=1, concepto=501, nat=2, bod='NB1'):
    from app.services.kardex_service import KardexMovimiento
    db.session.add(KardexMovimiento(
        fecha=_hoy() - timedelta(days=dias_atras), tipo_docto='X', bodega=bod,
        referencia=ref, concepto=concepto, naturaleza=nat, cantidad=cant, costo_promedio=0))


def _siesa(db, ref, dias, cant=1, desde=1, bod='NB1', huecos=()):
    """`dias` días cubiertos desde ayer hacia atrás, con venta `cant` cada día."""
    from app.models.demanda_siesa import DemandaDiaCubierto, DemandaDiaSiesa
    for d in range(desde, desde + dias):
        if d in huecos:
            continue
        f = _hoy() - timedelta(days=d)
        db.session.add(DemandaDiaCubierto(fecha=f, consulta='x'))
        if cant:
            db.session.add(DemandaDiaSiesa(fecha=f, bodega=bod, referencia=ref,
                                           vendido=cant, devuelto=0))


def _pedidos(db, ref, dias, cant=1):
    """Foto de ventas desde pedido COMPLETA para todo CO operado, `dias` días."""
    from app.models.fotos_siesa import FotoCorrida, FotoVentaLinea, TipoFoto
    from app.services.fotos_siesa_service import cos_operados
    rowid = 5000
    for d in range(1, dias + 1):
        f = _hoy() - timedelta(days=d)
        for co in cos_operados():
            run = f'run-{co}-{d}'
            db.session.add(FotoCorrida(run_id=run, tipo=TipoFoto.VENTAS, alcance=co,
                                       dia_operativo=f, completa=True, filas=1, paginas=1))
            if co == '003':
                rowid += 1
                db.session.add(FotoVentaLinea(
                    f470_rowid=rowid, run_id=run, completa=True, dia_operativo=f, co=co,
                    bodega='NB1', referencia=ref, concepto=501, naturaleza=2,
                    cantidad=Decimal(cant), estado_docto=1))


class TestLaCascada:

    def test_sin_nada_no_hay_fuente_y_lo_dice(self, app, db):
        from app.services.demanda_fuentes import fuente_de_demanda
        f = fuente_de_demanda()
        assert f['fuente'] == 'NINGUNA' and f['hay_dato'] is False
        assert not any(f['apta_para'].values())
        assert 'papeleriamedellin_WMS_Ventas_Dia' in f['que_hacer']

    def test_siesa_manda_sobre_el_kardex(self, app, db):
        from app.services.demanda_fuentes import fuente_de_demanda
        _kardex(db, 'A', 300)
        _kardex(db, 'A', 2)
        _siesa(db, 'A', 60)
        db.session.commit()
        f = fuente_de_demanda()
        assert f['fuente'] == 'SIESA_VENTAS_DIA' and f['incluye_caja'] is True
        assert f['cobertura']['dias'] == 60 and f['apta_para']['bandeja'] is True
        assert f['apta_para']['contenedor'] is False, 'solo 60 días: el contenedor pide 180'

    def test_con_pocos_dias_de_siesa_se_usa_el_kardex(self, app, db):
        from app.services.demanda_fuentes import fuente_de_demanda
        _kardex(db, 'A', 300)
        _kardex(db, 'A', 2)
        _siesa(db, 'A', 10)
        db.session.commit()
        assert fuente_de_demanda()['fuente'] == 'KARDEX'

    def test_el_kardex_viejo_de_produccion_no_tapa_los_pedidos(self, app, db):
        """Producción, medido el 2026-09-27: 4.672 movimientos de enero de 2024."""
        from app.services.demanda_fuentes import fuente_de_demanda
        for d in range(700, 726):
            _kardex(db, 'A', d)
        _pedidos(db, 'A', 40)
        db.session.commit()
        f = fuente_de_demanda()
        assert f['fuente'] == 'VENTAS_DESDE_PEDIDO'
        assert f['parcial'] is True and f['incluye_caja'] is False
        assert f['apta_para'] == {'bandeja': True, 'contenedor': False,
                                  'temporada': False, 'bloqueo': False}
        assert 'COTA INFERIOR' in f['texto']

    def test_un_hueco_corta_la_cobertura(self, app, db):
        from app.services.demanda_fuentes import fuente_de_demanda
        _siesa(db, 'A', 90, huecos={40})
        db.session.commit()
        f = fuente_de_demanda()
        assert f['cobertura']['dias'] == 39
        c = next(x for x in f['candidatas'] if x['fuente'] == 'SIESA_VENTAS_DIA')
        assert c['detalle']['dias_antes_del_hueco'] == 50

    def test_atrasada_se_usa_pero_no_es_apta_para_decidir(self, app, db):
        from app.services.demanda_fuentes import fuente_de_demanda
        _siesa(db, 'A', 60, desde=15)
        db.session.commit()
        f = fuente_de_demanda()
        assert f['fuente'] == 'SIESA_VENTAS_DIA' and f['al_dia'] is False
        assert f['apta_para']['bandeja'] is False and 'atraso' in f['no_apta_por']['bandeja']


# ═════════════════════════════════════════════════════════════════════════════
# La demanda que sale de cada fuente
# ═════════════════════════════════════════════════════════════════════════════

class TestLaDemandaDeSiesa:

    def test_es_la_venta_sobre_los_dias_observados(self, app, db):
        """120 días cubiertos, 2 u por día en dos bodegas → 4 u/día en la red."""
        from app.services.kardex_service import KardexService
        _siesa(db, 'A', 120, cant=2, bod='NB1')
        from app.models.demanda_siesa import DemandaDiaSiesa
        for d in range(1, 121):
            db.session.add(DemandaDiaSiesa(fecha=_hoy() - timedelta(days=d), bodega='NS1',
                                           referencia='A', vendido=2, devuelto=0))
        db.session.commit()
        d = KardexService.demanda_descensurada(12, 'red')['A']
        assert d['d_avg'] == pytest.approx(4.0)
        assert d['dias_ventana'] == 120 and d['fuente_demanda'] == 'SIESA_VENTAS_DIA'
        assert d['hasta'] == (_hoy() - timedelta(days=1)).isoformat(), \
            'hoy no está leído: la ventana termina ayer'

    def test_la_devolucion_se_netea_igual_que_con_el_kardex(self, app, db):
        """Misma historia por las dos fuentes → misma demanda (D6)."""
        from app.models.demanda_siesa import DemandaDiaSiesa
        from app.services.kardex_service import serie_demanda
        from app.services.demanda_fuentes import fuente_de_demanda
        hoy = _hoy()
        a, b = hoy - timedelta(days=10), hoy - timedelta(days=9)
        _kardex(db, 'A', 10, 100)
        _kardex(db, 'A', 9, 30, concepto=502, nat=1)
        db.session.commit()
        fk = fuente_de_demanda()
        sk = serie_demanda(a, b, 'red', fuente=fk)['A']
        db.session.add(DemandaDiaSiesa(fecha=a, bodega='NB1', referencia='A',
                                       vendido=100, devuelto=0))
        db.session.add(DemandaDiaSiesa(fecha=b, bodega='NB1', referencia='A',
                                       vendido=0, devuelto=30))
        db.session.commit()
        ss = serie_demanda(a, b, 'red', fuente={'fuente': 'SIESA_VENTAS_DIA'})['A']
        assert ss == sk and ss['por_dia'] == {a: 70.0}

    def test_la_de_pedidos_no_cuenta_anuladas(self, app, db):
        from app.models.fotos_siesa import FotoVentaLinea
        from app.services.kardex_service import KardexService
        _pedidos(db, 'A', 40, cant=3)
        db.session.flush()
        FotoVentaLinea.query.filter(FotoVentaLinea.dia_operativo ==
                                    _hoy() - timedelta(days=1)).update({'estado_docto': 9})
        db.session.commit()
        d = KardexService.demanda_descensurada(12, 'red')['A']
        assert d['demanda_neta'] == 3 * 39 and d['demanda_parcial'] is True


class TestLasDecisiones:

    def _mundo_bandeja(self, db, fuente):
        from app.models.producto import Producto
        from app.models.stock_siesa import StockSiesa
        db.session.add(Producto(codigo='A', nombre='Producto A', codigo_siesa='A',
                                activo=True, precio_compra=0))
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='A', existencia=5,
                                  comprometido=0, salida_sin_conf=0))
        if fuente == 'siesa':
            _siesa(db, 'A', 200, cant=10)
        else:
            _pedidos(db, 'A', 60, cant=10)
        db.session.commit()

    def test_la_bandeja_propone_sin_kardex(self, app, db):
        from app.services import compras_bandeja
        self._mundo_bandeja(db, 'siesa')
        r = compras_bandeja.bandeja()
        assert r['estado'] == 'OK', r.get('falta')
        refs = [l['referencia'] for p in r['proveedores'] for l in p['lineas']]
        assert 'A' in refs
        assert r['demanda']['fuente'] == 'SIESA_VENTAS_DIA' and r['demanda']['nivel'] == 'ok'

    def test_con_pedidos_propone_y_dice_que_es_cota_inferior(self, app, db):
        from app.services import compras_bandeja
        self._mundo_bandeja(db, 'pedidos')
        r = compras_bandeja.bandeja()
        assert r['estado'] == 'OK'
        assert r['demanda']['parcial'] is True and r['demanda']['nivel'] == 'aviso'
        k = next(x for x in compras_bandeja.confianza()['renglones'] if x['clave'] == 'demanda')
        assert k['nivel'] == 'aviso' and 'cota inferior' in k['titulo']

    def test_el_contenedor_no_se_arma_con_la_venta_parcial(self, app, db):
        from app.services.armador_service import aptitud_de_la_propuesta
        from app.services.demanda_fuentes import fuente_de_demanda
        _pedidos(db, 'A', 200)
        db.session.commit()
        ins = {'hay_dato': True, 'fuentes': {'OC_SIESA': {'espejo_completo': True}}}
        ap = aptitud_de_la_propuesta(ins, fuente_de_demanda())
        assert ap['apta'] is False and 'caja' in ap['no_apta_por'][0]

    def test_el_contenedor_si_con_siesa_de_medio_anio(self, app, db):
        from app.services.armador_service import aptitud_de_la_propuesta
        from app.services.demanda_fuentes import fuente_de_demanda
        _siesa(db, 'A', 200)
        db.session.commit()
        ins = {'hay_dato': True, 'fuentes': {'OC_SIESA': {'espejo_completo': True}}}
        assert aptitud_de_la_propuesta(ins, fuente_de_demanda())['apta'] is True

    def test_con_venta_parcial_no_se_bloquea_nada(self, app, db):
        from app.services.bloqueo_recompra_service import BloqueoRecompraService
        _pedidos(db, 'A', 60)
        db.session.commit()
        r = BloqueoRecompraService.poblar_lista_inicial()
        assert r['no_se_bloqueo_por'] == 'DEMANDA_PARCIAL'


# ═════════════════════════════════════════════════════════════════════════════
# Rutas
# ═════════════════════════════════════════════════════════════════════════════

def _cab(app, db, almacen, rol):
    from flask_jwt_extended import create_access_token
    from app.models.usuario import Usuario
    u = Usuario(email=f'{rol}@demanda.test', nombre=rol, rol=rol, activo=True,
                almacen_id=almacen.id)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


class TestRutas:

    def test_compras_ve_la_cascada_y_un_operario_no(self, app, db, client, almacen):
        r = client.get('/api/compras/fuentes/demanda', headers=_cab(app, db, almacen, 'compras'))
        assert r.status_code == 200 and r.get_json()['fuente']['fuente'] == 'NINGUNA'
        r = client.get('/api/compras/fuentes/demanda', headers=_cab(app, db, almacen, 'operario'))
        assert r.status_code == 403

    def test_leer_arranca_en_segundo_plano_sin_hablar_con_siesa_en_el_request(
            self, app, db, client, almacen, monkeypatch):
        from app.services import demanda_fuentes as dfu
        visto = {}
        monkeypatch.setattr(dfu, 'disparar_descarga',
                            lambda app_, reciente=False, lanzar=None:
                            visto.update(reciente=reciente) or {'ok': True, 'codigo': 202})
        r = client.post('/api/compras/fuentes/demanda/leer', json={'reciente': True},
                        headers=_cab(app, db, almacen, 'compras'))
        assert r.status_code == 202 and visto == {'reciente': True}

    def test_rellenar_pedidos_valida_los_dias(self, app, db, client, almacen):
        r = client.post('/api/compras/fuentes/demanda/rellenar-pedidos', json={'dias': 900},
                        headers=_cab(app, db, almacen, 'compras'))
        assert r.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# La pantalla — Node con util.js real
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0]; const r = JSON.parse(args[1]);
const ctx = { console };
vm.createContext(ctx);
for (const f of ['util.js', 'compras_fuentes.js', 'compras_bandeja.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
ctx.__r = r;
console.log(JSON.stringify({
  fuentes: vm.runInContext('fuentesHtmlDemanda(__r.estado)', ctx),
  bandeja: vm.runInContext('cmpDemandaHtml(__r.demanda)', ctx),
}));
"""


def _render(r):
    if not shutil.which('node'):
        pytest.skip('sin node')
    p = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), json.dumps(r)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


class TestLaPantalla:

    def test_pinta_la_cascada_real_escapada_y_sin_undefined(self, app, db):
        from app.services.demanda_fuentes import fuente_de_demanda
        _pedidos(db, 'A', 40)
        db.session.commit()
        f = fuente_de_demanda()
        f['texto'] = f['texto'] + MALO
        estado = {'fuente': f, 'ultima_lectura_siesa': {'estado': 'fallo', 'resultado':
                                                         {'motivo': MALO}},
                  'consultas': {'reciente': MALO, 'historico': 'x'}}
        demanda = {'fuente': f['fuente'], 'nombre': MALO, 'parcial': True,
                   'cobertura': f['cobertura'], 'texto': f['texto'], 'nivel': 'aviso'}
        h = _render({'estado': estado, 'demanda': demanda})
        for html in h.values():
            assert '<img' not in html and 'undefined' not in html and 'null' not in html
        assert 'CORTAS' in h['bandeja'] and 'se usa' in h['fuentes']
        assert "fuentesDemandaLeer(event, 'reciente')" in h['fuentes']


# ═════════════════════════════════════════════════════════════════════════════
# TRINQUETE — una fuente de demanda (AST, inventario que solo encoge)
# ═════════════════════════════════════════════════════════════════════════════

#: Quién toca las tablas de la venta diaria de Siesa, y por qué. Nadie más.
TOCAN_LA_VENTA_DE_SIESA = {
    ('app/services/demanda_fuentes.py', '_guardar_lectura'):
        'El único escritor: upsert del tramo completo y los días cubiertos.',
    ('app/services/demanda_fuentes.py', 'cobertura_siesa'):
        'Qué días leyó una lectura completa: la cobertura de la fuente.',
    ('app/services/kardex_service.py', 'serie_demanda'):
        'EL numerador: la única que convierte las filas en demanda por día.',
}
#: Quién decide qué fuente vale: solo la cascada.
DECIDEN_LA_FUENTE = {'cobertura_siesa', 'cobertura_kardex', 'cobertura_pedidos'}
#: Módulos de compras que NO pueden juzgar la demanda por el kardex a mano.
MODULOS_COMPRAS = ('app/services/compras_bandeja.py', 'app/services/armador_service.py',
                   'app/services/temporada_service.py',
                   'app/services/bloqueo_recompra_service.py')
KARDEX_A_MANO = {'inicio_cobertura_kardex', 'KardexMovimiento'}
#: `salud_kardex` en compras solo para refinar el renglón cuando la fuente ES
#: el kardex (una descarga a medias no se pinta verde).
KARDEX_A_MANO_DECLARADO = {
    ('app/services/compras_bandeja.py', '_renglon_demanda'):
        'La fuente elegida es el kardex: su salud refina el renglón, no decide la fuente.',
}

_MODELOS = {'DemandaDiaSiesa', 'DemandaDiaCubierto'}


def _usos(src, archivo, nombres):
    """[(archivo, función externa, nombre)] de Name/Attribute/import: código,
    no texto (docstrings y comentarios no cuentan)."""
    arbol = ast.parse(src)
    usos = []

    def visitar(nodo, funcion):
        for hijo in ast.iter_child_nodes(nodo):
            f = funcion
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f = funcion or hijo.name
            if isinstance(hijo, ast.Name) and hijo.id in nombres:
                usos.append((archivo, f, hijo.id))
            elif isinstance(hijo, ast.Attribute) and hijo.attr in nombres:
                usos.append((archivo, f, hijo.attr))
            elif isinstance(hijo, ast.ImportFrom):
                for a in hijo.names:
                    if a.name in nombres:
                        usos.append((archivo, f, a.name))
            visitar(hijo, f)
    visitar(arbol, None)
    return usos


def _archivos():
    for p in sorted((RAIZ / 'app').rglob('*.py')):
        rel = p.relative_to(RAIZ).as_posix()
        if rel in ('app/models/demanda_siesa.py', 'app/models/__init__.py'):
            continue
        yield rel, p.read_text(encoding='utf-8')


def _todos(nombres):
    out = []
    for rel, src in _archivos():
        out.extend(_usos(src, rel, nombres))
    return out


class TestUnaFuenteDeDemanda:

    def test_nadie_mas_toca_la_venta_de_siesa(self):
        fuera = sorted({(a, f) for a, f, _n in _todos(_MODELOS)
                        if (a, f) not in TOCAN_LA_VENTA_DE_SIESA})
        assert not fuera, (f'{fuera}: leen o escriben la venta diaria de Siesa por su '
                           'cuenta. La demanda sale de `serie_demanda` con la fuente '
                           'que elige `demanda_fuentes.fuente_de_demanda`.')

    def test_nadie_mas_decide_la_fuente(self):
        fuera = sorted({(a, f) for a, f, _n in _todos(DECIDEN_LA_FUENTE)
                        if a != 'app/services/demanda_fuentes.py'})
        assert not fuera, f'{fuera}: juzgan la cobertura de una fuente por fuera de la cascada'

    def test_compras_no_juzga_la_demanda_por_el_kardex_a_mano(self):
        fuera = []
        for rel, src in _archivos():
            if rel not in MODULOS_COMPRAS:
                continue
            for a, f, n in _usos(src, rel, KARDEX_A_MANO | {'salud_kardex'}):
                if (a, f) in KARDEX_A_MANO_DECLARADO:
                    continue
                fuera.append((a, f, n))
        assert not fuera, (f'{fuera}: miran el kardex para decidir si hay demanda. '
                           'Eso lo contesta `fuente_de_demanda` (y la ventana, '
                           '`ventana_observada`).')

    def test_el_inventario_solo_encoge(self):
        assert len(TOCAN_LA_VENTA_DE_SIESA) <= 3
        assert len(KARDEX_A_MANO_DECLARADO) <= 1
        assert all(len(v) > 30 for v in {**TOCAN_LA_VENTA_DE_SIESA,
                                         **KARDEX_A_MANO_DECLARADO}.values())

    def test_cada_entrada_existe_de_verdad(self):
        usados = {(a, f) for a, f, _n in _todos(_MODELOS)}
        assert not (set(TOCAN_LA_VENTA_DE_SIESA) - usados)
        usados_k = {(a, f) for a, f, _n in _todos({'salud_kardex'})}
        assert not (set(KARDEX_A_MANO_DECLARADO) - usados_k)


class TestElDetectorMuerde:

    def test_ve_una_lectura_del_modelo(self):
        src = 'def mi_demanda():\n    return DemandaDiaSiesa.query.all()\n'
        assert ('x.py', 'mi_demanda', 'DemandaDiaSiesa') in _usos(src, 'x.py', _MODELOS)

    def test_ve_el_import(self):
        src = 'def f():\n    from app.models.demanda_siesa import DemandaDiaCubierto\n'
        assert ('x.py', 'f', 'DemandaDiaCubierto') in _usos(src, 'x.py', _MODELOS)

    def test_una_anidada_es_de_su_funcion_externa(self):
        src = 'def serie_demanda():\n    def _s():\n        return DemandaDiaSiesa\n'
        assert _usos(src, 'x.py', _MODELOS) == [('x.py', 'serie_demanda', 'DemandaDiaSiesa')]

    def test_no_marca_docstrings_ni_comentarios(self):
        src = ('def f():\n    """Lee DemandaDiaSiesa y salud_kardex."""\n'
               '    # DemandaDiaSiesa\n    return 1\n')
        assert _usos(src, 'x.py', _MODELOS | {'salud_kardex'}) == []

    def test_piso_minimo(self):
        """Si el escáner se rompe, esto se pone rojo en vez de reportar cero."""
        assert len(_todos(_MODELOS)) >= 6
        assert len(_todos(DECIDEN_LA_FUENTE)) >= 3
