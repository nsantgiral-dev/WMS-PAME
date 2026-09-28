"""
Validación crítica 2026-09-26 (inventario, cartera-cupo, procesos automáticos)
sobre integra-voz d9da743d. Cada test reproduce un hallazgo y está marcado
xfail(strict=True): hoy falla porque el defecto existe; cuando se arregle, el
xfail estricto se pone rojo y obliga a quitar la marca.
"""
from datetime import datetime, timedelta

import pytest

from app.services import cartera_service as cs
from tests.test_cartera_retencion import (NIT, _codigos, _historia, _inicio,  # noqa: F401
                                          _tarea, fake)


# ═════════════════════════════════════════════════════════════════════════════
# P1 · Cupo: la FE que el 142943 emite NO deja fe_consec → consume para siempre
# ═════════════════════════════════════════════════════════════════════════════

def _despachada_sin_fe_anotada(db, almacen, clave, *, hace_dias, valor):
    """El camino NORMAL del cierre: `_persistir_resultado` pone DESPACHADO y
    `fe_confirmada_at`, pero el 142943 no devuelve el consecutivo y
    `_anotar_fe_encontrada` solo lo anota en el camino idempotente → la tarea
    queda con `fe_consec = NULL` hasta que alguien llame `resolver_fe`."""
    t = _tarea(db, almacen, clave, estado='DESPACHADO', valor=valor, cond='C04')
    t.siesa_triggered = True
    t.rm_tipo, t.rm_consec = 'RM', '1500'
    t.fecha_despachado = datetime.utcnow() - timedelta(days=hace_dias)
    t.fe_confirmada_at = t.fecha_despachado
    db.session.commit()
    assert t.fe_consec is None
    return t


