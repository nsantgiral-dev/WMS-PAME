"""
📈 Analítica → 🩺 Salud del dato y 📜 Bitácora (Fase 1, 2026-09-24).

`app/services/analitica_salud.py`, `app/routes/analitica_salud.py`,
`GET /api/analitica/bitacora` (enriquecido) y las vistas
`analitica_salud.js` / `analitica_bitacora.js`.

## Qué protege

1. **Cada fuente tiene cinco veredictos y los cinco se distinguen**: al día,
   atrasada, incompleta, apagada, sin datos — armados con los datos que
   escriben los servicios reales (registros_sync, stock_siesa, fotos,
   serie_vigia, siesa_jobs).
2. **El veredicto global nunca dice «confiable» con una fuente apagada o sin
   datos**, ni con hallazgos que bloquean, envíos fallidos o flujos que la
   auditoría no alcanzó a mirar.
3. **El reloj operativo**: una fuente que no puede refrescarse de noche no
   está «atrasada» a las 6 a. m.
4. **La bitácora se lee como frase**: quién (nombre), qué, de qué pedido, a
   qué hora de Bogotá, y el antes → después campo por campo.
5. **Todo dato se escapa**: el render real corre en Node con `util.js` real.
"""
import json
import pathlib
import shutil
import subprocess
from datetime import date, datetime, timedelta

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
X = '<img src=x onerror=alert(1)>'

#: Jueves 2026-09-24, 12:00 Bogotá — dentro de toda ventana.
MEDIODIA = datetime(2026, 9, 24, 17, 0)
HOY = date(2026, 9, 24)


def _svc():
    from app.services import analitica_salud
    return analitica_salud


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    _svc()._CACHE_AUDITORIA.update({'resultado': None, 'calculado_utc': None})
    for v in ('FOTOS_SIESA', 'VIGIA_INGESTA_FACTURACION'):
        monkeypatch.delenv(v, raising=False)
    yield
    _svc()._CACHE_AUDITORIA.update({'resultado': None, 'calculado_utc': None})


def _h(app, usuario):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


# ─────────────────────────────────────────────────────────────────────────────
# Reloj operativo
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.usefixtures('ventana_qa')
class TestRelojOperativo:

    def test_la_noche_no_cuenta(self):
        s = _svc()
        # 20:55 Bogotá → 06:30 del día siguiente: solo 30 min de ventana (desde
        # las 06:00; la ventana de Siesa es UNA, 06:00–19:30, desde 2026-09-25).
        desde = datetime(2026, 9, 25, 1, 55)
        hasta = datetime(2026, 9, 25, 11, 30)
        assert s.tiempo_operativo(desde, hasta) == timedelta(minutes=30)

    def test_de_dia_es_el_reloj(self):
        s = _svc()
        assert s.tiempo_operativo(MEDIODIA - timedelta(hours=2), MEDIODIA) == timedelta(hours=2)

    def test_dos_dias_cuentan_dos_ventanas(self):
        s = _svc()
        # 12:00 Bogotá del 22 → 12:00 del 24: 7,5 h + 13,5 h + 6 h = 27 h de
        # ventana 06:00–19:30.
        assert s.tiempo_operativo(MEDIODIA - timedelta(days=2), MEDIODIA) == timedelta(hours=27)


# ─────────────────────────────────────────────────────────────────────────────
# Fuentes
# ─────────────────────────────────────────────────────────────────────────────

def _registro_pedidos(db, inicio, fin, ok, error=None):
    from app.models.registro_sync import RegistroSync
    r = RegistroSync(tipo='pedidos', inicio=inicio, fin=fin, ok=ok,
                     resultado=json.dumps({'paginacion_completa': bool(ok)}), error=error)
    db.session.add(r)
    db.session.commit()
    return r


class TestPedidos:

    def test_sin_datos_es_critico(self, db):
        f = _svc().fuente_pedidos(MEDIODIA)
        assert f['veredicto'] == 'SIN_DATOS' and f['nivel'] == 'critico'
        assert f['que_hacer']

    def test_al_dia(self, db):
        _registro_pedidos(db, MEDIODIA - timedelta(minutes=6), MEDIODIA - timedelta(minutes=5), True)
        f = _svc().fuente_pedidos(MEDIODIA)
        assert f['veredicto'] == 'AL_DIA', f
        assert f['nivel'] == 'ok' and f['completa'] is True
        assert f['cron']['tag'] == '[PEDIDOS_SCHEDULER]'

    def test_atrasada(self, db):
        _registro_pedidos(db, MEDIODIA - timedelta(hours=2, minutes=1), MEDIODIA - timedelta(hours=2), True)
        f = _svc().fuente_pedidos(MEDIODIA)
        assert f['veredicto'] == 'ATRASADA', f
        assert f['nivel'] == 'advertencia'

    def test_al_amanecer_no_esta_atrasada(self, db, ventana_qa):
        """La última corrida fue 19:29 de anoche (la ventana cierra 19:30); a las
        06:10 lleva 11 min operativos: al día."""
        _registro_pedidos(db, datetime(2026, 9, 25, 0, 28), datetime(2026, 9, 25, 0, 29), True)
        f = _svc().fuente_pedidos(datetime(2026, 9, 25, 11, 10))
        assert f['veredicto'] == 'AL_DIA', f

    def test_barrido_incompleto(self, db):
        _registro_pedidos(db, MEDIODIA - timedelta(minutes=30), MEDIODIA - timedelta(minutes=29), True)
        _registro_pedidos(db, MEDIODIA - timedelta(minutes=2), MEDIODIA - timedelta(minutes=1), False,
                          error='barrido incompleto: la página 3 falló')
        f = _svc().fuente_pedidos(MEDIODIA)
        assert f['veredicto'] == 'INCOMPLETA'
        assert 'página 3' in f['motivo']

    def test_con_el_servicio_real_de_registro(self, db):
        from app.services import registro_sync_service as reg
        rid = reg.abrir('pedidos')
        reg.cerrar_ok(rid, {'paginacion_completa': True})
        assert _svc().fuente_pedidos(datetime.utcnow())['veredicto'] == 'AL_DIA'


