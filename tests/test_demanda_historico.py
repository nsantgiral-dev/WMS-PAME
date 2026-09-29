"""
La venta de Siesa: el histórico que completa el año, con valor y costo
(P1-2 y P1-5b de la auditoría de compras, 2026-09-27).

## La clase

*Una lectura larga que siempre vuelve a empezar nunca termina.* La consulta
histórica era UNA, relativa (400 días hacia atrás), ordenada por fecha
descendente y leída desde la página 1 con un tope de 50 minutos: si el año no
cabía en 50 minutos, cada corrida volvía a traer los mismos días recientes y la
cobertura se estancaba — nunca llegaba a 180 (contenedor) ni a 360 (temporada).

## Ahora

- Una consulta por PERÍODO del histórico (trimestre por defecto; mes con
  `DEMANDA_PERIODO_HISTORICO=MES`), con **fechas fijas**, más la reciente
  (relativa, 14 días, la de todos los días). Con fechas fijas la numeración no
  cambia de un día a otro, y la lectura de un período **se retoma donde quedó**
  (`DemandaVentanaLectura`), verificando antes la última fila guardada (el
  ancla) y el total; si cambiaron, se relee el período entero y se dice.
- El cron lee la reciente primero y, con el tiempo que queda, los períodos con
  días sin cubrir, del más reciente al más viejo (la cobertura crece hacia
  atrás sin huecos). Un período sin registrar (401) se declara y se sigue.
- El SQL trae valor SIN impuesto y costo (vendido y devuelto) y cuántas líneas
  vinieron sin valor o sin costo; `valor_realizado` es el único lector.
"""
import ast
import pathlib
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.utils.fecha import dia_operativo

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _hoy():
    return dia_operativo()


# ─────────────────────────────────────────────────────────────────────────────
# Una Siesa falsa con varias consultas, la forma real del endpoint dinámico
# ─────────────────────────────────────────────────────────────────────────────

