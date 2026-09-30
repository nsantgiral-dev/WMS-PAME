"""
La caja del conductor: una liquidación = arqueo + documentos (m051liqcaja,
2026-09-27).

La clase: **se da por entregada plata que nadie contó.** Hasta acá «liquidar»
era un clic: el WMS cuadraba cobros contra recibos y nunca contra la plata
física; un faltante quedaba sin dueño, o se tapaba corrigiendo el cobro del
cliente, o se ajustaba al peso. Y el envío a Siesa era un segundo paso que se
olvidaba (5 cobros de QA, $235.402, sin recibo nunca encolado).

Cubre, por comportamiento y por la puerta HTTP:
- el esperado (efectivo declarado, sin los otros medios), los gastos del
  recaudo (se aceptan o rechazan, nunca se descuentan solos);
- la diferencia exige motivo y queda a cargo del conductor;
- el conductor confirma u objeta en su teléfono; quien liquida declara que no
  confirmó; anular libera rutas y gastos;
- `liquidar_ruta` exige el acta (el admin fuerza con motivo) y encola;
- efectivo en poder, reconciliación (cuarta columna), diferencias por
  conductor, avisos del resumen diario;
- trinquetes AST (con meta-tests y piso): una sola función decide el
  efectivo de una parada; solo la política escribe las actas; liquidar
  pregunta por el acta antes de marcar.
"""
import ast
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_cartera_retencion import _jwt, _usuario

RAIZ = Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════════════════
# El mundo
# ═════════════════════════════════════════════════════════════════════════

def _conductor(db, con_usuario=True):
    from app.models.conductor import Conductor
    u = _usuario(db, 'conductor') if con_usuario else None
    c = Conductor(nombre=f'Cond {uuid.uuid4().hex[:4]}', cedula=f'C{uuid.uuid4().hex[:8]}',
                  activo=True, usuario_id=u.id if u else None)
    db.session.add(c)
    db.session.commit()
    return c, u


def _ruta(db, almacen, conductor, paradas, estado='ENTREGADA', financiero='PENDIENTE'):
    """`paradas`: [(estado_entrega, forma_pago, monto, valor_factura)]."""
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    from app.utils.fecha import dia_operativo
    ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado=estado,
                        estado_financiero=financiero, fecha_programada=dia_operativo(),
                        fecha_entregada=datetime.utcnow())
    db.session.add(ruta)
    db.session.flush()
    recaudos = []
    for est, fp, monto, vf in paradas:
        t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                         almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa=1, numero_pedido_siesa=f'PD{uuid.uuid4().hex[:10]}',
                         cliente='Cliente <b>X</b>', valor_factura=vf,
                         cond_pago='C02', cobro_contraentrega=True, dias_credito=1,
                         clasif_origen='MAESTRO')
        db.session.add(t)
        db.session.flush()
        db.session.add(Bulto(tarea_id=t.id, codigo_barras=f'B-{uuid.uuid4().hex[:8]}',
                             tipo='Caja', numero=1, total=1, estado='ENTREGADO',
                             ruta_despacho_id=ruta.id))
        r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega=est,
                           forma_pago=fp, monto_cobrado=monto, cobro_contraentrega=True)
        db.session.add(r)
        db.session.flush()
        recaudos.append(r)
    db.session.commit()
    return ruta, recaudos


def _gasto(db, conductor_usuario, valor=180000, origen='efectivo_conductor', con_foto=False):
    from flota.adaptadores.modelos import Foto, Gasto, LecturaOdometro
    from app.models.vehiculo import Vehiculo
    v = Vehiculo(placa=f'G{uuid.uuid4().hex[:5]}', tipo='camion', activo=True)
    db.session.add(v)
    db.session.flush()
    lec = LecturaOdometro(vehiculo_id=v.id, valor_km=1000, ts=datetime.utcnow(),
                          origen='tanqueo', autor_usuario_id=conductor_usuario.id,
                          confianza='alta')
    db.session.add(lec)
    db.session.flush()
    g = Gasto(vehiculo_id=v.id, categoria='combustible', fecha=date.today(), valor=valor,
              lectura_id=lec.id, proveedor='Terpel', periodo_desde=date.today(),
              periodo_hasta=date.today(), origen_costo=origen,
              registrado_por_usuario_id=conductor_usuario.id)
    db.session.add(g)
    db.session.commit()
    return g