def _stock(db, bodega, actualizado):
    from app.models.stock_siesa import StockSiesa
    db.session.add(StockSiesa(bodega=bodega, codigo_siesa=f'SKU-{bodega}', existencia=5,
                              updated_at=actualizado))


class TestExistencias:

    def test_sin_datos(self, db):
        f = _svc().fuente_stock(MEDIODIA)
        assert f['veredicto'] == 'SIN_DATOS' and f['nivel'] == 'critico'

    def test_al_dia_con_todas_las_bodegas(self, db):
        from app.services.inventario_siesa_service import _BODEGAS_INVENTARIO
        for b in _BODEGAS_INVENTARIO:
            _stock(db, b, MEDIODIA - timedelta(minutes=30))
        db.session.commit()
        f = _svc().fuente_stock(MEDIODIA)
        assert f['veredicto'] == 'AL_DIA', f

    def test_una_bodega_sin_filas_es_incompleta(self, db):
        from app.services.inventario_siesa_service import _BODEGAS_INVENTARIO
        for b in _BODEGAS_INVENTARIO[1:]:
            _stock(db, b, MEDIODIA - timedelta(minutes=30))
        db.session.commit()
        f = _svc().fuente_stock(MEDIODIA)
        assert f['veredicto'] == 'INCOMPLETA'
        assert _BODEGAS_INVENTARIO[0] in f['motivo']

    def test_una_bodega_vieja_es_atrasada(self, db):
        from app.services.inventario_siesa_service import _BODEGAS_INVENTARIO
        for b in _BODEGAS_INVENTARIO:
            _stock(db, b, MEDIODIA - timedelta(minutes=30))
        _stock(db, 'XX9', MEDIODIA)                       # no esperada: no cuenta
        db.session.commit()
        from app.models.stock_siesa import StockSiesa
        StockSiesa.query.filter_by(bodega='NC1').update({'updated_at': MEDIODIA - timedelta(hours=5)})
        db.session.commit()
        f = _svc().fuente_stock(MEDIODIA)
        assert f['veredicto'] == 'ATRASADA' and 'NC1' in f['motivo']

    def test_con_almacen_solo_mira_su_bodega(self, db, almacen):
        _stock(db, 'NB1', MEDIODIA - timedelta(minutes=10))
        db.session.commit()
        assert _svc().fuente_stock(MEDIODIA, almacen.id)['veredicto'] == 'AL_DIA'
        assert _svc().fuente_stock(MEDIODIA)['veredicto'] == 'INCOMPLETA'


def _corrida(db, tipo, alcance, dia, completa=True, detalle=None, motivo=None):
    from app.models.fotos_siesa import FotoCorrida
    db.session.add(FotoCorrida(run_id='r1', tipo=tipo, alcance=alcance, dia_operativo=dia,
                               completa=completa, motivo=None if completa else (motivo or 'x'),
                               detalle=json.dumps(detalle) if detalle else None,
                               terminada_at=MEDIODIA))
    db.session.commit()