class SiesaVentas:
    """Cada consulta registrada es `{nombre: (filas, desde, hasta)}`; las filas
    son `(fecha, bodega, ref, vendido, devuelto, valor, costo)`. Orden
    DESCENDENTE por fecha, como el SQL. Una consulta que no está registrada
    responde 401. `reloj` avanza `paso` segundos por petición."""
    url_get_dinamico = 'https://siesa.falsa/dinamico'

    def __init__(self, consultas, paso=1.0, sin_valor=False, lento_una_vez=()):
        self.consultas = {}
        for nombre, (filas, desde, hasta) in consultas.items():
            self.registrar(nombre, filas, desde, hasta)
        self.t = 0.0
        self.paso = paso
        self.sin_valor = sin_valor
        self.lento_una_vez = set(lento_una_vez)
        self.pedidas = []

    def registrar(self, nombre, filas, desde, hasta):
        orden = sorted(filas, key=lambda f: (-f[0].toordinal(), f[1], f[2]))
        self.consultas[nombre] = [{
            'orden': i + 1, 'total_filas': len(orden),
            'ventana_desde': desde.isoformat() + 'T00:00:00',
            'ventana_hasta': hasta.isoformat() + 'T00:00:00',
            'fecha': f[0].isoformat() + 'T00:00:00', 'bodega': f[1], 'referencia': f[2],
            'vendido': f[3], 'devuelto': f[4], 'lineas': 1,
            'valor_vendido': f[5], 'valor_devuelto': 0, 'costo_vendido': f[6],
            'costo_devuelto': 0, 'lineas_sin_valor': 0, 'lineas_sin_costo': 0,
        } for i, f in enumerate(orden)]

    def reloj(self):
        return self.t

    def _get(self, nombre, params, url=None, timeout=None):
        assert url == self.url_get_dinamico
        assert 'parametros' not in params, 'una dinámica con parámetros da 400'
        pag = int(params['paginacion'].split('|')[0].split('=')[1])
        assert 'tamPag=100' in params['paginacion']
        self.t += self.paso
        self.pedidas.append((nombre, pag))
        if (nombre, pag) in self.lento_una_vez:
            self.lento_una_vez.discard((nombre, pag))
            raise Exception('Connekta no respondió — reintente')
        if nombre not in self.consultas:
            raise Exception('401 Client Error: Unauthorized')
        rows = self.consultas[nombre]
        trozo = [dict(r) for r in rows[(pag - 1) * 100:pag * 100]]
        if self.sin_valor:
            for r in trozo:
                for c in ('valor_vendido', 'valor_devuelto', 'costo_vendido',
                          'costo_devuelto', 'lineas_sin_valor', 'lineas_sin_costo'):
                    r.pop(c)
        return {'codigo': 0, 'detalle': {'Datos': trozo, 'total_registros': len(rows),
                                         'total_páginas': (len(rows) + 99) // 100}}


def _filas(desde, hasta, refs=('A', 'B', 'C'), bodegas=('NB1', 'NS1'), salta=()):
    """Cada SKU × bodega vende 1 u por día (valor 1.000, costo 600), salvo los
    días de `salta` (sin venta: tienen que quedar CUBIERTOS igual)."""
    out, d = [], desde
    while d <= hasta:
        if d not in salta:
            for ref in refs:
                for b in bodegas:
                    out.append((d, b, ref, 1, 0, 1000, 600))
        d += timedelta(days=1)
    return out


def _periodo(dfu, hoy, i=0):
    return dfu.ventanas_historicas(hoy)[i]


def _cubiertos():
    from app.models.demanda_siesa import DemandaDiaCubierto
    return {c.fecha for c in DemandaDiaCubierto.query.all()}


def _dias(desde, hasta):
    return {desde + timedelta(days=i) for i in range((hasta - desde).days + 1)}


# ═════════════════════════════════════════════════════════════════════════════
# 1 · El SQL
# ═════════════════════════════════════════════════════════════════════════════

class TestElSql:

    def test_el_periodo_tiene_fechas_fijas_y_la_reciente_relativa(self):
        from app.services import demanda_fuentes as dfu
        sql = dfu.sql_ventas_periodo(date(2025, 7, 1), date(2025, 9, 30))
        assert "CAST('20250701' AS date) AS desde" in sql
        assert "CAST('20250930' AS date) AS hasta" in sql
        assert 'GETDATE' not in sql, 'un período con fecha relativa corre la numeración cada día'
        rec = dfu.sql_ventas_dia(14)
        assert 'DATEADD(DAY, -14, GETDATE())' in rec

    def test_la_reciente_es_corta(self):
        """La reciente se relee ENTERA todos los días (para ver anulaciones):
        si fuera larga, volvería a ser el histórico que nunca termina."""
        from app.services import demanda_fuentes as dfu
        assert dfu.DIAS_RECIENTE <= 31
        assert dfu.ventana_reciente()['dias'] == dfu.DIAS_RECIENTE
        assert f'-{dfu.DIAS_RECIENTE}, GETDATE()' in dfu.sql_de_ventana(dfu.ventana_reciente())

    def test_forma_que_connekta_acepta(self):
        from app.services import demanda_fuentes as dfu
        for sql in (dfu.sql_ventas_dia(14), dfu.sql_ventas_periodo(date(2026, 7, 1),
                                                                    date(2026, 9, 26))):
            assert sql.rstrip().endswith('OFFSET 0 ROWS')
            assert 'ORDER BY v.fecha DESC' in sql
            assert not re.search(r'\bWITH\b', sql)
            assert '{' not in sql and '}' not in sql
            assert "''" not in sql, 'en el texto del SQL las fechas llevan UNA comilla'

    def test_trae_todas_las_columnas_que_el_lector_espera(self):
        from app.services import demanda_fuentes as dfu
        sql = dfu.sql_ventas_dia(14)
        select = sql[:sql.index('FROM (')]
        for c in dfu.COLUMNAS_VENTAS_DIA + dfu.COLUMNAS_VALOR:
            assert re.search(rf'\bv\.{c}\b|AS {c}\b', select), c

    def test_el_valor_es_la_misma_regla_de_vigia(self):
        """Una política, una función: el valor sin impuesto del SQL usa las
        mismas columnas que `vigia_service.valor_linea_sin_impuesto`."""
        from app.services import demanda_fuentes as dfu
        src = (RAIZ / 'app/services/vigia_service.py').read_text(encoding='utf-8')
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == 'valor_linea_sin_impuesto')
        de_vigia = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)
                    and isinstance(n.value, str) and n.value.startswith('f470_')}
        del_sql = set(re.findall(r'f470_\w+', dfu.SQL_VALOR_SIN_IMPUESTO))
        assert de_vigia and de_vigia == del_sql

    def test_el_paso_0_valida_cada_columna_que_el_sql_usa(self):
        from app.services import demanda_fuentes as dfu
        esquema = dfu.SQL_VALIDACION['esquema']
        prefijos = re.findall(r"LIKE '([a-z0-9_]+)%'", esquema)
        exactos = set(re.findall(r"'(f\d{3}_[a-z0-9_]+)'", esquema))
        usadas = set(re.findall(r'\b[mdieb]\.(f\d{3}_[a-z0-9_]+)', dfu.sql_ventas_dia(14)))
        assert usadas
        sin_validar = {c for c in usadas
                       if c not in exactos and not any(c.startswith(p) for p in prefijos)}
        assert not sin_validar, f'el paso 0 no valida {sorted(sin_validar)}'


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Las ventanas
# ═════════════════════════════════════════════════════════════════════════════

