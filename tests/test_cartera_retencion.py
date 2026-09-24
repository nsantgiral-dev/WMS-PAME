"""
Retención de cartera (mora y cupo) — `app/services/cartera_service.py`.

Un mundo armado con una cartera falsa (`FakeSiesa`, el puerto `usar_fuente`)
donde cada caso se distingue de los demás: sin vencidas pasa, vencida real
retiene, contado de moroso pasa, EN_CAJA no cuenta, RESIDUO no cuenta, DEVUELTA
resta, cupo exacto pasa y +1 retiene, dos pedidos seguidos, cupo 0, sin dato,
maestro de otra compañía, saldo a favor, institucional, acuerdo vigente,
excepción del Gestor, autorización por la API (token, usuario, iniciador,
bitácora, idempotencia), conversión a contado (la FE sale C02), re-evaluación
tras el pago, lock por NIT, G2 con valor empacado distinto y la emisión.
"""
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.cartera import (CarteraCliente, CarteraHabilitacion, EstadoRetencion,
                                RetencionCartera)
from app.services import cartera_service as cs
from app.utils.fecha import dia_operativo

NIT = '900111222'
TOKEN = 'secreto-del-gestor'


# ═════════════════════════════════════════════════════════════════════════════
# La cartera falsa
# ═════════════════════════════════════════════════════════════════════════════

class FakeSiesa:
    """Implementa el puerto de `cartera_service.FuenteSiesa`: clientes,
    cartera abierta 1305 y líneas de pedido, por NIT. `falla=True` simula a
    Siesa sin responder."""

    def __init__(self):
        self.clientes_por_nit = {}
        self.cartera_por_nit = {}
        self.pedidos = {}
        self.falla = False
        self.lecturas = 0

    def simulada(self):
        return False

    def en_ventana(self, ahora=None):
        return True

    def clientes(self, nit):
        self.lecturas += 1
        if self.falla:
            raise cs.CarteraNoDisponible('Siesa no respondió')
        return list(self.clientes_por_nit.get(nit, []))

    def cartera(self, nit):
        if self.falla:
            raise cs.CarteraNoDisponible('Siesa no respondió')
        return list(self.cartera_por_nit.get(nit, []))

    def pedido(self, co, tipo, consec):
        if self.falla:
            raise cs.CarteraNoDisponible('Siesa no respondió')
        return list(self.pedidos.get((str(co), str(tipo), str(consec)), []))

    # ── armado ──
    def cliente(self, nit=NIT, cupo=1_000_000, sucursal='001', cia=1, gracia=0,
                ts='2026-05-20T10:00:00'):
        self.clientes_por_nit.setdefault(nit, []).append({
            'f201_id_cia': cia, 'f200_rowid': len(self.clientes_por_nit.get(nit, [])) + 1,
            'f200_id': nit, 'f201_id_sucursal': sucursal, 'f201_cupo_credito': cupo,
            'f201_dias_gracia': gracia, 'f201_id_cond_pago': 'C04', 'f201_ts': ts,
            'f201_ind_bloqueo_cupo': 0, 'f201_ind_bloqueo_mora': 0,
            'f201_id_tipo_cli': '0001', 'f201_descripcion_sucursal': 'CLIENTE PRUEBA'})

    def factura(self, nit=NIT, saldo=100_000, vence_hace=None, vence_en=None, tipo='FE',
                consec=None, fecha=None, sucursal='001', cia=1, pagado=0):
        hoy = dia_operativo()
        vence = (hoy - timedelta(days=vence_hace) if vence_hace is not None
                 else hoy + timedelta(days=vence_en if vence_en is not None else 20))
        fecha = fecha or (vence - timedelta(days=30))
        consec = consec or (len(self.cartera_por_nit.get(nit, [])) + 700)
        self.cartera_por_nit.setdefault(nit, []).append({
            'f353_rowid': uuid.uuid4().int % 10**9, 'f353_id_cia': cia,
            'f353_id_co_cruce': '003', 'f353_id_tipo_docto_cruce': tipo,
            'f353_consec_docto_cruce': consec, 'f353_fecha': fecha.isoformat() + 'T00:00:00',
            'f353_fecha_vcto': vence.isoformat() + 'T00:00:00', 'f200_id': nit,
            'f201_id_sucursal': sucursal, 'f253_id': '13050501',
            'f353_total_db': float(saldo) + float(pagado), 'f353_total_cr': float(pagado)})


@pytest.fixture
def fake(monkeypatch):
    for v in ('CARTERA_COMPUERTA', 'CARTERA_TOLERANCIA_SALDO', 'SIESA_ID_CIA',
              'COBRO_CONTRAENTREGA_MAX_DIAS', 'SIESA_COND_PAGO_DIAS',
              'CONNEKTA_CONSULTA_COND_PAGO', 'CONTADO_DESPLIEGUE_FECHA'):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv('CARTERA_GESTOR_TOKEN', TOKEN)
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02')
    f = FakeSiesa()
    cs.usar_fuente(f)
    yield f
    cs.usar_fuente(None)


# ═════════════════════════════════════════════════════════════════════════════
# El mundo
# ═════════════════════════════════════════════════════════════════════════════

def _historia(db, clave, nit=NIT, cond='C04', lineas=(('SKU1', 10, 500_000),)):
    from app.models.pedido_historia import PedidoHistoria
    co, tipo, consec = clave.split('-')
    for item, cant, neto in lineas:
        db.session.add(PedidoHistoria(
            linea_rowid=uuid.uuid4().int % 10**12, pedido_clave=clave, co=co,
            tipo_docto=tipo, consec_docto=int(consec), bodega='NB1', item_codigo=item,
            cliente_id=nit, cliente='CLIENTE PRUEBA', vendedor_id='V01', cond_pago=cond,
            cantidad_pedida=cant, cantidad_pedida_inicial=cant, cantidad_remisionada=0,
            vlr_neto=neto, estado_siesa=3, primer_dia_visto=dia_operativo(),
            ultimo_dia_visto=dia_operativo()))
    db.session.commit()


def _producto(db, codigo='SKU1'):
    from app.models.producto import Producto
    p = Producto.query.filter_by(codigo=codigo).first()
    if p is None:
        p = Producto(codigo=codigo, codigo_siesa=codigo, nombre=f'Producto {codigo}')
        db.session.add(p)
        db.session.flush()
    return p


def _tarea(db, almacen, clave, *, estado='VERIFICADO', esperado=10, real=None,
           cond=None, fe=None, valor=None, iniciador=None, item='SKU1'):
    from app.models.packing import ItemPacking, TareaPacking
    co, tipo, consec = clave.split('-')
    t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:8]}', estado=estado, almacen_id=almacen.id,
                     tipo_docto_pedido_siesa=tipo, consec_docto_pedido_siesa=consec,
                     numero_pedido_siesa=f'{tipo}{consec}', pedido_clave=clave,
                     cliente='CLIENTE PRUEBA', cond_pago=cond, valor_factura=valor,
                     despacho_iniciado_por_id=iniciador)
    if fe:
        t.fe_tipo, t.fe_consec = fe
        t.siesa_triggered = True
    db.session.add(t)
    db.session.flush()
    db.session.add(ItemPacking(tarea_id=t.id, producto_id=_producto(db, item).id,
                               cantidad_esperada=esperado,
                               cantidad_real=esperado if real is None else real))
    db.session.commit()
    return t