class TestFotos:

    @pytest.fixture(autouse=True)
    def _un_co(self, monkeypatch):
        from app.services import fotos_siesa_service
        monkeypatch.setattr(fotos_siesa_service, 'cos_operados', lambda: ['003', '001'])

    def test_apagada_sin_corridas(self, db):
        f = _svc().fuente_foto('VENTAS', MEDIODIA)
        assert f['veredicto'] == 'APAGADA' and f['nivel'] == 'advertencia'

    def test_encendida_sin_corridas_es_sin_datos(self, db, monkeypatch):
        monkeypatch.setenv('FOTOS_SIESA', 'true')
        assert _svc().fuente_foto('VENTAS', MEDIODIA)['veredicto'] == 'SIN_DATOS'

    def test_al_dia(self, db):
        """Antes de las 19:30 la foto esperada es la de ayer. Con dato fresco
        manda el dato, aunque el interruptor de ESTE proceso esté apagado: el
        cron corre en el worker."""
        for co in ('003', '001'):
            _corrida(db, 'VENTAS', co, HOY - timedelta(days=1))
        f = _svc().fuente_foto('VENTAS', MEDIODIA)
        assert f['veredicto'] == 'AL_DIA', f

    def test_vieja_y_encendida_es_atrasada(self, db, monkeypatch):
        monkeypatch.setenv('FOTOS_SIESA', 'true')
        for co in ('003', '001'):
            _corrida(db, 'VENTAS', co, HOY - timedelta(days=2))
        assert _svc().fuente_foto('VENTAS', MEDIODIA)['veredicto'] == 'ATRASADA'

    def test_vieja_y_apagada_es_apagada(self, db):
        for co in ('003', '001'):
            _corrida(db, 'VENTAS', co, HOY - timedelta(days=2))
        assert _svc().fuente_foto('VENTAS', MEDIODIA)['veredicto'] == 'APAGADA'

    def test_un_co_sin_corrida_completa_es_incompleta(self, db):
        _corrida(db, 'VENTAS', '003', HOY - timedelta(days=1))
        _corrida(db, 'VENTAS', '001', HOY - timedelta(days=1), completa=False, motivo='breaker')
        f = _svc().fuente_foto('VENTAS', MEDIODIA)
        assert f['veredicto'] == 'INCOMPLETA' and '001' in f['motivo']

    def test_ninguna_corrida_completa(self, db):
        _corrida(db, 'CARTERA', 'TODAS', HOY - timedelta(days=1), completa=False, motivo='página tope')
        f = _svc().fuente_foto('CARTERA', MEDIODIA)
        assert f['veredicto'] == 'INCOMPLETA' and 'página tope' in f['motivo']

    def test_stock_con_costo_incompleto_no_esta_al_dia(self, db):
        from app.services.inventario_siesa_service import _BODEGAS_INVENTARIO, _BODEGAS_PV
        for b in _BODEGAS_INVENTARIO:
            completo = b != 'NB1'
            _corrida(db, 'STOCK', b, HOY - timedelta(days=1),
                     detalle={'costo': {'completo': completo if b in _BODEGAS_PV else False}})
        f = _svc().fuente_foto('STOCK', MEDIODIA)
        assert f['veredicto'] == 'INCOMPLETA'
        assert f['detalle']['costo_incompleto_en'] == ['NB1']


class TestKardexYVigia:

    def test_kardex_vacio_es_sin_datos(self, db):
        f = _svc().fuente_kardex()
        assert f['veredicto'] == 'SIN_DATOS'

    def test_un_codigo_desconocido_no_se_lee_como_al_dia(self, db, monkeypatch):
        from app.services import kardex_service
        monkeypatch.setattr(kardex_service, 'salud_kardex',
                            lambda: {'veredicto': 'ALGO_NUEVO', 'confiable': False, 'problemas': [
                                {'codigo': 'ALGO_NUEVO', 'titulo': 't', 'que_hacer': 'q'}]})
        assert _svc().fuente_kardex()['veredicto'] == 'INCOMPLETA'

    def _serie(self, db, serie, semana, fuente='PRODUCCION'):
        from app.services.vigia_service import SerieVigia
        db.session.add(SerieVigia(serie=serie, semana=semana, valor=1, registros=1, fuente=fuente))
        db.session.commit()

    def test_adopcion(self, db):
        s = _svc()
        assert s.fuente_vigia_adopcion(MEDIODIA)['veredicto'] == 'SIN_DATOS'
        self._serie(db, 'adopcion_picking', date(2026, 9, 7))
        assert s.fuente_vigia_adopcion(MEDIODIA)['veredicto'] == 'ATRASADA'
        self._serie(db, 'adopcion_picking', date(2026, 9, 14))
        assert s.fuente_vigia_adopcion(MEDIODIA)['veredicto'] == 'AL_DIA'

    def test_el_lunes_temprano_se_espera_la_semana_anterior(self, db):
        self._serie(db, 'adopcion_picking', date(2026, 9, 7))
        # Lunes 2026-09-14 05:00 Bogotá: el cron de las 05:30 no corrió todavía.
        assert _svc().fuente_vigia_adopcion(datetime(2026, 9, 14, 10, 0))['veredicto'] == 'AL_DIA'

    def test_facturacion_apagada_y_con_evidencia(self, db, monkeypatch):
        s = _svc()
        assert s.fuente_vigia_facturacion(MEDIODIA)['veredicto'] == 'APAGADA'
        monkeypatch.setenv('VIGIA_INGESTA_FACTURACION', 'true')
        assert s.fuente_vigia_facturacion(MEDIODIA)['veredicto'] == 'SIN_DATOS'
        self._serie(db, 'facturacion_003', date(2026, 8, 3), fuente='HISTORICO')
        assert s.fuente_vigia_facturacion(MEDIODIA)['veredicto'] == 'ATRASADA'
        monkeypatch.delenv('VIGIA_INGESTA_FACTURACION')
        assert s.fuente_vigia_facturacion(MEDIODIA)['veredicto'] == 'APAGADA'
        self._serie(db, 'facturacion_003', date(2026, 9, 14))
        assert s.fuente_vigia_facturacion(MEDIODIA)['veredicto'] == 'AL_DIA'


# ─────────────────────────────────────────────────────────────────────────────
# Cola de Siesa y cobertura de claves
# ─────────────────────────────────────────────────────────────────────────────