class TestLasVentanas:

    def test_trimestres_por_defecto(self, monkeypatch):
        from app.services import demanda_fuentes as dfu
        monkeypatch.delenv('DEMANDA_PERIODO_HISTORICO', raising=False)
        vs = dfu.ventanas_historicas(date(2026, 9, 28))
        assert [v['consulta'] for v in vs] == [
            f'papeleriamedellin_WMS_Ventas_{e}'
            for e in ('2026T3', '2026T2', '2026T1', '2025T4', '2025T3')]
        assert vs[0]['desde'] == date(2026, 7, 1) and vs[0]['hasta'] == date(2026, 9, 27)
        assert vs[-1]['desde'] == date(2025, 7, 1) and vs[-1]['hasta'] == date(2025, 9, 30)
        # Cubren los 400 días.
        assert vs[-1]['desde'] <= date(2026, 9, 28) - timedelta(days=dfu.DIAS_HISTORICO)

    def test_por_mes(self, monkeypatch):
        from app.services import demanda_fuentes as dfu
        monkeypatch.setenv('DEMANDA_PERIODO_HISTORICO', 'MES')
        vs = dfu.ventanas_historicas(date(2026, 9, 28))
        assert len(vs) == 14 and vs[0]['consulta'].endswith('_202609')
        assert vs[-1]['consulta'].endswith('_202508')

    def test_un_periodo_ilegible_es_trimestre(self, monkeypatch):
        from app.services import demanda_fuentes as dfu
        monkeypatch.setenv('DEMANDA_PERIODO_HISTORICO', 'semestre')
        assert dfu.periodo_historico() == dfu.PERIODO_TRIMESTRE


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Un período se retoma donde quedó
# ═════════════════════════════════════════════════════════════════════════════

