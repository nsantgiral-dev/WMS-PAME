"""
Dos defectos de `liquidacion_service`, medidos sobre la ruta de LIQUIDACIÓN.

## 1 · La devolución pendiente de ruta empareja por referencia, no por línea

Este repo tiene **productos de doble unidad**: la misma referencia se factura en
DOS líneas, una en PQ y otra en UND, con `f470_rowid` y valores distintos
(PAPELSP6741 — documentado en `despacho_parcial_service.py:102-106`).

El arreglo de la clave llegó a la ruta de **Devolución de Cliente**
(`siesa_job_service._construir_lineas_nc`, `tests/test_nc_doble_unidad.py`).
`liquidacion_service._crear_devolucion_pendiente` quedó sin arreglar: indexa lo
declarado por el conductor por **código** y recorre la factura resolviendo
referencia → producto, así que las dos filas resuelven al MISMO producto y se
crean **dos** `LineaDevolucionCliente` con la cantidad declarada entera cada una.

Medido por un auditor sobre la misma factura, 1 unidad devuelta::

    ruta de RECEPCIÓN     1 línea NC · 24250.00   ✓
    ruta de LIQUIDACIÓN   2 líneas NC · 25058.33  ✗

Y el guard de `_construir_lineas_nc` **no puede atraparlo**: esas dos líneas
llegan al job ya con `f470_rowid` válido cada una, así que se dan por
desambiguadas. La sobredeclaración nace acá.

## 2 · El DC que no se encola y el contador que dice 1

`_encolar_documento_contable` tenía cinco `return` vacíos —uno de ellos dentro
de un `except Exception` alrededor de `get_rowids_factura`—, así que el llamador
no podía distinguir «encolado» de «abortado» y hacía `resultado['dc'] = 1`
incondicionalmente. El tablero informaba un documento de retención que nunca se
envió: la tercera bandera de idempotencia financiera otra vez (CLAUDE.md,
«Las tres banderas»).

## Detector en las dos direcciones

`TestUnidadUnicaNoCambia` y `TestDCQueSiEncola` fijan lo que **no** debe moverse:
un producto de unidad única sigue creando exactamente una línea con la misma
cantidad, y una retención que sí encola sigue contando 1 y devolviendo lo mismo.
Un detector que solo prueba que dispara, prueba la mitad.
"""
import uuid

import pytest
from unittest.mock import patch


# ── Factura de doble unidad: la misma referencia en PQ y en UND ────────
def _factura_doble_unidad(ref):
    """1 PQ ($24.250) + 1 UND ($2.425). Dos rowids, dos valores."""
    return [
        {'f470_rowid': 2857568, 'f120_referencia': ref, 'f470_cant_base': 1,
         'f470_id_unidad_medida': 'PQ', 'f150_id': 'NB1',
         'f470_vlr_bruto': 20378.0, 'f470_vlr_imp': 3872.0, 'f470_vlr_neto': 24250.0},
        {'f470_rowid': 2857569, 'f120_referencia': ref, 'f470_cant_base': 1,
         'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
         'f470_vlr_bruto': 2038.0, 'f470_vlr_imp': 387.0, 'f470_vlr_neto': 2425.0},
    ]


def _factura_unidad_unica(ref):
    """El caso sano: una referencia, una línea. 5 UND facturadas."""
    return [
        {'f470_rowid': 2857568, 'f120_referencia': ref, 'f470_cant_base': 5,
         'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
         'f470_vlr_bruto': 61134.0, 'f470_vlr_imp': 11616.0, 'f470_vlr_neto': 72750.0},
    ]