def recibir_caja_de(db, ruta_id, usuario_id=None):
    """Para los tests que liquidan: quien liquida recibe la caja del conductor
    de la ruta, contando exactamente lo esperado (y aceptando sus gastos).
    Desde m051liqcaja una ruta con efectivo no se liquida sin su acta."""
    from app.models.ruta_despacho import RutaDespacho
    from app.models.usuario import Usuario
    from app.services import caja_conductor as cc
    from app.models.conductor import Conductor
    ruta = db.session.get(RutaDespacho, ruta_id)
    if db.session.get(Conductor, ruta.conductor_id) is None:
        # Algunos mundos de prueba ponen un id de usuario como conductor (SQLite
        # no exige la FK): se completa el maestro para poder recibirle la caja.
        db.session.add(Conductor(id=ruta.conductor_id, nombre='Conductor de prueba',
                                 cedula=f'P{uuid.uuid4().hex[:8]}', activo=True))
        db.session.commit()
    if usuario_id is None:
        u = Usuario.query.filter_by(rol='admin').first() or _usuario(db, 'admin')
        usuario_id = u.id
    e = cc.esperado_de_entrega(ruta.conductor_id)
    if not e['rutas'] and not e['gastos']:
        return None
    return cc.registrar_acta(ruta.conductor_id, e['efectivo'], usuario_id,
                             gastos=[{'gasto_id': g['gasto_id'], 'aceptado': True}
                                     for g in e['gastos']])


# ═════════════════════════════════════════════════════════════════════════
# 1 · Lo que se esperaba
# ═════════════════════════════════════════════════════════════════════════

class TestElEsperado:

    def test_solo_el_efectivo_se_cuenta(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 100000, 100000),
                               ('ENTREGADO', 'TRANSFERENCIA_BBVA', 50000, 50000),
                               ('PARCIAL', 'EFECTIVO', 30000, 60000),
                               ('RECHAZADO', 'EFECTIVO', 0, 40000),
                               ('ENTREGADO', 'TARJETA', 20000, 20000)])
        e = cc.esperado_de_entrega(c.id)
        assert e['efectivo'] == 130000
        assert e['otros_medios']['BANCARIO'] == {'n': 1, 'valor': 50000.0,
                                                 'por_verificar': 1, 'no_encontradas': 0}
        assert e['otros_medios']['TARJETA']['valor'] == 20000
        assert len(e['paradas_efectivo']) == 2

    def test_un_rechazo_con_forma_pago_vieja_no_es_efectivo(self, db, almacen):
        """`forma_pago_de`: un RECHAZADO que conservó EFECTIVO no puso plata."""
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _, [r] = _ruta(db, almacen, c, [('RECHAZADO', 'EFECTIVO', 5000, 5000)])
        assert cc.efectivo_de_recaudo(r) == 0

    def test_una_ruta_en_camino_o_ya_con_acta_no_entra(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 10000, 10000)], estado='EN_TRANSITO')
        assert cc.esperado_de_entrega(c.id)['efectivo'] == 0
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 7000, 7000)])
        cc.registrar_acta(c.id, 7000, _usuario(db, 'liquidador').id)
        assert cc.esperado_de_entrega(c.id)['rutas'] == []

    def test_los_gastos_del_recaudo_son_del_conductor(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        otro, u2 = _conductor(db)
        _gasto(db, u, 180000)
        _gasto(db, u, 50000, origen='tarjeta_convenio')
        _gasto(db, u2, 90000)
        e = cc.esperado_de_entrega(c.id)
        assert [g['valor'] for g in e['gastos']] == [180000.0]
        assert e['gastos_total'] == 180000

    def test_cobro_menor_que_la_factura_se_informa_no_se_suma(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 80000, 100000)])
        e = cc.esperado_de_entrega(c.id)
        assert e['efectivo'] == 80000
        [m] = e['cobro_menor_que_factura']
        assert m['esperado'] == 100000 and m['cobrado'] == 80000


# ═════════════════════════════════════════════════════════════════════════
# 2 · El acta
# ═════════════════════════════════════════════════════════════════════════