class TestUnPeriodoSeRetoma:

    def _mundo(self, dfu, hoy, dias=40, **kw):
        v = _periodo(dfu, hoy)
        v = dict(v, desde=v['hasta'] - timedelta(days=dias - 1))
        gw = SiesaVentas({v['consulta']: (_filas(v['desde'], v['hasta'], **kw),
                                          v['desde'], v['hasta'])}, paso=61.0)
        return v, gw

    def _leer(self, dfu, v, gw, minutos=2):
        return dfu.descargar_ventana(v, gateway=gw, pausa_s=0, reloj=gw.reloj,
                                     max_minutos=minutos)

    def test_avanza_de_una_corrida_a_la_siguiente(self, app, db):
        """40 días × 6 filas = 240 filas, 3 páginas; cada corrida alcanza ~2
        páginas. Sin retomar, la segunda corrida volvería a la página 1 y el
        período no terminaría nunca."""
        from app.services import demanda_fuentes as dfu
        hoy = _hoy()
        v, gw = self._mundo(dfu, hoy)
        r1 = self._leer(dfu, v, gw)
        assert not r1['completa'] and r1['dias_guardados']
        cubiertos1 = _cubiertos()
        r2 = self._leer(dfu, v, gw)
        assert r2['completa'], r2
        assert r2['retomada_desde_fila'] and r2['retomada_desde_fila'] > 1
        assert _cubiertos() == _dias(v['desde'], v['hasta'])
        assert cubiertos1 < _cubiertos()
        # La segunda corrida no volvió a la página 1 (salvo que el ancla viva ahí).
        paginas2 = [p for n, p in gw.pedidas[len(gw.pedidas) - r2['paginas'] - 1:]]
        assert 1 not in paginas2[1:]
        from app.models.demanda_siesa import DemandaVentanaLectura
        c = DemandaVentanaLectura.query.filter_by(consulta=v['consulta']).one()
        assert c.completa and c.cubierto_desde == v['desde']

    def test_la_cobertura_crece_hacia_atras_sin_huecos(self, app, db):
        from app.services import demanda_fuentes as dfu
        hoy = _hoy()
        v, gw = self._mundo(dfu, hoy)
        self._leer(dfu, v, gw)
        cub = dfu.cobertura_siesa()
        assert cub['hasta'] == v['hasta'] and cub['dias_antes_del_hueco'] == 0

    def test_un_periodo_que_cambio_se_relee_entero(self, app, db):
        from app.services import demanda_fuentes as dfu
        hoy = _hoy()
        v, gw = self._mundo(dfu, hoy)
        self._leer(dfu, v, gw)
        # Una venta fechada atrás en el día más reciente: corre la numeración.
        filas = _filas(v['desde'], v['hasta']) + [(v['hasta'], 'NB1', 'NUEVA', 5, 0, 10, 6)]
        gw.registrar(v['consulta'], filas, v['desde'], v['hasta'])
        r = self._leer(dfu, v, gw, minutos=10)
        assert r['retomada_desde_fila'] is None
        assert 'cambió' in (r['motivo'] or '')
        assert r['completa']
        from app.models.demanda_siesa import DemandaDiaSiesa
        assert DemandaDiaSiesa.query.filter_by(referencia='NUEVA').one().vendido == 5

    def test_sin_registrar_se_declara(self, app, db):
        from app.services import demanda_fuentes as dfu
        from app.models.demanda_siesa import DemandaVentanaLectura
        hoy = _hoy()
        v = _periodo(dfu, hoy)
        gw = SiesaVentas({})
        r = dfu.descargar_ventana(v, gateway=gw, pausa_s=0, reloj=gw.reloj)
        assert r['sin_registrar'] and '401' in r['motivo']
        assert DemandaVentanaLectura.query.filter_by(consulta=v['consulta']).one().sin_registrar

    def test_la_primera_pagina_lenta_se_reintenta(self, app, db, monkeypatch):
        """Medido en producción: la primera consulta en frío tardó 108 s."""
        from app.services import demanda_fuentes as dfu
        monkeypatch.setattr(dfu.time, 'sleep', lambda s: None)
        hoy = _hoy()
        v, gw = self._mundo(dfu, hoy, dias=10)
        gw.paso = 1.0
        gw.lento_una_vez = {(v['consulta'], 1)}
        r = self._leer(dfu, v, gw)
        assert r['completa'], r