def _conductor(db):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    s = uuid.uuid4().hex[:6]
    u = Usuario(email=f'cond_{s}@t.com', nombre='Conductor', rol='conductor', activo=True)
    u.set_password('x')
    db.session.add(u)
    db.session.flush()
    c = Conductor(usuario_id=u.id, nombre='Conductor', cedula=f'CC{s}', activo=True)
    db.session.add(c)
    db.session.flush()
    return c


def _recaudo(db, tarea, estado='ENTREGADO', forma='EFECTIVO', monto=0, rc=None, rc_hace_h=None,
             nc=False, descuento=0):
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    ruta = RutaDespacho(conductor_id=_conductor(db).id, tipo_ruta='Urbana', estado='ENTREGADA')
    db.session.add(ruta)
    db.session.flush()
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=tarea.id, estado_entrega=estado,
                       forma_pago=forma, monto_cobrado=monto, monto_descuento=descuento,
                       siesa_nc_triggered=nc)
    if rc:
        r.siesa_rc_resultado = rc
        r.siesa_rc_at = datetime.utcnow() - timedelta(hours=rc_hace_h or 1)
    db.session.add(r)
    db.session.commit()
    return r


def _usuario(db, rol='admin', flag=False, email=None):
    from app.models.usuario import Usuario
    s = uuid.uuid4().hex[:6]
    u = Usuario(email=email or f'{rol}_{s}@t.com', nombre=f'{rol} {s}', rol=rol, activo=True,
                puede_autorizar_cartera=flag)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _jwt(app, u):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


def _gestor(clave=None):
    h = {'Authorization': f'Bearer {TOKEN}'}
    h['Idempotency-Key'] = clave or uuid.uuid4().hex
    return h


USUARIO_GESTOR = {'username': 'coord.cartera', 'nombre': 'Coordinadora Cartera',
                  'email': 'cartera@papeleria.com', 'rol': 'coordinador', 'sistema': 'gestor'}


def _retener(db, fake, almacen, clave='003-PD-100', valor_lineas=500_000, iniciador=None):
    """Una retención viva de G1 por mora."""
    fake.cliente(cupo=10_000_000)
    fake.factura(saldo=300_000, vence_hace=10)
    _historia(db, clave, lineas=(('SKU1', 10, valor_lineas),))
    tipo, consec = clave.split('-')[1:]
    paso = _inicio(f'{tipo}{consec}', tipo, consec, co='003',
                   items=[{'item_codigo': 'SKU1', 'cantidad_pendiente': 10}],
                   almacen_id=almacen.id, usuario_id=iniciador)
    assert not paso.pasa, paso.evaluacion
    return paso.retencion


def _inicio(*a, **k):
    with cs.compuerta_inicio(*a, **k) as paso:
        return paso


def _codigos(ev):
    return {m['codigo'] for m in ev['motivos'] if m['retiene']}


# ═════════════════════════════════════════════════════════════════════════════
# evaluar — la política
# ═════════════════════════════════════════════════════════════════════════════

class TestEvaluar:

    def test_sin_vencidas_y_con_cupo_pasa(self, db, fake):
        fake.cliente(cupo=1_000_000)
        fake.factura(saldo=200_000, vence_en=10)
        ev = cs.evaluar(NIT, '001', 300_000, 'C04')
        assert ev['decision'] == cs.PASA, ev['motivos']
        assert ev['vencidas_n'] == 0
        assert ev['disponible'] == pytest.approx(500_000)

    def test_una_vencida_real_retiene_por_mora(self, db, fake):
        fake.cliente(cupo=10_000_000)
        fake.factura(saldo=100_000, vence_hace=3)
        ev = cs.evaluar(NIT, '001', 50_000, 'C04')
        assert ev['decision'] == cs.RETIENE
        assert _codigos(ev) == {cs.MORA}
        assert ev['vencidas_n'] == 1 and ev['dias_max'] == 3
        assert ev['vencidas'][0]['saldo'] == pytest.approx(100_000)

    def test_vencida_bajo_la_tolerancia_no_es_mora(self, db, fake):
        fake.cliente(cupo=10_000_000)
        fake.factura(saldo=4_000, vence_hace=40)
        ev = cs.evaluar(NIT, '001', 50_000, 'C04')
        assert ev['decision'] == cs.PASA, ev['motivos']

    def test_la_gracia_de_la_sucursal_se_respeta(self, db, fake):
        fake.cliente(cupo=10_000_000, gracia=5)
        fake.factura(saldo=100_000, vence_hace=3)
        assert cs.evaluar(NIT, '001', 50_000, 'C04')['decision'] == cs.PASA

    def test_contado_de_un_cliente_moroso_pasa_sin_mirar_la_cartera(self, db, fake):
        fake.cliente(cupo=0)
        fake.factura(saldo=9_000_000, vence_hace=200)
        ev = cs.evaluar(NIT, '001', 50_000, 'C02')
        assert ev['decision'] == cs.PASA and ev['aplica'] is False
        assert fake.lecturas == 0

    def test_supuesto_sin_condicion_pasa(self, db, fake):
        fake.cliente(cupo=0)
        assert cs.evaluar(NIT, '001', 50_000, None)['decision'] == cs.PASA

    def test_cupo_exacto_pasa_y_un_peso_mas_retiene(self, db, fake):
        fake.cliente(cupo=1_000_000)
        fake.factura(saldo=400_000, vence_en=5)
        ok = cs.evaluar(NIT, '001', 600_000, 'C04')
        assert ok['decision'] == cs.PASA and ok['disponible'] == 0
        no = cs.evaluar(NIT, '001', 600_001, 'C04')
        assert no['decision'] == cs.RETIENE and _codigos(no) == {cs.CUPO_EXCEDIDO}
        assert no['exceso'] == pytest.approx(1)

    def test_cupo_cero_retiene_sin_cupo(self, db, fake):
        fake.cliente(cupo=0)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert _codigos(ev) == {cs.SIN_CUPO}

    def test_cliente_sin_maestro_en_la_compania_retiene_sin_cupo(self, db, fake):
        fake.cliente(cupo=5_000_000, cia=2)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert _codigos(ev) == {cs.SIN_CUPO}

    def test_maestro_de_otra_compania_se_declara_y_no_retiene(self, db, fake):
        fake.cliente(cupo=2_000_000, cia=1)
        fake.cliente(cupo=15_000_000, cia=2)
        ev = cs.evaluar(NIT, '001', 1_000_000, 'C04')
        assert ev['decision'] == cs.PASA
        dup = [m for m in ev['motivos'] if m['codigo'] == cs.MAESTRO_DUPLICADO]
        assert dup and dup[0]['retiene'] is False
        assert ev['cupo'] == pytest.approx(2_000_000), 'se usó el cupo de otra compañía'

    def test_duplicado_en_la_misma_compania_usa_la_fila_mas_reciente(self, db, fake):
        fake.cliente(cupo=5_000_000, ts='2024-01-01T00:00:00')
        fake.cliente(cupo=100_000, ts='2026-05-20T00:00:00')
        ev = cs.evaluar(NIT, '001', 200_000, 'C04')
        assert ev['cupo'] == pytest.approx(100_000)
        assert _codigos(ev) == {cs.CUPO_EXCEDIDO}
        assert any(m['codigo'] == cs.MAESTRO_DUPLICADO for m in ev['motivos'])

    def test_saldo_a_favor_se_declara_neto(self, db, fake):
        fake.cliente(cupo=10_000_000)
        fake.factura(saldo=100_000, vence_hace=10)
        fake.factura(saldo=-80_000, vence_en=10, tipo='NCE')
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['saldo_a_favor'] == pytest.approx(80_000)
        assert ev['vencido_neto'] == pytest.approx(20_000)
        assert ev['saldo'] == pytest.approx(20_000)
        assert _codigos(ev) == {cs.MORA}, 'la mora se mide por fila, no por el neto'

    def test_sin_dato_retiene_credito(self, db, fake):
        fake.falla = True
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert _codigos(ev) == {cs.SIN_DATO} and ev['origen'] == cs.SIN_DATO_ORIGEN

    def test_sin_dato_deja_pasar_contado(self, db, fake):
        fake.falla = True
        assert cs.evaluar(NIT, '001', 1_000, 'C01')['decision'] == cs.PASA

    def test_foto_de_menos_de_24h_sirve_cuando_siesa_falla(self, db, fake):
        db.session.add(CarteraCliente(
            nit=NIT, as_of=datetime.utcnow() - timedelta(hours=5),
            clientes=[{'f201_id_cia': 1, 'f201_id_sucursal': '001',
                       'f201_cupo_credito': 1_000_000}], filas=[]))
        db.session.commit()
        fake.falla = True
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['decision'] == cs.PASA and ev['origen'] == cs.FOTO

    def test_foto_de_mas_de_24h_no_sirve(self, db, fake):
        db.session.add(CarteraCliente(
            nit=NIT, as_of=datetime.utcnow() - timedelta(hours=30),
            clientes=[{'f201_id_cia': 1, 'f201_id_sucursal': '001',
                       'f201_cupo_credito': 1_000_000}], filas=[]))
        db.session.commit()
        fake.falla = True
        assert _codigos(cs.evaluar(NIT, '001', 1_000, 'C04')) == {cs.SIN_DATO}

    def test_valor_desconocido_con_cupo_retiene(self, db, fake):
        fake.cliente(cupo=1_000_000)
        assert _codigos(cs.evaluar(NIT, '001', None, 'C04')) == {cs.VALOR_DESCONOCIDO}

    def test_nit_con_digito_de_verificacion_se_normaliza(self, db, fake):
        fake.cliente(cupo=0)
        assert cs.evaluar(f'{NIT}-7', '1', 1, 'C04')['nit'] == NIT


