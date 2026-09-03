"""
El veredicto de la reconciliación comparaba dos poblaciones distintas.

`«✓ Sin diferencias — WMS y Siesa coinciden (N productos)»` es la casilla de la
**Fase 4 de `docs/arranque_produccion.md`**: la luz verde para que los operarios
arranquen en producción, y el criterio para decidir si hay que volver a correr
«Cargar stock inicial». Se emitía sobre dos universos que no se pueden comparar:

    WMS    →  SUM(UbicacionProducto.cantidad) GROUP BY producto_id
              — TODOS los almacenes, incluidas las tiendas PV-NC1, PV-PC1…
    Siesa  →  solo `connekta.bodega`, o sea NB1

Tres desalineaciones estructurales, todas verificadas en el código:

  1. **Tiendas.** `tienda_oc_service.resolver_almacen` y `recepcion_service`
     crean almacenes reales por bodega PV con stock físico. Ese stock entra en
     el lado WMS y no en el de Siesa.
  2. **Averías.** `devolucion_service` suma la avería a una ubicación
     `AVERIADOS` (zona CUARENTENA) **dentro del almacén NB1**, mientras Siesa la
     mueve de NB1 a AV1. AV1 ni siquiera se descargaba. Cada avería producía un
     `WMS_MAYOR` permanente.
  3. **Traslados en tránsito.** TRA1 tampoco se descargaba.

Lo que costaba: el número podía mentir en las **dos** direcciones.

  · Falso positivo — el stock de una tienda, correcto en las dos partes, se
    reportaba como sobrante del WMS (`TestElStockDeUnaTiendaNoEsUnSobrante`).
  · Falso negativo — un faltante real en NB1 tapado por el stock de una tienda
    que Siesa nunca reportó: el veredicto decía «✓ Sin diferencias» con cuatro
    unidades perdidas (`TestElVeredictoDeSinDiferenciasMentia`). **Este es el
    caro**: es el que da la luz verde de la Fase 4.

Y el detector se mide en las dos direcciones: sobre operación sana —un solo
almacén, una sola bodega, todo cuadrado— el veredicto tiene que seguir siendo
«sin diferencias» exactamente como antes (`TestOperacionSanaSigueEnVerde`). Un
detector que solo prueba que dispara, prueba la mitad.
"""
import pytest

from app.models.almacen import Almacen
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto
from app.services import inventario_siesa_service as inv


# ─────────────────────────────────────────────
# Utilidades de armado
# ─────────────────────────────────────────────

def _siesa(por_bodega: dict) -> dict:
    """`{'NB1': {'SKU1': 10}}` → la forma real de `_descargar_inventario_siesa_raw`."""
    return {
        bodega: {
            codigo: {
                'existencia': float(cant),
                'comprometido': 0.0,
                'salida_sin_conf': 0.0,
                'descripcion': '',
                'unidad': 'UND',
            }
            for codigo, cant in productos.items()
        }
        for bodega, productos in por_bodega.items()
    }


def _almacen(db, codigo, bodega_siesa_id):
    a = Almacen(codigo=codigo, nombre=codigo, bodega_siesa_id=bodega_siesa_id,
                activo=True)
    db.session.add(a)
    db.session.flush()
    u = Ubicacion(codigo=f'{codigo}-GENERAL', almacen_id=a.id, zona='GENERAL',
                  tipo='estanteria', tipo_zona='GENERAL', activo=True)
    db.session.add(u)
    db.session.flush()
    return a, u


def _producto(db, codigo):
    p = Producto(codigo=codigo, nombre=f'Producto {codigo}', codigo_siesa=codigo,
                 activo=True)
    db.session.add(p)
    db.session.flush()
    return p


def _stock(db, ubicacion, producto, cantidad):
    db.session.add(UbicacionProducto(ubicacion_id=ubicacion.id,
                                     producto_id=producto.id,
                                     cantidad=cantidad))
    db.session.flush()