class TestElActa:

    def test_cuadra_sin_motivo(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 100000, 100000)])
        a = cc.registrar_acta(c.id, 100000, _usuario(db, 'liquidador').id)
        assert float(a.diferencia) == 0 and a.estado == 'PENDIENTE_CONDUCTOR'
        db.session.refresh(ruta)
        assert ruta.entrega_caja_id == a.id

    def test_faltante_exige_motivo_y_queda_a_cargo(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 2100000, 2100000)])
        liq = _usuario(db, 'liquidador')
        with pytest.raises(ValueError, match='diferencia_sin_motivo: faltan \\$100,000'):
            cc.registrar_acta(c.id, 2000000, liq.id)
        a = cc.registrar_acta(c.id, 2000000, liq.id, motivo_diferencia='Dice que dio vueltas')
        assert float(a.diferencia) == -100000
        [d] = cc.diferencias_por_conductor()
        assert d['faltante'] == 100000 and d['actas_con_diferencia'] == 1

    def test_el_gasto_aceptado_cuenta_y_el_rechazado_no(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 1200000, 1200000)])
        g = _gasto(db, u, 180000)
        liq = _usuario(db, 'liquidador')
        # Sin decidir el gasto no se firma.
        with pytest.raises(ValueError, match='sin decidir'):
            cc.registrar_acta(c.id, 1020000, liq.id)
        with pytest.raises(ValueError, match='motivo'):
            cc.registrar_acta(c.id, 1020000, liq.id,
                              gastos=[{'gasto_id': g.id, 'aceptado': False}])
        a = cc.registrar_acta(c.id, 1020000, liq.id,
                              gastos=[{'gasto_id': g.id, 'aceptado': True}])
        assert float(a.diferencia) == 0 and float(a.gastos_aceptados) == 180000
        # Legalizado una vez: ya no aparece.
        assert cc.gastos_por_legalizar(c) == []

    def test_gasto_rechazado_deja_el_faltante(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 1200000, 1200000)])
        g = _gasto(db, u, 180000)
        a = cc.registrar_acta(c.id, 1020000, _usuario(db, 'liquidador').id,
                              gastos=[{'gasto_id': g.id, 'aceptado': False,
                                       'motivo': 'Sin recibo legible'}],
                              motivo_diferencia='Tanqueo sin soporte')
        assert float(a.diferencia) == -180000

    def test_el_esperado_cambio_mientras_contaba(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 50000, 50000)])
        with pytest.raises(ValueError, match='cambió mientras contaba'):
            cc.registrar_acta(c.id, 40000, _usuario(db, 'liquidador').id, esperado_visto=40000)

    def test_contado_negativo_o_texto(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 50000, 50000)])
        liq = _usuario(db, 'liquidador')
        for malo in (-1, 'mucho', None):
            with pytest.raises(ValueError):
                cc.registrar_acta(c.id, malo, liq.id)

    def test_sin_nada_que_recibir(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        with pytest.raises(ValueError, match='no hay caja'):
            cc.registrar_acta(c.id, 0, _usuario(db, 'liquidador').id)

    def test_la_base_exige_motivo_con_diferencia(self, db, almacen):
        """El CHECK: aunque un camino futuro se salte la política."""
        from sqlalchemy.exc import IntegrityError
        from app.models.entrega_caja import EntregaCaja
        c, _ = _conductor(db)
        db.session.add(EntregaCaja(conductor_id=c.id, dia=date.today(), estado='CONFIRMADA',
                                   esperado_efectivo=10, contado_efectivo=5, diferencia=-5,
                                   registrada_por_id=_usuario(db).id))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestLaRespuestaDelConductor:

    def _acta(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        return c, u, cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)

    def test_confirma_solo_el_suyo(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u, a = self._acta(db, almacen)
        _, otro = _conductor(db)
        with pytest.raises(PermissionError):
            cc.responder_conductor(a.id, otro.id, True)
        a = cc.responder_conductor(a.id, u.id, True)
        assert a.estado == 'CONFIRMADA'
        with pytest.raises(ValueError, match='ya tiene respuesta'):
            cc.responder_conductor(a.id, u.id, True)

    def test_objetar_exige_comentario(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u, a = self._acta(db, almacen)
        with pytest.raises(ValueError, match='por qué'):
            cc.responder_conductor(a.id, u.id, False)
        a = cc.responder_conductor(a.id, u.id, False, 'Entregué 35.000')
        assert a.estado == 'OBJETADA'

    def test_sin_confirmar_con_motivo_y_bitacora(self, db, almacen):
        from app.models.bitacora import BitacoraAccion
        from app.services import caja_conductor as cc
        c, u, a = self._acta(db, almacen)
        liq = _usuario(db, 'liquidador')
        with pytest.raises(ValueError):
            cc.marcar_sin_confirmar(a.id, liq.id, '  ')
        a = cc.marcar_sin_confirmar(a.id, liq.id, 'Se fue sin teléfono')
        assert a.estado == 'SIN_CONFIRMAR'
        assert BitacoraAccion.query.filter_by(entidad='EntregaCaja', entidad_id=a.id).count() == 1

    def test_anular_libera_rutas_y_gastos(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        g = _gasto(db, u, 10000)
        liq = _usuario(db, 'liquidador')
        a = cc.registrar_acta(c.id, 20000, liq.id,
                              gastos=[{'gasto_id': g.id, 'aceptado': True}])
        cc.anular_acta(a.id, liq.id, 'Contó mal')
        db.session.refresh(ruta)
        assert ruta.entrega_caja_id is None
        assert [x.id for x in cc.gastos_por_legalizar(c)] == [g.id]
        assert a.estado == 'ANULADA' and a.detalle['rutas'][0]['ruta_id'] == ruta.id
        assert cc.diferencias_por_conductor() == []

    def test_no_se_anula_con_una_ruta_liquidada(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        a = cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        ruta.estado_financiero = 'LIQUIDADA'
        db.session.commit()
        with pytest.raises(ValueError, match='ya liquidadas'):
            cc.anular_acta(a.id, _usuario(db).id, 'x')


# ═════════════════════════════════════════════════════════════════════════
# 3 · Liquidar exige el acta, y encola
# ═════════════════════════════════════════════════════════════════════════

class TestLiquidarExigeElActa:

    def test_sin_acta_no_se_liquida(self, db, almacen):
        from app.services.ruta_service import RutaService
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        with pytest.raises(ValueError, match='^caja_sin_acta:'):
            RutaService.liquidar_ruta(ruta.id, usuario_id=_usuario(db, 'liquidador').id,
                                      encolar_documentos=False)
        db.session.refresh(ruta)
        assert ruta.estado_financiero == 'PENDIENTE'

    def test_con_acta_se_liquida(self, db, almacen):
        from app.services import caja_conductor as cc
        from app.services.ruta_service import RutaService
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        a = cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        r = RutaService.liquidar_ruta(ruta.id, usuario_id=a.registrada_por_id,
                                      encolar_documentos=False)
        assert r['acta_caja_id'] == a.id
        assert r['ruta']['estado_financiero'] == 'LIQUIDADA'

    def test_una_ruta_sin_efectivo_no_pide_acta(self, db, almacen):
        from app.services.ruta_service import RutaService
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'TRANSFERENCIA_BBVA', 30000, 30000)])
        r = RutaService.liquidar_ruta(ruta.id, usuario_id=_usuario(db, 'liquidador').id,
                                      encolar_documentos=False)
        assert r['acta_caja_id'] is None and r['ruta']['estado_financiero'] == 'LIQUIDADA'

    def test_efectivo_que_entro_despues_del_acta(self, db, almacen):
        from app.services import caja_conductor as cc
        from app.services.ruta_service import RutaService
        c, _ = _conductor(db)
        ruta, [r] = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        r.monto_cobrado = 45000
        db.session.commit()
        with pytest.raises(ValueError, match='cambió después del acta'):
            RutaService.liquidar_ruta(ruta.id, usuario_id=1, encolar_documentos=False)

    def test_forzar_sin_acta_solo_admin_con_motivo(self, db, almacen):
        from app.models.bitacora import BitacoraAccion
        from app.services.ruta_service import RutaService
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        with pytest.raises(ValueError, match='Solo el administrador'):
            RutaService.liquidar_ruta(ruta.id, usuario_id=1, motivo_sin_acta='apuro',
                                      encolar_documentos=False)
        RutaService.liquidar_ruta(ruta.id, usuario_id=1, motivo_sin_acta='El conductor se accidentó',
                                  puede_liquidar_sin_acta=True, encolar_documentos=False)
        f = BitacoraAccion.query.filter_by(accion='FORZAR', entidad='RutaDespacho',
                                           entidad_id=ruta.id).one()
        assert f.despues['forzado'] == 'liquidacion_sin_acta_de_caja'

    def test_liquidar_encola_lo_que_esta_listo(self, db, almacen, monkeypatch):
        """Una acción: la liquidación llama al mismo encolado de «Enviar todo»."""
        from app.services import caja_conductor as cc
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService
        llamadas = []
        monkeypatch.setattr(LiquidacionService, 'liquidar_ruta_siesa',
                            staticmethod(lambda rid, admin_id=None: llamadas.append(rid) or
                                         {'rc_encolados': 1, 'errores': []}))
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        r = RutaService.liquidar_ruta(ruta.id, usuario_id=1)
        assert llamadas == [ruta.id] and r['siesa']['rc_encolados'] == 1

    def test_si_el_encolado_revienta_la_ruta_queda_liquidada(self, db, almacen, monkeypatch):
        from app.services import caja_conductor as cc
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService

        def _revienta(rid, admin_id=None):
            raise RuntimeError('Siesa no responde')
        monkeypatch.setattr(LiquidacionService, 'liquidar_ruta_siesa', staticmethod(_revienta))
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        r = RutaService.liquidar_ruta(ruta.id, usuario_id=1)
        assert r['ruta']['estado_financiero'] == 'LIQUIDADA'
        assert 'Siesa no responde' in r['siesa_error']
        assert [d['documentos'] for d in r['falta_en_siesa']] == [['RC']]


# ═════════════════════════════════════════════════════════════════════════
# 4 · Lecturas
# ═════════════════════════════════════════════════════════════════════════

class TestLasLecturas:

    def test_efectivo_en_poder_termina_con_el_acta(self, db, almacen):
        from app.services import caja_conductor as cc
        from app.services import senales_ruta as sr
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 5000, 5000)], estado='EN_TRANSITO')
        [f] = sr.efectivo_en_poder_por_conductor()
        assert f['efectivo'] == 35000
        cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        [f] = sr.efectivo_en_poder_por_conductor()
        assert f['efectivo'] == 5000     # la ruta en camino sigue afuera

    def test_la_cuarta_columna_de_la_reconciliacion(self, db, almacen):
        from app.services import caja_conductor as cc
        from app.services.reconciliacion_ruta import reconciliar
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 100000, 100000),
                                         ('ENTREGADO', 'TRANSFERENCIA_BBVA', 50000, 50000)])
        col = reconciliar(ruta.id)['columnas']['recaudo_verificado']
        assert col['capturado'] is False and col['valor'] is None
        cc.registrar_acta(c.id, 90000, _usuario(db, 'liquidador').id, motivo_diferencia='x')
        rec = reconciliar(ruta.id)
        col = rec['columnas']['recaudo_verificado']
        assert col['capturado'] is True
        assert col['valor'] == 90000            # 100.000 − faltante; la transferencia sin ver
        assert col['bancario_por_verificar'] == 1
        assert col['acta']['diferencia'] == -10000
        tramo = next(f for f in rec['fugas'] if f['tramo'] == 'recibo_sin_verificar')
        assert tramo['medible'] is True

    def test_la_diferencia_se_reparte_entre_rutas(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        r1, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 75000, 75000)])
        r2, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 25000, 25000)])
        cc.registrar_acta(c.id, 96000, _usuario(db, 'liquidador').id, motivo_diferencia='x')
        v1, v2 = cc.verificado_de_ruta(r1), cc.verificado_de_ruta(r2)
        assert (v1['diferencia'], v2['diferencia']) == (-3000, -1000)
        assert v1['prorrateado'] is True

    def test_avisos_del_resumen(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 100000, 100000)])
        a = cc.registrar_acta(c.id, 70000, _usuario(db, 'liquidador').id, motivo_diferencia='x')
        a.registrada_en = datetime.utcnow() - timedelta(hours=30)
        db.session.commit()
        lineas = cc.lineas_de_aviso()
        assert any('faltante a cargo del conductor' in x and '$30,000' in x for x in lineas)
        assert any('sin respuesta del conductor' in x for x in lineas)

    def test_el_resumen_diario_trae_las_lineas(self, db, almacen):
        from app.services import alertas_service
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 100000, 100000)])
        cc.registrar_acta(c.id, 70000, _usuario(db, 'liquidador').id, motivo_diferencia='x')
        hoy = datetime.utcnow()
        lineas = alertas_service.avisos_sin_canal(hoy - timedelta(days=1), hoy)
        assert any('faltante a cargo del conductor' in x for x in lineas)