class TestHabilitaciones:

    def _hab(self, db, **k):
        db.session.add(CarteraHabilitacion(nit=NIT, sucursal=k.pop('sucursal', ''),
                                           as_of=datetime.utcnow(), **k))
        db.session.commit()

    def test_institucional_no_se_retiene_por_mora(self, db, fake):
        fake.cliente(cupo=100_000_000)
        fake.factura(saldo=40_000_000, vence_hace=300)
        self._hab(db, canal='INSTITUCIONAL')
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['decision'] == cs.PASA
        assert any(m['codigo'] == cs.MORA_EXENTA and not m['retiene'] for m in ev['motivos'])

    def test_institucional_si_respeta_el_cupo(self, db, fake):
        fake.cliente(cupo=100_000)
        self._hab(db, canal='INSTITUCIONAL')
        assert _codigos(cs.evaluar(NIT, '001', 200_000, 'C04')) == {cs.CUPO_EXCEDIDO}

    def test_sin_canal_no_se_exime(self, db, fake):
        fake.cliente(cupo=10_000_000)
        fake.factura(saldo=100_000, vence_hace=3)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert _codigos(ev) == {cs.MORA} and 'canal desconocido' in ev['resumen']

    def test_acuerdo_vigente_retiene_el_credito(self, db, fake):
        fake.cliente(cupo=10_000_000)
        self._hab(db, acuerdo_vigente=True, acuerdo_vence=dia_operativo() + timedelta(days=5))
        assert _codigos(cs.evaluar(NIT, '001', 1_000, 'C04')) == {cs.ACUERDO_VIGENTE}

    def test_acuerdo_vencido_no_retiene(self, db, fake):
        fake.cliente(cupo=10_000_000)
        self._hab(db, acuerdo_vigente=True, acuerdo_vence=dia_operativo() - timedelta(days=1))
        assert cs.evaluar(NIT, '001', 1_000, 'C04')['decision'] == cs.PASA

    def test_excepcion_vigente_cubre_hasta_su_tope(self, db, fake):
        fake.cliente(cupo=0)
        vence = (dia_operativo() + timedelta(days=5)).isoformat()
        self._hab(db, excepciones=[{'codigo': 'E1', 'tope': 500_000, 'vence': vence}])
        ok = cs.evaluar(NIT, '001', 400_000, 'C04')
        assert ok['decision'] == cs.PASA and ok['excepcion_gestor']['codigo'] == 'E1'
        assert cs.evaluar(NIT, '001', 600_000, 'C04')['decision'] == cs.RETIENE

    def test_la_excepcion_no_cubre_un_acuerdo_vigente(self, db, fake):
        fake.cliente(cupo=10_000_000)
        vence = (dia_operativo() + timedelta(days=5)).isoformat()
        self._hab(db, acuerdo_vigente=True,
                  excepciones=[{'codigo': 'E1', 'tope': 9_000_000, 'vence': vence}])
        assert cs.evaluar(NIT, '001', 1_000, 'C04')['decision'] == cs.RETIENE


# ═════════════════════════════════════════════════════════════════════════════
# Clasificación de las FE del WMS
# ═════════════════════════════════════════════════════════════════════════════