@pytest.fixture
def recaudo_liq(db, almacen):
    """RecaudoEntrega con ruta/tarea/conductor mínimos."""
    def _make(estado='ENTREGADO', pago='EFECTIVO', monto=1500000,
              rc=False, nc=False, dc=False, motivo_desc=None, items_ent=None,
              monto_desc=0):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.packing import TareaPacking
        from app.models.ruta_despacho import RutaDespacho
        from app.models.conductor import Conductor

        conductor = Conductor.query.filter_by(cedula='99900123').first()
        if not conductor:
            conductor = Conductor(nombre='Conductor Doble', cedula='99900123',
                                  activo=True)
            db.session.add(conductor)
            db.session.flush()

        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana',
                            estado='ENTREGADA')
        db.session.add(ruta)
        db.session.flush()

        tarea = TareaPacking(
            codigo=f'PK-DBL-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
            almacen_id=almacen.id,
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=999,
            numero_pedido_siesa='PED-DBL',
        )
        db.session.add(tarea)
        db.session.flush()

        recaudo = RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega=estado, forma_pago=pago, monto_cobrado=monto,
            siesa_rc_triggered=rc, siesa_nc_triggered=nc, siesa_dc_triggered=dc,
            motivo_descuento=motivo_desc, monto_descuento=monto_desc,
            items_entregados=items_ent,
        )
        db.session.add(recaudo)
        db.session.commit()
        return recaudo
    return _make


def _procesar(recaudo, db):
    """`_procesar_recaudo` con el tercero mockeado (única llamada a Siesa aparte
    de `get_rowids_factura`, que cada test decide cómo se comporta)."""
    with patch('app.services.liquidacion_service._obtener_tercero',
               return_value=('900123456', '001')):
        from app.services.liquidacion_service import _procesar_recaudo
        resultado = _procesar_recaudo(recaudo, 'test doble unidad')
        db.session.commit()
        return resultado


def _lineas_de(recaudo_id):
    from app.models.devolucion_cliente import (DevolucionCliente,
                                                LineaDevolucionCliente)
    dev = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo_id).first()
    if not dev:
        return dev, []
    return dev, LineaDevolucionCliente.query.filter_by(devolucion_id=dev.id).all()