def _reconciliar(app, monkeypatch, siesa_por_bodega: dict) -> dict:
    """Corre la reconciliación real con Siesa mockeado y devuelve el resultado.

    Se parchean **las dos** puertas de descarga a propósito: la vieja
    (`_descargar_inventario_siesa`, que aplasta todo a `connekta.bodega`) y la
    multi-bodega. Así este mismo test corre contra el código de antes y contra
    el de después sin tocar una línea — que es lo que hace que la evidencia del
    «rojo antes del arreglo» valga.
    """
    multi = _siesa(siesa_por_bodega)
    principal = multi.get(inv.connekta.bodega, {})
    monkeypatch.setattr(inv, '_descargar_inventario_siesa_raw',
                        lambda forzar=False: multi)
    monkeypatch.setattr(inv, '_descargar_inventario_siesa',
                        lambda forzar=False: principal)
    # El guard anti-respuesta-parcial exige ≥50 SKU — es correcto en producción
    # y no tiene nada que ver con lo que este archivo mide. `raising=False` para
    # que el mismo test corra contra la versión de antes, donde no existía.
    monkeypatch.setattr(inv, '_verificar_respuesta_no_parcial',
                        lambda _inventario: None, raising=False)

    inv._estado_reconciliacion['ultimo_resultado'] = None
    inv._estado_reconciliacion['ultimo_error'] = None
    inv._run_reconciliacion(app)

    assert inv._estado_reconciliacion['ultimo_error'] is None, (
        'la reconciliación reventó: '
        + str(inv._estado_reconciliacion['ultimo_error']))
    resultado = inv._estado_reconciliacion['ultimo_resultado']
    assert resultado is not None
    return resultado


def _bodega_de(resultado, bodega):
    for fila in resultado.get('por_bodega', []):
        if fila['bodega'] == bodega:
            return fila
    return None


# ─────────────────────────────────────────────
# El defecto — dispara
# ─────────────────────────────────────────────

class TestElStockDeUnaTiendaNoEsUnSobrante:
    """Falso positivo: todo cuadra bodega contra bodega y aun así hay 1 discrepancia."""

    def test_cada_bodega_se_compara_con_la_suya(self, app, db, monkeypatch):
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _, ub_tienda = _almacen(db, 'PV-NC1', 'NC1')
        _stock(db, ub_cd, p, 10)
        _stock(db, ub_tienda, p, 4)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 10}, 'NC1': {'SKU1': 4}})

        assert r['total_discrepancias'] == 0, (
            'las 4 unidades de la tienda se sumaron al lado WMS y se restaron '
            'contra la existencia de NB1: ' + str(r['discrepancias']))
        assert r['sin_diferencias'] is True


class TestElVeredictoDeSinDiferenciasMentia:
    """Falso negativo — el caro. Es el que enciende la luz verde de la Fase 4."""

    def test_un_faltante_en_nb1_no_lo_tapa_el_stock_de_una_tienda(
            self, app, db, monkeypatch):
        # Siesa dice 10 en NB1 y nada en NC1. El WMS tiene 6 en NB1 y 4 en la
        # tienda: los totales dan 10 = 10 y el veredicto decía «sin diferencias»
        # con cuatro unidades perdidas en el CD.
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _, ub_tienda = _almacen(db, 'PV-NC1', 'NC1')
        _stock(db, ub_cd, p, 6)
        _stock(db, ub_tienda, p, 4)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 10}})

        nb1 = [d for d in r['discrepancias'] if d.get('bodega') == 'NB1']
        assert len(nb1) == 1, (
            'el veredicto declara ' + str(r['total_discrepancias']) +
            ' diferencias con 4 unidades perdidas en NB1 — esta es la luz '
            'verde de la Fase 4 de docs/arranque_produccion.md')
        assert r['sin_diferencias'] is False, (
            'el veredicto sigue diciendo que WMS y Siesa coinciden')
        assert nb1[0]['stock_wms'] == 6
        assert nb1[0]['stock_siesa'] == 10
        assert nb1[0]['estado'] == 'SIESA_MAYOR'


class TestCadaDiscrepanciaDiceDeQueBodegaEs:
    def test_la_bodega_viaja_en_la_fila(self, app, db, monkeypatch):
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 3)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 9}})

        assert r['total_discrepancias'] == 1
        assert r['discrepancias'][0]['bodega'] == 'NB1', (
            'una diferencia sin bodega no se puede ir a contar a ningún lado')


# ─────────────────────────────────────────────
# El detector, en la otra dirección
# ─────────────────────────────────────────────