class TestCupoConFeSinAnotar:

    def test_fe_pagada_sin_consec_anotado_no_consume(self, db, fake, almacen):
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-501', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-502', lineas=(('SKU1', 10, 300_000),))
        _despachada_sin_fe_anotada(db, almacen, '003-PD-501', hace_dias=20, valor=900_000)
        # la cartera abierta está vacía: el cliente pagó esa FE hace días
        paso = _inicio('PD502', 'PD', '502', co='003', almacen_id=almacen.id)
        assert paso.pasa, paso.evaluacion.get('motivos')

    def test_fe_abierta_sin_consec_anotado_no_se_cuenta_dos_veces(self, db, fake, almacen):
        fake.cliente(cupo=2_000_000)
        fake.factura(saldo=900_000, vence_en=10, consec='951')   # al día
        _historia(db, '003-PD-511', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-512', lineas=(('SKU1', 10, 900_000),))
        _despachada_sin_fe_anotada(db, almacen, '003-PD-511', hace_dias=3, valor=900_000)
        paso = _inicio('PD512', 'PD', '512', co='003', almacen_id=almacen.id)
        # cupo 2.0M − saldo 0.9M − este 0.9M = +0.2M → debe pasar
        assert paso.pasa, (paso.evaluacion.get('motivos'), paso.evaluacion.get('consumo_wms'))


# ═════════════════════════════════════════════════════════════════════════════
# P1 · reabrir_picking con la caja del pedido ya DESPACHADA
# ═════════════════════════════════════════════════════════════════════════════

class TestReabrirConCajaDespachada:

    def test_no_nace_una_tarea_que_ninguna_caja_puede_llevar(self, db, almacen):
        from app.models.picking import TareaPicking
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        from tests.test_inventario_no_se_resta_sin_destino import _escenario, _en_hueco
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7101')
        caja = PackingService.crear_desde_picking(
            tareas_picking_ids=[t.id], numero_pedido_siesa='PD7101',
            almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa='7101')
        # la caja salió con las 7: remisión y factura
        caja.estado = 'DESPACHADO'
        caja.siesa_triggered = True
        caja.rm_tipo, caja.rm_consec = 'RM', '1601'
        db.session.commit()
        antes = _en_hueco(db, ub, p).cantidad                     # 13

        r = PickingService.reabrir_picking(t.id, sup.id, motivo='llegó reposición')
        if r.estado == 'PENDIENTE' and r.id != t.id:
            PickingService.iniciar_picking(r.id, op.id)
            PickingService.confirmar_picking(r.id, r.cantidad_solicitada, op.id)
        # Ninguna caja puede llevar esas 3: la del pedido está DESPACHADA y
        # `crear_desde_picking` se niega a abrir otra. Si salieron del hueco,
        # salieron sin destino.
        assert _en_hueco(db, ub, p).cantidad == antes, (
            'se restaron unidades que ninguna caja puede llevar')
        assert TareaPicking.query.filter_by(referencia_documento='PD7101',
                                            estado='PENDIENTE').count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# P1 · Crash entre el pre-flag y el POST: ENTRADA_OC se cierra «idempotente»
# ═════════════════════════════════════════════════════════════════════════════

class TestCrashEntrePreflagYPost:

    def test_entrada_oc_con_bandera_y_sin_respuesta_no_se_da_por_hecha(self, db, almacen):
        from app.models.recepcion import RecepcionMercancia
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        rec = RecepcionMercancia(codigo='REC-VAL-1', numero_oc_siesa='OC99',
                                 almacen_id=almacen.id, estado='CONFIRMADA')
        # El proceso murió con el POST en vuelo (deploy): la bandera quedó
        # puesta y no hay respuesta guardada.
        rec.siesa_triggered = True
        rec.siesa_triggered_at = datetime.utcnow() - timedelta(minutes=15)
        rec.siesa_response = None
        db.session.add(rec)
        db.session.commit()
        job = SiesaJob.encolar('ENTRADA_OC', {'recepcion_id': rec.id},
                               referencia_tipo='RecepcionMercancia', referencia_id=rec.id)
        job.intentos = 1        # lo reseteó el barrido de PROCESANDO > 10 min
        db.session.commit()
        with pytest.raises(Exception):
            sjs._ejecutar_job(job)       # debería levantar «sin verificar», no cerrar


# ═════════════════════════════════════════════════════════════════════════════
# P1 · Carga física: «completo» = alguna pasada sin páginas perdidas
# ═════════════════════════════════════════════════════════════════════════════

class TestCargaFisicaCompleto:

    def test_una_sola_pasada_no_es_dato_completo(self, db, monkeypatch):
        from app.services import inventario_siesa_service as inv
        pasadas = iter([
            {'NB1': {f'SKU{i}': {'existencia': 5.0, 'comprometido': 0.0,
                                'salida_sin_conf': 0.0, 'descripcion': '', 'unidad': 'UND'}
                     for i in range(80)}},
            None,   # 429 x3 en una página → pasada descartada
            None,
        ])
        monkeypatch.setattr(inv, '_descargar_una_pasada_custom', lambda: next(pasadas))
        monkeypatch.setattr(inv, '_cache_inventario_multibodega',
                            {'data': None, 'ts': None, 'degradado': False,
                             'bodegas_frescas': frozenset()})
        inv._descargar_inventario_siesa_raw(forzar=True)
        # Un dict sin prueba de completitud no autoriza a escribir: el bulk
        # zero de la carga pondría en 0 lo que la lectura no trajo. Desde el
        # 2026-09-27 la prueba es UNA lectura de a 100 con `LineaRegistro`
        # 1…N contra el total declarado (`test_existencias_verdaderas.py`).
        assert inv.fuente_para_escribir('NB1') != ''


# ═════════════════════════════════════════════════════════════════════════════
# P2 · 🩺 Salud: una carga física que no corre hace días se ve «OK»
# ═════════════════════════════════════════════════════════════════════════════

class TestSaludCargaVieja:

    def test_carga_de_hace_cinco_dias_no_es_ok(self, db):
        from app.models.registro_sync import RegistroSync
        from app.services import analitica_salud
        hace = datetime.utcnow() - timedelta(days=5)
        for tipo in ('stock', 'stock_ns1', 'stock_nc1'):
            db.session.add(RegistroSync(tipo=tipo, inicio=hace, fin=hace, ok=True))
        db.session.commit()
        assert analitica_salud.carga_fisica()['nivel'] != analitica_salud.OK


# ═════════════════════════════════════════════════════════════════════════════
# P2 · stock_siesa: el merge BD+API re-sella «fresco» lo que Siesa no trajo
# ═════════════════════════════════════════════════════════════════════════════

class TestFrescuraNoSeReSellaSinDato:
    """P2-9 (2026-09-26) y P0-1 de compras (2026-09-27): un SKU que Siesa no
    reportó no se re-sella con su valor viejo. Sin prueba de completitud no
    se toca; con la lectura completa queda en CERO con fecha — «Siesa no lo
    tiene» es un dato de hoy, su último positivo no."""

    def _sembrar(self, db):
        from app.models.stock_siesa import StockSiesa
        vieja = datetime.utcnow() - timedelta(days=10)
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='AGOTADO', existencia=40,
                                  comprometido=0, salida_sin_conf=0, updated_at=vieja))
        db.session.commit()
        fila = {f'SKU{i}': {'existencia': 5.0, 'comprometido': 0.0, 'salida_sin_conf': 0.0,
                            'descripcion': '', 'unidad': 'UND'} for i in range(80)}
        return {'NB1': fila}

    def _correr(self, monkeypatch, lectura):
        from app.services import inventario_siesa_service as inv
        monkeypatch.setattr(inv, '_descargar_una_pasada_custom', lambda: lectura)
        monkeypatch.setattr(inv, '_cache_inventario_multibodega',
                            {'data': None, 'ts': None, 'degradado': False,
                             'bodegas_frescas': frozenset(), 'completa': False, 'motivo': ''})
        monkeypatch.setattr(inv, '_cache_inventario_siesa', {})
        monkeypatch.setattr('time.sleep', lambda s: None)
        inv._descargar_inventario_siesa_raw(forzar=True)

    def test_sin_prueba_de_completitud_conserva_su_fecha(self, db, monkeypatch):
        from app.models.stock_siesa import StockSiesa
        self._correr(monkeypatch, self._sembrar(db))
        db.session.expire_all()
        r = StockSiesa.query.filter_by(bodega='NB1', codigo_siesa='AGOTADO').first()
        assert r.existencia == 40 and r.updated_at < datetime.utcnow() - timedelta(days=5), (
            'la fila que Siesa no reportó quedó sellada como recién leída')

    def test_con_lectura_completa_queda_en_cero_con_fecha(self, db, monkeypatch):
        from app.models.stock_siesa import StockSiesa
        from app.services import inventario_siesa_service as inv
        self._correr(monkeypatch, inv.ResultadoDescarga(self._sembrar(db), completa=True))
        db.session.expire_all()
        r = StockSiesa.query.filter_by(bodega='NB1', codigo_siesa='AGOTADO').first()
        assert r.existencia == 0 and r.ausente_desde is not None, (
            'el agotado conservó su último positivo')
