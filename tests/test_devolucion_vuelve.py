"""
Si el cliente no paga, la mercancía VUELVE y entra al inventario — sin cabos
sueltos (m045devol, 2026-09-24).

Cada clase cierra una CLASE de cabo suelto, no solo el caso:

1. **La devolución nacía al liquidar.** Entre el regreso del camión y la
   liquidación la mercancía no existía en el WMS. Ahora nace EN_CAMION en
   `confirmar_parada`, con UNA función (`devolucion_ruta._crear_de_ruta`).
   Trinquete AST: toda escritura de `estado_entrega` pasa por
   `sincronizar_con_parada`; nadie más pasa `recaudo_entrega_id` a
   `crear_devolucion`.
2. **Recepción no tenía cola.** «Llegó el camión»: bultos RETORNADO / FALTANTE,
   cuadre exacto, y el conteo acepta el cero (FALTANTE_TOTAL, medido).
3. **Doble reingreso.** Una devolución activa por línea de factura (índice en
   la base) y el tope cuenta las previas; mostrador sobre ruta activa → no.
4. **Reingreso vendible con la NC en Elaboración.** Zona DEVOLUCION hasta la NC
   aprobada; `liberar_reingreso` es la única puerta a picking. Trinquete AST.
5. **«Ya la aprobé» sin verificar.** Cron que lee `f350_ind_estado`; el botón
   queda como respaldo con motivo y bitácora.
6. **La liquidación dejaba cabos.** No liquida con mercancía sin contar (salvo
   motivo), el RC sale por lo cobrado cuando la NC no va a llegar, la NC sin
   líneas ya no se da por completada, la referencia sin producto es un error
   visible.
7. **Invariantes con detector ciego** (DEV-06 … DEV-13) y DEV-04 arreglado.
8. **Medición**: horas rechazo→conteo→aprobación, faltante valorizado, cuadre.
"""
import ast
import json
import pathlib
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.bulto import Bulto, EstadoBulto
from app.models.devolucion_cliente import (DevolucionCliente, EstadoDevolucionCliente as E,
                                           LineaDevolucionCliente)
from app.models.recaudo_entrega import RecaudoEntrega
from app.services import devolucion_ruta as dr
from app.services.devolucion_cliente_service import DevolucionClienteService as DCS
from app.services.ruta_service import RutaService

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'
PWA = APP / 'static' / 'pwa'

V4 = {'version_formulario': dr.VERSION_FORMULARIO_DEVOLUCION}


# ═════════════════════════════════════════════════════════════════════════════
# Mundo
# ═════════════════════════════════════════════════════════════════════════════

def _usuario(db, rol, almacen=None):
    from app.models.usuario import Usuario
    u = Usuario(email=f'{rol}_{uuid.uuid4().hex[:6]}@dev.test', nombre=f'{rol} dev', rol=rol,
                activo=True, almacen_id=almacen.id if almacen else None)
    u.password_hash = 'x'       # sin hash: el scrypt de verdad cuesta ~1 s por usuario
    db.session.add(u)
    db.session.flush()
    return u


def _mundo(db, almacen, productos, cantidades=None, n_bultos=2, fe=('FEW', '500')):
    """Ruta EN_TRANSITO con una parada: empaque despachado con su FE, los
    productos empacados y sus bultos cargados. Nada de Siesa."""
    from app.models.conductor import Conductor
    from app.models.packing import ItemPacking, TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    if not isinstance(productos, (list, tuple)):
        productos = [productos]
    cantidades = cantidades or [10] * len(productos)
    u = _usuario(db, 'conductor')
    c = Conductor(usuario_id=u.id, nombre='Conductor Dev', cedula=f'CC{uuid.uuid4().hex[:6]}',
                  activo=True)
    db.session.add(c); db.session.flush()
    ruta = RutaDespacho(conductor_id=c.id, tipo_ruta='Urbana', estado='EN_TRANSITO',
                        fecha_cierre=datetime.utcnow() - timedelta(hours=3))
    db.session.add(ruta); db.session.flush()
    sufijo = uuid.uuid4().hex[:6]
    tarea = TareaPacking(codigo=f'PK-DV-{sufijo}', estado='DESPACHADO', tipo_documento='PEDIDO',
                         almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa=1, numero_pedido_siesa=f'PD-DV-{sufijo}',
                         cliente='Tienda La Esquina', municipio='Neiva', siesa_triggered=True,
                         fe_tipo=fe[0] if fe else None, fe_consec=fe[1] if fe else None)
    db.session.add(tarea); db.session.flush()
    for p, cant in zip(productos, cantidades):
        db.session.add(ItemPacking(tarea_id=tarea.id, producto_id=p.id, cantidad_esperada=cant,
                                   cantidad_real=cant, verificado=True))
    for i in range(1, n_bultos + 1):
        db.session.add(Bulto(tarea_id=tarea.id, ruta_despacho_id=ruta.id,
                             codigo_barras=f'B-{uuid.uuid4().hex[:8]}', tipo='CAJA',
                             numero=i, total=n_bultos, estado='CARGADO'))
    db.session.commit()
    return SimpleNamespace(ruta=ruta, tarea=tarea, conductor=c, uid=u.id)


def _fila(p, rowid='700', cant=10, neto=100000.0, uom='UND', ref=None):
    return {'f470_rowid': rowid, 'f120_referencia': ref or p.codigo_siesa, 'f470_cant_base': cant,
            'f470_vlr_neto': neto, 'f470_id_unidad_medida': uom, 'f150_id': 'NB1'}


def _gw(filas):
    gw = MagicMock()
    gw.get_rowids_factura.return_value = filas
    gw.modo_simulacion = False
    gw.bodega = 'NB1'
    return gw


def _rechazar(m, motivo='CLIENTE_CERRADO', datos=None):
    return RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
        **V4, 'estado_entrega': 'RECHAZADO', 'motivo_rechazo': motivo,
        'observaciones': 'no recibió', **(datos or {})})


def _parcial(m, producto, entregado, pedida=10, datos=None):
    return RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
        **V4, 'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 1000,
        'observaciones': 'devolvió una parte',
        'items_entregados': [{'codigo': producto.codigo, 'nombre': producto.nombre,
                              'unidad': 'und', 'cantidad_pedida': pedida,
                              'cantidad_entregada': entregado}],
        **(datos or {})})


def _dev(m):
    rec = RecaudoEntrega.query.filter_by(ruta_id=m.ruta.id, tarea_id=m.tarea.id).one()
    return rec, DevolucionCliente.query.filter_by(recaudo_entrega_id=rec.id).order_by(
        DevolucionCliente.id.desc()).first()


def _contar(dev, gw, lineas=None, uid=None):
    with patch('app.services.devolucion_cliente_service.connekta', gw), \
            patch('app.services.siesa_job_service.disparar_dlq_inmediato'):
        return DCS.confirmar_entrada_fisica(dev.id, recepcionista_id=uid, lineas_ajustadas=lineas)


def _stock(producto_id, zona=None):
    from app.models.inventario import UbicacionProducto
    from app.models.ubicacion import Ubicacion
    q = (UbicacionProducto.query.join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
         .filter(UbicacionProducto.producto_id == producto_id))
    if zona:
        q = q.filter(Ubicacion.tipo_zona == zona)
    return sum(float(r.cantidad or 0) for r in q.all())


def _jobs(tipo):
    from app.models.siesa_job import SiesaJob
    return SiesaJob.query.filter_by(tipo=tipo).all()


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La devolución nace con el rechazo
# ═════════════════════════════════════════════════════════════════════════════