class TestOperacionSanaSigueEnVerde:
    """Un solo almacén, una sola bodega, todo cuadrado.

    Esto pasaba ANTES del arreglo y tiene que seguir pasando: un detector que
    solo prueba que dispara, prueba la mitad.
    """

    def test_sin_diferencias_exactamente_como_antes(self, app, db, monkeypatch):
        p1 = _producto(db, 'SKU1')
        p2 = _producto(db, 'SKU2')
        _, ub = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub, p1, 10)
        _stock(db, ub, p2, 5)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 10, 'SKU2': 5}})

        assert r['total_discrepancias'] == 0
        assert r['discrepancias'] == []
        # `.get` con default: antes del arreglo estas claves no existían, y este
        # test tiene que pasar en las dos versiones.
        assert r.get('total_no_comparable', 0) == 0
        assert r.get('sin_diferencias', True) is True


# ─────────────────────────────────────────────
# El tercer estado: «no se puede comparar»
# ─────────────────────────────────────────────

class TestNoSePuedeCompararNoEsCuadra:

    def test_almacen_sin_bodega_siesa_asignada(self, app, db, monkeypatch):
        """Regla 0: ante dato ausente, fallar conservador y declararlo.

        Antes, ese stock se sumaba en silencio al total del WMS y aparecía como
        sobrante contra NB1."""
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        huerfano = Almacen(codigo='TALLER', nombre='Taller', activo=True)
        db.session.add(huerfano)
        db.session.flush()
        ub_h = Ubicacion(codigo='TALLER-GENERAL', almacen_id=huerfano.id,
                         zona='GENERAL', tipo='estanteria', tipo_zona='GENERAL',
                         activo=True)
        db.session.add(ub_h)
        db.session.flush()
        _stock(db, ub_h, p, 7)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 10}})

        assert r['total_discrepancias'] == 0, (
            'las 7 unidades del almacén sin bodega no son un sobrante de NB1')
        sin_bodega = r['no_comparable']['almacenes_sin_bodega_siesa']
        assert [a['almacen'] for a in sin_bodega] == ['TALLER']
        assert sin_bodega[0]['unidades'] == 7
        assert r['total_no_comparable'] >= 1
        assert r['sin_diferencias'] is False, (
            '«no se puede comparar» no puede salir en verde')

    def test_bodega_siesa_sin_almacen_wms(self, app, db, monkeypatch):
        """AV1 (averías) es el caso vivo: Siesa la mueve de NB1 a AV1 y el WMS
        la deja en una ubicación CUARENTENA dentro de NB1.

        Acá se mide que la bodega **se declare y se mida**. Si además
        descalifica el veredicto es otra pregunta, y la contesta
        `TestElIncomparableEsperadoNoDescalificaElVerde`: AV1 está prevista y
        declarada, así que no lo descalifica; una bodega que nadie previó, sí.
        """
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        db.session.commit()

        r = _reconciliar(app, monkeypatch,
                         {'NB1': {'SKU1': 10}, 'AV1': {'SKU1': 2}})

        sin_almacen = r['no_comparable']['bodegas_siesa_sin_almacen_wms']
        assert [b['bodega'] for b in sin_almacen] == ['AV1']
        assert sin_almacen[0]['unidades_siesa'] == 2
        assert r['total_no_comparable'] == 1

    def test_bodega_wms_sin_datos_de_siesa(self, app, db, monkeypatch):
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _, ub_tienda = _almacen(db, 'PV-PC1', 'PC1')
        _stock(db, ub_cd, p, 10)
        _stock(db, ub_tienda, p, 3)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 10}})

        sin_datos = r['no_comparable']['bodegas_wms_sin_datos_siesa']
        assert [b['bodega'] for b in sin_datos] == ['PC1']
        assert sin_datos[0]['unidades_wms'] == 3
        assert r['total_discrepancias'] == 0
        assert r['sin_diferencias'] is False


# ─────────────────────────────────────────────
# Cobertura: el denominador tiene que verse
# ─────────────────────────────────────────────