class TestAvance:
    """La función pura: qué días nuevos quedaron enteros al retomar."""

    def _po(self, fechas, desde=1):
        return {desde + i: {'fecha': f} for i, f in enumerate(fechas)}

    def test_el_dia_de_la_ultima_fila_no_esta_entero(self):
        from app.services.demanda_fuentes import avance
        d = date(2026, 9, 20)
        po = self._po([d, d, d - timedelta(days=1), d - timedelta(days=2)])
        tramo, orden_hasta, desde = avance(po, 10, 1, date(2026, 9, 1), d)
        assert tramo == (d - timedelta(days=1), d)
        assert orden_hasta == 3 and desde == d - timedelta(days=1)

    def test_completa_cubre_hasta_el_principio(self):
        from app.services.demanda_fuentes import avance
        d = date(2026, 9, 20)
        po = self._po([d, d - timedelta(days=5)])
        tramo, orden_hasta, desde = avance(po, 2, 1, date(2026, 9, 1), d)
        assert tramo == (date(2026, 9, 1), d) and orden_hasta == 2

    def test_retomada_empieza_bajo_lo_ya_cubierto(self):
        from app.services.demanda_fuentes import avance
        d = date(2026, 9, 10)
        po = self._po([d, d, d - timedelta(days=3)], desde=11)
        tramo, orden_hasta, desde = avance(po, 20, 11, date(2026, 9, 1), date(2026, 9, 12))
        # 9/11 y 9/12 no tienen filas y 9/8–9/9 tampoco (las filas saltan del
        # 10 al 7): son días enteros SIN venta. El 7 no está entero.
        assert tramo == (date(2026, 9, 8), date(2026, 9, 12))
        assert orden_hasta == 12

    def test_sin_filas_nuevas_no_avanza(self):
        from app.services.demanda_fuentes import avance
        tramo, orden_hasta, desde = avance({}, 20, 11, date(2026, 9, 1), date(2026, 9, 12))
        assert tramo is None and orden_hasta == 10 and desde == date(2026, 9, 13)


# ═════════════════════════════════════════════════════════════════════════════
# 4 · El histórico y el ciclo
# ═════════════════════════════════════════════════════════════════════════════