class TestElCobroSinReciboAvisa:
    """P0-3: una ruta liquidada con un cobro cuyo recibo nunca se encoló se
    olvidaba (QA: 5 cobros, $235.402). El resumen diario lo dice."""

    def _liquidada_hace(self, db, almacen, horas):
        c, _ = _conductor(db)
        ruta, [r] = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)],
                          financiero='LIQUIDADA')
        ruta.liquidada_en = datetime.utcnow() - timedelta(hours=horas)
        db.session.commit()
        return ruta, r

    def test_pasadas_dos_horas_sin_recibo(self, db, almacen):
        from app.services import politica_cobro as pc
        ruta, r = self._liquidada_hace(db, almacen, 3)
        [x] = pc.cobros_sin_recibo_encolado()
        assert x['recaudo_id'] == r.id
        assert '1 cobro(s) de rutas liquidadas sin recibo de caja encolado ($30,000)' in \
            pc.lineas_de_aviso_rc()[0]

    def test_recien_liquidada_o_con_el_recibo_en_cola_no(self, db, almacen):
        import json
        from app.models.siesa_job import SiesaJob
        from app.services import politica_cobro as pc
        self._liquidada_hace(db, almacen, 1)
        _, r = self._liquidada_hace(db, almacen, 5)
        db.session.add(SiesaJob(tipo='RECIBO_CAJA', referencia_tipo='RecaudoEntrega',
                                referencia_id=r.id, payload=json.dumps({}), estado='PENDIENTE'))
        db.session.commit()
        assert pc.cobros_sin_recibo_encolado() == []