class TestLaCoberturaSePublicaPorBodega:

    def test_cuadre_con_su_denominador(self, app, db, monkeypatch):
        p1 = _producto(db, 'SKU1')
        p2 = _producto(db, 'SKU2')
        p3 = _producto(db, 'SKU3')
        _, ub = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub, p1, 10)   # cuadra
        _stock(db, ub, p2, 5)    # cuadra
        _stock(db, ub, p3, 1)    # no cuadra
        db.session.commit()

        r = _reconciliar(app, monkeypatch,
                         {'NB1': {'SKU1': 10, 'SKU2': 5, 'SKU3': 4}})

        nb1 = _bodega_de(r, 'NB1')
        assert nb1 is not None
        assert nb1['denominador'] == 3
        assert nb1['cuadran'] == 2
        assert nb1['cuadre_pct'] == pytest.approx(66.7, abs=0.1)
        assert r['cuadre_pct'] == pytest.approx(66.7, abs=0.1)

    def test_la_cobertura_es_un_valor_calculado_desde_lo_sembrado(
            self, app, db, monkeypatch):
        """`cobertura_pct` se afirma como VALOR, no como presencia de la clave.

        Es el denominador del catálogo —cuántos de los productos que Siesa
        reporta tienen stock en el WMS— y quien lo lee decide si la
        reconciliación cubre lo suficiente como para creerle
        (`devoluciones_activas` cuelga del mismo número, umbral 20%).

        El número de acá está calculado a mano desde los datos sembrados, no
        copiado de una corrida: 3 productos con stock en el WMS sobre los 7
        códigos que Siesa reporta = 42.857… → 42.9. Un `cobertura_pct` fijo
        (100, o el que sea) no puede satisfacer esto.
        """
        p1 = _producto(db, 'SKU1')
        p2 = _producto(db, 'SKU2')
        p3 = _producto(db, 'SKU3')
        _producto(db, 'SKU4')          # en catálogo, SIN stock: no cuenta arriba
        _, ub = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub, p1, 10)
        _stock(db, ub, p2, 5)
        _stock(db, ub, p3, 1)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {
            'SKU1': 10, 'SKU2': 5, 'SKU3': 1,
            'SKU4': 0,                                  # sin stock en ninguno
            'SKU90': 3, 'SKU91': 3, 'SKU92': 3,         # sin producto en el WMS
        }})

        assert r['total_productos_wms'] == 3
        assert r['total_productos_siesa'] == 7
        assert r['cobertura_pct'] == pytest.approx(42.9, abs=0.05)
        # El umbral de 20% que enciende `devoluciones_activas` cuelga del mismo
        # número: si la cobertura miente, esto miente con ella.
        assert r['devoluciones_activas'] is True

    def test_cobertura_baja_apaga_devoluciones(self, app, db, monkeypatch):
        """La otra dirección del mismo número: 1 de 10 = 10% < 20%."""
        p1 = _producto(db, 'SKU1')
        _, ub = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub, p1, 10)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': dict(
            {'SKU1': 10}, **{f'SKU9{i}': 2 for i in range(9)})})

        assert r['total_productos_wms'] == 1
        assert r['total_productos_siesa'] == 10
        assert r['cobertura_pct'] == pytest.approx(10.0, abs=0.05)
        assert r['devoluciones_activas'] is False


class TestElUniversoDescargadoIncluyeLasBodegasDeServicio:

    def test_av1_y_tra1_estan_en_el_universo(self):
        assert 'AV1' in inv._BODEGAS_INVENTARIO
        assert 'TRA1' in inv._BODEGAS_INVENTARIO

    def test_bodegas_pv_no_las_incluye(self):
        """`_BODEGAS_PV` sigue significando «bodegas que el WMS opera» — es lo
        que vigila `tests/test_bodegas_coherentes.py`. Las de servicio son otra
        lista, no una entrada más en esa."""
        assert 'AV1' not in inv._BODEGAS_PV
        assert 'TRA1' not in inv._BODEGAS_PV

    def test_la_descarga_devuelve_av1_y_tra1(self, monkeypatch):
        """Sobre el RESULTADO de la descarga, no sobre su código fuente.

        La versión anterior de este test leía `inspect.getsource` y exigía que
        el identificador `_BODEGAS_INVENTARIO` apareciera en el cuerpo. Un
        auditor de mutación reescribió el filtro dejando el identificador
        escrito —`bodega not in (_BODEGAS_PV if _BODEGAS_INVENTARIO else [])`—
        y volvió a tirar AV1/TRA1: **12 passed**. Es la falla que `CLAUDE.md`
        documenta, «el nombre aparece también en la asignación».

        Acá se siembran filas de las tres clases de bodega y se mira qué sale.
        """
        filas = [
            {'f150_id': 'NB1', 'f120_referencia': 'SKU1',
             'f400_cant_existencia_1': 10},
            {'f150_id': 'AV1', 'f120_referencia': 'SKU1',
             'f400_cant_existencia_1': 2},
            {'f150_id': 'TRA1', 'f120_referencia': 'SKU2',
             'f400_cant_existencia_1': 3},
            # `FD1` es una de las bodegas «DUPLICADA» que `CLAUDE.md` manda
            # ignorar: el filtro también tiene que seguir TIRÁNDOLA. Un
            # detector que solo prueba que deja pasar, prueba la mitad.
            {'f150_id': 'FD1', 'f120_referencia': 'SKU3',
             'f400_cant_existencia_1': 9},
        ]
        paginas = {1: filas}
        monkeypatch.setattr(
            inv.connekta, '_get',
            lambda api, params, url=None: {
                'detalle': {'Datos': paginas.get(
                    int(params['paginacion'].split('numPag=')[1].split('|')[0]),
                    [])}})

        res = inv._descargar_una_pasada_custom()

        assert set(res) == {'NB1', 'AV1', 'TRA1'}, (
            'el filtro de la descarga volvió a tirar las bodegas de servicio '
            '(o dejó entrar una bodega que el WMS no debe tocar)')
        assert res['AV1']['SKU1']['existencia'] == 2.0
        assert res['TRA1']['SKU2']['existencia'] == 3.0
        assert 'FD1' not in res