class TestElHistorico:

    def test_lee_los_pendientes_del_mas_reciente_y_declara_los_sin_registrar(self, app, db):
        from app.services import demanda_fuentes as dfu
        hoy = _hoy()
        vs = [v for v in dfu.ventanas_historicas(hoy) if not v['en_curso']]
        v0, v1 = vs[0], vs[1]
        gw = SiesaVentas({v0['consulta']: (_filas(v0['desde'], v0['hasta'], refs=('A',),
                                                  bodegas=('NB1',)),
                                           v0['desde'], v0['hasta'])})
        r = dfu.descargar_historico(gateway=gw, pausa_s=0, reloj=gw.reloj, hoy=hoy)
        assert r['ventanas_leidas'][0]['consulta'] == v0['consulta']
        assert v1['consulta'] in r['sin_registrar']
        assert v0['consulta'] not in [p['consulta'] for p in r['pendientes']]
        q = dfu._que_hacer([{'fuente': dfu.FUENTE_SIESA, 'usable': True}])
        assert v1['consulta'] in q

    def test_los_dias_que_alcanza_la_reciente_no_son_hueco(self, app, db):
        """Un período leído entero con su `hasta` a pocos días de hoy: lo que
        sigue lo cubre la reciente (14 días), así que no vuelve a la lista."""
        from app.services import demanda_fuentes as dfu
        hoy = _hoy()
        v0 = dfu.ventanas_historicas(hoy)[0]
        registrado_hasta = v0['hasta'] - timedelta(days=5)
        gw = SiesaVentas({v0['consulta']: (_filas(v0['desde'], registrado_hasta, refs=('A',),
                                                  bodegas=('NB1',)),
                                           v0['desde'], registrado_hasta)})
        r = dfu.descargar_ventana(v0, gateway=gw, pausa_s=0, reloj=gw.reloj)
        assert r['completa']
        assert v0['consulta'] not in [p['consulta'] for p in dfu.ventanas_pendientes(hoy)]
        assert dfu.cobertura_siesa()['hasta'] == registrado_hasta

    def test_el_hueco_despues_del_hasta_se_declara_con_que_hacer(self, app, db):
        """Validación P1-C2: un período cerrado registrado a medias. Del día
        siguiente a su `hasta` hasta donde llega la reciente no lo cubre nada:
        vuelve a la lista como HUECO, con «qué hacer». Se relee solo cuando la
        consulta se registra de nuevo con el período entero."""
        from app.models.demanda_siesa import DemandaDiaCubierto
        from app.services import demanda_fuentes as dfu
        hoy = date(2026, 10, 20)
        t3 = dfu.ventanas_historicas(hoy)[1]
        assert not t3['en_curso']
        a_medias = date(2026, 9, 20)
        gw = SiesaVentas({t3['consulta']: (_filas(t3['desde'], a_medias, refs=('A',),
                                                  bodegas=('NB1',)), t3['desde'], a_medias)})
        assert dfu.descargar_ventana(t3, gateway=gw, pausa_s=0, reloj=gw.reloj)['completa']
        for d in _dias(date(2026, 10, 1), hoy - timedelta(days=1)):
            db.session.add(DemandaDiaCubierto(fecha=d))
        db.session.commit()
        p = next(x for x in dfu.ventanas_pendientes(hoy) if x['consulta'] == t3['consulta'])
        assert p['estado'] == 'HUECO_DESPUES_DEL_HASTA'
        assert p['primer_dia_sin_cubrir'] == date(2026, 9, 21)
        assert 'vuelva a copiar' in p['que_hacer'] and '2026-09-30' in p['que_hacer']
        r = dfu.descargar_historico(gateway=gw, pausa_s=0, reloj=gw.reloj, hoy=hoy)
        assert not r['completo']
        gw.registrar(t3['consulta'], _filas(t3['desde'], t3['fin'], refs=('A',),
                                            bodegas=('NB1',)), t3['desde'], t3['fin'])
        dfu.descargar_historico(gateway=gw, pausa_s=0, reloj=gw.reloj, hoy=hoy)
        assert t3['consulta'] not in [x['consulta'] for x in dfu.ventanas_pendientes(hoy)]
        assert date(2026, 9, 25) in dfu.cobertura_siesa(con_dias=True)['dias_cubiertos']

    def test_registrada_de_nuevo_sin_ventas_nuevas_igual_se_relee(self, app, db):
        """Los días del hueco no tuvieron venta: la consulta registrada de nuevo
        trae las mismas filas (mismo total, misma ancla). Solo el `hasta`
        distinto dice que hay que releer — y cubrir esos días en cero."""
        from app.models.demanda_siesa import DemandaDiaCubierto
        from app.services import demanda_fuentes as dfu
        hoy = date(2026, 10, 20)
        t3 = dfu.ventanas_historicas(hoy)[1]
        a_medias = date(2026, 9, 20)
        filas = _filas(t3['desde'], a_medias, refs=('A',), bodegas=('NB1',))
        gw = SiesaVentas({t3['consulta']: (filas, t3['desde'], a_medias)})
        dfu.descargar_ventana(t3, gateway=gw, pausa_s=0, reloj=gw.reloj, hoy=hoy)
        for d in _dias(date(2026, 10, 1), hoy - timedelta(days=1)):
            db.session.add(DemandaDiaCubierto(fecha=d))
        db.session.commit()
        gw.registrar(t3['consulta'], filas, t3['desde'], t3['fin'])
        dfu.descargar_historico(gateway=gw, pausa_s=0, reloj=gw.reloj, hoy=hoy)
        assert date(2026, 9, 25) in dfu.cobertura_siesa(con_dias=True)['dias_cubiertos']

    def test_el_periodo_en_curso_no_se_registra_ni_se_lee(self, app, db):
        from app.services import demanda_fuentes as dfu
        hoy = _hoy()
        vs = dfu.ventanas_historicas(hoy)
        assert vs[0]['en_curso'] and not any(v['en_curso'] for v in vs[1:])
        gw = SiesaVentas({})
        dfu.descargar_historico(gateway=gw, pausa_s=0, reloj=gw.reloj, hoy=hoy)
        assert vs[0]['consulta'] not in [n for n, _p in gw.pedidas]
        p = next(x for x in dfu.ventanas_pendientes(hoy) if x['consulta'] == vs[0]['consulta'])
        assert p['estado'] == 'SE_REGISTRA_AL_CERRAR'

    def test_completo_no_lee_nada(self, app, db):
        from app.services import demanda_fuentes as dfu
        from app.models.demanda_siesa import DemandaDiaCubierto
        hoy = _hoy()
        for d in _dias(hoy - timedelta(days=dfu.DIAS_HISTORICO + 5), hoy - timedelta(days=1)):
            db.session.add(DemandaDiaCubierto(fecha=d))
        db.session.commit()
        gw = SiesaVentas({})
        r = dfu.descargar_historico(gateway=gw, pausa_s=0, reloj=gw.reloj, hoy=hoy)
        assert r['completo'] and not gw.pedidas