class TestLaDevolucionNaceConElRechazo:

    def test_rechazado_crea_en_camion_con_lo_empacado(self, db, almacen, producto, producto2):
        m = _mundo(db, almacen, [producto, producto2], [10, 4])
        _rechazar(m)
        rec, dev = _dev(m)
        assert dev.estado == E.EN_CAMION and dev.es_total is True
        assert {(l.producto_id, float(l.cantidad_declarada)) for l in dev.lineas} == {
            (producto.id, 10.0), (producto2.id, 4.0)}
        assert (dev.tipo_docto_fe, dev.consec_fe) == ('FEW', '500')
        assert dev.declaracion_conductor['estado'] == 'RECHAZADO'
        assert all(b.estado == EstadoBulto.RECHAZADO
                   for b in Bulto.query.filter_by(tarea_id=m.tarea.id).all())

    def test_mercancia_averiada_llega_premarcada(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m, motivo='MERCANCIA_AVERIADA')
        _, dev = _dev(m)
        assert dev.lineas[0].averiadas() == 10.0 and dev.lineas[0].sanas() == 0

    def test_parcial_crea_con_lo_declarado(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _parcial(m, producto, entregado=7)
        _, dev = _dev(m)
        assert dev.estado == E.EN_CAMION and dev.es_total is False
        assert [(float(l.cantidad_devuelta), float(l.cantidad_declarada)) for l in dev.lineas] \
            == [(3.0, 3.0)]

    def test_parcial_sin_referencia_devuelta_se_rechaza(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        with pytest.raises(ValueError, match='dice qué volvió'):
            _parcial(m, producto, entregado=10)

    def test_parcial_sin_items_con_formulario_nuevo_se_rechaza(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        with pytest.raises(ValueError, match='dice qué volvió'):
            RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
                **V4, 'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO',
                'monto_cobrado': 1000, 'observaciones': 'x'})

    def test_parcial_sin_items_de_la_cola_vieja_no_se_traba(self, db, almacen, producto):
        """Un ítem de la cola offline armado por un PWA < 4: no se rechaza (se
        quedaría en el teléfono). Su devolución nace SIN declaración: recepción
        cuenta contra la factura. Antes se volvía devolución TOTAL."""
        m = _mundo(db, almacen, producto)
        RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
            'version_formulario': 3, 'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO',
            'monto_cobrado': 1000, 'observaciones': 'x'})
        _, dev = _dev(m)
        assert dev.es_total is False and dev.lineas == []
        assert dev.declaracion_conductor['sin_items'] is True
        dr.vincular_a_factura(dev, gateway=_gw([_fila(producto)]))
        assert [(float(l.cantidad_devuelta), l.cantidad_declarada) for l in dev.lineas] == [(0.0, None)]

    def test_reconfirmar_no_duplica_y_actualiza_con_bitacora(self, db, almacen, producto):
        from app.models.bitacora import BitacoraAccion
        m = _mundo(db, almacen, producto)
        _parcial(m, producto, entregado=7)
        _parcial(m, producto, entregado=5)
        rec, dev = _dev(m)
        assert DevolucionCliente.query.filter_by(recaudo_entrega_id=rec.id).count() == 1
        assert float(dev.lineas[0].cantidad_declarada) == 5.0
        assert BitacoraAccion.query.filter_by(accion='EDITAR', entidad='DevolucionCliente',
                                              entidad_id=dev.id).count() == 1

    def test_reconfirmar_igual_no_toca_nada(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev1 = _dev(m)
        _rechazar(m)
        _, dev2 = _dev(m)
        assert dev1.id == dev2.id and len(dev2.lineas) == 1

    def test_reconfirmar_como_entregado_la_cancela_el_sistema(self, db, almacen, producto):
        from app.models.bitacora import BitacoraAccion
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
            **V4, 'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 5000})
        _, dev = _dev(m)
        assert dev.estado == E.CANCELADA and dev.cancelada_at is not None
        assert BitacoraAccion.query.filter_by(accion='CANCELAR', entidad_id=dev.id).count() == 1

    def test_no_pago_y_se_quedo_no_crea_devolucion(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
            'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO_SE_QUEDO',
            'observaciones': 'se quedó'})
        rec, dev = _dev(m)
        assert rec.estado_entrega == 'ENTREGADO_SIN_PAGO' and dev is None

    def test_si_armarla_falla_la_parada_se_confirma_igual(self, db, almacen, producto, monkeypatch):
        """La entrega no se traba por la devolución (SAVEPOINT). El hueco lo
        ve DEV-06 y la liquidación la asegura después."""
        m = _mundo(db, almacen, producto)

        def _revienta(*a, **k):
            raise RuntimeError('boom')
        monkeypatch.setattr(dr, '_crear_de_ruta', _revienta)
        _rechazar(m)
        rec, dev = _dev(m)
        assert rec.estado_entrega == 'RECHAZADO' and dev is None

    def test_forzar_cierre_crea_la_devolucion_de_lo_no_gestionado(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        admin = _usuario(db, 'admin')
        RutaService.forzar_cierre_ruta(m.ruta.id, admin.id, motivo='el conductor no confirmó')
        _, dev = _dev(m)
        assert dev is not None and dev.estado == E.EN_CAMION and dev.es_total is True


class TestLaReconfirmacionNoReescribeLoRecibido:

    def test_con_la_devolucion_contada_no_se_cambia_la_entrega(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        with pytest.raises(ValueError, match='ya se contó'):
            RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
                **V4, 'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                'monto_cobrado': 5000})
        db.session.rollback()
        # Las observaciones sí.
        _rechazar(m, datos={'observaciones': 'corrijo el texto'})

    def test_un_bulto_recibido_no_se_pisa(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        b = Bulto.query.filter_by(tarea_id=m.tarea.id).first()
        dr.escanear_bulto_de_vuelta(m.ruta.id, b.codigo_barras, m.uid)
        with pytest.raises(ValueError, match='ya se recibieron'):
            RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
                **V4, 'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                'monto_cobrado': 5000})
        db.session.rollback()
        _rechazar(m, datos={'observaciones': 'otra'})
        assert db.session.get(Bulto, b.id).estado == EstadoBulto.RETORNADO


# ═════════════════════════════════════════════════════════════════════════════
# 2 · «Llegó el camión»
# ═════════════════════════════════════════════════════════════════════════════

class TestLlegoElCamion:

    def test_escanear_y_cerrar_dejan_el_cuadre_exacto(self, db, almacen, producto):
        m = _mundo(db, almacen, producto, n_bultos=3)
        _rechazar(m)
        cola = dr.cola_llegadas()
        assert [r['ruta_id'] for r in cola] == [m.ruta.id]
        assert cola[0]['cuadre']['en_camion_sin_recibir'] == 3
        bultos = Bulto.query.filter_by(tarea_id=m.tarea.id).order_by(Bulto.numero).all()
        dr.escanear_bulto_de_vuelta(m.ruta.id, bultos[0].codigo_barras, m.uid)
        dr.escanear_bulto_de_vuelta(m.ruta.id, bultos[1].codigo_barras, m.uid)
        r = dr.cerrar_llegada(m.ruta.id, m.uid)
        assert r['bultos_faltantes'] == 1 and r['devoluciones_en_bodega'] == 1
        c = dr.cuadre_de_bultos(m.ruta.id)
        assert (c['salieron'], c['retornados'], c['faltantes'], c['exacto']) == (3, 2, 1, True)
        _, dev = _dev(m)
        assert dev.estado == E.ABIERTA and dev.fecha_llegada is not None
        assert db.session.get(Bulto, bultos[2].id).estado == EstadoBulto.FALTANTE
        # La ruta sigue en la cola: falta contar la devolución.
        assert dr.cola_llegadas()[0]['cuadre']['en_camion_sin_recibir'] == 0

    def test_no_recibe_un_bulto_que_el_conductor_entrego(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        RutaService.confirmar_parada(m.ruta.id, m.tarea.id, m.uid, {
            **V4, 'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 5000})
        b = Bulto.query.filter_by(tarea_id=m.tarea.id).first()
        with pytest.raises(ValueError, match='declaró el bulto'):
            dr.escanear_bulto_de_vuelta(m.ruta.id, b.codigo_barras, m.uid)

    def test_parcial_acepta_una_caja_no_marcada_y_lo_dice(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _parcial(m, producto, entregado=7)
        b = Bulto.query.filter_by(tarea_id=m.tarea.id).first()
        r = dr.escanear_bulto_de_vuelta(m.ruta.id, b.codigo_barras, m.uid)
        assert r['no_declarado'] is True

    def test_bulto_de_otra_ruta(self, db, almacen, producto):
        m1 = _mundo(db, almacen, producto)
        m2 = _mundo(db, almacen, producto)
        _rechazar(m1)
        b = Bulto.query.filter_by(tarea_id=m1.tarea.id).first()
        with pytest.raises(ValueError, match='es de la ruta'):
            dr.escanear_bulto_de_vuelta(m2.ruta.id, b.codigo_barras, m2.uid)


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Contar: sanas, averiadas, cero
# ═════════════════════════════════════════════════════════════════════════════

class TestContar:

    def test_sanas_a_devoluciones_averiadas_a_averiados(self, db, almacen, producto):
        from app.services.picking_service import ZONA_AVERIAS, ZONA_DEVOLUCION
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        gw = _gw([_fila(producto, rowid='700', cant=10)])
        with patch('app.services.devolucion_cliente_service.connekta', gw):
            dr.vincular_a_factura(dev, gateway=gw)
        linea = dev.lineas[0]
        _contar(dev, gw, [{'linea_id': linea.id, 'cantidad_devuelta': 6, 'cantidad_averiada': 2}])
        assert dev.estado == E.CONFIRMADA
        assert _stock(producto.id, ZONA_DEVOLUCION) == 4
        assert _stock(producto.id, ZONA_AVERIAS) == 2
        job = _jobs('NOTA_CREDITO_DEVOLUCION_CLIENTE')[0]
        item = json.loads(job.payload)['items_devueltos'][0]
        assert (item['cantidad_devuelta'], item['es_averiado'], item['cantidad_averiada'],
                item['f470_rowid']) == (6.0, False, 2.0, '700')

    def test_toda_averiada_va_entera_a_averias_en_la_nc(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m, motivo='MERCANCIA_AVERIADA')
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        item = json.loads(_jobs('NOTA_CREDITO_DEVOLUCION_CLIENTE')[0].payload)['items_devueltos'][0]
        assert item['es_averiado'] is True

    def test_contar_en_cero_es_faltante_total_medido(self, db, almacen, producto):
        from app.models.inventario import MovimientoInventario
        from app.services import senales_ruta as sr
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        rec, dev = _dev(m)
        gw = _gw([_fila(producto)])
        dr.vincular_a_factura(dev, gateway=gw)
        _contar(dev, gw, [{'linea_id': dev.lineas[0].id, 'cantidad_devuelta': 0}])
        assert dev.estado == E.FALTANTE_TOTAL and dev.fecha_confirmacion is not None
        assert _jobs('NOTA_CREDITO_DEVOLUCION_CLIENTE') == []
        assert MovimientoInventario.query.filter_by(producto_id=producto.id).count() == 0
        f = sr.faltante_de_retorno(dev)
        assert f['faltante_unidades'] == 10
        assert dr.nc_no_llegara(rec) is True

    def test_mostrador_en_cero_sigue_prohibido(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        dev = DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None, [
            {'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
             'cantidad_facturada': 10, 'cantidad_devuelta': 2, 'f470_rowid': '700'}])
        with pytest.raises(ValueError, match='cancele'):
            _contar(dev, _gw([_fila(producto)]), [{'producto_id': producto.id,
                                                    'cantidad_devuelta': 0}])

    def test_referencia_de_la_factura_sin_producto_es_un_error_visible(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        gw = _gw([_fila(producto), _fila(producto, rowid='701', ref='REF-FANTASMA')])
        with pytest.raises(ValueError, match='REF-FANTASMA'):
            _contar(dev, gw)
        db.session.refresh(dev)
        assert 'REF-FANTASMA' in dev.problema_factura and dev.estado == E.EN_CAMION

    def test_doble_unidad_total_entra_entera_y_parcial_se_cuenta_por_linea(
            self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _parcial(m, producto, entregado=9)
        _, dev = _dev(m)
        gw = _gw([_fila(producto, rowid='800', cant=1, uom='PQ', neto=24250),
                  _fila(producto, rowid='801', cant=5, uom='UND', neto=12125)])
        dr.vincular_a_factura(dev, gateway=gw)
        assert sorted(l.f470_rowid for l in dev.lineas) == ['800', '801']
        assert sum(float(l.cantidad_devuelta) for l in dev.lineas) == 0
        pq = next(l for l in dev.lineas if l.f470_rowid == '800')
        _contar(dev, gw, [{'linea_id': pq.id, 'cantidad_devuelta': 1}])
        assert dev.estado == E.CONFIRMADA
        from app.services import senales_ruta as sr
        f = sr.faltante_de_retorno(dev)
        assert f is not None and f['faltante_unidades'] == 0  # declaró 1, volvió 1

    def test_lo_declarado_fuera_de_la_factura_no_entra_con_cantidad(self, db, almacen, producto,
                                                                     producto2):
        m = _mundo(db, almacen, [producto, producto2], [10, 4])
        _rechazar(m)
        _, dev = _dev(m)
        # La factura solo trae el primero (lo del segundo se facturó en otra).
        m2 = dev.lineas
        gw = _gw([_fila(producto)])
        dr.vincular_a_factura(dev, gateway=gw)
        assert [l.codigo_siesa for l in dev.lineas] == [producto.codigo_siesa]


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Una línea de factura, una devolución activa, y el tope cuenta las previas
# ═════════════════════════════════════════════════════════════════════════════

class TestUnaDevolucionActivaPorLineaDeFactura:

    def test_mostrador_sobre_una_devolucion_de_ruta_activa_se_rechaza(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        with pytest.raises(ValueError, match='devolución de ruta'):
            DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None, [
                {'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
                 'cantidad_facturada': 10, 'cantidad_devuelta': 2, 'f470_rowid': '700'}])

    def test_el_tope_cuenta_las_devoluciones_anteriores(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        linea = {'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
                 'cantidad_facturada': 10, 'f470_rowid': '700'}
        d1 = DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None,
                                  [{**linea, 'cantidad_devuelta': 6}])
        _contar(d1, _gw([_fila(producto)]))
        with pytest.raises(ValueError, match='ya se devolvieron 6'):
            DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None,
                                 [{**linea, 'cantidad_devuelta': 5}])
        # La de ruta sobre la misma factura: arranca de lo que queda (4).
        _rechazar(m)
        _, dev = _dev(m)
        dr.vincular_a_factura(dev, gateway=_gw([_fila(producto)]))
        assert float(dev.lineas[0].cantidad_devuelta) == 4.0

    def test_dos_activas_sobre_la_misma_linea_se_rechaza_y_cancelar_la_libera(
            self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        linea = {'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
                 'cantidad_facturada': 10, 'cantidad_devuelta': 1, 'f470_rowid': '700'}
        d1 = DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None, [linea])
        with pytest.raises(ValueError, match='ya está en la devolución'):
            DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None, [linea])
        db.session.rollback()
        sup = _usuario(db, 'supervisor')
        DCS.cancelar(d1.id, sup.id, motivo='se armó por error')
        DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None, [linea])

    def test_la_base_lo_impide_aunque_alguien_se_salte_el_servicio(self, db, almacen, producto):
        from sqlalchemy.exc import IntegrityError
        m = _mundo(db, almacen, producto)
        linea = {'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
                 'cantidad_facturada': 10, 'cantidad_devuelta': 1, 'f470_rowid': '700'}
        DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None, [linea])
        d2 = DevolucionCliente(codigo='DEVC-A-MANO', tarea_packing_id=m.tarea.id,
                               tipo_docto_fe='FEW', consec_fe='500', almacen_id=almacen.id,
                               estado='ABIERTA')
        db.session.add(d2); db.session.flush()
        db.session.add(LineaDevolucionCliente(devolucion_id=d2.id, producto_id=producto.id,
                                              codigo_siesa='x', cantidad_devuelta=1,
                                              f470_rowid='700', f470_rowid_activo='700'))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Liquidación: encuentra, no crea; no liquida sin contar; el RC sale
# ═════════════════════════════════════════════════════════════════════════════

def _liquidar_siesa(ruta_id, gw):
    from app.services.liquidacion_service import LiquidacionService
    with patch('app.services.connekta_gateway.connekta', gw), \
            patch('app.services.liquidacion_service._obtener_tercero',
                  return_value=('900123456', '001')), \
            patch('app.services.liquidacion_service._resolver_cuenta_cxc',
                  return_value=('003', '13050501', '99')):
        return LiquidacionService.liquidar_ruta_siesa(ruta_id)


class TestLaLiquidacionEncuentraLaDevolucion:

    def test_encuentra_la_de_la_parada_y_no_crea_otra(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        r = _liquidar_siesa(m.ruta.id, _gw([_fila(producto)]))
        rec, dev = _dev(m)
        assert r['nc_encolados'] == 1 and not r['errores']
        assert DevolucionCliente.query.filter_by(recaudo_entrega_id=rec.id).count() == 1
        assert dev.vinculada_factura_at is not None

    def test_la_referencia_sin_producto_llega_a_los_errores(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        r = _liquidar_siesa(m.ruta.id, _gw([_fila(producto),
                                             _fila(producto, rowid='9', ref='SIN-WMS')]))
        assert any('SIN-WMS' in e['error'] for e in r['errores'])

    def test_no_liquida_con_mercancia_sin_contar_salvo_motivo(self, db, almacen, producto):
        from app.models.bitacora import BitacoraAccion
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        m.ruta.estado = 'ENTREGADA'
        db.session.commit()
        with pytest.raises(ValueError, match='devoluciones_sin_contar'):
            RutaService.liquidar_ruta(m.ruta.id)
        db.session.rollback()
        with patch('app.services.connekta_gateway.connekta', _gw([_fila(producto)])):
            RutaService.liquidar_ruta(m.ruta.id, motivo_devoluciones='cierre de mes')
        b = BitacoraAccion.query.filter_by(accion='FORZAR', entidad='RutaDespacho').one()
        assert b.motivo == 'cierre de mes'

    def test_contada_no_bloquea(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        m.ruta.estado = 'ENTREGADA'
        db.session.commit()
        with patch('app.services.connekta_gateway.connekta', _gw([_fila(producto)])):
            assert RutaService.liquidar_ruta(m.ruta.id)['ok'] is True


class TestElReciboDeCajaNoEsperaUnaNotaQueNoVaALlegar:

    def _rc(self, db, m, producto):
        _parcial(m, producto, entregado=7)
        _liquidar_siesa(m.ruta.id, _gw([_fila(producto)]))
        job = _jobs('RECIBO_CAJA')[0]
        assert json.loads(job.payload)['depende_de_nc'] is True
        return job

    def _ejecutar(self, job):
        from app.services.siesa_job_service import _ejecutar_job
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            mc.trigger_recibo_caja.return_value = {'codigo': 0}
            try:
                _ejecutar_job(job)
            except Exception as e:      # lo de después del POST no es de este test
                if type(e).__name__ == 'DependenciaPendiente':
                    raise
            return mc

    def test_con_la_nc_en_camino_espera(self, db, almacen, producto):
        from app.services.siesa_job_service import DependenciaPendiente
        m = _mundo(db, almacen, producto)
        job = self._rc(db, m, producto)
        with pytest.raises(DependenciaPendiente):
            self._ejecutar(job)

    def test_contada_en_cero_sale_por_lo_cobrado(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        job = self._rc(db, m, producto)
        _, dev = _dev(m)
        job.proximo_intento = datetime.utcnow() + timedelta(minutes=30)
        db.session.commit()
        _contar(dev, _gw([_fila(producto)]), [{'linea_id': dev.lineas[0].id,
                                                'cantidad_devuelta': 0}])
        db.session.refresh(job)
        assert job.proximo_intento <= datetime.utcnow()      # destrabado ya
        mc = self._ejecutar(job)
        mc.trigger_recibo_caja.assert_called_once()

    def test_cancelada_sale_por_lo_cobrado(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        job = self._rc(db, m, producto)
        _, dev = _dev(m)
        DCS.cancelar(dev.id, _usuario(db, 'supervisor').id, motivo='se armó mal')
        mc = self._ejecutar(job)
        mc.trigger_recibo_caja.assert_called_once()


class TestLaNotaSinLineasNoSeDaPorHecha:

    def test_falla_determinista_y_no_marca_nada(self, db, almacen, producto):
        from app.services.siesa_job_service import NotaCreditoSinLineas, _ejecutar_job
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        job = _jobs('NOTA_CREDITO_DEVOLUCION_CLIENTE')[0]
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            mc.get_rowids_factura.return_value = [_fila(producto, rowid='999', ref='OTRA')]
            with pytest.raises(NotaCreditoSinLineas):
                _ejecutar_job(job)
        db.session.refresh(dev)
        assert dev.siesa_nc_triggered is False


# ═════════════════════════════════════════════════════════════════════════════
# 6 · Reingreso no vendible hasta la NC aprobada; aprobación verificada
# ═════════════════════════════════════════════════════════════════════════════

class TestReingresoNoVendibleHastaLaNCAprobada:

    def _contada(self, db, almacen, producto, ub_picking):
        ub_picking.producto_asignado_id = producto.id
        db.session.commit()
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        dev.siesa_nc_triggered = True
        dev.siesa_nc_consec = '61'
        db.session.commit()
        return dev

    def test_lo_devuelto_no_es_vendible(self, db, almacen, producto, ub_picking):
        dev = self._contada(db, almacen, producto, ub_picking)
        from app.models.producto import Producto
        p = db.session.get(Producto, producto.id)
        assert p.stock_vendible == 0 and p.stock_total == 10

    def test_liberar_sin_nc_aprobada_levanta(self, db, almacen, producto, ub_picking):
        dev = self._contada(db, almacen, producto, ub_picking)
        with pytest.raises(ValueError, match='no está aprobada'):
            DCS.liberar_reingreso(dev)

    def test_el_respaldo_manual_pide_motivo_y_libera_con_bitacora(self, db, almacen, producto,
                                                                  ub_picking):
        from app.models.bitacora import BitacoraAccion
        from app.models.inventario import MovimientoInventario
        from app.services.bitacora import MotivoRequerido
        dev = self._contada(db, almacen, producto, ub_picking)
        sup = _usuario(db, 'supervisor')
        with pytest.raises(MotivoRequerido):
            DCS.marcar_nc_aprobada(dev.id, sup.id)
        DCS.marcar_nc_aprobada(dev.id, sup.id, motivo='aprobada en el escritorio, NCE-61')
        assert dev.nc_aprobada_fuente == 'MANUAL' and dev.reingreso_liberado_at
        assert _stock(producto.id, 'PICKING') == 10 and _stock(producto.id, 'DEVOLUCION') == 0
        movs = MovimientoInventario.query.filter_by(tipo='LIBERACION_DEVOLUCION').all()
        assert sorted(m.cantidad for m in movs) == [-10, 10]
        assert BitacoraAccion.query.filter_by(accion='FORZAR', entidad='DevolucionCliente').count() == 1

    def _gw_nc(self, filas, consulta='papeleriamedellin_WMS_NC_Consecutivo'):
        gw = MagicMock()
        gw.consulta_nc_consecutivo = consulta
        gw.tipo_docto_nota_credito = 'NCE'
        gw.centro_op = '003'
        gw._filas_nc_encabezado.return_value = filas
        return gw

    def _fila_nc(self, consec, estado, co='003'):
        return {'f350_rowid': 1000 + consec, 'f350_id_co': co, 'f350_id_tipo_docto': 'NCE',
                'f350_consec_docto': consec, 'f350_ind_estado': estado}

    def test_el_cron_marca_la_aprobada_por_siesa_y_libera(self, db, almacen, producto, ub_picking):
        from app.services import devolucion_nc_verificador as ver
        dev = self._contada(db, almacen, producto, ub_picking)
        r = ver.verificar(gateway=self._gw_nc([self._fila_nc(61, 1)]))
        assert r['aprobadas'] == 1 and r['liberadas'] == 1
        db.session.refresh(dev)
        assert (dev.nc_aprobada_siesa, dev.nc_aprobada_fuente, dev.nc_estado_siesa) == (True, 'SIESA', 1)
        assert _stock(producto.id, 'PICKING') == 10

    @pytest.mark.parametrize('filas,clave', [
        ([{'f350_rowid': 1, 'f350_id_co': '003', 'f350_id_tipo_docto': 'NCE',
           'f350_consec_docto': 61, 'f350_ind_estado': 0}], 'en_elaboracion'),
        ([{'f350_rowid': 1, 'f350_id_co': '003', 'f350_id_tipo_docto': 'NCE',
           'f350_consec_docto': 61, 'f350_ind_estado': 2}], 'anuladas'),
        ([{'f350_rowid': 1, 'f350_id_co': '001', 'f350_id_tipo_docto': 'NCE',
           'f350_consec_docto': 61, 'f350_ind_estado': 1}], 'fuera_de_la_consulta'),
        ([], 'fuera_de_la_consulta'),
    ])
    def test_solo_marca_lo_que_leyo_aprobado(self, db, almacen, producto, ub_picking, filas, clave):
        from app.services import devolucion_nc_verificador as ver
        dev = self._contada(db, almacen, producto, ub_picking)
        r = ver.verificar(gateway=self._gw_nc(filas))
        assert r[clave] == 1 and r['aprobadas'] == 0
        db.session.refresh(dev)
        assert dev.nc_aprobada_siesa is False and _stock(producto.id, 'PICKING') == 0

    def test_sin_consecutivo_o_sin_consulta_no_marca(self, db, almacen, producto, ub_picking):
        from app.services import devolucion_nc_verificador as ver
        dev = self._contada(db, almacen, producto, ub_picking)
        assert 'omitido' in ver.verificar(gateway=self._gw_nc([], consulta=''))
        dev.siesa_nc_consec = None
        db.session.commit()
        assert ver.verificar(gateway=self._gw_nc([self._fila_nc(61, 1)]))['sin_consecutivo'] == 1

    def test_el_cron_nace_apagado_y_respeta_la_ventana(self, db, monkeypatch, ventana_qa):
        from app.services import devolucion_nc_verificador as ver
        monkeypatch.delenv('DEVOLUCIONES_VERIFICAR_NC', raising=False)
        assert 'nace apagado' in ver.correr()['omitido']
        monkeypatch.setenv('DEVOLUCIONES_VERIFICAR_NC', 'true')
        tarde = datetime(2026, 9, 24, 21, 0)
        assert 'ventana' in ver.correr(reloj=lambda: tarde)['omitido']

    def test_linea_mixta_traslada_las_averiadas_al_aprobarse(self, db, almacen, producto,
                                                              ub_picking, monkeypatch):
        ub_picking.producto_asignado_id = producto.id
        db.session.commit()
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        gw = _gw([_fila(producto)])
        dr.vincular_a_factura(dev, gateway=gw)
        _contar(dev, gw, [{'linea_id': dev.lineas[0].id, 'cantidad_devuelta': 10,
                           'cantidad_averiada': 3}])
        dev.siesa_nc_triggered = True
        db.session.commit()
        monkeypatch.setattr('app.services.devolucion_cliente_service.connekta',
                            SimpleNamespace(bodega='NB1', bodega_averias='AV1'))
        with patch('app.services.connekta_gateway.connekta',
                   SimpleNamespace(bodega='NB1', bodega_averias='AV1')):
            DCS.marcar_nc_aprobada(dev.id, _usuario(db, 'supervisor').id, motivo='aprobada')
        jobs = _jobs('TRASLADO_AVERIAS')
        assert len(jobs) == 1 and json.loads(jobs[0].payload)['cantidad'] == 3
        assert _stock(producto.id, 'PICKING') == 7


class TestLaMismaPoliticaDeVendibleParaTodos:
    """La zona DEVOLUCION entra a la política única (`picking_service`): FEFO,
    alerta de mínimos, carga inicial, ABC y traslados la heredan."""

    def test_la_politica_la_excluye(self, db, almacen):
        from app.models.ubicacion import Ubicacion
        from app.services.picking_service import (campos_ubicacion_devolucion,
                                                  es_ubicacion_vendible,
                                                  filtro_ubicacion_vendible)
        ub = Ubicacion(codigo='DEVOLUCIONES', almacen_id=almacen.id, activo=True,
                       **campos_ubicacion_devolucion())
        db.session.add(ub); db.session.commit()
        assert es_ubicacion_vendible(ub) is False
        assert Ubicacion.query.filter(filtro_ubicacion_vendible(),
                                      Ubicacion.id == ub.id).count() == 0

    def test_fefo_no_saca_de_la_zona_de_devoluciones(self, db, almacen, producto):
        from app.models.inventario import UbicacionProducto
        from app.models.ubicacion import Ubicacion
        from app.services.picking_service import PickingService, campos_ubicacion_devolucion
        ub = Ubicacion(codigo='DEVOLUCIONES', almacen_id=almacen.id, activo=True,
                       **campos_ubicacion_devolucion())
        db.session.add(ub); db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=producto.id,
                                         cantidad=50, reservado=0, bloqueado=0))
        db.session.commit()
        plan = PickingService.calcular_fefo(producto.id, 5, almacen.id)
        assert plan['asignaciones'] == [] and plan['cantidad_faltante'] == 5


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Avisos y medición
# ═════════════════════════════════════════════════════════════════════════════

class TestAvisosYMedicion:

    def test_los_avisos_por_antiguedad(self, db, almacen, producto):
        from app.models.siesa_job import SiesaJob
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        rec, dev = _dev(m)
        rec.fecha_confirmacion = datetime.utcnow() - timedelta(hours=30)
        db.session.commit()
        a = dr.avisos()
        assert [d['codigo'] for d in a['sin_contar_24h']] == [dev.codigo] and not a['sin_contar_48h']
        rec.fecha_confirmacion = datetime.utcnow() - timedelta(hours=50)
        dev2 = DevolucionCliente(codigo='DEVC-NC-VIEJA', tarea_packing_id=m.tarea.id,
                                 tipo_docto_fe='FEW', consec_fe='1', almacen_id=almacen.id,
                                 estado='CONFIRMADA', siesa_nc_triggered=True,
                                 siesa_nc_triggered_at=datetime.utcnow() - timedelta(days=4))
        dev3 = DevolucionCliente(codigo='DEVC-NC-ANULADA', tarea_packing_id=m.tarea.id,
                                 tipo_docto_fe='FEW', consec_fe='2', almacen_id=almacen.id,
                                 estado='CONFIRMADA', siesa_nc_triggered=True, nc_estado_siesa=2,
                                 siesa_nc_triggered_at=datetime.utcnow())
        db.session.add_all([dev2, dev3])
        job = SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': rec.id, 'depende_de_nc': True,
                                               'monto': 1}, referencia_tipo='RecaudoEntrega',
                               referencia_id=rec.id)
        job.fecha_creacion = datetime.utcnow() - timedelta(hours=50)
        db.session.commit()
        a = dr.avisos()
        assert [d['codigo'] for d in a['sin_contar_48h']] == [dev.codigo]
        assert [d['codigo'] for d in a['nc_sin_aprobar_3d']] == ['DEVC-NC-VIEJA']
        assert [d['codigo'] for d in a['nc_anuladas']] == ['DEVC-NC-ANULADA']
        assert [d['job_id'] for d in a['rc_esperando_nc_48h']] == [job.id]
        frases = ' '.join(dr.lineas_de_aviso(a))
        assert '48 h' in frases and 'ANULADA' in frases and 'recibo(s) de caja' in frases

    def test_la_medicion(self, db, almacen, producto):
        m = _mundo(db, almacen, producto, n_bultos=2)
        _rechazar(m)
        rec, dev = _dev(m)
        rec.fecha_confirmacion = datetime.utcnow() - timedelta(hours=5)
        db.session.commit()
        b = Bulto.query.filter_by(tarea_id=m.tarea.id).first()
        dr.escanear_bulto_de_vuelta(m.ruta.id, b.codigo_barras, m.uid)
        dr.cerrar_llegada(m.ruta.id, m.uid)
        gw = _gw([_fila(producto, cant=10, neto=100000)])
        dr.vincular_a_factura(dev, gateway=gw)
        _contar(dev, gw, [{'linea_id': dev.lineas[0].id, 'cantidad_devuelta': 7}])
        med = dr.medicion()
        assert med['horas_rechazo_a_conteo']['n'] == 1
        assert 4.5 <= med['horas_rechazo_a_conteo']['mediana'] <= 5.5
        assert med['faltante_de_retorno'] == {**med['faltante_de_retorno'],
                                              'unidades': 3.0, 'valor': 30000.0}
        assert med['cuadre_de_bultos']['exactas'] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 8 · Endpoints
# ═════════════════════════════════════════════════════════════════════════════

def _token(app, u):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


class TestEndpoints:

    def test_la_cola_es_de_recepcion(self, app, client, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        rec_u = _usuario(db, 'recepcionista', almacen)
        op = _usuario(db, 'operario', almacen)
        db.session.commit()
        r = client.get('/api/devoluciones/llegadas', headers=_token(app, rec_u))
        assert r.status_code == 200 and r.get_json()['total'] == 1
        assert client.get('/api/devoluciones/llegadas', headers=_token(app, op)).status_code == 403
        b = Bulto.query.filter_by(tarea_id=m.tarea.id).first()
        r = client.post(f'/api/devoluciones/llegadas/{m.ruta.id}/bulto',
                        json={'codigo_barras': b.codigo_barras}, headers=_token(app, rec_u))
        assert r.status_code == 200 and r.get_json()['bulto']['estado'] == 'RETORNADO'
        r = client.post(f'/api/devoluciones/llegadas/{m.ruta.id}/cerrar', headers=_token(app, rec_u))
        assert r.status_code == 200 and r.get_json()['bultos_faltantes'] == 1
        assert client.get('/api/devoluciones/tablero', headers=_token(app, rec_u)).status_code == 200

    def test_preparar_conteo_y_verificar(self, app, client, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        rec_u = _usuario(db, 'recepcionista', almacen)
        admin = _usuario(db, 'admin', almacen)
        db.session.commit()
        with patch('app.services.devolucion_cliente_service.connekta', _gw([_fila(producto)])):
            r = client.post(f'/api/devoluciones/{dev.id}/preparar-conteo', headers=_token(app, rec_u))
        assert r.status_code == 200 and r.get_json()['vinculacion']['ok'] is True
        assert r.get_json()['lineas'][0]['f470_rowid'] == '700'
        assert client.post('/api/devoluciones/verificar-nc',
                           headers=_token(app, rec_u)).status_code == 403
        assert client.post('/api/devoluciones/verificar-nc',
                           headers=_token(app, admin)).status_code == 200

    def test_marcar_a_mano_sin_motivo_es_400(self, app, client, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        dev = DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None, [
            {'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
             'cantidad_facturada': 10, 'cantidad_devuelta': 2, 'f470_rowid': '700'}])
        dev.siesa_nc_triggered = True
        admin = _usuario(db, 'admin', almacen)
        db.session.commit()
        r = client.post(f'/api/devoluciones/{dev.id}/marcar-nc-aprobada', json={},
                        headers=_token(app, admin))
        assert r.status_code == 400 and 'motivo' in r.get_json()['error'].lower()


# ═════════════════════════════════════════════════════════════════════════════
# 9 · Invariantes: detectores ciegos (construyen la violación y exigen verla)
# ═════════════════════════════════════════════════════════════════════════════

def _correr(codigo):
    from app.services import auditoria
    inv = next(i for i in auditoria.registrados() if i.codigo == codigo)
    return inv.evaluar()


class TestDetectoresDeDevolucionDeRuta:

    def test_ve_una_ruta_liquidada_con_un_rechazo_sin_devolucion(self, db, almacen, producto,
                                                                  monkeypatch):
        m = _mundo(db, almacen, producto)
        monkeypatch.setattr(dr, '_crear_de_ruta', lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError('la vía rota')))
        _rechazar(m)
        assert _correr('DEV-06') == []                 # ruta sin liquidar: todavía no
        m.ruta.estado_financiero = 'LIQUIDADA'
        db.session.commit()
        assert len(_correr('DEV-06')) == 1

    def test_dev06_no_dispara_con_la_via_sana(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        m.ruta.estado_financiero = 'LIQUIDADA'
        db.session.commit()
        assert _correr('DEV-06') == []

    def test_ve_una_devolucion_sin_contar_hace_mas_de_un_dia(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        assert _correr('DEV-07') == []
        rec, _ = _dev(m)
        rec.fecha_confirmacion = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
        assert len(_correr('DEV-07')) == 1

    def test_ve_un_rechazo_con_la_devolucion_cancelada(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        assert _correr('DEV-08') == []
        _, dev = _dev(m)
        DCS.cancelar(dev.id, _usuario(db, 'supervisor').id, motivo='no sé')
        assert len(_correr('DEV-08')) == 1

    def test_ve_un_rc_esperando_una_nc_que_no_llegara(self, db, almacen, producto, monkeypatch):
        from app.models.siesa_job import SiesaJob
        m = _mundo(db, almacen, producto)
        _parcial(m, producto, entregado=7)
        rec, dev = _dev(m)
        SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': rec.id, 'depende_de_nc': True, 'monto': 1},
                         referencia_tipo='RecaudoEntrega', referencia_id=rec.id)
        db.session.commit()
        assert _correr('DEV-09') == []                  # la NC está en camino
        DCS.cancelar(dev.id, _usuario(db, 'supervisor').id, motivo='x')
        dev.cancelada_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        assert len(_correr('DEV-09')) == 1

    def test_ve_dos_devoluciones_sobre_la_misma_linea_de_factura(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        linea = {'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
                 'cantidad_facturada': 10, 'f470_rowid': '700'}
        d1 = DCS.crear_devolucion(m.tarea.id, 'FEW', '500', almacen.id, None,
                                  [{**linea, 'cantidad_devuelta': 6}])
        _contar(d1, _gw([_fila(producto)]))
        assert _correr('DEV-10') == []
        # Una segunda contada escrita por fuera de la guarda (el camino roto).
        d2 = DevolucionCliente(codigo='DEVC-POR-FUERA', tarea_packing_id=m.tarea.id,
                               tipo_docto_fe='FEW', consec_fe='500', almacen_id=almacen.id,
                               estado='CONFIRMADA')
        db.session.add(d2); db.session.flush()
        db.session.add(LineaDevolucionCliente(devolucion_id=d2.id, producto_id=producto.id,
                                              codigo_siesa='x', cantidad_facturada=10,
                                              cantidad_devuelta=6, f470_rowid='700'))
        db.session.commit()
        assert len(_correr('DEV-10')) == 1

    def test_ve_un_reingreso_vendible_sin_nc_aprobada(self, db, almacen, producto, ub_picking):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        assert _correr('DEV-11') == []
        dev.lineas[0].ubicacion_id = ub_picking.id       # el camino roto: directo a picking
        db.session.commit()
        assert len(_correr('DEV-11')) == 1

    def test_ve_una_nc_sin_aprobar_hace_mas_de_tres_dias(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        dev.siesa_nc_triggered = True
        dev.siesa_nc_triggered_at = datetime.utcnow() - timedelta(days=1)
        db.session.commit()
        assert _correr('DEV-12') == []
        dev.siesa_nc_triggered_at = datetime.utcnow() - timedelta(days=4)
        db.session.commit()
        assert len(_correr('DEV-12')) == 1

    def test_ve_bultos_declarados_de_vuelta_tras_cerrar_la_llegada(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        dr.cerrar_llegada(m.ruta.id, m.uid)            # llegada cerrada sin nada que recibir
        assert _correr('DEV-13') == []
        _rechazar(m)                                   # la cola del conductor llega tarde
        assert len(_correr('DEV-13')) == 1

    def test_dev04_no_cuenta_las_ya_aprobadas(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        dev.siesa_nc_triggered = True
        dev.siesa_nc_triggered_at = datetime.utcnow()
        db.session.commit()
        assert len(_correr('DEV-04')) == 1
        dev.nc_aprobada_siesa = True
        db.session.commit()
        assert _correr('DEV-04') == []

    def test_dev05_no_cuenta_las_ya_aprobadas(self, db, almacen, producto):
        m = _mundo(db, almacen, producto)
        _rechazar(m)
        _, dev = _dev(m)
        _contar(dev, _gw([_fila(producto)]))
        dev.siesa_nc_triggered = True
        dev.siesa_nc_consec = '62'
        db.session.commit()
        assert len(_correr('DEV-05')) == 1
        dev.nc_aprobada_siesa = True
        db.session.commit()
        assert _correr('DEV-05') == []


# ═════════════════════════════════════════════════════════════════════════════
# 10 · Trinquetes de clase por AST (con meta-tests y pisos)
# ═════════════════════════════════════════════════════════════════════════════

def _funciones(arbol):
    """(nombre, nodo) de toda función, anidadas incluidas, SIN mirar dentro de
    las funciones hijas al juzgar a la madre."""
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n.name, n


def _propio(fn):
    """Los nodos de ESTA función, sin los de sus funciones anidadas."""
    _anidada = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    pila, out = [n for n in fn.body if not isinstance(n, _anidada)], []
    while pila:
        n = pila.pop()
        out.append(n)
        for h in ast.iter_child_nodes(n):
            if not isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                pila.append(h)
    return out


def _llama(nodos, nombre):
    for n in nodos:
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == nombre) or \
                    (isinstance(f, ast.Name) and f.id == nombre):
                return True
    return False


def _escribe_estado_entrega(nodos):
    for n in nodos:
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            objetivos = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Attribute) and t.attr == 'estado_entrega' for t in objetivos):
                return True
        if isinstance(n, ast.Call) and any(k.arg == 'estado_entrega' for k in n.keywords):
            f = n.func
            nombre = f.id if isinstance(f, ast.Name) else getattr(f, 'attr', '')
            if nombre == 'RecaudoEntrega':
                return True
    return False


def _escritores_de_estado_entrega(base=RAIZ):
    out = []
    for f in sorted((base / 'app').rglob('*.py')):
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        for nombre, fn in _funciones(arbol):
            nodos = _propio(fn)
            if _escribe_estado_entrega(nodos):
                out.append((str(f.relative_to(base)), nombre,
                            _llama(nodos, 'sincronizar_con_parada')))
    return out


class TestTodaEscrituraDelEstadoDeLaParadaSincronizaSuDevolucion:
    """Toda función de `app/` que escribe `RecaudoEntrega.estado_entrega` llama
    a `devolucion_ruta.sincronizar_con_parada` en la MISMA función. Así un
    RECHAZADO/PARCIAL nuevo no puede nacer sin su devolución, y un ENTREGADO no
    puede dejar una viva. Inventario de excepciones: vacío."""

    EXCEPCIONES = {}

    def test_todos_sincronizan(self):
        malos = [(a, f) for a, f, ok in _escritores_de_estado_entrega()
                 if not ok and (a, f) not in self.EXCEPCIONES]
        assert not malos, f'escriben estado_entrega sin sincronizar la devolución: {malos}'

    def test_piso(self):
        assert len(_escritores_de_estado_entrega()) >= 2   # confirmar_parada, forzar_cierre

    def _en(self, tmp_path, fuente):
        f = tmp_path / 'app' / 'x.py'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fuente, encoding='utf-8')
        return _escritores_de_estado_entrega(base=tmp_path)

    def test_ve_la_asignacion(self, tmp_path):
        assert self._en(tmp_path, 'def f(r):\n    r.estado_entrega = "RECHAZADO"\n') == [
            ('app/x.py', 'f', False)]

    def test_ve_el_constructor(self, tmp_path):
        assert self._en(tmp_path, 'def f():\n    RecaudoEntrega(estado_entrega="PARCIAL")\n')[0][2] is False

    def test_la_llamada_en_una_hija_no_cuenta(self, tmp_path):
        r = self._en(tmp_path, 'def f(r):\n    r.estado_entrega = "X"\n'
                               '    def g():\n        sincronizar_con_parada(r)\n')
        assert ('app/x.py', 'f', False) in r

    def test_no_marca_una_comparacion_ni_un_docstring(self, tmp_path):
        assert self._en(tmp_path, 'def f(r):\n    """r.estado_entrega = 1"""\n'
                                  '    return r.estado_entrega == "X"\n') == []


def _creadores_de_ruta(base=RAIZ):
    out = []
    for f in sorted((base / 'app').rglob('*.py')):
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        for nombre, fn in _funciones(arbol):
            for n in _propio(fn):
                if isinstance(n, ast.Call) and getattr(n.func, 'attr', getattr(n.func, 'id', None)) \
                        == 'crear_devolucion' and any(k.arg == 'recaudo_entrega_id' for k in n.keywords):
                    out.append((str(f.relative_to(base)), nombre))
    return out


class TestUnaFuncionCreaDevolucionesDeRuta:

    def test_solo_crear_de_ruta(self):
        assert _creadores_de_ruta() == [('app/services/devolucion_ruta.py', '_crear_de_ruta')]

    def test_meta_ve_otro_creador(self, tmp_path):
        f = tmp_path / 'app' / 'y.py'
        f.parent.mkdir(parents=True)
        f.write_text('def otro(s):\n    s.crear_devolucion(1, recaudo_entrega_id=2)\n'
                     'def sano(s):\n    s.crear_devolucion(1)\n', encoding='utf-8')
        assert _creadores_de_ruta(base=tmp_path) == [('app/y.py', 'otro')]


def _escritores_de_estado_devolucion(base=RAIZ):
    """(archivo, función) que asignan `x.estado = EstadoDevolucionCliente.*`
    (o sus alias `E`/`_E`) o construyen `DevolucionCliente(estado=...)`."""
    alias = {'EstadoDevolucionCliente', 'E', '_E'}
    out = []
    for f in sorted((base / 'app').rglob('*.py')):
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        for nombre, fn in _funciones(arbol):
            for n in _propio(fn):
                if isinstance(n, ast.Assign) and any(
                        isinstance(t, ast.Attribute) and t.attr == 'estado' for t in n.targets):
                    v = n.value
                    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) \
                            and v.value.id in alias:
                        out.append((str(f.relative_to(base)), nombre, 'asigna'))
                    elif isinstance(v, ast.Name) and v.id == 'nuevo' and nombre == 'cambiar_estado':
                        out.append((str(f.relative_to(base)), nombre, 'asigna'))
                if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'DevolucionCliente' \
                        and any(k.arg == 'estado' for k in n.keywords):
                    out.append((str(f.relative_to(base)), nombre, 'crea'))
    return sorted(set(out))


class TestUnaFuncionEscribeElEstadoDeLaDevolucion:

    PERMITIDOS = {
        ('app/services/devolucion_cliente_service.py', 'cambiar_estado', 'asigna'),
        ('app/services/devolucion_cliente_service.py', 'crear_devolucion', 'crea'),
    }

    def test_nadie_mas(self):
        assert set(_escritores_de_estado_devolucion()) == self.PERMITIDOS

    def test_meta_ve_la_escritura_directa(self, tmp_path):
        f = tmp_path / 'app' / 'z.py'
        f.parent.mkdir(parents=True)
        f.write_text('def mala(d):\n    d.estado = EstadoDevolucionCliente.CONFIRMADA\n'
                     'def sana(d):\n    return d.estado == EstadoDevolucionCliente.CONFIRMADA\n',
                     encoding='utf-8')
        assert _escritores_de_estado_devolucion(base=tmp_path) == [('app/z.py', 'mala', 'asigna')]


class TestNingunReingresoTocaInventarioVendibleSinNCAprobada:
    """La clase: *un reingreso de devolución que llega a inventario vendible sin
    la NC aprobada*. Por AST sobre `devolucion_cliente_service`:
    · `_destino_vendible` (lo único que elige una ubicación vendible) tiene UN
      llamador: `liberar_reingreso`;
    · `liberar_reingreso` empieza exigiendo `nc_aprobada_siesa`;
    · `_buscar_ubicacion_optima` solo se usa dentro de `_destino_vendible`;
    · `confirmar_entrada_fisica` no llama a ninguna de las dos, y todo
      `_resolver_ubicacion` que haga pasa `es_averiado=True` literal (lo sano va
      por `_ubicacion_devolucion`)."""

    ARCHIVO = APP / 'services' / 'devolucion_cliente_service.py'

    def _arbol(self, fuente=None):
        return ast.parse(fuente if fuente is not None else self.ARCHIVO.read_text(encoding='utf-8'))

    def _llamadores(self, arbol, nombre):
        return sorted({fn for fn, nodo in _funciones(arbol) if _llama(_propio(nodo), nombre)})

    def test_destino_vendible_tiene_un_solo_llamador(self):
        assert self._llamadores(self._arbol(), '_destino_vendible') == ['liberar_reingreso']

    def test_la_ubicacion_optima_solo_desde_destino_vendible(self):
        assert self._llamadores(self._arbol(), '_buscar_ubicacion_optima') == ['_destino_vendible']

    def test_liberar_empieza_exigiendo_la_nc_aprobada(self):
        fn = next(n for nm, n in _funciones(self._arbol()) if nm == 'liberar_reingreso')
        primero = next(s for s in fn.body if not (isinstance(s, ast.Expr)
                                                  and isinstance(s.value, ast.Constant)))
        assert isinstance(primero, ast.If)
        assert any(isinstance(x, ast.Attribute) and x.attr == 'nc_aprobada_siesa'
                   for x in ast.walk(primero.test))
        assert any(isinstance(x, ast.Raise) for x in primero.body)

    def test_contar_no_toca_una_ubicacion_vendible(self):
        fn = next(n for nm, n in _funciones(self._arbol()) if nm == 'confirmar_entrada_fisica')
        nodos = _propio(fn)
        assert not _llama(nodos, '_destino_vendible')
        assert not _llama(nodos, '_buscar_ubicacion_optima')
        llamadas = [n for n in nodos if isinstance(n, ast.Call)
                    and getattr(n.func, 'attr', None) == '_resolver_ubicacion']
        assert llamadas, 'piso: la cuenta resuelve el bin de averías'
        for c in llamadas:
            k = next((k for k in c.keywords if k.arg == 'es_averiado'), None)
            assert k is not None and isinstance(k.value, ast.Constant) and k.value.value is True
        assert _llama(nodos, '_ubicacion_devolucion')

    def test_meta_ve_un_segundo_llamador(self):
        arbol = self._arbol('def liberar_reingreso():\n    _destino_vendible()\n'
                            'def confirmar_entrada_fisica():\n    _destino_vendible()\n')
        assert self._llamadores(arbol, '_destino_vendible') == ['confirmar_entrada_fisica',
                                                                 'liberar_reingreso']


# ═════════════════════════════════════════════════════════════════════════════
# 11 · La pantalla de recepción (Node, util.js real)
# ═════════════════════════════════════════════════════════════════════════════

_HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const caso = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const traza = { abiertas: [], posts: [] };
const inputs = caso.inputs || {};
const ctx = { console,
  document: { getElementById: (id) => (id in inputs ? { value: String(inputs[id]) } : null),
              querySelector: () => null, querySelectorAll: () => [], addEventListener() {} },
  window: { addEventListener() {} }, navigator: { onLine: true },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} } };
ctx.globalThis = ctx;
vm.createContext(ctx);
ctx.__traza = traza;
vm.runInContext(fs.readFileSync(process.argv[2] + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/recepcion.js', 'utf-8'), ctx);
vm.runInContext('abrirPendienteDeRuta = (id) => __traza.abiertas.push(id);', ctx);
vm.runInContext('_REC_LLEGADAS = ' + JSON.stringify(caso.rutas) + ';', ctx);
traza.html = ctx.recLlegadasHtml(caso.rutas);
traza.avisos = ctx.recAvisosHtml(caso.tablero);
traza.lineas = ctx.recLineasContadas(caso.devolucion);
const onclicks = [...traza.html.matchAll(/onclick="([^"]*)"/g)].map(m => m[1]);
traza.onclicks = onclicks;
const contar = onclicks.find(o => o.startsWith('recLlegadaContar'));
if (contar) vm.runInContext(contar, ctx);
process.stdout.write(JSON.stringify(traza));
"""


@pytest.mark.skipif(shutil.which('node') is None, reason='sin node')
class TestLaPantallaDeRecepcion:

    def _correr(self, tmp_path, caso):
        h = tmp_path / 'h.mjs'
        h.write_text(_HARNESS, encoding='utf-8')
        c = tmp_path / 'c.json'
        c.write_text(json.dumps(caso), encoding='utf-8')
        out = subprocess.run(['node', str(h), str(PWA), str(c)], capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    CASO = {
        'rutas': [{'ruta_id': 7, 'conductor': '<img src=x onerror=alert(1)>', 'placa': 'ABC123',
                   'horas_sin_contar': 50,
                   'cuadre': {'salieron': 3, 'entregados': 1, 'retornados': 1, 'faltantes': 0,
                              'en_camion_sin_recibir': 1, 'exacto': False},
                   'devoluciones': [{'id': 41, 'codigo': 'DEVC-1', 'estado': 'EN_CAMION',
                                     'pedido': "PD'1", 'cliente': '"><b>x</b>', 'es_total': True,
                                     'problema_factura': None}]}],
        'tablero': {'avisos': {'sin_contar_48h': [{}], 'sin_contar_24h': [], 'nc_anuladas': [],
                               'nc_sin_aprobar_3d': [{}, {}], 'rc_esperando_nc_48h': []},
                    'medicion': {'horas_rechazo_a_conteo': {'n': 2, 'mediana': 5, 'p90': 9},
                                 'faltante_de_retorno': {'unidades': 3, 'valor': 30000,
                                                         'lineas_sin_valor': 0}}},
        'devolucion': {'id': 41, 'lineas': [{'id': 900, 'producto_id': 5, 'codigo_siesa': 'R',
                                             'cantidad_facturada': 10}]},
        'inputs': {'cant-dev-0': 6, 'averiadas-dev-0': 2},
    }

    def test_escapa_y_en_los_onclick_solo_viajan_posiciones(self, tmp_path):
        t = self._correr(tmp_path, self.CASO)
        assert '<img' not in t['html'] and '<b>x</b>' not in t['html']
        assert '&lt;img' in t['html']
        import re
        for o in t['onclicks']:
            assert re.fullmatch(r"rec\w+\((\d+(, \d+)?)?\)", o), o
        assert t['abiertas'] == [41]

    def test_lo_contado_viaja_por_linea_con_averiadas(self, tmp_path):
        t = self._correr(tmp_path, self.CASO)
        l = t['lineas'][0]
        assert (l['linea_id'], l['cantidad_devuelta'], l['cantidad_averiada'], l['es_averiado']) \
            == (900, 6, 2, False)

    def test_los_avisos_se_pintan(self, tmp_path):
        t = self._correr(tmp_path, self.CASO)
        assert 'más de 48 h' in t['avisos'] and '2 nota(s) crédito' in t['avisos']
        assert 'mediana 5 h' in t['avisos']