class TestElIncomparableEsperadoNoDescalificaElVerde:
    """El verde de la Fase 4 tiene que ser ALCANZABLE.

    `sin_diferencias` exigía cero incomparables de cualquier clase. `AV1` no
    tiene almacén WMS y **nunca lo va a tener** (`CLAUDE.md` la declara bodega
    de servicio): cada vez que tuviera saldo en Siesa —o sea, cada avería— el
    veredicto quedaba en ámbar para siempre. Una casilla que no se puede poner
    en verde deja de leerse, y lo que se deja de leer no avisa nada.

    Lo que sí tiene que descalificar es el incomparable que **nadie previó**.
    """

    def test_av1_con_saldo_sigue_dando_verde(self, app, db, monkeypatch):
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        db.session.commit()

        r = _reconciliar(app, monkeypatch,
                         {'NB1': {'SKU1': 10}, 'AV1': {'SKU1': 2}})

        assert r['total_discrepancias'] == 0
        assert r['sin_diferencias'] is True, (
            'AV1 tiene contraparte declarada y medida y aun así descalificaba '
            'el veredicto — la casilla de la Fase 4 quedaba en ámbar siempre')
        assert r['total_no_comparable_imprevisto'] == 0
        assert r['total_no_comparable_esperado'] == 1

    def test_av1_sigue_declarado_medido_y_visible(self, app, db, monkeypatch):
        """No descalificar no es esconder. «Esperado» sigue saliendo en la lista
        con su contraparte medida — si dejara de verse, el arreglo sería una
        excepción silenciosa, que es lo contrario de declararla."""
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        db.session.commit()

        r = _reconciliar(app, monkeypatch,
                         {'NB1': {'SKU1': 10}, 'AV1': {'SKU1': 2}})

        sin_almacen = r['no_comparable']['bodegas_siesa_sin_almacen_wms']
        assert [b['bodega'] for b in sin_almacen] == ['AV1']
        assert sin_almacen[0]['unidades_siesa'] == 2
        assert sin_almacen[0]['esperado'] is True
        assert sin_almacen[0]['justificacion']
        assert sin_almacen[0]['contraparte_wms']['tipo'] == 'UBICACION'
        # La clave vieja se conserva y sigue contando TODO lo incomparable.
        assert r['total_no_comparable'] == 1

    def test_la_fila_por_bodega_tambien_lo_distingue(self, app, db, monkeypatch):
        """La tabla por bodega es lo que se mira primero. Si ahí AV1 se viera
        igual que una bodega inesperada, la distinción del veredicto quedaría
        escondida en el bloque de abajo."""
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 10},
                                            'AV1': {'SKU1': 2},
                                            'BC99': {'SKU1': 7}})

        assert _bodega_de(r, 'AV1')['esperado'] is True
        assert _bodega_de(r, 'BC99')['esperado'] is False
        assert _bodega_de(r, 'NB1')['comparable'] is True

    def test_una_bodega_que_nadie_previo_si_descalifica(
            self, app, db, monkeypatch):
        """`BC99` (Bodega Contratación) existe en Siesa y el WMS no la toca.
        El día que aparezca con saldo, nadie lo decidió: eso es exactamente lo
        que el ámbar tiene que seguir diciendo."""
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        db.session.commit()

        r = _reconciliar(app, monkeypatch,
                         {'NB1': {'SKU1': 10}, 'BC99': {'SKU1': 7}})

        sin_almacen = r['no_comparable']['bodegas_siesa_sin_almacen_wms']
        assert [b['bodega'] for b in sin_almacen] == ['BC99']
        assert sin_almacen[0]['esperado'] is False
        assert sin_almacen[0]['justificacion'] is None
        assert r['total_no_comparable_imprevisto'] == 1
        assert r['sin_diferencias'] is False, (
            'una bodega de Siesa con saldo que nadie previó no puede salir en '
            'verde: «no sé» no es «está bien»')

    def test_el_esperado_no_tapa_al_imprevisto(self, app, db, monkeypatch):
        """Los dos a la vez: el esperado no puede absorber al que sí importa."""
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        db.session.commit()

        r = _reconciliar(app, monkeypatch, {'NB1': {'SKU1': 10},
                                            'AV1': {'SKU1': 2},
                                            'BC99': {'SKU1': 7}})

        assert r['total_no_comparable_esperado'] == 1
        assert r['total_no_comparable_imprevisto'] == 1
        assert r['sin_diferencias'] is False

    def test_una_discrepancia_real_sigue_descalificando(
            self, app, db, monkeypatch):
        """Con AV1 exento, una diferencia de verdad en NB1 tiene que seguir
        rompiendo el verde. El exento no es una puerta trasera al veredicto."""
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 6)
        db.session.commit()

        r = _reconciliar(app, monkeypatch,
                         {'NB1': {'SKU1': 10}, 'AV1': {'SKU1': 2}})

        assert r['total_discrepancias'] == 1
        assert r['sin_diferencias'] is False

    def test_un_almacen_wms_sin_bodega_nunca_es_esperado(
            self, app, db, monkeypatch):
        """La exención vale SOLO para bodegas de Siesa sin almacén WMS. Un
        almacén del WMS sin `bodega_siesa_id` es un dato que falta, no una
        decisión de diseño — y sigue descalificando."""
        p = _producto(db, 'SKU1')
        _, ub_cd = _almacen(db, 'NB1', 'NB1')
        _stock(db, ub_cd, p, 10)
        huerfano = Almacen(codigo='TALLER', nombre='Taller', activo=True)
        db.session.add(huerfano)
        db.session.flush()
        ub_h = Ubicacion(codigo='TALLER-GENERAL', almacen_id=huerfano.id,
                         zona='GENERAL', tipo='estanteria', tipo_zona='GENERAL',
                         activo=True)
        db.session.add(ub_h)
        db.session.flush()
        _stock(db, ub_h, p, 7)
        db.session.commit()

        r = _reconciliar(app, monkeypatch,
                         {'NB1': {'SKU1': 10}, 'AV1': {'SKU1': 2}})

        assert r['total_no_comparable_imprevisto'] >= 1
        assert r['sin_diferencias'] is False