# ═══════════════════════════════════════════════════════════════════
# DEFECTO 1 — doble unidad en la ruta de liquidación
# ═══════════════════════════════════════════════════════════════════
class TestDobleUnidadEnLaDevolucionDeRuta:

    def test_una_unidad_declarada_no_puede_crear_dos_lineas(
            self, app, db, recaudo_liq, producto):
        """El conductor declaró 1 unidad de una referencia que la factura trae
        en dos líneas (PQ y UND). No hay dato que diga a cuál corresponde:
        **no se crea la devolución**, se declara la ambigüedad."""
        from app.services.siesa_job_service import LineaDevueltaAmbigua
        from app.services.liquidacion_service import _crear_devolucion_pendiente

        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 1}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_doble_unidad(producto.codigo_siesa)):
            with pytest.raises(LineaDevueltaAmbigua) as exc:
                _crear_devolucion_pendiente(
                    recaudo, recaudo.tarea, 'FEW', '1466',
                    items_devueltos=items, notas='test')

        mensaje = str(exc.value)
        assert producto.codigo_siesa in mensaje or producto.codigo in mensaje, (
            'el error tiene que nombrar la referencia ambigua')
        assert '2857568' in mensaje and '2857569' in mensaje, (
            'el error tiene que decir entre qué rowids está la ambigüedad')

    def test_no_queda_una_devolucion_a_medias(self, app, db, recaudo_liq, producto):
        """Ni la devolución ni sus líneas: lo que se declaró de más es
        inventario que reingresa sin haber vuelto y cartera cruzada de más
        (Regla 21). Antes del arreglo esto dejaba 2 líneas de 1 unidad."""
        from app.services.siesa_job_service import LineaDevueltaAmbigua

        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 1}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_doble_unidad(producto.codigo_siesa)):
            try:
                _procesar(recaudo, db)
            except LineaDevueltaAmbigua:
                pass
            db.session.rollback()

        dev, lineas = _lineas_de(recaudo.id)
        detalle = '\n'.join(
            f'  rowid={l.f470_rowid} uom={l.f470_id_unidad_medida} '
            f'cant={l.cantidad_devuelta}' for l in lineas)
        assert dev is None and lineas == [], (
            f'se creó una devolución con {len(lineas)} línea(s) para 1 unidad '
            f'declarada:\n{detalle}')

    def test_la_liquidacion_de_ruta_declara_el_error_y_no_cuenta_la_nc(
            self, app, db, recaudo_liq, producto):
        """A nivel ruta: el recaudo ambiguo no suma `nc_encolados` y aparece en
        `errores` —lo que la pantalla pinta en rojo—, en vez de contar una NC
        que no debía existir."""
        from app.services.liquidacion_service import LiquidacionService

        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 1}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.liquidacion_service._obtener_tercero',
                   return_value=('900123456', '001')):
            with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                       return_value=_factura_doble_unidad(producto.codigo_siesa)):
                resumen = LiquidacionService.liquidar_ruta_siesa(recaudo.ruta_id)

        assert resumen['nc_encolados'] == 0
        assert len(resumen['errores']) == 1, (
            f'la ambigüedad no se declaró: {resumen}')
        assert 'ambig' in resumen['errores'][0]['error'].lower()

    def test_declarar_las_dos_lineas_por_rowid_si_es_posible(
            self, app, db, recaudo_liq, producto):
        """La otra mitad: **con** `f470_rowid` la correspondencia es exacta y sí
        se crea una línea por línea de factura.

        Hoy ningún productor lo manda —`ruta_service.confirmar_parada` recorta
        `items_entregados` a codigo/nombre/unidad/cantidades—, pero la política
        tiene que ser la misma que la de `_construir_lineas_nc`: el rowid manda
        cuando está, y la ambigüedad solo existe cuando falta."""
        from app.services.liquidacion_service import _crear_devolucion_pendiente

        items = [
            {'codigo': producto.codigo, 'cantidad_devuelta': 1, 'f470_rowid': '2857568'},
            {'codigo': producto.codigo, 'cantidad_devuelta': 1, 'f470_rowid': '2857569'},
        ]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_doble_unidad(producto.codigo_siesa)):
            assert _crear_devolucion_pendiente(
                recaudo, recaudo.tarea, 'FEW', '1466',
                items_devueltos=items, notas='test') is True
        db.session.commit()

        _dev, lineas = _lineas_de(recaudo.id)
        assert {l.f470_rowid for l in lineas} == {'2857568', '2857569'}
        assert {l.f470_id_unidad_medida for l in lineas} == {'PQ', 'UND'}

    def test_un_solo_rowid_declarado_crea_una_sola_linea(
            self, app, db, recaudo_liq, producto):
        """Con el rowid del PAQUETE sale el paquete y nada más — 1 línea, PQ."""
        from app.services.liquidacion_service import _crear_devolucion_pendiente

        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 1,
                  'f470_rowid': '2857569'}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_doble_unidad(producto.codigo_siesa)):
            _crear_devolucion_pendiente(
                recaudo, recaudo.tarea, 'FEW', '1466',
                items_devueltos=items, notas='test')
        db.session.commit()

        _dev, lineas = _lineas_de(recaudo.id)
        assert len(lineas) == 1
        assert lineas[0].f470_rowid == '2857569'
        assert lineas[0].f470_id_unidad_medida == 'UND'

    def test_dos_declaraciones_de_la_misma_referencia_sin_rowid_es_ambiguo(
            self, app, db, recaudo_liq, producto):
        """Aunque la factura traiga una sola línea: dos declaraciones del mismo
        código sin rowid no se pueden repartir. El dict de antes se quedaba con
        la última, en silencio."""
        from app.services.siesa_job_service import LineaDevueltaAmbigua
        from app.services.liquidacion_service import _crear_devolucion_pendiente

        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 1},
                 {'codigo': producto.codigo, 'cantidad_devuelta': 2}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_unidad_unica(producto.codigo_siesa)):
            with pytest.raises(LineaDevueltaAmbigua):
                _crear_devolucion_pendiente(
                    recaudo, recaudo.tarea, 'FEW', '1466',
                    items_devueltos=items, notas='test')


