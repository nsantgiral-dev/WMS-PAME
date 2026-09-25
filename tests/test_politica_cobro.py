"""
La plata del conductor: **una función por pregunta** (`services/politica_cobro`).

Auditoría 2026-09-25 — cuatro preguntas contestadas dos o tres veces, y las
copias divergían:

| Pregunta | Qué pasaba | Ahora |
|---|---|---|
| ¿Se emite la retención? | «Registrar cobro» bloqueaba una rechazada; «Liquidar ruta» encolaba la NI con solo mirar `motivo_descuento` (P0-5) | `exigir_retencion_aplicable`, en el único encolador (`_encolar_retencion`) y revalidada en el ejecutor |
| ¿Por cuánto el RC? | En una PARCIAL con retención, lo cobrado ya es neto y «Registrar cobro» restaba la retención otra vez (P1-7) | `monto_rc`, las dos puertas |
| ¿Sobre qué base se retiene? | La factura entera, con lo devuelto | `base_retencion_entregada` |
| ¿Se puede cambiar el cobro? | Congelado recién al enviar el RC (P1-3) | `puede_editar_cobro`: al encolar |

Y un trinquete de clase por pregunta, por AST, con meta-tests y piso. Más
uno de la liquidación: solo `liquidar_ruta` llama `_marcar_liquidada` (P1-5).
"""
import ast
import json
import pathlib
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'

LINEA = {'f470_vlr_bruto': 840336, 'f470_vlr_imp': 159664, 'f470_vlr_neto': 1000000,
         'f120_referencia': 'REF001', 'f470_rowid': 'R1', 'f470_cant_base': 10}


@pytest.fixture
def recaudo(db, almacen):
    def _make(estado='ENTREGADO', pago='EFECTIVO', monto=1000000, motivo=None,
              confirmada=None, monto_desc=0, rc=False):
        from app.models.conductor import Conductor
        from app.models.packing import TareaPacking
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho
        c = Conductor(nombre='Cond', cedula=f'C{uuid.uuid4().hex[:8]}', activo=True)
        db.session.add(c)
        db.session.flush()
        ruta = RutaDespacho(conductor_id=c.id, tipo_ruta='Urbana', estado='ENTREGADA')
        db.session.add(ruta)
        db.session.flush()
        t = TareaPacking(codigo=f'PK-PC-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                         almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa=555,
                         numero_pedido_siesa=f'PD{uuid.uuid4().hex[:6]}',
                         cond_pago='C01')
        db.session.add(t)
        db.session.flush()
        r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega=estado,
                           forma_pago=pago, monto_cobrado=monto, motivo_descuento=motivo,
                           monto_descuento=monto_desc, retencion_confirmada=confirmada,
                           siesa_rc_triggered=rc)
        db.session.add(r)
        db.session.commit()
        return r
    return _make


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La retención
# ═════════════════════════════════════════════════════════════════════════════