# ═════════════════════════════════════════════════════════════════════════
# 5 · Por la puerta HTTP
# ═════════════════════════════════════════════════════════════════════════

class TestPorHTTP:

    def test_recibir_la_caja(self, app, client, db, almacen):
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        liq = _usuario(db, 'liquidador')
        d = client.get('/api/rutas/caja/por-recibir', headers=_jwt(app, liq)).get_json()
        [e] = [x for x in d['conductores'] if x['conductor_id'] == c.id]
        assert e['efectivo'] == 30000 and d['permisos']['recibir_caja'] is True
        r = client.post(f'/api/rutas/caja/conductor/{c.id}/acta', headers=_jwt(app, liq),
                        json={'contado_efectivo': 30000, 'esperado_visto': 30000})
        assert r.status_code == 200, r.get_json()

    @pytest.mark.parametrize('rol', ['gerente', 'jefe_almacen', 'lider_cartera', 'conductor',
                                     'operario'])
    def test_solo_admin_y_liquidador_reciben(self, app, client, db, almacen, rol):
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        r = client.post(f'/api/rutas/caja/conductor/{c.id}/acta',
                        headers=_jwt(app, _usuario(db, rol)), json={'contado_efectivo': 30000})
        assert r.status_code == 403

    def test_el_gerente_ve_y_no_recibe(self, app, client, db, almacen):
        d = client.get('/api/rutas/caja/por-recibir',
                       headers=_jwt(app, _usuario(db, 'gerente'))).get_json()
        assert d['permisos']['recibir_caja'] is False

    def test_el_conductor_ve_su_caja_y_confirma(self, app, client, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        d = client.get('/api/rutas/caja/mi-resumen', headers=_jwt(app, u)).get_json()
        assert d['efectivo'] == 30000 and d['actas'] == []
        a = cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        d = client.get('/api/rutas/caja/mi-resumen', headers=_jwt(app, u)).get_json()
        assert [x['id'] for x in d['actas']] == [a.id] and d['efectivo'] == 0
        r = client.post(f'/api/rutas/caja/actas/{a.id}/responder', headers=_jwt(app, u),
                        json={'de_acuerdo': True})
        assert r.status_code == 200 and r.get_json()['acta']['estado'] == 'CONFIRMADA'

    def test_otro_conductor_no_responde_un_acta_ajena(self, app, client, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _, otro = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        a = cc.registrar_acta(c.id, 30000, _usuario(db, 'liquidador').id)
        r = client.post(f'/api/rutas/caja/actas/{a.id}/responder', headers=_jwt(app, otro),
                        json={'de_acuerdo': True})
        assert r.status_code == 403

    def test_liquidar_sin_acta_da_400_con_la_salida(self, app, client, db, almacen):
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        r = client.post(f'/api/rutas/{ruta.id}/liquidar',
                        headers=_jwt(app, _usuario(db, 'liquidador')), json={})
        assert r.status_code == 400 and r.get_json()['error'].startswith('caja_sin_acta:')

    def test_enviar_todo_exige_la_ruta_liquidada(self, app, client, db, almacen):
        c, _ = _conductor(db)
        ruta, [rec] = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        h = _jwt(app, _usuario(db, 'liquidador'))
        assert client.post(f'/api/rutas/{ruta.id}/liquidar-siesa', headers=h).status_code == 409
        assert client.post(f'/api/rutas/{ruta.id}/recaudos/{rec.id}/registrar-cobro',
                           headers=h, json={}).status_code == 409


# ═════════════════════════════════════════════════════════════════════════
# 6 · Trinquetes (AST): una política, una función
# ═════════════════════════════════════════════════════════════════════════

def _archivos():
    for base in ('app', 'flota'):
        for p in sorted((RAIZ / base).rglob('*.py')):
            yield p


def _funciones(arbol):
    """[(qualname, nodo)] con el módulo como `<modulo>`."""
    out = [('<modulo>', arbol)]

    def visitar(nodo, prefijo):
        for h in ast.iter_child_nodes(nodo):
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f'{prefijo}{h.name}', h))
                visitar(h, f'{prefijo}{h.name}.')
            elif isinstance(h, ast.ClassDef):
                visitar(h, f'{prefijo}{h.name}.')
            else:
                visitar(h, prefijo)
    visitar(arbol, '')
    return out