class TestLaListaDeExentasEsCortaYVigilada:
    """Una lista de excepciones escrita a mano se convierte en el sitio donde
    alguien mete lo que le molesta. Ésta tiene dos entradas, cada una con su
    motivo verificable, y este test es el que no la deja crecer callada."""

    def test_son_exactamente_av1_y_tra1(self):
        assert set(inv._BODEGAS_SERVICIO) == {'AV1', 'TRA1'}, (
            'creció la lista de bodegas exentas del veredicto — cada entrada '
            'nueva necesita su justificación y su línea en CLAUDE.md')

    def test_cada_exenta_trae_su_justificacion_y_su_contraparte(self):
        for bodega in inv._BODEGAS_SERVICIO:
            motivo = inv._incomparable_esperado(bodega)
            assert motivo, f'{bodega} está exenta sin decir por qué'
            assert inv._contraparte_declarada(bodega) is not None, (
                f'{bodega} está exenta sin declarar dónde vive su contraparte '
                'en el WMS (o su ausencia)')

    def test_ninguna_bodega_operada_esta_exenta(self):
        """Eximir una bodega que el WMS opera sería apagar el detector para el
        stock que sí se cuadra."""
        for bodega in inv._BODEGAS_PV:
            assert inv._incomparable_esperado(bodega) is None

    def test_una_bodega_cualquiera_no_esta_exenta(self):
        assert inv._incomparable_esperado('BC99') is None
        assert inv._incomparable_esperado('FD1') is None
        assert inv._incomparable_esperado('') is None