class TestColaSiesa:

    def _job(self, db, estado, creado):
        from app.models.siesa_job import SiesaJob
        db.session.add(SiesaJob(tipo='RECIBO_CAJA', payload='{}', estado=estado,
                                fecha_creacion=creado))
        db.session.commit()

    def test_vacia(self, db):
        c = _svc().cola_siesa(MEDIODIA)
        assert c['nivel'] == 'ok' and c['fallidos'] == 0

    def test_pendiente_reciente(self, db):
        self._job(db, 'PENDIENTE', MEDIODIA - timedelta(minutes=10))
        assert _svc().cola_siesa(MEDIODIA)['nivel'] == 'ok'

    def test_pendiente_viejo(self, db):
        self._job(db, 'PENDIENTE', MEDIODIA - timedelta(hours=3))
        assert _svc().cola_siesa(MEDIODIA)['nivel'] == 'advertencia'

    def test_fallido_es_critico(self, db):
        self._job(db, 'FALLIDO', MEDIODIA - timedelta(days=1))
        c = _svc().cola_siesa(MEDIODIA)
        assert c['nivel'] == 'critico' and c['fallidos'] == 1
        assert c['fallidos_por_tipo'][0]['tipo'] == 'RECIBO_CAJA'


class TestCoberturaDeClaves:

    def test_cuenta_solo_tareas_de_pedido_del_rango(self, db, almacen, producto, ub_picking):
        from app.models.packing import TareaPacking
        from app.models.picking import TareaPicking
        dentro = datetime(2026, 9, 24, 15, 0)
        n = 0

        def pk(tipo, clave, cuando=dentro):
            nonlocal n
            n += 1
            db.session.add(TareaPicking(codigo=f'PK-{n}', producto_id=producto.id, cantidad_solicitada=1,
                                        ubicacion_id=ub_picking.id, almacen_id=almacen.id,
                                        tipo_documento=tipo, referencia_documento=f'PD{n}',
                                        pedido_clave=clave, fecha_creacion=cuando))
        for i in range(3):
            pk('PEDIDO', f'003-PD-{i}')
        pk('PEDIDO', None)
        pk('TRASLADO', None)
        pk('TRASLADO', None)
        pk('PEDIDO', None, datetime(2026, 8, 1, 15, 0))
        db.session.add(TareaPacking(codigo='PAK-1', almacen_id=almacen.id, tipo_documento='PEDIDO',
                                    pedido_clave='003-PD-1', fecha_creacion=dentro))
        db.session.commit()
        c = _svc().cobertura_claves(HOY, HOY, None)
        assert c['picking'] == {'n': 4, 'con_clave': 3, 'sin_clave': 1, 'pct': 0.75}
        assert c['packing']['n'] == 1
        assert c['n'] == 5 and c['pct'] == pytest.approx(0.8)
        assert c['nivel'] == 'advertencia'
        assert _svc().cobertura_claves(HOY, HOY, almacen.id + 999)['n'] == 0

    def test_sin_tareas_no_es_cero_por_ciento(self, db):
        c = _svc().cobertura_claves(HOY, HOY)
        assert c['pct'] is None and c['n'] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Auditoría con tope
# ─────────────────────────────────────────────────────────────────────────────

def _reporte(bloq=0, avisos=0):
    res = []
    if bloq:
        res.append({'codigo': 'VTA-30', 'flujo': 'venta', 'frontera': 'f', 'severidad': 'BLOQUEA',
                    'consecuencia': 'sale sin factura', 'error': None, 'total': bloq,
                    'hallazgos': [{'referencia': 'PD1', 'detalle': 'd', 'datos': {}}], 'truncado': False})
    if avisos:
        res.append({'codigo': 'VTA-21', 'flujo': 'venta', 'frontera': 'f', 'severidad': 'AVISA',
                    'consecuencia': 'c', 'error': None, 'total': avisos, 'hallazgos': [],
                    'truncado': False})
    return {'invariantes_corridos': 3, 'consultas_truncadas': [], 'invariantes_rotos': len(res),
            'hallazgos_totales': bloq + avisos, 'bloqueantes': bloq, 'errores': [],
            'resultados': res}