def _propios(fn):
    anidadas = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    pila = [n for n in (fn.body if hasattr(fn, 'body') else []) if not isinstance(n, anidadas)]
    while pila:
        n = pila.pop()
        yield n
        pila.extend(h for h in ast.iter_child_nodes(n) if not isinstance(h, anidadas))


def _es_literal_efectivo(n) -> bool:
    if isinstance(n, ast.Constant) and n.value == 'EFECTIVO':
        return True
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        return any(isinstance(e, ast.Constant) and e.value == 'EFECTIVO' for e in n.elts)
    return False


def _compara_con_efectivo(nodo) -> bool:
    """`x == 'EFECTIVO'`, `'EFECTIVO' != x`, `x in ('EFECTIVO', …)`: decidir si
    una parada es efectivo. Un dict con esa clave o un default no deciden."""
    if not isinstance(nodo, ast.Compare):
        return False
    return any(_es_literal_efectivo(x) for x in [nodo.left, *nodo.comparators])


def _sitios(detector, fuente_por_archivo=None):
    out = {}
    fuentes = fuente_por_archivo or {p.relative_to(RAIZ).as_posix(): p.read_text(encoding='utf-8')
                                     for p in _archivos()}
    for rel, texto in fuentes.items():
        arbol = ast.parse(texto)
        for q, fn in _funciones(arbol):
            n = sum(1 for x in _propios(fn) if detector(x))
            if n:
                out[(rel, q)] = n
    return out