class TestDobleUnidadNoRompeLaDevolucionTotal:
    """RECHAZADO → `items_devueltos=None`: la factura entera, todas las líneas
    completas. Ahí la doble unidad NO es ambigua —se devolvió todo— y las dos
    líneas tienen que seguir entrando. Queda escrito para que el arreglo de la
    rama parcial no se lleve puesta esta."""

    def test_rechazado_total_incluye_las_dos_lineas(
            self, app, db, recaudo_liq, producto):
        recaudo = recaudo_liq(estado='RECHAZADO', pago='EFECTIVO', monto=0)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_doble_unidad(producto.codigo_siesa)):
            resultado = _procesar(recaudo, db)

        assert resultado['nc'] == 1
        dev, lineas = _lineas_de(recaudo.id)
        assert dev.es_total is True
        assert len(lineas) == 2
        assert {l.f470_id_unidad_medida for l in lineas} == {'PQ', 'UND'}
        assert {float(l.cantidad_devuelta) for l in lineas} == {1.0}


class TestUnidadUnicaNoCambia:
    """**El detector no dispara sobre operación sana.** Un producto de unidad
    única crea exactamente una línea, con la misma cantidad de siempre."""

    def test_parcial_de_una_referencia_crea_una_linea(
            self, app, db, recaudo_liq, producto):
        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 2}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_unidad_unica(producto.codigo_siesa)):
            resultado = _procesar(recaudo, db)

        assert resultado['nc'] == 1 and resultado['rc'] == 1
        _dev, lineas = _lineas_de(recaudo.id)
        assert len(lineas) == 1
        assert float(lineas[0].cantidad_devuelta) == 2.0
        assert float(lineas[0].cantidad_facturada) == 5.0
        assert lineas[0].f470_rowid == '2857568'
        assert lineas[0].f470_id_unidad_medida == 'UND'
        assert lineas[0].f150_id_bodega == 'NB1'

    def test_declarar_mas_de_lo_facturado_sigue_topeando(
            self, app, db, recaudo_liq, producto):
        """`min(cant_devuelta, cant_facturada)` — comportamiento viejo que se
        conserva: no se puede devolver más de lo que se facturó."""
        from app.services.liquidacion_service import _crear_devolucion_pendiente

        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 99}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=_factura_unidad_unica(producto.codigo_siesa)):
            _crear_devolucion_pendiente(
                recaudo, recaudo.tarea, 'FEW', '1466',
                items_devueltos=items, notas='test')
        db.session.commit()

        _dev, lineas = _lineas_de(recaudo.id)
        assert float(lineas[0].cantidad_devuelta) == 5.0

    def test_dos_referencias_distintas_una_linea_cada_una(
            self, app, db, recaudo_liq, producto, producto2):
        from app.services.liquidacion_service import _crear_devolucion_pendiente

        factura = (_factura_unidad_unica(producto.codigo_siesa)
                   + [{'f470_rowid': 2857569, 'f120_referencia': producto2.codigo_siesa,
                       'f470_cant_base': 4, 'f470_id_unidad_medida': 'UND',
                       'f150_id': 'NB1', 'f470_vlr_neto': 400.0}])
        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 2},
                 {'codigo': producto2.codigo, 'cantidad_devuelta': 4}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=factura):
            _crear_devolucion_pendiente(
                recaudo, recaudo.tarea, 'FEW', '1466',
                items_devueltos=items, notas='test')
        db.session.commit()

        _dev, lineas = _lineas_de(recaudo.id)
        por_codigo = {l.codigo_siesa: float(l.cantidad_devuelta) for l in lineas}
        assert por_codigo == {producto.codigo_siesa: 2.0,
                              producto2.codigo_siesa: 4.0}

    def test_lo_no_devuelto_no_entra(self, app, db, recaudo_liq, producto, producto2):
        """Una referencia facturada que el conductor no devolvió no genera
        línea. Comportamiento viejo, se conserva."""
        from app.services.liquidacion_service import _crear_devolucion_pendiente

        factura = (_factura_unidad_unica(producto.codigo_siesa)
                   + [{'f470_rowid': 2857569, 'f120_referencia': producto2.codigo_siesa,
                       'f470_cant_base': 4, 'f470_id_unidad_medida': 'UND',
                       'f150_id': 'NB1', 'f470_vlr_neto': 400.0}])
        items = [{'codigo': producto2.codigo, 'cantidad_devuelta': 1}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              items_ent=items)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=factura):
            _crear_devolucion_pendiente(
                recaudo, recaudo.tarea, 'FEW', '1466',
                items_devueltos=items, notas='test')
        db.session.commit()

        _dev, lineas = _lineas_de(recaudo.id)
        assert [l.codigo_siesa for l in lineas] == [producto2.codigo_siesa]