class TestClasificacionWMS:

    def _fe_wms(self, db, fake, almacen, saldo=200_000, vence_hace=40, **rec):
        clave = f'003-PD-{uuid.uuid4().int % 9000 + 1000}'
        t = _tarea(db, almacen, clave, estado='DESPACHADO', cond='C02', valor=saldo,
                   fe=('FEW', str(uuid.uuid4().int % 90000 + 10000)))
        fake.factura(saldo=saldo, vence_hace=vence_hace, tipo='FEW', consec=t.fe_consec,
                     fecha=dia_operativo() - timedelta(days=vence_hace + 30))
        if rec:
            _recaudo(db, t, **rec)
        return t

    def test_en_caja_no_cuenta(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        self._fe_wms(db, fake, almacen, estado='ENTREGADO', monto=200_000)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['decision'] == cs.PASA, ev['motivos']
        assert [f['clase'] for f in ev['filas']] == [cs.EN_CAJA]

    def test_cobrado_con_rc_aplicado_hace_mas_de_48h_y_residuo_no_es_mora(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        self._fe_wms(db, fake, almacen, saldo=3_000, estado='ENTREGADO', monto=197_000,
                     rc='ENVIADO', rc_hace_h=72, descuento=0)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['decision'] == cs.PASA

    def test_residuo_de_retencion_no_es_mora(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        self._fe_wms(db, fake, almacen, saldo=9_000, estado='ENTREGADO', monto=191_000,
                     rc='ENVIADO', rc_hace_h=72, descuento=8_000)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['decision'] == cs.PASA and ev['filas'][0]['clase'] == cs.RESIDUO

    def test_rechazada_devuelta_no_cuenta(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        self._fe_wms(db, fake, almacen, estado='RECHAZADO', forma='EFECTIVO', monto=0)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['decision'] == cs.PASA and ev['filas'][0]['clase'] == cs.DEVUELTA

    def test_parcial_con_nc_creada_resta_lo_devuelto(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        self._fe_wms(db, fake, almacen, estado='PARCIAL', monto=120_000, nc=True)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert ev['decision'] == cs.PASA and ev['filas'][0]['clase'] == cs.DEVUELTA

    def test_parcial_sin_nc_el_resto_es_deuda(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        self._fe_wms(db, fake, almacen, estado='PARCIAL', monto=120_000, nc=False)
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert _codigos(ev) == {cs.MORA}
        assert ev['vencidas'][0]['cuenta'] == pytest.approx(80_000)

    def test_entregado_sin_pago_es_deuda(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        self._fe_wms(db, fake, almacen, estado='ENTREGADO_SIN_PAGO', forma=None, monto=0)
        assert _codigos(cs.evaluar(NIT, '001', 1_000, 'C04')) == {cs.MORA}

    def test_fe_del_wms_vence_por_la_politica_no_por_el_mas_30(self, db, fake, almacen):
        """FE C02 del WMS facturada hace 10 días: Siesa dice que vence en 20
        (+30 fijo, artefacto); la política dice +15 → vencida hace... no:
        10 días < 15, no vence. Con 20 días sí."""
        fake.cliente(cupo=10_000_000)
        clave = '003-PD-4321'
        t = _tarea(db, almacen, clave, estado='DESPACHADO', cond='C02', fe=('FEW', '55501'))
        fake.factura(saldo=100_000, vence_en=10, tipo='FEW', consec='55501',
                     fecha=dia_operativo() - timedelta(days=20))
        ev = cs.evaluar(NIT, '001', 1_000, 'C04')
        assert _codigos(ev) == {cs.MORA}, 'la FE del WMS se juzgó con el +30 de Siesa'
        assert ev['vencidas'][0]['wms'] is True and t.id


# ═════════════════════════════════════════════════════════════════════════════
# Compuertas
# ═════════════════════════════════════════════════════════════════════════════

class TestCompuertaInicio:

    def test_retiene_y_registra_con_bitacora(self, db, fake, almacen):
        from app.models.bitacora import BitacoraAccion
        u = _usuario(db)
        r = _retener(db, fake, almacen, iniciador=u.id)
        assert r.estado == EstadoRetencion.RETENIDO and r.compuerta == 'G1'
        assert r.valor == pytest.approx(Decimal('500000'))
        assert r.iniciado_por_id == u.id and u.email in r.iniciado_por
        assert BitacoraAccion.query.filter_by(entidad='RetencionCartera',
                                              accion='BLOQUEAR').count() == 1

    def test_contado_pasa_sin_leer_cartera(self, db, fake, almacen):
        _historia(db, '003-PD-200', cond='C01')
        paso = _inicio('PD200', 'PD', '200', co='003', almacen_id=almacen.id)
        assert paso.pasa and paso.decision == 'NO_APLICA' and fake.lecturas == 0

    def test_una_retencion_por_pedido(self, db, fake, almacen):
        r1 = _retener(db, fake, almacen)
        paso = _inicio('PD100', 'PD', '100', co='003', almacen_id=almacen.id,
                                   items=[{'item_codigo': 'SKU1', 'cantidad_pendiente': 10}])
        assert not paso.pasa and paso.retencion.id == r1.id
        assert RetencionCartera.query.count() == 1

    def test_dos_pedidos_seguidos_el_segundo_cabe(self, db, fake, almacen):
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-301', lineas=(('SKU1', 10, 400_000),))
        _historia(db, '003-PD-302', lineas=(('SKU1', 10, 600_000),))
        assert _inicio('PD301', 'PD', '301', co='003', almacen_id=almacen.id).pasa
        _tarea(db, almacen, '003-PD-301', estado='PENDIENTE')
        assert _inicio('PD302', 'PD', '302', co='003', almacen_id=almacen.id).pasa

    def test_dos_pedidos_seguidos_el_segundo_no_cabe(self, db, fake, almacen):
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-311', lineas=(('SKU1', 10, 400_000),))
        _historia(db, '003-PD-312', lineas=(('SKU1', 10, 600_001),))
        assert _inicio('PD311', 'PD', '311', co='003', almacen_id=almacen.id).pasa
        _tarea(db, almacen, '003-PD-311', estado='PENDIENTE')
        paso = _inicio('PD312', 'PD', '312', co='003', almacen_id=almacen.id)
        assert not paso.pasa
        assert _codigos(paso.evaluacion) == {cs.CUPO_EXCEDIDO}
        assert paso.evaluacion['consumo_wms'] == pytest.approx(400_000)

    def test_un_pedido_contado_del_mismo_cliente_no_consume_cupo(self, db, fake, almacen):
        fake.cliente(cupo=1_000_000)
        _historia(db, '003-PD-321', cond='C01', lineas=(('SKU1', 10, 900_000),))
        _historia(db, '003-PD-322', lineas=(('SKU1', 10, 900_000),))
        _tarea(db, almacen, '003-PD-321', estado='PENDIENTE', cond='C01')
        assert _inicio('PD322', 'PD', '322', co='003', almacen_id=almacen.id).pasa

    def test_modo_informa_deja_pasar_sin_retener(self, db, fake, almacen, monkeypatch):
        monkeypatch.setenv('CARTERA_COMPUERTA', 'INFORMA')
        fake.cliente(cupo=0)
        _historia(db, '003-PD-330')
        paso = _inicio('PD330', 'PD', '330', co='003', almacen_id=almacen.id)
        assert paso.pasa and paso.habria_retenido
        assert RetencionCartera.query.count() == 0

    def test_modo_ilegible_no_apaga_la_compuerta(self, db, fake, almacen, monkeypatch):
        monkeypatch.setenv('CARTERA_COMPUERTA', 'apagada')
        fake.cliente(cupo=0)
        _historia(db, '003-PD-331')
        assert not _inicio('PD331', 'PD', '331', co='003',
                                       almacen_id=almacen.id).pasa

    def test_http_retiene_con_409_y_no_crea_picking(self, app, client, db, fake, almacen):
        from app.models.picking import TareaPicking
        admin = _usuario(db)
        fake.cliente(cupo=0)
        _historia(db, '003-PD-340')
        p = _producto(db)
        db.session.commit()
        r = client.post('/api/siesa/iniciar-despacho', headers=_jwt(app, admin), json={
            'numero_pedido': 'PD340', 'tipo_docto': 'PD', 'consec_docto': 340, 'co': '003',
            'almacen_id': almacen.id,
            'items': [{'producto_id': p.id, 'item_codigo': 'SKU1', 'cantidad_pendiente': 10}]})
        assert r.status_code == 409, r.get_json()
        assert r.get_json()['retenido_por_cartera'] is True
        assert TareaPicking.query.count() == 0

    def test_http_otro_despacho_del_mismo_cliente_en_curso_da_409(self, app, client, db, fake,
                                                                    almacen):
        from app.utils import lock as L
        admin = _usuario(db)
        fake.cliente(cupo=10_000_000)
        _historia(db, '003-PD-350')
        p = _producto(db)
        db.session.commit()

        class _NoTomado:
            tomado = False

            def liberar(self):
                pass
        with patch.object(L, 'tomar_lock_de_sesion', return_value=_NoTomado()):
            r = client.post('/api/siesa/iniciar-despacho', headers=_jwt(app, admin), json={
                'numero_pedido': 'PD350', 'tipo_docto': 'PD', 'consec_docto': 350, 'co': '003',
                'almacen_id': almacen.id,
                'items': [{'producto_id': p.id, 'item_codigo': 'SKU1', 'cantidad_pendiente': 1}]})
        assert r.status_code == 409 and 'se está evaluando' in r.get_json()['error']

    def test_el_lock_es_por_nit_y_esta_en_el_registro(self):
        from app.utils import lock as L
        k1, k2 = cs.clave_lock_nit('900111222'), cs.clave_lock_nit('900111222-5')
        assert k1 == k2
        base, tam = L.RANGO_CARTERA_NIT
        assert base <= k1 < base + tam
        L._validar_clave(k1)

    def test_la_cola_muestra_la_retencion(self, app, client, db, fake, almacen):
        from app.models.pedido_siesa import PedidoSiesa
        _retener(db, fake, almacen)
        db.session.add(PedidoSiesa(tipo_docto='PD', consec_docto=100, centro_op='003',
                                   bodega='NB1', numero_pedido='PD100', item_codigo='SKU1',
                                   cantidad_pedida=10, cantidad_pendiente=10))
        db.session.commit()
        from app.services.connekta_gateway import connekta
        with patch.object(connekta, 'modo_simulacion', False):
            r = client.get('/api/siesa/pedidos', headers=_jwt(app, _usuario(db)))
        ped = next(p for p in r.get_json()['pedidos'] if p['numero_pedido'] == 'PD100')
        assert ped['retencion_cartera']['motivos'] == [cs.MORA]
        assert ped['cartera_liberada'] is False

    def test_la_cola_ofrece_cerrar_la_caja_liberada(self, app, client, db, fake, almacen):
        from app.models.pedido_siesa import PedidoSiesa
        fake.cliente(cupo=0)
        _historia(db, '003-PD-360')
        t = _tarea(db, almacen, '003-PD-360', cond='C04')
        r = cs.compuerta_cierre(t).retencion
        db.session.commit()
        cs.autorizar(r.id, USUARIO_GESTOR, 'pago acordado', tope_valor=9_000_000)
        db.session.add(PedidoSiesa(tipo_docto='PD', consec_docto=360, centro_op='003',
                                   bodega='NB1', numero_pedido='PD360', item_codigo='SKU1',
                                   cantidad_pedida=10, cantidad_pendiente=10))
        db.session.commit()
        from app.services.connekta_gateway import connekta
        with patch.object(connekta, 'modo_simulacion', False):
            d = client.get('/api/siesa/pedidos', headers=_jwt(app, _usuario(db))).get_json()
        ped = next(p for p in d['pedidos'] if p['numero_pedido'] == 'PD360')
        assert ped['retencion_cartera'] is None and ped['cartera_liberada'] is True


class TestCompuertaCierre:

    def test_contado_marca_no_aplica(self, db, fake, almacen):
        t = _tarea(db, almacen, '003-PD-400', cond='C01')
        paso = cs.compuerta_cierre(t)
        assert paso.pasa and t.cartera_decision == 'NO_APLICA'

    def test_credito_con_mora_retiene_con_el_valor_empacado(self, db, fake, almacen):
        fake.cliente(cupo=10_000_000)
        fake.factura(saldo=100_000, vence_hace=4)
        _historia(db, '003-PD-401', lineas=(('SKU1', 10, 1_000_000),))
        t = _tarea(db, almacen, '003-PD-401', cond='C04', esperado=10, real=4)
        paso = cs.compuerta_cierre(t)
        assert not paso.pasa and paso.retencion.compuerta == 'G2'
        assert paso.retencion.valor == pytest.approx(Decimal('400000'))
        assert t.cartera_decision is None

    def test_autorizado_en_g1_pasa_si_lo_empacado_no_supera_el_tope(self, db, fake, almacen):
        r = _retener(db, fake, almacen, valor_lineas=500_000)
        cs.autorizar(r.id, USUARIO_GESTOR, 'cliente al día por fuera', tope_valor=300_000)
        t = _tarea(db, almacen, '003-PD-100', cond='C04', esperado=10, real=6)
        paso = cs.compuerta_cierre(t)
        assert paso.pasa and paso.decision == 'AUTORIZADO'

    def test_g2_vuelve_a_retener_si_lo_empacado_supera_el_tope(self, db, fake, almacen):
        r = _retener(db, fake, almacen, valor_lineas=500_000)
        cs.autorizar(r.id, USUARIO_GESTOR, 'cliente al día por fuera', tope_valor=300_000)
        t = _tarea(db, almacen, '003-PD-100', cond='C04', esperado=10, real=7)
        paso = cs.compuerta_cierre(t)
        assert not paso.pasa and paso.retencion.id != r.id

    def test_reintento_del_cierre_no_vuelve_a_preguntar(self, db, fake, almacen):
        t = _tarea(db, almacen, '003-PD-402', cond='C04')
        t.cartera_decision = 'PASA'
        db.session.commit()
        fake.falla = True
        assert cs.compuerta_cierre(t).pasa

    def test_el_closer_deja_la_caja_verificada_con_bultos_y_sin_job(self, db, fake, almacen):
        from app.models.bulto import Bulto
        from app.models.siesa_job import SiesaJob
        from app.services.closing.pedido_closer import PedidoPackingCloser
        fake.cliente(cupo=0)
        _historia(db, '003-PD-403')
        t = _tarea(db, almacen, '003-PD-403', cond='C04')
        with patch.object(PedidoPackingCloser, '_precheck_siesa', return_value=None):
            res = PedidoPackingCloser().ejecutar_cierre(t.id, [{'tipo': 'Caja', 'cantidad': 2}], 0)
        assert not res.exitoso and 'Retenido por cartera' in res.error
        db.session.refresh(t)
        assert t.estado == 'VERIFICADO'
        assert Bulto.query.filter_by(tarea_id=t.id).count() == 2
        assert SiesaJob.query.count() == 0
        # Liberado: el cierre sin bultos nuevos pasa y encola.
        r = RetencionCartera.query.filter_by(pedido_clave='003-PD-403').one()
        cs.autorizar(r.id, USUARIO_GESTOR, 'pago acordado', tope_valor=10_000_000)
        with patch.object(PedidoPackingCloser, '_precheck_siesa', return_value=None):
            res2 = PedidoPackingCloser().ejecutar_cierre(t.id, [], 0)
        assert res2.exitoso, res2.error
        assert SiesaJob.query.filter_by(tipo='DESPACHO_F470').count() == 1


class TestCompuertaEmision:

    def _cab(self, cond='C04'):
        return {'f430_id_cond_pago': cond, 'f200_id_pedido_fact': NIT,
                'f430_id_sucursal_pedido_fact': '001', 'f430_usuario_creacion': 'crm.siesa'}

    def test_contado_de_la_cabecera_sigue(self, db, fake, almacen):
        t = _tarea(db, almacen, '003-PD-500')
        assert cs.compuerta_emision(t, self._cab('C02'))['f430_id_cond_pago'] == 'C02'

    def test_supuesto_que_la_cabecera_desmiente_se_evalua_y_retiene(self, db, fake, almacen):
        from app.services.siesa_job_service import DependenciaPendiente
        fake.cliente(cupo=0)
        t = _tarea(db, almacen, '003-PD-501')
        t.cartera_decision = 'NO_APLICA'
        db.session.commit()
        with pytest.raises(cs.RetenidoPorCartera) as e:
            cs.compuerta_emision(t, self._cab('C04'))
        assert isinstance(e.value, DependenciaPendiente), 'el DLQ gastaría reintentos'
        r = RetencionCartera.query.one()
        assert r.compuerta == 'EMISION' and r.contexto_siesa['usuario_creacion'] == 'crm.siesa'

    def test_decision_pasa_no_sale_a_la_red(self, db, fake, almacen):
        t = _tarea(db, almacen, '003-PD-502')
        t.cartera_decision = 'PASA'
        db.session.commit()
        fake.falla = True
        assert cs.compuerta_emision(t, self._cab())

    def test_despachar_parcial_no_llama_al_244328_si_retiene(self, db, fake, almacen):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.despacho_parcial_service import DespachoParialService
        fake.cliente(cupo=0)
        t = _tarea(db, almacen, '003-PD-503')
        llamadas = []
        with patch.object(ConnektaGateway, 'get_pedido_cabecera',
                          lambda self, *a, **k: self_cab), \
                patch.object(ConnektaGateway, 'trigger_comprometer_pedido',
                             lambda self, *a, **k: llamadas.append(a)):
            self_cab = {**self._cab(), 'f430_rowid': 1}
            with pytest.raises(cs.RetenidoPorCartera):
                DespachoParialService.despachar_parcial(t, {'SKU1': 1})
        assert llamadas == []

    def test_la_ruta_admin_responde_409(self, app, client, db, fake, almacen):
        from app.services.connekta_gateway import ConnektaGateway
        fake.cliente(cupo=0)
        t = _tarea(db, almacen, '003-PD-504')
        with patch.object(ConnektaGateway, 'get_pedido_cabecera',
                          lambda self, *a, **k: {'f430_id_cond_pago': 'C04',
                                                 'f200_id_pedido_fact': NIT, 'f430_rowid': 1}):
            r = client.post(f'/api/despacho_parcial/{t.id}/despachar',
                            headers=_jwt(app, _usuario(db)), json={'cantidades': {'SKU1': 1}})
        assert r.status_code == 409 and r.get_json()['retenido_por_cartera'] is True


# ═════════════════════════════════════════════════════════════════════════════
# Resolver
# ═════════════════════════════════════════════════════════════════════════════

class TestResolver:

    def test_autorizar_registra_en_bitacora_con_el_usuario_del_gestor(self, db, fake, almacen):
        from app.models.bitacora import BitacoraAccion
        r = _retener(db, fake, almacen)
        cs.autorizar(r.id, USUARIO_GESTOR, 'compromiso de pago escrito', tope_valor=600_000,
                     codigo_excepcion='E2a')
        assert r.estado == EstadoRetencion.AUTORIZADO
        assert 'Coordinadora Cartera' in r.resuelta_por and r.resuelta_origen == 'GESTOR'
        b = BitacoraAccion.query.filter_by(accion='FORZAR').one()
        assert b.origen == 'gestor_cartera' and b.motivo == 'compromiso de pago escrito'
        assert b.despues['usuario']['username'] == 'coord.cartera'

    def test_quien_inicio_el_pedido_no_puede_autorizarlo(self, db, fake, almacen):
        u = _usuario(db, email='juan.despacho@papeleria.com')
        r = _retener(db, fake, almacen, iniciador=u.id)
        with pytest.raises(cs.AccionRechazada):
            cs.autorizar(r.id, {'username': 'juan.despacho', 'email': u.email}, 'x',
                         tope_valor=1)

    def test_el_creador_en_siesa_tampoco(self, db, fake, almacen):
        fake.pedidos[('003', 'PD', '100')] = [{'f430_usuario_creacion': 'crm.siesa',
                                              'f430_id_sucursal_pedido_fact': '001'}]
        r = _retener(db, fake, almacen)
        assert r.siesa_usuario_creacion == 'crm.siesa'
        with pytest.raises(cs.AccionRechazada):
            cs.autorizar(r.id, {'username': 'crm.siesa'}, 'x', tope_valor=1)

    def test_motivo_y_tope_obligatorios_vigencia_maxima(self, db, fake, almacen):
        r = _retener(db, fake, almacen)
        with pytest.raises(ValueError):
            cs.autorizar(r.id, USUARIO_GESTOR, '   ', tope_valor=1)
        with pytest.raises(ValueError):
            cs.autorizar(r.id, USUARIO_GESTOR, 'ok', tope_valor=None)
        with pytest.raises(ValueError):
            cs.autorizar(r.id, USUARIO_GESTOR, 'ok', tope_valor=1,
                         vence_en=(dia_operativo() + timedelta(days=31)).isoformat())
        with pytest.raises(ValueError):
            cs.autorizar(r.id, USUARIO_GESTOR, 'ok', tope_valor=1, codigo_excepcion='E9')

    def test_autorizacion_vencida_no_deja_pasar(self, db, fake, almacen):
        r = _retener(db, fake, almacen)
        cs.autorizar(r.id, USUARIO_GESTOR, 'ok', tope_valor=9_000_000)
        r.vence_en = dia_operativo() - timedelta(days=1)
        db.session.commit()
        assert cs.resolucion_vigente('003-PD-100', 1) is None

    def test_convertir_a_contado_la_fe_sale_c02(self, db, fake, almacen):
        r = _retener(db, fake, almacen)
        t = _tarea(db, almacen, '003-PD-100', cond='C04')
        cs.convertir_a_contado(r.id, USUARIO_GESTOR, 'solo contado hasta ponerse al día')
        assert r.estado == EstadoRetencion.CONVERTIDO_CONTADO
        cab = cs.cabecera_para_factura(t, {'f430_id_cond_pago': 'C04'})
        assert cab['f430_id_cond_pago'] == 'C02'
        paso = cs.compuerta_cierre(t)
        assert paso.pasa and paso.decision == 'CONTADO'
        assert t.cond_pago_fe == 'C02' and t.cobro_contraentrega is True

    def test_la_factura_convertida_vence_a_15_dias(self, db, fake, almacen):
        from app.services import cond_pago as cp
        assert cp.vencimiento_fe('20260901', 'C02') == '20260916'

    def test_convertir_sin_condicion_de_ruta_se_rechaza(self, db, fake, almacen, monkeypatch):
        from app.services.connekta_gateway import connekta
        r = _retener(db, fake, almacen)
        monkeypatch.setattr(connekta, 'cond_pago_ruta', '')
        with pytest.raises(cs.AccionRechazada):
            cs.convertir_a_contado(r.id, USUARIO_GESTOR, 'x')

    def test_reevaluar_libera_cuando_el_cliente_pago(self, db, fake, almacen):
        r = _retener(db, fake, almacen)
        fake.cartera_por_nit[NIT] = []
        cs.reevaluar(r.id, USUARIO_GESTOR, origen='GESTOR')
        assert r.estado == EstadoRetencion.LIBERADO_PAGO and r.reevaluaciones == 1

    def test_reevaluar_sin_pago_sigue_retenido(self, db, fake, almacen):
        r = _retener(db, fake, almacen)
        cs.reevaluar(r.id)
        assert r.estado == EstadoRetencion.RETENIDO and r.reevaluaciones == 1

    def test_barrido_libera_y_cancela_lo_anulado(self, db, fake, almacen):
        from app.models.pedido_historia import MotivoSalidaPedido, PedidoHistoria
        r1 = _retener(db, fake, almacen, clave='003-PD-601')
        _historia(db, '003-PD-602')
        r2 = _inicio('PD602', 'PD', '602', co='003', almacen_id=almacen.id).retencion
        for h in PedidoHistoria.query.filter_by(pedido_clave='003-PD-602'):
            h.motivo_salida = MotivoSalidaPedido.ANULADO
        db.session.commit()
        fake.cartera_por_nit[NIT] = []
        res = cs.barrido(datetime(2026, 9, 24, 10, 0))
        assert res['canceladas'] == 1 and res['liberadas'] == 1
        assert r1.estado == EstadoRetencion.LIBERADO_PAGO
        assert r2.estado == EstadoRetencion.CANCELADO

    def test_barrido_fuera_de_ventana_no_hace_nada(self, db, fake):
        assert 'omitido' in cs.barrido(datetime(2026, 9, 24, 21, 0))


# ═════════════════════════════════════════════════════════════════════════════
# API del Gestor
# ═════════════════════════════════════════════════════════════════════════════

class TestApiGestor:

    def test_sin_token_configurado_503(self, client, db, fake, monkeypatch):
        monkeypatch.delenv('CARTERA_GESTOR_TOKEN')
        assert client.get('/api/cartera/retenciones', headers=_gestor()).status_code == 503

    def test_token_invalido_401_y_en_la_url_no_sirve(self, client, db, fake):
        assert client.get('/api/cartera/retenciones',
                          headers={'Authorization': 'Bearer otro'}).status_code == 401
        assert client.get(f'/api/cartera/retenciones?token={TOKEN}').status_code == 401

    def test_listar_trae_el_contrato(self, client, db, fake, almacen):
        u = _usuario(db, email='inicia@t.com')
        _retener(db, fake, almacen, iniciador=u.id)
        r = client.get('/api/cartera/retenciones?estado=RETENIDO', headers=_gestor())
        d = r.get_json()
        assert r.status_code == 200 and d['cursor'] and d['politica']['version']
        x = d['retenciones'][0]
        for k in ('id', 'estado', 'pedido', 'cliente', 'nit', 'sucursal', 'vendedor', 'valor',
                  'motivos', 'motivos_codigos', 'vencidas', 'cupo', 'disponible', 'exceso',
                  'as_of', 'iniciado_por', 'contexto_siesa', 'politica', 'actualizada_en'):
            assert k in x, k
        assert x['motivos_codigos'] == ['MORA'] and x['iniciado_por']['email'] == 'inicia@t.com'
        assert x['vencidas'][0]['saldo'] == pytest.approx(300_000)

    def test_cursor_trae_tambien_las_resueltas(self, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        c1 = client.get('/api/cartera/retenciones', headers=_gestor()).get_json()['cursor']
        assert client.get(f'/api/cartera/retenciones?cambiados_desde={c1}',
                          headers=_gestor()).get_json()['retenciones'] == []
        cs.autorizar(r.id, USUARIO_GESTOR, 'ok', tope_valor=1_000_000)
        d = client.get(f'/api/cartera/retenciones?cambiados_desde={c1}',
                       headers=_gestor()).get_json()
        assert [x['estado'] for x in d['retenciones']] == ['AUTORIZADO']

    def test_una_lectura_que_falla_es_503_no_lista_vacia(self, client, db, fake):
        with patch.object(cs, 'listar', side_effect=RuntimeError('base caída')):
            r = client.get('/api/cartera/retenciones', headers=_gestor())
        assert r.status_code == 503 and 'base caída' in r.get_json()['error']

    def test_autorizar_idempotente_y_409_con_el_estado(self, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        body = {'usuario': USUARIO_GESTOR, 'motivo': 'pagó por transferencia',
                'codigo_excepcion': 'E1', 'tope_valor': 600_000}
        h = _gestor('clave-1')
        a = client.post(f'/api/cartera/retenciones/{r.id}/autorizar', headers=h, json=body)
        assert a.status_code == 200 and a.get_json()['retencion']['estado'] == 'AUTORIZADO'
        b = client.post(f'/api/cartera/retenciones/{r.id}/autorizar', headers=h, json=body)
        assert b.status_code == 200 and b.get_json()['idempotente'] is True
        c = client.post(f'/api/cartera/retenciones/{r.id}/autorizar', headers=_gestor(),
                        json=body)
        assert c.status_code == 409 and c.get_json()['estado'] == 'AUTORIZADO'

    def test_sin_idempotency_key_400(self, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        h = {'Authorization': f'Bearer {TOKEN}'}
        assert client.post(f'/api/cartera/retenciones/{r.id}/autorizar', headers=h,
                           json={}).status_code == 400

    def test_usuario_debe_ser_objeto(self, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        x = client.post(f'/api/cartera/retenciones/{r.id}/autorizar', headers=_gestor(),
                        json={'usuario': 'ana', 'motivo': 'x', 'tope_valor': 1})
        assert x.status_code == 400

    def test_clave_reusada_en_otra_operacion_409(self, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        h = _gestor('clave-2')
        client.post(f'/api/cartera/retenciones/{r.id}/reevaluar', headers=h, json={})
        x = client.post(f'/api/cartera/retenciones/{r.id}/convertir-contado', headers=h,
                        json={'usuario': USUARIO_GESTOR, 'motivo': 'x'})
        assert x.status_code == 409

    def test_convertir_por_api(self, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        x = client.post(f'/api/cartera/retenciones/{r.id}/convertir-contado', headers=_gestor(),
                        json={'usuario': USUARIO_GESTOR, 'motivo': 'solo contado'})
        assert x.status_code == 200 and x.get_json()['retencion']['estado'] == \
            'CONVERTIDO_CONTADO'

    def test_habilitaciones_por_api(self, client, db, fake):
        vence = (dia_operativo() + timedelta(days=10)).isoformat()
        x = client.post('/api/cartera/habilitaciones', headers=_gestor(), json={
            'usuario': USUARIO_GESTOR, 'clientes': [
                {'nit': f'{NIT}-3', 'canal': 'INSTITUCIONAL', 'acuerdo_vigente': False,
                 'excepciones': [{'codigo': 'E3', 'tope': 100000, 'vence': vence}],
                 'as_of': '2026-09-24T15:00:00Z'}]})
        assert x.status_code == 200, x.get_json()
        h = cs.habilitacion_de(NIT, '001')
        assert h['canal'] == 'INSTITUCIONAL' and h['excepciones'][0]['codigo'] == 'E3'

    def test_habilitacion_vieja_no_pisa_la_nueva(self, db, fake):
        cs.registrar_habilitacion({'nit': NIT, 'canal': 'MAYORISTA',
                                   'as_of': '2026-09-24T15:00:00'})
        cs.registrar_habilitacion({'nit': NIT, 'canal': 'INSTITUCIONAL',
                                   'as_of': '2026-09-20T15:00:00'})
        db.session.commit()
        assert cs.habilitacion_de(NIT)['canal'] == 'MAYORISTA'

    def test_habilitacion_sin_as_of_400(self, client, db, fake):
        x = client.post('/api/cartera/habilitaciones', headers=_gestor(),
                        json={'usuario': USUARIO_GESTOR, 'nit': NIT, 'canal': 'MAYORISTA'})
        assert x.status_code == 400

    def test_salud(self, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        r.creada_en = datetime.utcnow() - timedelta(days=4)
        db.session.commit()
        d = client.get('/api/cartera/salud', headers=_gestor()).get_json()
        assert d['retenidos'] == 1 and d['retenidos_por_antiguedad']['>3d'] == 1
        assert d['alertas'][0]['pedido'] == 'PD100'
        assert d['cartera']['nits_en_foto'] == 1 and d['gestor_token_configurado'] is True


# ═════════════════════════════════════════════════════════════════════════════
# Respaldo en el WMS
# ═════════════════════════════════════════════════════════════════════════════

class TestPanelWMS:

    def test_sin_el_permiso_no_autoriza_ni_el_admin(self, app, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        x = client.post(f'/api/cartera/panel/retenciones/{r.id}/autorizar',
                        headers=_jwt(app, _usuario(db, 'admin')),
                        json={'motivo': 'x', 'tope_valor': 1})
        assert x.status_code == 403

    def test_con_el_permiso_autoriza(self, app, client, db, fake, almacen):
        r = _retener(db, fake, almacen)
        u = _usuario(db, 'supervisor', flag=True)
        x = client.post(f'/api/cartera/panel/retenciones/{r.id}/autorizar',
                        headers=_jwt(app, u), json={'motivo': 'visto con gerencia',
                                                    'tope_valor': 900_000})
        assert x.status_code == 200 and r.resuelta_por_id == u.id and r.resuelta_origen == 'WMS'

    def test_el_iniciador_no_se_autoriza_a_si_mismo_en_el_wms(self, app, client, db, fake,
                                                              almacen):
        u = _usuario(db, 'admin', flag=True)
        r = _retener(db, fake, almacen, iniciador=u.id)
        x = client.post(f'/api/cartera/panel/retenciones/{r.id}/autorizar',
                        headers=_jwt(app, u), json={'motivo': 'x', 'tope_valor': 1})
        assert x.status_code == 409

    def test_conductor_con_la_casilla_no_ve_nada(self, app, client, db, fake):
        u = _usuario(db, 'conductor', flag=True)
        assert client.get('/api/cartera/panel/retenciones',
                          headers=_jwt(app, u)).status_code == 403

    def test_gestion_ve_las_retenidas_sin_botones_de_decidir(self, app, client, db, fake,
                                                             almacen):
        _retener(db, fake, almacen)
        d = client.get('/api/cartera/panel/retenciones',
                       headers=_jwt(app, _usuario(db, 'jefe_almacen'))).get_json()
        assert len(d['retenciones']) == 1
        assert d['puede_autorizar'] is False and d['puede_autorizar_lote'] is False


class TestLoteAnterioresAContado:

    def _parada_vieja(self, db, almacen, dias_atras):
        t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 9000 + 1000}', estado='DESPACHADO',
                   cond='C02', valor=100_000)
        r = _recaudo(db, t, estado='ENTREGADO', forma='CREDITO', monto=0)
        r.fecha_confirmacion = datetime.utcnow() - timedelta(days=dias_atras)
        db.session.commit()
        return r

    def test_sin_fecha_de_despliegue_no_adivina(self, app, client, db, fake):
        x = client.get('/api/cartera/panel/credito-lote', headers=_jwt(app, _usuario(db)))
        assert x.status_code == 409

    def test_solo_las_anteriores_y_bitacora_por_parada(self, app, client, db, fake, almacen,
                                                      monkeypatch):
        from app.models.bitacora import BitacoraAccion
        monkeypatch.setenv('CONTADO_DESPLIEGUE_FECHA',
                           (dia_operativo() - timedelta(days=3)).isoformat())
        vieja = self._parada_vieja(db, almacen, 10)
        nueva = self._parada_vieja(db, almacen, 1)
        admin = _usuario(db)
        prev = client.get('/api/cartera/panel/credito-lote', headers=_jwt(app, admin)).get_json()
        assert [p['recaudo_id'] for p in prev['paradas']] == [vieja.id]
        x = client.post('/api/cartera/panel/credito-lote', headers=_jwt(app, admin),
                        json={'motivo': 'anterior a la regla de contado'})
        assert x.status_code == 200 and x.get_json()['autorizadas'] == 1
        assert vieja.credito_autorizado_en is not None and nueva.credito_autorizado_en is None
        assert BitacoraAccion.query.filter_by(entidad='RecaudoEntrega', accion='EDITAR').count() == 1

    def test_lote_sin_motivo_400(self, app, client, db, fake, monkeypatch):
        monkeypatch.setenv('CONTADO_DESPLIEGUE_FECHA', '2026-09-24')
        x = client.post('/api/cartera/panel/credito-lote', headers=_jwt(app, _usuario(db)),
                        json={'motivo': ' '})
        assert x.status_code == 400


class TestInformeRuta:

    def test_g3_informa_la_autorizacion(self, db, fake, almacen):
        r = _retener(db, fake, almacen)
        cs.autorizar(r.id, USUARIO_GESTOR, 'pago acordado', tope_valor=1_000_000)
        t = _tarea(db, almacen, '003-PD-100', estado='DESPACHADO')
        inf = cs.informe_de_tarea(t, {'tarea_id': t.id})
        assert [i['clave'] for i in inf] == ['cartera_autorizado']