#: Quién decide «esta parada es efectivo». Solo la política.
DUENOS_EFECTIVO = {('app/services/caja_conductor.py', 'efectivo_de_recaudo'),
                   ('app/services/caja_conductor.py', 'medio_de_recaudo')}
#: Excepciones con su porqué. Solo encoge.
EFECTIVO_DECLARADOS = {}


class TestUnaSolaFuncionDecideElEfectivo:
    """La clase: *se da por entregada plata que nadie contó*. Cada sitio que
    decidía por su cuenta qué era efectivo (el tablero, el efectivo en poder,
    la planilla) contaba un universo distinto."""

    def test_nadie_mas_compara_contra_efectivo(self):
        sitios = _sitios(_compara_con_efectivo)
        nuevos = {k: v for k, v in sitios.items()
                  if k not in DUENOS_EFECTIVO and k not in EFECTIVO_DECLARADOS}
        assert not nuevos, (
            f'Decide «efectivo» por su cuenta: {nuevos}. Use '
            'caja_conductor.efectivo_de_recaudo / medio_de_recaudo.')

    def test_el_inventario_solo_encoge_y_dice_por_que(self):
        sitios = _sitios(_compara_con_efectivo)
        assert all(k in sitios for k in EFECTIVO_DECLARADOS), 'sacar del inventario lo que ya no compara'
        assert all(len(v.strip()) >= 40 for v in EFECTIVO_DECLARADOS.values())

    def test_piso_el_detector_ve_a_la_politica(self):
        sitios = _sitios(_compara_con_efectivo)
        assert DUENOS_EFECTIVO <= set(sitios), sitios

    def test_meta_ve_las_formas_y_no_lo_sano(self):
        fuente = {
            'm.py': (
                "def a(fp):\n    return fp == 'EFECTIVO'\n"
                "def b(fp):\n    return 'EFECTIVO' != fp\n"
                "def c(fp):\n    return fp in ('EFECTIVO', 'CHEQUE')\n"
                "def d(q, R):\n    return q.filter(R.forma_pago == 'EFECTIVO')\n"
                "def sano(p):\n    '''fp == 'EFECTIVO' en el docstring'''\n"
                "    # fp == 'EFECTIVO' en un comentario\n"
                "    t = {'EFECTIVO': 0}\n    return p.get('forma', 'EFECTIVO'), t\n"
                "def madre():\n    def hija(fp):\n        return fp == 'EFECTIVO'\n    return hija\n")}
        sitios = _sitios(_compara_con_efectivo, fuente)
        assert set(q for _, q in sitios) == {'a', 'b', 'c', 'd', 'madre.hija'}