class TestAuditoria:

    def test_corre_de_verdad_sobre_la_base(self, db):
        a = _svc().resumen_auditoria(MEDIODIA)
        from app.services import auditoria
        assert [x['flujo'] for x in a['por_flujo']] == auditoria.flujos()
        assert a['flujos_no_evaluados'] == []
        assert sum(x['invariantes'] for x in a['por_flujo']) >= 30

    def test_bloqueantes_es_critico(self, db, monkeypatch):
        from app.services import auditoria
        monkeypatch.setattr(auditoria, 'flujos', lambda: ['venta'])
        monkeypatch.setattr(auditoria, 'auditar', lambda f=None: _reporte(bloq=2, avisos=1))
        a = _svc().resumen_auditoria(MEDIODIA)
        assert a['nivel'] == 'critico' and a['bloqueantes'] == 2 and a['avisos'] == 1
        assert a['por_flujo'][0]['peores'][0]['codigo'] == 'VTA-30'

    def test_el_tope_deja_flujos_sin_mirar_y_lo_declara(self, db, monkeypatch):
        from app.services import auditoria
        monkeypatch.setattr(auditoria, 'flujos', lambda: ['a', 'b', 'c'])
        monkeypatch.setattr(auditoria, 'auditar', lambda f=None: _reporte())
        tiempos = iter([0, 0, 999, 999, 999, 999])
        a = _svc().resumen_auditoria(MEDIODIA, reloj=lambda: next(tiempos))
        assert [x['flujo'] for x in a['por_flujo']] == ['a']
        assert a['flujos_no_evaluados'] == ['b', 'c']
        assert a['nivel'] == 'advertencia'

    def test_se_guarda_y_se_declara(self, db, monkeypatch):
        from app.services import auditoria
        llamadas = []
        monkeypatch.setattr(auditoria, 'flujos', lambda: ['a'])
        monkeypatch.setattr(auditoria, 'auditar', lambda f=None: llamadas.append(f) or _reporte())
        s = _svc()
        assert s.resumen_auditoria(MEDIODIA)['desde_cache'] is False
        assert s.resumen_auditoria(MEDIODIA + timedelta(minutes=5))['desde_cache'] is True
        assert s.resumen_auditoria(MEDIODIA + timedelta(minutes=11))['desde_cache'] is False
        assert len(llamadas) == 2


# ─────────────────────────────────────────────────────────────────────────────
# El veredicto global
# ─────────────────────────────────────────────────────────────────────────────

_OK = {'nivel': 'ok'}


def _f(veredicto, critica=False, clave='x'):
    return _svc()._fuente(clave, 'Fuente', veredicto, 'm', 'q', alimenta='a', critica=critica)