class TestElCiclo:

    def test_la_reciente_primero_y_despues_el_historico(self, app, db, monkeypatch):
        from app.services import demanda_fuentes as dfu
        monkeypatch.setenv('DEMANDA_SIESA', 'true')
        orden = []
        monkeypatch.setattr(dfu, 'descargar_ventana',
                            lambda v, **k: orden.append(v['tipo']) or {'completa': True})
        monkeypatch.setattr(dfu, 'descargar_historico',
                            lambda **k: orden.append('HISTORICO') or {'completo': True})
        r = dfu.ciclo()
        assert orden == ['RECIENTE', 'HISTORICO'] and r['reciente']['completa']


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Valor y costo
# ═════════════════════════════════════════════════════════════════════════════

class TestValorYCosto:

    def _leer_reciente(self, dfu, gw):
        return dfu.descargar_ventana(dfu.ventana_reciente(), gateway=gw, pausa_s=0,
                                     reloj=gw.reloj)

    def test_se_guardan_y_dan_el_precio_y_el_costo(self, app, db):
        from app.services import demanda_fuentes as dfu
        hoy = _hoy()
        desde = hoy - timedelta(days=14)
        gw = SiesaVentas({dfu.consulta_ventas_dia(True): (
            _filas(desde, hoy, refs=('A',), bodegas=('NB1',)), desde, hoy)})
        assert self._leer_reciente(dfu, gw)['completa']
        v = dfu.valor_realizado({'A'})['A']
        assert v['unidades'] == 14 and v['dias'] == 14      # hoy no se guarda
        assert v['precio_unitario'] == Decimal('1000.00')
        assert v['costo_unitario'] == Decimal('600.00')

    def test_una_consulta_sin_las_columnas_da_valor_desconocido(self, app, db):
        from app.services import demanda_fuentes as dfu
        from app.models.demanda_siesa import DemandaDiaSiesa
        hoy = _hoy()
        desde = hoy - timedelta(days=14)
        gw = SiesaVentas({dfu.consulta_ventas_dia(True): (
            _filas(desde, hoy, refs=('A',), bodegas=('NB1',)), desde, hoy)}, sin_valor=True)
        assert self._leer_reciente(dfu, gw)['completa']
        assert DemandaDiaSiesa.query.first().valor_vendido is None
        v = dfu.valor_realizado({'A'})['A']
        assert v['precio_unitario'] is None and v['valor_incompleto'] and v['valor'] is None

    def test_una_linea_sin_valor_en_siesa_no_da_precio(self, app, db):
        from app.services import demanda_fuentes as dfu
        from app.models.demanda_siesa import DemandaDiaCubierto, DemandaDiaSiesa
        d = _hoy() - timedelta(days=2)
        db.session.add(DemandaDiaCubierto(fecha=d))
        db.session.add(DemandaDiaSiesa(fecha=d, bodega='NB1', referencia='X', vendido=10,
                                       devuelto=0, valor_vendido=9000, valor_devuelto=0,
                                       costo_vendido=5000, costo_devuelto=0,
                                       lineas_sin_valor=1, lineas_sin_costo=0))
        db.session.commit()
        v = dfu.valor_realizado({'X'})['X']
        assert v['precio_unitario'] is None and v['costo_unitario'] == Decimal('500.00')

    def test_neto_de_devoluciones_y_solo_el_tramo_cubierto(self, app, db):
        from app.services import demanda_fuentes as dfu
        from app.models.demanda_siesa import DemandaDiaCubierto, DemandaDiaSiesa
        d = _hoy() - timedelta(days=3)
        db.session.add(DemandaDiaCubierto(fecha=d))
        for dia, vend, dev, val, vdev in ((d, 10, 2, 10000, 2000),
                                          (d - timedelta(days=9), 99, 0, 1, 0)):
            db.session.add(DemandaDiaSiesa(fecha=dia, bodega='NB1', referencia='Y',
                                           vendido=vend, devuelto=dev, valor_vendido=val,
                                           valor_devuelto=vdev, costo_vendido=0,
                                           costo_devuelto=0, lineas_sin_valor=0,
                                           lineas_sin_costo=0))
        db.session.commit()
        v = dfu.valor_realizado({'Y'})['Y']
        assert v['unidades'] == 8 and v['precio_unitario'] == Decimal('1000.00')