class TestDecisionDeRetencion:

    def test_los_cuatro_estados(self, db, recaudo):
        from app.services import politica_cobro as pc
        assert pc.decision_retencion(recaudo()) == pc.SIN_DECLARAR
        assert pc.decision_retencion(recaudo(motivo='RETEIVA')) == pc.PENDIENTE
        assert pc.decision_retencion(recaudo(motivo='RETEIVA', confirmada=True)) == pc.CONFIRMADA
        assert pc.decision_retencion(recaudo(motivo='RETEIVA', confirmada=False)) == pc.RECHAZADA

    def test_pendiente_bloquea_toda_retencion(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(motivo='RETEIVA')
        for tipo in ('RETEIVA', 'RETEFUENTE_2.5', None):
            with pytest.raises(pc.RetencionNoAplicable, match='rechácela'):
                pc.exigir_retencion_aplicable(r, tipo)

    def test_rechazada_bloquea_esa_y_no_otra_que_elija_quien_liquida(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(motivo='RETEIVA', confirmada=False)
        with pytest.raises(pc.RetencionNoAplicable, match='rechazada'):
            pc.exigir_retencion_aplicable(r, 'RETEIVA')
        pc.exigir_retencion_aplicable(r, 'ICA_4X1000')

    def test_confirmada_y_sin_declarar_proceden(self, db, recaudo):
        from app.services import politica_cobro as pc
        pc.exigir_retencion_aplicable(recaudo(motivo='RETEIVA', confirmada=True), 'RETEIVA')
        pc.exigir_retencion_aplicable(recaudo(), 'RETEFUENTE_2.5')


def _procesar(r):
    from app.services.liquidacion_service import _procesar_recaudo
    with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
               return_value=[dict(LINEA)]), \
            patch('app.services.liquidacion_service._obtener_tercero',
                  return_value=('900', '001')), \
            patch('app.services.liquidacion_service._resolver_cuenta_cxc',
                  return_value=('003', '13050501', '99')), \
            patch('app.services.fe_resolver.resolver_fe', return_value=('FEW', '1')):
        return _procesar_recaudo(r, 'notas', admin_id=1)


def _jobs(tipo, rid):
    from app.models.siesa_job import SiesaJob
    return SiesaJob.query.filter_by(tipo=tipo, referencia_id=rid).all()


class TestElBotonMasivoLeeLaPolitica:
    """P0-5: `_procesar_recaudo` («Liquidar ruta») encolaba la NI con solo
    mirar `motivo_descuento`, sin leer la decisión."""

    RET = round(840336 * 0.025, 2)

    def test_pendiente_no_encola_ni_rc_ni_dc(self, db, recaudo):
        r = recaudo(motivo='RETEFUENTE_2.5', monto=1000000 - self.RET)
        res = _procesar(r)
        assert _jobs('DOCUMENTO_CONTABLE_RET', r.id) == []
        assert _jobs('RECIBO_CAJA', r.id) == []
        assert any('rechácela' in e for e in res['errores'])

    def test_rechazada_no_emite_la_ni(self, db, recaudo):
        """El cliente descontó la retención y la oficina dijo que no tenía
        derecho: ni NI, y el RC no sale por menos de la factura (la diferencia
        la bloquea el guard, que es la regla de «Registrar cobro»)."""
        r = recaudo(motivo='RETEFUENTE_2.5', confirmada=False, monto=1000000 - self.RET)
        with pytest.raises(ValueError, match='diferencia'):
            _procesar(r)
        assert _jobs('DOCUMENTO_CONTABLE_RET', r.id) == []

    def test_confirmada_encola_rc_neto_y_dc(self, db, recaudo):
        r = recaudo(motivo='RETEFUENTE_2.5', confirmada=True, monto=1000000 - self.RET)
        res = _procesar(r)
        assert res['rc'] == 1 and res['dc'] == 1
        [rc] = _jobs('RECIBO_CAJA', r.id)
        [dc] = _jobs('DOCUMENTO_CONTABLE_RET', r.id)
        assert rc.get_payload()['monto'] == round(1000000 - self.RET, 2)
        assert dc.get_payload()['monto'] == self.RET
        assert dc.get_payload()['tipo_retencion'] == 'RETEFUENTE_2.5'


class TestElEjecutorRevalidaLaRetencion:

    def test_una_ni_encolada_y_despues_rechazada_no_sale(self, app, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import ErrorDeterminista, _ejecutar_job
        r = recaudo(motivo='RETEFUENTE_2.5', confirmada=False, rc=True)
        job = SiesaJob.encolar('DOCUMENTO_CONTABLE_RET', {
            'recaudo_id': r.id, 'tercero_nit': '900', 'cuenta_puc': '13551501',
            'monto': 1, 'tipo_docto_fe': 'FEW', 'consec_fe': '1'},
            referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        db.session.commit()
        with patch('app.services.connekta_gateway.connekta') as mc:
            with pytest.raises(ErrorDeterminista, match='rechazada'):
                _ejecutar_job(job)
        mc.trigger_documento_contable.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El monto del RC y la base de la retención
# ═════════════════════════════════════════════════════════════════════════════

class TestMontoRC:

    def test_parcial_es_lo_cobrado_sin_restar_otra_vez(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(estado='PARCIAL', monto=500000)
        assert pc.monto_rc(r, total_neto_siesa=1000000, retencion=12000) == 500000
        assert pc.monto_rc(r, retencion=12000, monto_override=480000) == 480000
        assert pc.rc_resta_retencion(r) is False

    def test_entregado_es_el_neto_de_siesa_menos_la_retencion(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(monto=999000)
        assert pc.monto_rc(r, total_neto_siesa=1000000, retencion=21008.4) == 978991.6
        assert pc.monto_rc(r, retencion=0) == 999000         # sin Siesa: lo declarado
        assert pc.monto_rc(r, total_neto_siesa=1000000, monto_override=990000,
                           retencion=10) == 989990
        assert pc.rc_resta_retencion(r) is True


def _devolucion(db, r, lineas, vinculada=True, estado='ABIERTA', por_producto=None):
    from app.models.devolucion_cliente import DevolucionCliente, LineaDevolucionCliente
    from app.models.producto import Producto
    p = Producto(codigo=f'P{uuid.uuid4().hex[:6]}', nombre='x', codigo_siesa='REF001')
    db.session.add(p)
    db.session.flush()
    d = DevolucionCliente(codigo=f'DEV-{uuid.uuid4().hex[:6]}', tarea_packing_id=r.tarea_id,
                          tipo_docto_fe='FEW', consec_fe='1', almacen_id=r.tarea.almacen_id,
                          estado=estado, recaudo_entrega_id=r.id,
                          vinculada_factura_at=datetime.utcnow() if vinculada else None,
                          declaracion_conductor=({'declarado_por_producto': por_producto}
                                                 if por_producto else None))
    db.session.add(d)
    db.session.flush()
    for rowid, dec, cont in lineas:
        db.session.add(LineaDevolucionCliente(
            devolucion_id=d.id, producto_id=p.id, codigo_siesa='REF001',
            cantidad_facturada=10, cantidad_devuelta=cont, cantidad_declarada=dec,
            f470_rowid=rowid))
    db.session.commit()
    return d


class TestBaseDeLoEntregado:

    def test_entregado_es_la_factura_entera(self, db, recaudo):
        from app.services import politica_cobro as pc
        assert pc.base_retencion_entregada(recaudo(), [dict(LINEA)]) == (840336, 159664)

    def test_parcial_descuenta_lo_declarado_de_vuelta(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(estado='PARCIAL', monto=600000)
        _devolucion(db, r, [('R1', 4, 0)])
        base, iva = pc.base_retencion_entregada(r, [dict(LINEA)])
        assert base == round(840336 * 0.6, 2) and iva == round(159664 * 0.6, 2)

    def test_parcial_sin_devolucion_amarrada_no_inventa_base(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(estado='PARCIAL', monto=600000)
        assert pc.base_retencion_entregada(r, [dict(LINEA)]) is None
        _devolucion(db, r, [('R1', 4, 0)], vinculada=False)
        assert pc.base_retencion_entregada(r, [dict(LINEA)]) is None

    def test_doble_unidad_por_referencia_sin_contar_no_se_reparte(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(estado='PARCIAL', monto=600000)
        _devolucion(db, r, [('R1', None, 0)], por_producto={'1': 4})
        assert pc.base_retencion_entregada(r, [dict(LINEA)]) is None


class TestLasDosPuertasDanElMismoRC:
    """P1-7: una PARCIAL con retención confirmada — lo cobrado ya es neto."""

    def _mock(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.get_rowids_factura.return_value = [dict(LINEA)]
        m.get_pedido_cabecera.return_value = {'f430_id_co': '003',
                                              'f200_id_pedido_fact': '900'}
        m.get_cxc_general.return_value = []
        return m

    def test_registrar_cobro_no_resta_la_retencion_dos_veces(self, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services.liquidacion_service import LiquidacionService
        r = recaudo(estado='PARCIAL', monto=580000, motivo='RETEFUENTE_2.5',
                    confirmada=True, monto_desc=20000)
        _devolucion(db, r, [('R1', 4, 0)])
        with patch('app.services.connekta_gateway.connekta', self._mock()), \
                patch('app.services.fe_resolver.resolver_fe', return_value=('FEW', '1')):
            res = LiquidacionService.registrar_cobro_recaudo(
                r.id, admin_id=1, retenciones=[{'tipo': 'RETEFUENTE_2.5'}],
                monto_override=580000)
        assert res['monto_neto_rc'] == 580000, 'lo cobrado ya viene neto de la retención'
        [dc] = SiesaJob.query.filter_by(tipo='DOCUMENTO_CONTABLE_RET', referencia_id=r.id).all()
        # Sobre lo que se quedó (6 de 10), no sobre la factura entera.
        assert dc.get_payload()['monto'] == round(840336 * 0.6 * 0.025, 2)


# ═════════════════════════════════════════════════════════════════════════════
# 3 · ¿Se puede cambiar el cobro?
# ═════════════════════════════════════════════════════════════════════════════

class TestCongeladoAlEncolar:

    def test_sin_nada_en_cola_se_edita(self, db, recaudo):
        from app.services import politica_cobro as pc
        assert pc.puede_editar_cobro(recaudo()) is True

    @pytest.mark.parametrize('tipo', ['RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET'])
    def test_un_documento_en_cola_congela(self, db, recaudo, tipo):
        from app.models.siesa_job import SiesaJob
        from app.services import politica_cobro as pc
        r = recaudo()
        SiesaJob.encolar(tipo, {'recaudo_id': r.id}, referencia_tipo='RecaudoEntrega',
                         referencia_id=r.id)
        db.session.commit()
        assert pc.puede_editar_cobro(r) is False
        assert 'en cola' in pc.motivo_cobro_congelado(r)

    def test_un_fallido_con_la_bandera_abajo_ya_no_congela(self, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services import politica_cobro as pc
        r = recaudo()
        j = SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': r.id},
                             referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        j.estado = 'FALLIDO'
        db.session.commit()
        assert pc.puede_editar_cobro(r) is True

    def test_la_bandera_puesta_congela(self, db, recaudo):
        from app.services import politica_cobro as pc
        assert pc.puede_editar_cobro(recaudo(rc=True)) is False


class TestLaParadaConElReciboEnCola:
    """P1-3 por comportamiento: el RC está en la cola (encolado, sin POST
    todavía): la parada no cambia monto ni estado; las observaciones sí."""

    def test_el_monto_y_el_estado_quedan(self, db, almacen):
        from app.models.siesa_job import SiesaJob
        from app.models.usuario import Usuario
        from app.services.ruta_service import RutaService
        from tests.flujo import conductor_de_flujo as cf
        u = Usuario(email=f'c_{uuid.uuid4().hex[:5]}@t.co', nombre='C', rol='conductor', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        cuerpo = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 1000}
        f = cf.flujo_completo(db, almacen, u.id, 1, **cuerpo)
        SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': f.recaudo_id},
                         referencia_tipo='RecaudoEntrega', referencia_id=f.recaudo_id)
        db.session.commit()
        with pytest.raises(ValueError, match='en cola'):
            RutaService.confirmar_parada(f.ruta_id, f.packing_id, u.id,
                                         {**cuerpo, 'monto_cobrado': 900})
        db.session.rollback()
        with pytest.raises(ValueError, match='estado_entrega'):
            RutaService.confirmar_parada(f.ruta_id, f.packing_id, u.id, {
                'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'CLIENTE_CERRADO',
                'observaciones': 'x'})
        db.session.rollback()
        RutaService.confirmar_parada(f.ruta_id, f.packing_id, u.id,
                                     {**cuerpo, 'observaciones': 'dejó dicho que vuelve'})


class TestElEjecutorComparaLaParada:

    def test_la_parada_cambio_despues_de_encolar(self, app, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services import politica_cobro as pc
        from app.services.siesa_job_service import ErrorDeterminista, _ejecutar_job
        r = recaudo(monto=50000)
        foto = pc.instantanea_cobro(r)
        r.monto_cobrado = 10000          # por un camino que no pasó por la política
        job = SiesaJob.encolar('RECIBO_CAJA', {
            'recaudo_id': r.id, 'tercero_nit': '900', 'monto': 50000,
            'tipo_docto_fe': 'FEW', 'consec_fe': '1', 'instantanea': foto},
            referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        db.session.commit()
        with patch('app.services.connekta_gateway.connekta') as mc:
            with pytest.raises(ErrorDeterminista, match='cambió'):
                _ejecutar_job(job)
        mc.trigger_recibo_caja.assert_not_called()

    def test_una_parada_que_ya_no_cobra_no_manda_recibo(self, app, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import ErrorDeterminista, _ejecutar_job
        r = recaudo(estado='RECHAZADO', pago=None, monto=0)
        job = SiesaJob.encolar('RECIBO_CAJA', {
            'recaudo_id': r.id, 'tercero_nit': '900', 'monto': 50000,
            'tipo_docto_fe': 'FEW', 'consec_fe': '1'},
            referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        db.session.commit()
        with patch('app.services.connekta_gateway.connekta') as mc:
            with pytest.raises(ErrorDeterminista, match='RECHAZADO'):
                _ejecutar_job(job)
        mc.trigger_recibo_caja.assert_not_called()

    def test_sin_instantanea_no_hay_contra_que_comparar(self, db, recaudo):
        from app.services import politica_cobro as pc
        assert pc.difiere_de_instantanea(recaudo(), None) == []
        r = recaudo(monto=35700)
        assert pc.difiere_de_instantanea(r, {'monto_cobrado': 35700.0}) == []


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Los trinquetes de clase (AST)
# ═════════════════════════════════════════════════════════════════════════════

def _fuentes():
    return {str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8')
            for p in APP.rglob('*.py')}


def _funciones_con_nombre(arbol, prefijo=''):
    """(nombre calificado, nodo) de toda función (métodos con su clase)."""
    out = []
    for n in ast.iter_child_nodes(arbol):
        if isinstance(n, ast.ClassDef):
            out += _funciones_con_nombre(n, f'{prefijo}{n.name}.')
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((f'{prefijo}{n.name}', n))
            out += _funciones_con_nombre(n, f'{prefijo}{n.name}.')
    return out


def _llamadas(fn):
    pila = list(ast.iter_child_nodes(fn))
    while pila:
        n = pila.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            yield n
        pila.extend(ast.iter_child_nodes(n))


def _nombre(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)


def _tipo_encolado(call):
    if _nombre(call) != 'encolar':
        return None
    arg = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg == 'tipo'), None)
    return arg.value if isinstance(arg, ast.Constant) else None


# ── 4.1 · Toda NI de retención pasa por la política ─────────────────────────

#: Las funciones que pueden llamar `.encolar('DOCUMENTO_CONTABLE_RET', …)`.
#: **Una.** Solo encoge.
ENCOLADORES_DE_RETENCION = {'app/services/liquidacion_service.py::_encolar_retencion'}


def encoladores_de_retencion(fuentes):
    """{sitio: llama a la política?}"""
    out = {}
    for archivo, src in fuentes.items():
        for nombre, fn in _funciones_con_nombre(ast.parse(src)):
            calls = list(_llamadas(fn))
            if any(_tipo_encolado(c) == 'DOCUMENTO_CONTABLE_RET' for c in calls):
                out[f'{archivo}::{nombre}'] = any(
                    _nombre(c) == 'exigir_retencion_aplicable' for c in calls)
    return out


class TestTodaRetencionPasaPorLaPolitica:

    def test_un_solo_encolador_y_llama_a_la_politica(self):
        sitios = encoladores_de_retencion(_fuentes())
        assert set(sitios) == ENCOLADORES_DE_RETENCION, (
            f'\nEncolan DOCUMENTO_CONTABLE_RET: {sorted(sitios)}. Solo '
            '`_encolar_retencion` puede: es la que pasa por '
            '`politica_cobro.exigir_retencion_aplicable`.')
        assert all(sitios.values()), f'{sitios}: el encolador ya no llama a la política'

    def test_meta_ve_las_dos_escrituras_y_no_otras(self):
        src = ("def a():\n    SiesaJob.encolar('DOCUMENTO_CONTABLE_RET', {})\n"
               "def b():\n    SiesaJob.encolar(tipo='DOCUMENTO_CONTABLE_RET', payload={})\n"
               "def c():\n    SiesaJob.encolar('RECIBO_CAJA', {})\n"
               "def d():\n    '''SiesaJob.encolar('DOCUMENTO_CONTABLE_RET')'''\n"
               "def e():\n    exigir_retencion_aplicable(r, t)\n"
               "    SiesaJob.encolar('DOCUMENTO_CONTABLE_RET', {})\n")
        s = encoladores_de_retencion({'x.py': src})
        assert s == {'x.py::a': False, 'x.py::b': False, 'x.py::e': True}

    def test_piso(self):
        assert len(encoladores_de_retencion(_fuentes())) >= 1


# ── 4.2 · El RC sale por `monto_rc` ─────────────────────────────────────────

#: Posición del argumento `monto` en `_encolar_recibo_caja(recaudo, tipo_docto_fe,
#: consec_fe, tercero_nit, sucursal, monto, …)`.
_POS_MONTO = 5


def montos_de_rc(fuentes):
    """{sitio: el monto sale de `monto_rc`?} para toda llamada a
    `_encolar_recibo_caja`."""
    out = {}
    for archivo, src in fuentes.items():
        for nombre, fn in _funciones_con_nombre(ast.parse(src)):
            # Toda asignación al nombre cuenta: `m = monto_rc(...)` y después
            # `m = m - ret` ya no es el monto de la política.
            asignados = {}
            for n in ast.walk(fn):
                objetivos = (n.targets if isinstance(n, ast.Assign) else
                             [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else [])
                for t in objetivos:
                    if isinstance(t, ast.Name):
                        v = getattr(n, 'value', None)
                        origen = (_nombre(v) if isinstance(n, ast.Assign)
                                  and isinstance(v, ast.Call) else '<expresión>')
                        asignados.setdefault(t.id, set()).add(origen)
            for c in _llamadas(fn):
                if _nombre(c) != '_encolar_recibo_caja':
                    continue
                arg = c.args[_POS_MONTO] if len(c.args) > _POS_MONTO else next(
                    (k.value for k in c.keywords if k.arg == 'monto'), None)
                ok = isinstance(arg, ast.Name) and asignados.get(arg.id) == {'monto_rc'}
                out[f'{archivo}::{nombre}:{c.lineno}'] = ok
    return out


class TestElReciboSalePorMontoRC:

    def test_toda_llamada_pasa_un_monto_de_monto_rc(self):
        sitios = montos_de_rc(_fuentes())
        malos = [s for s, ok in sitios.items() if not ok]
        assert not malos, (
            f'\n{malos}: el monto del recibo no sale de `politica_cobro.monto_rc`. '
            'Dos puertas con dos montos ya dejaron el RC corto (P1-7).')

    def test_meta(self):
        src = ("def bien():\n    m = monto_rc(r)\n    _encolar_recibo_caja(r, t, c, n, s, m, f)\n"
               "def crudo():\n    m = float(r.monto_cobrado)\n"
               "    _encolar_recibo_caja(r, t, c, n, s, m, f)\n"
               "def restado():\n    _encolar_recibo_caja(r, t, c, n, s, monto - ret, f)\n"
               "def mixto():\n    m = monto_rc(r)\n    m = m - ret\n"
               "    _encolar_recibo_caja(r, t, c, n, s, m, f)\n")
        s = montos_de_rc({'x.py': src})
        assert {k.split(':')[2]: v for k, v in s.items()} == {
            'bien': True, 'crudo': False, 'restado': False, 'mixto': False}

    def test_piso(self):
        assert len(montos_de_rc(_fuentes())) >= 3


# ── 4.3 · Toda escritura del cobro pregunta si se puede ─────────────────────

_CAMPOS_DEL_COBRO = ('monto_cobrado', 'estado_entrega')

#: Escriben esos campos sin ser un recaudo: el porqué. Solo encoge.
NO_SON_RECAUDOS = {
    'app/services/analitica_recorrido.py::_evaluar':
        '`p` es la fila del recorrido en memoria (un pedido), no un RecaudoEntrega',
}


def _llamadas_en_condiciones(fn):
    """Las Call que están en la CONDICIÓN de un `if`/`while`/ternario: una
    guarda. Nombrar la política en el texto de un error no es preguntarle
    (mutación M11: la guarda cambiada por la bandera y el mensaje intacto)."""
    for n in ast.walk(fn):
        if isinstance(n, (ast.If, ast.While, ast.IfExp)):
            for c in ast.walk(n.test):
                if isinstance(c, ast.Call):
                    yield c


def escritores_del_cobro(fuentes):
    """{sitio: pregunta a la política?} — toda función que asigna
    `x.monto_cobrado`/`x.estado_entrega` (o `setattr` con ese nombre), y si
    `puede_editar_cobro` aparece en la condición de un `if`."""
    out = {}
    for archivo, src in fuentes.items():
        for nombre, fn in _funciones_con_nombre(ast.parse(src)):
            escribe = False
            for n in ast.walk(fn):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn:
                    continue
                objetivos = (n.targets if isinstance(n, ast.Assign) else
                             [n.target] if isinstance(n, (ast.AugAssign, ast.AnnAssign)) else [])
                if any(isinstance(t, ast.Attribute) and t.attr in _CAMPOS_DEL_COBRO
                       for t in objetivos):
                    escribe = True
                if (isinstance(n, ast.Call) and _nombre(n) == 'setattr' and len(n.args) > 1
                        and isinstance(n.args[1], ast.Constant)
                        and n.args[1].value in _CAMPOS_DEL_COBRO):
                    escribe = True
            if escribe:
                out[f'{archivo}::{nombre}'] = any(
                    _nombre(c) == 'puede_editar_cobro' for c in _llamadas_en_condiciones(fn))
    return out


class TestTodaEscrituraDelCobroPregunta:

    def test_ninguna_escribe_sin_preguntar(self):
        sitios = escritores_del_cobro(_fuentes())
        malos = [s for s, ok in sitios.items() if not ok and s not in NO_SON_RECAUDOS]
        assert not malos, (
            f'\n{malos}: escriben el monto o el estado de una parada sin '
            '`politica_cobro.puede_editar_cobro`. Con el recibo en cola, Siesa '
            'recibiría una cifra y el WMS diría otra (P1-3).')

    def test_el_inventario_existe_y_solo_encoge(self):
        sitios = escritores_del_cobro(_fuentes())
        assert set(NO_SON_RECAUDOS) <= set(sitios), 'inventario viejo: sacar lo que ya no escribe'
        assert len(NO_SON_RECAUDOS) <= 1
        assert all(len(v) > 20 for v in NO_SON_RECAUDOS.values())

    def test_meta(self):
        src = ("def a(r):\n    r.monto_cobrado = 1\n"
               "def b(r):\n    setattr(r, 'estado_entrega', 'X')\n"
               "def c(r):\n    if puede_editar_cobro(r):\n        r.monto_cobrado = 1\n"
               "def g(r):\n    if r.siesa_rc_triggered:\n        raise E(puede_editar_cobro(r))\n"
               "    r.monto_cobrado = 1\n"
               "def d(r):\n    '''r.monto_cobrado = 1'''\n    return r.monto_cobrado\n"
               "def e(r):\n    r.monto_cobrado += 5\n")
        s = escritores_del_cobro({'x.py': src})
        assert s == {'x.py::a': False, 'x.py::b': False, 'x.py::c': True, 'x.py::e': False,
                     'x.py::g': False}

    def test_piso(self):
        sitios = escritores_del_cobro(_fuentes())
        assert 'app/services/ruta_service.py::RutaService.confirmar_parada' in sitios
        assert 'app/services/liquidacion_service.py::LiquidacionService.corregir_monto_declarado' in sitios


# ── 4.4 · Solo `liquidar_ruta` liquida ──────────────────────────────────────

LIQUIDAN = {'app/services/ruta_service.py::RutaService.liquidar_ruta'}


def llamadores_de_marcar_liquidada(fuentes):
    out = set()
    for archivo, src in fuentes.items():
        for nombre, fn in _funciones_con_nombre(ast.parse(src)):
            if any(_nombre(c) == '_marcar_liquidada' for c in _llamadas(fn)):
                out.add(f'{archivo}::{nombre}')
    return out


class TestSoloLiquidarRutaLiquida:
    """P1-5: `forzar_cierre_ruta` llamaba `_marcar_liquidada` y se saltaba las
    guardas de `liquidar_ruta` (crédito no autorizado, devoluciones sin
    contar): el cierre forzado era una puerta trasera a la liquidación."""

    def test_un_solo_llamador(self):
        assert llamadores_de_marcar_liquidada(_fuentes()) == LIQUIDAN

    def test_meta(self):
        src = ("class R:\n    def liquidar_ruta(self):\n        R._marcar_liquidada(1)\n"
               "    def forzar(self):\n        self._marcar_liquidada(1)\n"
               "    def doc(self):\n        '''_marcar_liquidada()'''\n")
        assert llamadores_de_marcar_liquidada({'x.py': src}) == {
            'x.py::R.liquidar_ruta', 'x.py::R.forzar'}


# ═════════════════════════════════════════════════════════════════════════════
# 5 · ¿El recibo llegó?
# ═════════════════════════════════════════════════════════════════════════════

class TestRcLlego:

    def test_senal_positiva(self, db, recaudo):
        from app.services import politica_cobro as pc
        r = recaudo(rc=True)
        assert pc.rc_llego_a_siesa(r) is False
        r.siesa_rc_resultado = 'ENVIADO'
        assert pc.rc_llego_a_siesa(r) is True
        r.siesa_rc_resultado = 'SIN_VERIFICAR'
        assert pc.rc_llego_a_siesa(r) is False

    def test_legado_completado_sin_verificar_no_cuenta(self, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services import politica_cobro as pc
        r = recaudo(rc=True)
        j = SiesaJob.encolar('RECIBO_CAJA', {}, referencia_tipo='RecaudoEntrega',
                             referencia_id=r.id)
        j.marcar_completado({'verificacion_imposible': True})
        db.session.commit()
        assert pc.rc_llego_a_siesa(r) is False
        j2 = SiesaJob.encolar('RECIBO_CAJA', {}, referencia_tipo='RecaudoEntrega',
                              referencia_id=r.id)
        j2.marcar_completado({'codigo': 0})
        db.session.commit()
        assert pc.rc_llego_a_siesa(r) is True