class TestVeredictoGlobal:

    def test_todo_al_dia_es_confiable(self):
        g = _svc().veredicto_global([_f('AL_DIA'), _f('AL_DIA', True)], _OK, _OK, _OK)
        assert g['veredicto'] == 'CONFIABLE' and g['razones'] == []

    @pytest.mark.parametrize('veredicto', ['ATRASADA', 'INCOMPLETA', 'APAGADA', 'SIN_DATOS'])
    def test_una_sola_fuente_que_no_esta_al_dia_basta(self, veredicto):
        g = _svc().veredicto_global([_f('AL_DIA'), _f(veredicto)], _OK, _OK, _OK)
        assert g['veredicto'] != 'CONFIABLE', veredicto
        assert g['razones'][0]['clave'] == 'x'

    @pytest.mark.parametrize('veredicto', ['APAGADA', 'SIN_DATOS'])
    def test_fuente_critica_apagada_o_sin_datos_no_es_confiable(self, veredicto):
        g = _svc().veredicto_global([_f(veredicto, critica=True)], _OK, _OK, _OK)
        assert g['veredicto'] == 'NO_CONFIABLE'

    def test_fuente_critica_atrasada_es_con_reservas(self):
        g = _svc().veredicto_global([_f('ATRASADA', critica=True)], _OK, _OK, _OK)
        assert g['veredicto'] == 'CON_RESERVAS'

    def test_bloqueantes_fallidos_y_cobertura(self):
        s = _svc()
        fuentes = [_f('AL_DIA')]
        assert s.veredicto_global(fuentes, {'nivel': 'critico', 'bloqueantes': 3}, _OK, _OK)['veredicto'] == 'NO_CONFIABLE'
        assert s.veredicto_global(fuentes, {'nivel': 'advertencia', 'flujos_no_evaluados': ['venta']},
                                  _OK, _OK)['veredicto'] == 'CON_RESERVAS'
        assert s.veredicto_global(fuentes, _OK, {'nivel': 'critico', 'texto': 't'}, _OK)['veredicto'] == 'NO_CONFIABLE'
        assert s.veredicto_global(fuentes, _OK, _OK, {'nivel': 'advertencia', 'texto': 't'})['veredicto'] == 'CON_RESERVAS'

    def test_los_criticos_van_primero(self):
        g = _svc().veredicto_global([_f('ATRASADA', clave='a'), _f('SIN_DATOS', True, clave='b')],
                                    _OK, _OK, _OK)
        assert [r['clave'] for r in g['razones']] == ['b', 'a']

    def test_base_vacia_nunca_es_confiable(self, db):
        r = _svc().salud_del_dato(HOY - timedelta(days=29), HOY, None, MEDIODIA)
        assert r['global']['veredicto'] == 'NO_CONFIABLE'
        assert len(r['fuentes']) == 8
        assert {f['veredicto'] for f in r['fuentes']} <= {'SIN_DATOS', 'APAGADA'}
        assert r['resumen']['fuentes_al_dia'] == 0
        assert r['meta']['calculado_en'] == MEDIODIA.isoformat()
        assert set(r['meta']['fuentes']) == {f['clave'] for f in r['fuentes']}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestEndpoints:

    def test_solo_gestion(self, client, db, jwt_token):
        h = {'Authorization': f'Bearer {jwt_token}'}
        assert client.get('/api/analitica/salud', headers=h).status_code == 403
        assert client.get('/api/analitica/bitacora/patrones', headers=h).status_code == 403

    @pytest.mark.parametrize('qs', ['desde=ayer', 'hasta=2026-13-01', 'almacen_id=abc',
                                    'almacen_id=-1', 'almacen_id=99999',
                                    'desde=2026-09-24&hasta=2026-09-01',
                                    'desde=2024-01-01&hasta=2026-01-02'])
    def test_basura_es_400(self, client, db, jwt_token_admin, qs):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        assert client.get(f'/api/analitica/salud?{qs}', headers=h).status_code == 400, qs

    def test_forma(self, client, db, jwt_token_admin, almacen):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.get(f'/api/analitica/salud?almacen_id={almacen.id}', headers=h)
        assert r.status_code == 200
        d = r.get_json()
        assert set(d) >= {'global', 'resumen', 'fuentes', 'crons', 'auditoria', 'cola_siesa',
                          'cobertura_claves', 'meta'}
        assert d['meta']['almacen_id'] == almacen.id
        assert (date.fromisoformat(d['meta']['hasta']) - date.fromisoformat(d['meta']['desde'])).days == 29
        assert 'La web y el worker' in d['crons']['nota']

    def test_patrones_validan(self, client, db, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        for qs in ('accion=BORRAR', 'usuario_id=x', 'entidad=a;drop', 'desde=mal'):
            assert client.get(f'/api/analitica/bitacora/patrones?{qs}', headers=h).status_code == 400, qs
        # La bitácora no tiene FKs: un almacén borrado sigue siendo filtro válido.
        assert client.get('/api/analitica/bitacora/patrones?almacen_id=99999', headers=h).status_code == 200

    def test_bitacora_no_ignora_un_filtro_ilegible(self, client, db, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        for campo in ('almacen_id', 'usuario_id', 'entidad_id'):
            assert client.get(f'/api/analitica/bitacora?{campo}=abc', headers=h).status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# Bitácora legible y patrones
# ─────────────────────────────────────────────────────────────────────────────

class TestBitacoraLegible:

    def test_la_frase(self, client, db, jwt_token_admin, usuario_admin, almacen, producto, ub_picking):
        from app.models.picking import TareaPicking
        from app.services.bitacora import registrar_accion
        t = TareaPicking(codigo='PK-123', producto_id=producto.id, cantidad_solicitada=5,
                         ubicacion_id=ub_picking.id, almacen_id=almacen.id, tipo_documento='PEDIDO',
                         referencia_documento='PD1502', pedido_clave='003-PD-1502')
        db.session.add(t)
        db.session.flush()
        registrar_accion('CANCELAR', t, usuario_id=usuario_admin.id, motivo='cliente anuló',
                         antes={'estado': 'PENDIENTE'}, despues={'estado': 'CANCELADO'})
        registrar_accion('ELIMINAR', 'TareaPicking', 999, entidad_codigo='PK-9',
                         antes={'referencia_documento': 'PD77', 'tipo_documento': 'PEDIDO',
                                'cantidad_recogida': 3})
        registrar_accion('REINTENTAR', 'SiesaJob', 5, usuario_id=777)
        db.session.commit()
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        acc = client.get('/api/analitica/bitacora', headers=h).get_json()['acciones']
        por = {a['entidad_codigo'] or a['entidad']: a for a in acc}
        c = por['PK-123']
        assert c['frase'] == 'Admin Test canceló el picking PK-123 del pedido 003-PD-1502'
        assert c['almacen_nombre'] == almacen.nombre
        assert c['cambios'] == [{'campo': 'estado', 'antes': 'PENDIENTE', 'despues': 'CANCELADO'}]
        assert c['hora_bogota'] and len(c['hora_bogota']) == 5
        e = por['PK-9']
        assert e['frase'] == 'El sistema eliminó el picking PK-9 del pedido PD77'
        assert {'campo': 'cantidad recogida', 'antes': '3', 'despues': '(borrado)'} in e['cambios']
        r = por['SiesaJob']
        assert r['frase'].startswith('Usuario #777 (ya no existe) reintentó el envío a Siesa')

    def test_patrones(self, client, db, jwt_token_admin, usuario_admin, usuario):
        from app.models.bitacora import BitacoraAccion
        from app.services.bitacora import registrar_accion
        from app.utils.fecha import dia_operativo
        filas = [registrar_accion('CANCELAR', 'TareaPicking', i, usuario_id=usuario_admin.id,
                                  motivo='m' if i else None) for i in range(3)]
        filas.append(registrar_accion('EDITAR', 'Ubicacion', 1, usuario_id=usuario.id))
        filas.append(registrar_accion('LIQUIDAR', 'RutaDespacho', 1))
        db.session.commit()
        # 15:30 UTC = 10:30 Bogotá; 01:10 UTC = 20:10 Bogotá del día anterior.
        hoy = dia_operativo()
        for f, hora in zip(filas, [datetime.combine(hoy, datetime.min.time()) + timedelta(hours=15, minutes=30)] * 3
                           + [datetime.combine(hoy, datetime.min.time()) + timedelta(hours=25, minutes=10)] * 2):
            BitacoraAccion.query.filter_by(id=f.id).update({'ocurrido_en': hora})
        db.session.commit()
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        p = client.get('/api/analitica/bitacora/patrones', headers=h).get_json()
        assert p['total'] == 5 and p['sin_motivo'] == 2, 'LIQUIDAR no pide motivo'
        assert p['sin_motivo_base'] == 4
        assert p['por_persona'][0] == {'usuario_id': usuario_admin.id, 'nombre': 'Admin Test', 'n': 3,
                                       'acciones': {'CANCELAR': 3}}
        assert {x['nombre'] for x in p['por_persona']} == {'Admin Test', 'Operario Test', 'El sistema'}
        assert p['por_accion'][0] == {'accion': 'CANCELAR', 'verbo': 'canceló', 'n': 3}
        horas = {x['hora']: x['n'] for x in p['por_hora']}
        assert horas[10] == 3 and horas[20] == 2 and sum(horas.values()) == 5
        assert p['por_hora_n'] == 5
        # Filtrar por persona no encoge los selectores.
        p2 = client.get(f'/api/analitica/bitacora/patrones?usuario_id={usuario.id}', headers=h).get_json()
        assert p2['total'] == 1
        assert len(p2['opciones']['personas']) == 2
        assert [x['n'] for x in p2['por_hora'] if x['n']] == [1]


# ─────────────────────────────────────────────────────────────────────────────
# Las pantallas — render real en Node con util.js real
# ─────────────────────────────────────────────────────────────────────────────

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], modo = args[1] || '';
const llamadas = [];
const ctx = { console, URLSearchParams, document: { getElementById: () => null }, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
// Shell mínimo con las firmas del contrato (analitica.js lo escribe otro agente).
vm.runInContext(`
  async function anCargarPanel(o) { const d = await o.pedir(); o.el.innerHTML = o.html(d); return d; }
  function anNum(n) { return n === null || n === undefined ? 'sin dato' : String(n); }
  function anPesos(n) { return n === null || n === undefined ? 'sin dato' : '$' + n; }
  function anPct(x, n) { return n ? Math.round(x * 100) + ' % de ' + n : '—'; }
  function anFrescura(m) { return '<div class="an-frescura">' + esc((m && m.calculado_en) || '') + '</div>'; }
  function anFiltros() { return { almacen_id: '3', desde: '2026-09-01', hasta: '2026-09-24' }; }
`, ctx);
ctx.get = async (url) => { llamadas.push(url); return ctx.__respuestas[url.split('?')[0]]; };
vm.runInContext(fs.readFileSync(base + '/analitica_salud.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/analitica_bitacora.js', 'utf8'), ctx);
if (modo === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
const X = '<img src=x onerror=alert(1)>';
const fuente = (i, nivel, veredicto) => ({ clave: 'f' + i, nombre: X, veredicto, veredicto_texto: X, nivel,
  critica: i === 0, motivo: X, que_hacer: X, ultima_actualizacion: X, completa: false, alimenta: X,
  cron: { tag: X, proceso: 'worker', en_este_proceso: false, nota: X },
  detalle: { bodegas: [{ bodega: X, actualizada_utc: X, filas: 3, atrasada: true }], problemas: [{ titulo: X }] } });
const salud = (veredicto) => ({
  global: { veredicto, texto: X, razones: [{ nivel: 'critico', texto: X, que_hacer: X }] },
  resumen: { fuentes_al_dia: 1, fuentes_total: 2, bloqueantes: 2, avisos: 1, jobs_fallidos: 1,
             cobertura_clave_pct: 0.82, cobertura_clave_n: 340 },
  fuentes: [fuente(0, 'critico', 'SIN_DATOS'), fuente(1, 'ok', 'AL_DIA')],
  crons: { rol_de_este_proceso: X, activos: [X], omitidos: [X], nota: X },
  auditoria: { nivel: 'critico', bloqueantes: 2, avisos: 1, error: X, nota: X, flujos_no_evaluados: [X],
    tope: { texto: X }, desde_cache: true, calculado_utc: X,
    por_flujo: [{ flujo: X, invariantes: 3, bloqueantes: 2, avisos: 1,
      peores: [{ codigo: X, severidad: 'BLOQUEA', consecuencia: X, total: 2, ejemplos: [X] }] }] },
  cola_siesa: { nivel: 'critico', texto: X, que_hacer: X, pendientes: 4, fallidos_por_tipo: [{ tipo: X, n: 1 }] },
  cobertura_claves: { nivel: 'advertencia', texto: X, que_hacer: X,
    picking: { n: 300, pct: 0.8, sin_clave: 60 }, packing: { n: 40, pct: 1, sin_clave: 0 } },
  meta: { calculado_en: X },
});
const accion = { accion: 'CANCELAR', entidad: X, entidad_codigo: X, frase: X, motivo: X, dia_operativo: X,
  hora_bogota: X, almacen_nombre: X, origen: X, cambios: [{ campo: X, antes: X, despues: X }], cambios_omitidos: 2 };
const patrones = { total: 4, sin_motivo: 1, por_hora_n: 4,
  por_persona: [{ usuario_id: 7, nombre: X, n: 3 }, { usuario_id: null, nombre: X, n: 1 }],
  por_accion: [{ accion: 'CANCELAR', verbo: X, n: 4 }],
  por_hora: Array.from({ length: 24 }, (_, h) => ({ hora: h, n: h === 10 ? 4 : 0 })),
  opciones: { acciones: [{ accion: 'CANCELAR', verbo: X }], entidades: [{ entidad: X, nombre: X, n: 4 }],
              personas: [{ usuario_id: 7, nombre: X, n: 3 }] },
  tope: { truncado: true, filas_por_hora: 20000 },
  meta: { calculado_en: X, fuentes: { bitacora_acciones: { nota: X } } } };
ctx.__respuestas = {
  '/api/analitica/salud': salud('NO_CONFIABLE'),
  '/api/analitica/bitacora': { acciones: [accion, accion], total: 5, vocabulario: ['CANCELAR'] },
  '/api/analitica/bitacora/patrones': patrones,
};
(async () => {
  const elS = { innerHTML: '' }, elB = { innerHTML: '' };
  await vm.runInContext('anSaludCargar', ctx)(elS, vm.runInContext('anFiltros', ctx)());
  vm.runInContext('anSaludAbrir', ctx)('auditoria');
  const saludAud = elS.innerHTML;
  vm.runInContext('anSaludAbrirFuente', ctx)(0);
  const saludFuente = elS.innerHTML;
  vm.runInContext('anSaludAbrir', ctx)('cola');
  const saludCola = elS.innerHTML;
  await vm.runInContext('anBitacoraCargar', ctx)(elB, vm.runInContext('anFiltros', ctx)());
  vm.runInContext('anBitAbrir', ctx)(1);
  const bit = elB.innerHTML;
  const sC = salud('CONFIABLE'); sC.global.razones = [];
  const confiable = vm.runInContext('anSaludHtml', ctx)(sC, null);
  const html = saludAud + saludFuente + saludCola + bit;
  const handlers = [...new Set([...html.matchAll(/on(?:click|change)="([A-Za-z_$][\w$]*)\(/g)].map(m => m[1]))];
  const sinDefinir = handlers.filter(h => typeof vm.runInContext('typeof ' + h + ' === "function" ? ' + h + ' : null', ctx) !== 'function');
  const anchos = [...html.matchAll(/(?:^|[;"\s])(?:min-)?width:\s*(\d+)px/g)].map(m => +m[1]);
  const chicos = [...html.matchAll(/font-size:\s*(\d+)px/g)].map(m => +m[1]).filter(n => n < 12);
  console.log(JSON.stringify({
    crudos: (html.match(/<img/g) || []).length,
    escapados: (html.match(/&lt;img/g) || []).length,
    llamadas, handlers, sinDefinir, anchoMax: Math.max(0, ...anchos), chicos,
    pildoraCritica: saludAud.includes('badge-red'),
    confiableVerde: confiable.slice(0, confiable.indexOf('kpi-grid')).includes('badge-green')
      && !confiable.slice(0, confiable.indexOf('kpi-grid')).includes('badge-red'),
    fraseVisible: bit.includes('motivo'), pct: saludAud.includes('82 % de 340'),
    cambios: bit.includes('→'), verMas: bit.includes('anBitMas()'),
  }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _render(modo=''):
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa), modo],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLasPantallas:

    def test_ningun_texto_llega_crudo(self):
        r = _render()
        assert r['crudos'] == 0, r
        assert r['escapados'] >= 40, f'piso: el arnés dejó de pintar ({r})'

    def test_el_arnes_muerde_con_esc_roto(self):
        assert _render('esc-roto')['crudos'] >= 40

    def test_piden_con_los_filtros_comunes(self):
        ll = _render()['llamadas']
        salud = [u for u in ll if u.startswith('/api/analitica/salud?')]
        assert salud and 'desde=2026-09-01' in salud[0] and 'hasta=2026-09-24' in salud[0] \
            and 'almacen_id=3' in salud[0]
        assert any(u.startswith('/api/analitica/bitacora?') and 'per_page=50' in u for u in ll)
        assert any(u.startswith('/api/analitica/bitacora/patrones?') and 'almacen_id=3' in u for u in ll)

    def test_cada_control_llama_una_funcion_que_existe(self):
        r = _render()
        assert r['sinDefinir'] == [], r
        assert {'anSaludAbrir', 'anSaludAbrirFuente', 'anBitAbrir', 'anBitFiltro',
                'anBitPorPersona', 'anBitMas'} <= set(r['handlers']), r['handlers']

    def test_el_estado_se_ve_en_forma_y_con_denominador(self):
        r = _render()
        assert r['pildoraCritica'] and r['confiableVerde']
        assert r['pct'] and r['cambios'] and r['verMas']

    def test_cabe_en_un_celular(self):
        r = _render()
        assert r['anchoMax'] <= 360, r
        assert r['chicos'] == []


class TestLaBitacoraTienePantalla:

    def test_salio_de_la_deuda_sin_ui(self):
        from tests.test_frontend_integrity import DEUDA_SIN_UI
        assert '/api/analitica/bitacora' not in DEUDA_SIN_UI