# ═════════════════════════════════════════════════════════════════════════════
# 6 · Trinquetes (AST)
# ═════════════════════════════════════════════════════════════════════════════

COLUMNAS_DE_VALOR = {'valor_vendido', 'valor_devuelto', 'costo_vendido', 'costo_devuelto',
                     'lineas_sin_valor', 'lineas_sin_costo'}
#: Quién lee el valor y el costo de la venta de Siesa. Solo encoge.
LEEN_EL_VALOR = {
    ('app/services/demanda_fuentes.py', 'valor_realizado'): 'el único lector del valor',
}
#: Quién pagina una consulta de ventas de Siesa. Solo encoge.
PAGINAN_LA_VENTA = {
    ('app/services/demanda_fuentes.py', 'descargar_ventana'): 'la lectura, con su cursor',
    ('app/services/demanda_fuentes.py', '_verificar_ancla'): 'relee la página del ancla',
}


def _usos(nombres, atributo=True):
    out = set()
    for p in sorted((RAIZ / 'app').rglob('*.py')):
        rel = p.relative_to(RAIZ).as_posix()
        if rel == 'app/models/demanda_siesa.py':
            continue
        arbol = ast.parse(p.read_text(encoding='utf-8'))

        def visitar(nodo, fn):
            for h in ast.iter_child_nodes(nodo):
                f = fn
                if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    f = fn or h.name
                if atributo and isinstance(h, ast.Attribute) and h.attr in nombres:
                    out.add((rel, f))
                if (isinstance(h, ast.Call) and isinstance(h.func, ast.Name)
                        and h.func.id in nombres):
                    out.add((rel, f))
                visitar(h, f)
        visitar(arbol, None)
    return out


def _usos_src(src, nombres):
    out = set()
    arbol = ast.parse(src)

    def visitar(nodo, fn):
        for h in ast.iter_child_nodes(nodo):
            f = fn
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f = fn or h.name
            if isinstance(h, ast.Attribute) and h.attr in nombres:
                out.add(f)
            if isinstance(h, ast.Call) and isinstance(h.func, ast.Name) and h.func.id in nombres:
                out.add(f)
            visitar(h, f)
    visitar(arbol, None)
    return out


class TestTrinquetes:

    def test_nadie_mas_lee_el_valor(self):
        fuera = _usos(COLUMNAS_DE_VALOR) - set(LEEN_EL_VALOR)
        assert not fuera, f'{sorted(fuera)}: leen el valor de la venta de Siesa por su cuenta'

    def test_nadie_mas_pagina_la_venta(self):
        fuera = _usos({'_leer_paginas'}) - set(PAGINAN_LA_VENTA)
        assert not fuera, f'{sorted(fuera)}: paginan una consulta de ventas sin el cursor'

    def test_cada_entrada_existe_y_dice_por_que(self):
        assert set(LEEN_EL_VALOR) <= _usos(COLUMNAS_DE_VALOR)
        assert set(PAGINAN_LA_VENTA) <= _usos({'_leer_paginas'})
        assert all(len(v) > 10 for v in {**LEEN_EL_VALOR, **PAGINAN_LA_VENTA}.values())

    def test_el_detector_ve_y_no_inventa(self):
        src = ('def a(f):\n    return f.valor_vendido\n'
               'def b():\n    """f.valor_vendido"""\n    # f.costo_vendido\n    return 1\n'
               'def c():\n    def d(f):\n        return f.costo_devuelto\n    return d\n'
               'def e():\n    return _leer_paginas(1)\n')
        assert _usos_src(src, COLUMNAS_DE_VALOR) == {'a', 'c'}
        assert _usos_src(src, {'_leer_paginas'}) == {'e'}

    def test_pisos(self):
        assert len(_usos(COLUMNAS_DE_VALOR)) >= 1
        assert len(_usos({'_leer_paginas'})) >= 2