# ═══════════════════════════════════════════════════════════════════
# DEFECTO 2 — el DC que no se encola y el contador que dice 1
# ═══════════════════════════════════════════════════════════════════
class TestDCQueNoSeEncola:

    @staticmethod
    def _dc_jobs(recaudo_id):
        from app.models.siesa_job import SiesaJob
        return SiesaJob.query.filter_by(
            referencia_id=recaudo_id, referencia_tipo='RecaudoEntrega',
            tipo='DOCUMENTO_CONTABLE_RET').all()

    def test_factura_ilegible_no_encola_y_no_cuenta(
            self, app, db, recaudo_liq, producto):
        """`get_rowids_factura` levanta (barrido incompleto). No hay base
        gravable → no se encola nada. El contador NO puede decir 1: sería un
        documento de retención informado que nunca se envió."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEIVA', monto_desc=0)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   side_effect=RuntimeError('barrido incompleto: página 2 de 5')):
            resultado = _procesar(recaudo, db)

        assert self._dc_jobs(recaudo.id) == [], 'se encoló un DC sin base gravable'
        assert resultado['dc'] == 0, (
            f"el tablero informa {resultado['dc']} DC y no se encoló ninguno")
        assert resultado.get('errores'), 'el fallo no se declaró en ningún lado'
        assert 'retención' in resultado['errores'][0].lower()

    def test_factura_sin_lineas_no_encola_y_no_cuenta(
            self, app, db, recaudo_liq, producto):
        """Mismo defecto por la otra puerta: la factura responde vacía."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEIVA', monto_desc=0)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=[]):
            resultado = _procesar(recaudo, db)

        assert self._dc_jobs(recaudo.id) == []
        assert resultado['dc'] == 0
        assert resultado.get('errores')

    def test_motivo_sin_puc_no_encola_y_no_cuenta(self, app, db, recaudo_liq):
        """Un motivo fuera del catálogo tampoco produce documento — y tampoco
        puede contar uno."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='INVENTADO_X', monto_desc=5000)

        resultado = _procesar(recaudo, db)

        assert self._dc_jobs(recaudo.id) == []
        assert resultado['dc'] == 0
        assert resultado.get('errores')

    def test_parcial_contado_tambien(self, app, db, recaudo_liq, producto):
        """La otra rama con DC (`PARCIAL` contado, `:1112`). El mismo contador
        incondicional vivía en las dos."""
        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 2}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000,
                              motivo_desc='RETEIVA', monto_desc=0,
                              items_ent=items)

        def _factura_o_error(tipo_docto, consec, *a, **kw):
            # La devolución pendiente sí lee la factura; el cálculo de la
            # retención se hace con la MISMA llamada. Se rompe solo la segunda
            # para aislar el defecto del DC.
            if _factura_o_error.llamadas:
                raise RuntimeError('barrido incompleto')
            _factura_o_error.llamadas += 1
            return _factura_unidad_unica(producto.codigo_siesa)
        _factura_o_error.llamadas = 0

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   side_effect=_factura_o_error):
            resultado = _procesar(recaudo, db)

        assert resultado['nc'] == 1 and resultado['rc'] == 1
        assert self._dc_jobs(recaudo.id) == []
        assert resultado['dc'] == 0
        assert resultado.get('errores')

    def test_la_ruta_declara_el_dc_fallido_en_errores(
            self, app, db, recaudo_liq, producto):
        """A nivel ruta: `dc_encolados` refleja la cola real y el fallo llega a
        `errores`, que es lo que la pantalla pinta en rojo (`rutas.js:2893`).
        Es el patrón del caller gemelo (`rutas.py:1259-1276`)."""
        from app.services.liquidacion_service import LiquidacionService

        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEIVA', monto_desc=0)

        with patch('app.services.liquidacion_service._obtener_tercero',
                   return_value=('900123456', '001')):
            with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                       side_effect=RuntimeError('barrido incompleto')):
                resumen = LiquidacionService.liquidar_ruta_siesa(recaudo.ruta_id)

        assert resumen['dc_encolados'] == 0, (
            f"la pantalla pintaría «{resumen['dc_encolados']} DC» sin documento")
        assert resumen['rc_encolados'] == 1, 'el RC sí se encoló y sí se cuenta'
        assert len(resumen['errores']) == 1


class TestDCQueSiEncola:
    """**La otra dirección.** Una retención que sí encola sigue contando 1 y
    devolviendo exactamente lo mismo que antes del arreglo."""

    def test_monto_declarado_encola_y_cuenta_uno(self, app, db, recaudo_liq):
        from app.models.siesa_job import SiesaJob

        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEFUENTE_2.5', monto_desc=21008.40)

        resultado = _procesar(recaudo, db)

        jobs = SiesaJob.query.filter_by(
            referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').all()
        assert len(jobs) == 1
        assert jobs[0].get_payload()['cuenta_puc'] == '13551501'
        assert jobs[0].get_payload()['monto'] == 21008.40
        assert resultado['dc'] == 1
        assert resultado['rc'] == 1
        assert not resultado.get('errores')

    def test_monto_calculado_de_siesa_encola_y_cuenta_uno(self, app, db, recaudo_liq):
        """El camino que pasa por `get_rowids_factura` cuando responde bien:
        reteIVA sobre el IVA real (159.664 × 0.15), no sobre lo cobrado."""
        from app.models.siesa_job import SiesaJob

        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEIVA', monto_desc=0)

        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=[{'f470_vlr_bruto': 840336, 'f470_vlr_imp': 159664,
                                  'f470_vlr_neto': 1000000, 'f120_referencia': 'REF001',
                                  'f470_rowid': 'R1'}]):
            resultado = _procesar(recaudo, db)

        job = SiesaJob.query.filter_by(
            referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').first()
        assert job is not None
        assert job.get_payload()['monto'] == round(159664 * 0.15, 2)
        assert resultado['dc'] == 1
        assert not resultado.get('errores')

    def test_dc_ya_en_cola_sigue_contando_uno_y_no_duplica(self, app, db, recaudo_liq):
        """Dedup: el DC de esa cuenta ya está en cola (lo dejó
        `/liquidar-completo`). No se encola un segundo job **y el contador
        sigue diciendo 1** — porque hay un documento real en la cola. No es un
        error: no se declara nada."""
        from app.models.siesa_job import SiesaJob

        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEFUENTE_2.5', monto_desc=21008.40)
        SiesaJob.encolar(
            tipo='DOCUMENTO_CONTABLE_RET',
            payload={'recaudo_id': recaudo.id, 'cuenta_puc': '13551501',
                     'monto': 21008.40, 'tipo_docto_fe': 'FEW', 'consec_fe': '1'},
            referencia_tipo='RecaudoEntrega', referencia_id=recaudo.id,
        )
        db.session.commit()

        resultado = _procesar(recaudo, db)

        jobs = SiesaJob.query.filter_by(
            referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').all()
        assert len(jobs) == 1
        assert resultado['dc'] == 1
        assert not resultado.get('errores')