def _escribe_el_acta(nodo) -> bool:
    """Crear filas del acta o escribir sus campos de plata / el vínculo ruta→acta."""
    if isinstance(nodo, ast.Call):
        f = nodo.func
        nombre = f.id if isinstance(f, ast.Name) else getattr(f, 'attr', '')
        return nombre in ('EntregaCaja', 'EntregaCajaGasto')
    objetivos = []
    if isinstance(nodo, ast.Assign):
        objetivos = nodo.targets
    elif isinstance(nodo, (ast.AugAssign, ast.AnnAssign)):
        objetivos = [nodo.target]
    return any(isinstance(t, ast.Attribute) and t.attr in (
        'entrega_caja_id', 'contado_efectivo', 'esperado_efectivo',
        'gastos_aceptados', 'verificado_banco_resultado') for t in objetivos)


#: Quién escribe el acta y la verificación del banco. Nadie más.
ESCRITORES_ACTA = {
    ('app/services/caja_conductor.py', 'registrar_acta'),
    ('app/services/caja_conductor.py', 'anular_acta'),
    ('app/services/verificacion_banco.py', 'verificar'),
}


class TestSoloLaPoliticaEscribeElActa:

    def test_nadie_mas_escribe(self):
        sitios = _sitios(_escribe_el_acta)
        nuevos = {k: v for k, v in sitios.items() if k not in ESCRITORES_ACTA}
        assert not nuevos, f'Escribe el acta de caja (o el banco) fuera de la política: {nuevos}'

    def test_piso(self):
        assert ESCRITORES_ACTA <= set(_sitios(_escribe_el_acta))

    def test_meta(self):
        fuente = {'m.py': ("def a(r):\n    r.entrega_caja_id = 3\n"
                           "def b(db):\n    db.session.add(EntregaCaja(dia=1))\n"
                           "def c(x):\n    x.contado_efectivo += 1\n"
                           "def sano(r):\n    '''r.entrega_caja_id = 3'''\n    return r.entrega_caja_id\n")}
        assert {q for _, q in _sitios(_escribe_el_acta, fuente)} == {'a', 'b', 'c'}


class TestLiquidarPreguntaPorElActaAntesDeMarcar:

    def _fn(self):
        arbol = ast.parse((RAIZ / 'app' / 'services' / 'ruta_service.py').read_text(encoding='utf-8'))
        return next(fn for q, fn in _funciones(arbol) if q == 'RutaService.liquidar_ruta')

    @staticmethod
    def _lineas(fn, nombre):
        return [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                and (getattr(n.func, 'attr', None) or getattr(n.func, 'id', None)) == nombre]

    def test_el_acta_va_antes_de_la_marca(self):
        fn = self._fn()
        acta, marca = self._lineas(fn, 'exigir_acta_para_liquidar'), self._lineas(fn, '_marcar_liquidada')
        assert acta and marca and max(acta) < min(marca), (acta, marca)

    def test_y_encola_despues(self):
        fn = self._fn()
        assert min(self._lineas(fn, '_marcar_liquidada')) < min(self._lineas(fn, 'liquidar_ruta_siesa'))
