"""
Producto de doble unidad: la clave del cruce de la NC no puede ser la referencia.

Este repo tiene productos que se facturan en DOS unidades de medida a la vez.
La misma referencia aparece en la factura como dos líneas —una en PQ y otra en
UND— con `f470_rowid` distintos y valores distintos. Está documentado en
`app/services/despacho_parcial_service.py:102-106` para PAPELSP6741, donde un
dict indexado solo por referencia *«guardaba la ÚLTIMA línea (UND) causando el
error 244328»*.

**Ese arreglo se hizo en un solo sitio.** `_construir_lineas_nc`
(`app/services/siesa_job_service.py:381`) sigue indexando por referencia sola::

    devueltos_map = {it['codigo']: it for it in items_devueltos ...}
    for row in rowids_data:
        item = devueltos_map.get(row.get('f120_referencia', ''))

Con las dos líneas de la misma referencia en `rowids_data`, **las dos matchean
el mismo item devuelto** y las dos entran a la NC con la cantidad devuelta
entera. Reproducido por ejecución sobre PAPELSP6741::

    devolvió 1 unidad → la NC sale con 2 línea(s)
      rowid=2857568  cant=1  uom=PQ   neto=24250
      rowid=2857569  cant=1  uom=UND  neto=2425
    F353_VLR_CRUCE = 26675          ← debía ser 24250

Se cruza de más contra la factura, y se devuelve inventario que nadie devolvió.

## ARREGLADO (2026-08-20) — los `xfail` se borraron

La clave del match es ahora `f470_rowid`, y `devolucion_cliente_service` lo
propaga al payload del job (el dato ya venía entero desde `buscar_pedido` y
desde el PWA; se perdía en el último salto).

Estos tres tests nacieron `xfail(strict=True)` porque el arreglo tocaba dos
archivos fuera de la propiedad de quien escribió el detector. Eso era lo
correcto: el estricto los ponía **rojos** el día que alguien arreglara, que es
la señal de borrar el marcador. Se borró.

**Detector en las dos direcciones**: `TestUnidadUnica` prueba que sobre un
producto de unidad única la NC sale **exactamente igual que antes** — mismas
líneas, mismo `F353_VLR_CRUCE`, mismo prorrateo. Un detector que solo prueba
que dispara, prueba la mitad. `TestItemSinRowid` cubre la Regla 0: qué pasa con
un payload viejo, encolado antes de este arreglo.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.services.siesa_job_service import (
    LineaDevueltaAmbigua, _construir_lineas_nc)


#: PAPELSP6741 facturado en sus dos unidades: 1 PQ ($24.250) y 1 UND ($2.425).
#: Misma referencia, dos `f470_rowid`, dos valores. Un paquete son 10 unidades.
FACTURA_DOBLE_UNIDAD = [
    {
        'f470_rowid': 2857568,
        'f120_referencia': 'PAPELSP6741',
        'f470_cant_base': 1,
        'f470_id_unidad_medida': 'PQ',
        'f150_id': 'NB1',
        'f470_vlr_bruto': 20378.0,
        'f470_vlr_imp': 3872.0,
        'f470_vlr_neto': 24250.0,
    },
    {
        'f470_rowid': 2857569,
        'f120_referencia': 'PAPELSP6741',
        'f470_cant_base': 1,
        'f470_id_unidad_medida': 'UND',
        'f150_id': 'NB1',
        'f470_vlr_bruto': 2038.0,
        'f470_vlr_imp': 387.0,
        'f470_vlr_neto': 2425.0,
    },
]

_ARGS = dict(causal='01', motivo='01', uom_default='UND', bodega_default='NB1')

#: Lo que la recepcionista contó: **un paquete**, la línea PQ (rowid 2857568).
#: Antes del arreglo este item se declaraba solo con la referencia y por eso no
#: había forma de saber si era el paquete o la unidad suelta. Hoy el PWA manda
#: `f470_rowid` por línea y `devolucion_cliente_service` lo propaga al payload.
_DEVUELTO_UN_PAQUETE = [{
    'codigo': 'PAPELSP6741',
    'cantidad_devuelta': 1,
    'f470_rowid': '2857568',
    'f470_id_unidad_medida': 'PQ',
}]


class TestDobleUnidadDevolucionParcial:
    """`es_total=False` — Devolución de Cliente. La rama que prorratea."""

    def test_devolver_un_paquete_genera_una_sola_linea(self):
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=False,
            items_devueltos=_DEVUELTO_UN_PAQUETE, **_ARGS)
        detalle = '\n'.join(
            f"  rowid={l['f470_rowid_movto']}  cant={l['f470_cant_base']}  "
            f"uom={l['f470_id_unidad_medida']}  neto={l['f470_vlr_neto_prorrateado']}"
            for l in lineas)
        assert len(lineas) == 1, (
            f'devolvió 1 unidad → la NC sale con {len(lineas)} línea(s)\n{detalle}')
        assert lineas[0]['f470_rowid_movto'] == 2857568
        assert lineas[0]['f470_id_unidad_medida'] == 'PQ'

    def test_el_valor_del_cruce_es_el_de_la_linea_devuelta(self):
        """`F353_VLR_CRUCE`. 24250, no 26675: la línea UND no se devolvió."""
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=False,
            items_devueltos=_DEVUELTO_UN_PAQUETE, **_ARGS)
        valor_cruce = sum(
            (l['f470_vlr_neto_prorrateado'] for l in lineas), Decimal(0))
        assert valor_cruce == Decimal('24250'), (
            f'F353_VLR_CRUCE = {valor_cruce} — se cruza de más contra la '
            'factura (la línea UND de la misma referencia entró sola)')

    def test_no_se_devuelve_inventario_que_nadie_devolvio(self):
        """Cada línea de la NC reingresa mercancía a la bodega de Siesa. La
        línea UND fantasma mete una unidad que nunca volvió del cliente."""
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=False,
            items_devueltos=_DEVUELTO_UN_PAQUETE, **_ARGS)
        total = sum(int(l['f470_cant_base']) for l in lineas)
        assert total == 1, f'la NC reingresa {total} unidades por una devuelta'

    def test_devolver_la_unidad_suelta_toma_la_otra_linea(self):
        """La otra mitad de la misma pregunta: con el rowid de la línea UND, lo
        que sale es la UND — 2425, no 24250. Sin esto el test anterior pasaría
        con un código que se quedara siempre con la primera línea."""
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=False,
            items_devueltos=[{'codigo': 'PAPELSP6741', 'cantidad_devuelta': 1,
                              'f470_rowid': '2857569',
                              'f470_id_unidad_medida': 'UND'}],
            **_ARGS)
        assert len(lineas) == 1
        assert lineas[0]['f470_rowid_movto'] == 2857569
        assert lineas[0]['f470_id_unidad_medida'] == 'UND'
        assert lineas[0]['f470_vlr_neto_prorrateado'] == Decimal('2425')

    def test_devolver_las_dos_lineas_las_incluye_a_las_dos(self):
        """Devolver el paquete Y la unidad suelta sí son dos líneas y sí cruza
        26675. El arreglo no puede colapsar la referencia repetida a una sola
        línea: cada rowid es una línea real de la factura."""
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=False,
            items_devueltos=[
                {'codigo': 'PAPELSP6741', 'cantidad_devuelta': 1,
                 'f470_rowid': '2857568', 'f470_id_unidad_medida': 'PQ'},
                {'codigo': 'PAPELSP6741', 'cantidad_devuelta': 1,
                 'f470_rowid': '2857569', 'f470_id_unidad_medida': 'UND'},
            ],
            **_ARGS)
        valor_cruce = sum(
            (l['f470_vlr_neto_prorrateado'] for l in lineas), Decimal(0))
        assert len(lineas) == 2
        assert valor_cruce == Decimal('26675')

    def test_el_prorrateo_sigue_en_decimal(self):
        """**Sin `xfail`: esto ya está bien hoy y tiene que seguir.**
        No romper lo que ya está bien: la rama parcial prorratea con
        `Decimal` a propósito, para no arrastrar error de redondeo al sumar
        varias líneas. Un arreglo de la clave que pase por `float` cambia un
        defecto por otro."""
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=False,
            items_devueltos=_DEVUELTO_UN_PAQUETE, **_ARGS)
        assert all(isinstance(l['f470_vlr_neto_prorrateado'], Decimal)
                   for l in lineas)


class TestItemSinRowid:
    """Regla 0 — el dato ausente. Un payload encolado ANTES de este arreglo no
    lleva `f470_rowid`. Qué se hace con él:

    · referencia con **una sola** línea en la factura → se usa esa línea. No es
      adivinar: la correspondencia es única. Es el caso de todas las
      devoluciones históricas (29 de 29 líneas en producción ya traen rowid, y
      ningún job de NC quedó pendiente con payload viejo — verificado
      2026-08-20).
    · referencia con **dos o más** líneas —doble unidad, el único caso donde la
      ambigüedad existe de verdad— → se levanta `LineaDevueltaAmbigua` y el job
      falla nombrando la devolución. Fallar es reversible; una NC que cruza de
      más es un documento fiscal que alguien reversa a mano en el ERP.
    """

    def test_doble_unidad_sin_rowid_no_adivina_falla(self):
        with pytest.raises(LineaDevueltaAmbigua) as exc:
            _construir_lineas_nc(
                FACTURA_DOBLE_UNIDAD, es_total=False,
                items_devueltos=[{'codigo': 'PAPELSP6741', 'cantidad_devuelta': 1}],
                **_ARGS)
        mensaje = str(exc.value)
        assert 'PAPELSP6741' in mensaje, 'el error tiene que nombrar la línea'
        assert '2857568' in mensaje and '2857569' in mensaje, (
            'el error tiene que decir entre qué rowids está la ambigüedad')

    def test_unidad_unica_sin_rowid_sigue_funcionando(self):
        """El payload viejo de un producto normal no se rompe: no hay a qué
        adivinarle. Un arreglo que hiciera fallar TODO payload sin rowid
        mandaría a FALLIDO cada devolución en vuelo el día del despliegue."""
        lineas = _construir_lineas_nc(
            TestUnidadUnica.FACTURA, es_total=False,
            items_devueltos=[{'codigo': 'PAPELSP9218', 'cantidad_devuelta': 2}],
            **_ARGS)
        assert len(lineas) == 1
        assert lineas[0]['f470_rowid_movto'] == 2857568
        assert lineas[0]['f470_vlr_neto_prorrateado'] == Decimal('29100')

    def test_dos_items_de_la_misma_referencia_sin_rowid_tambien_es_ambiguo(self):
        """Aunque la factura traiga una sola línea: dos declaraciones de la
        misma referencia sin rowid no se pueden repartir. El dict de antes se
        quedaba con la última, en silencio."""
        with pytest.raises(LineaDevueltaAmbigua):
            _construir_lineas_nc(
                TestUnidadUnica.FACTURA, es_total=False,
                items_devueltos=[
                    {'codigo': 'PAPELSP9218', 'cantidad_devuelta': 2},
                    {'codigo': 'PAPELSP9218', 'cantidad_devuelta': 3},
                ],
                **_ARGS)

    def test_el_rowid_matchea_aunque_venga_como_int(self):
        """`LineaDevolucionCliente.f470_rowid` es `String` y Siesa devuelve el
        rowid como número. Los dos lados se normalizan antes de comparar — si
        no, el match no ocurriría nunca y la NC saldría vacía."""
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=False,
            items_devueltos=[{'codigo': 'PAPELSP6741', 'cantidad_devuelta': 1,
                              'f470_rowid': 2857569}],
            **_ARGS)
        assert [l['f470_rowid_movto'] for l in lineas] == [2857569]


class TestDobleUnidadNoRompeLaDevolucionTotal:
    """`es_total=True` — Liquidación de ruta. No prorratea, y **no lleva
    clave**: toma todas las líneas de la factura completas.

    Por eso la doble unidad no la afecta: devolver la factura entera SÍ incluye
    las dos líneas. Queda escrito como test para que un arreglo de la rama
    parcial no «unifique» las dos ramas y borre este comportamiento correcto.
    """

    def test_la_nc_total_incluye_las_dos_lineas_completas(self):
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=True, items_devueltos=[], **_ARGS)
        assert len(lineas) == 2
        assert {l['f470_id_unidad_medida'] for l in lineas} == {'PQ', 'UND'}

    def test_el_cruce_total_es_el_saldo_entero(self):
        lineas = _construir_lineas_nc(
            FACTURA_DOBLE_UNIDAD, es_total=True, items_devueltos=[], **_ARGS)
        valor_cruce = sum(
            (l['f470_vlr_neto_prorrateado'] for l in lineas), Decimal(0))
        assert valor_cruce == Decimal('26675.0')


class TestUnidadUnica:
    """**El detector no dispara sobre operación sana.** Un producto de unidad
    única cruza exactamente lo mismo antes y después del arreglo de la clave:
    mismas líneas, mismo `F353_VLR_CRUCE`, mismo prorrateo. Estos tests estaban
    en verde antes del arreglo y siguen igual — ninguno se tocó."""

    FACTURA = [
        {
            'f470_rowid': 2857568, 'f120_referencia': 'PAPELSP9218',
            'f470_cant_base': 5, 'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
            'f470_vlr_bruto': 61134.0, 'f470_vlr_imp': 11616.0, 'f470_vlr_neto': 72750.0,
        },
        {
            'f470_rowid': 2857569, 'f120_referencia': 'PAPELSP9830',
            'f470_cant_base': 4, 'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
            'f470_vlr_bruto': 336.0, 'f470_vlr_imp': 64.0, 'f470_vlr_neto': 400.0,
        },
    ]

    def test_devolucion_parcial_de_una_referencia(self):
        """FEW-00001466, el caso verificado en vivo el 2026-07-31. Devolver 2
        de las 5 unidades facturadas → 72750 × 2/5 = 29100."""
        lineas = _construir_lineas_nc(
            self.FACTURA, es_total=False,
            items_devueltos=[{'codigo': 'PAPELSP9218', 'cantidad_devuelta': 2}],
            **_ARGS)
        assert len(lineas) == 1
        assert lineas[0]['f470_rowid_movto'] == 2857568
        assert lineas[0]['f470_cant_base'] == 2
        assert lineas[0]['f470_vlr_neto_prorrateado'] == Decimal('29100')

    def test_devolucion_parcial_de_las_dos_referencias(self):
        lineas = _construir_lineas_nc(
            self.FACTURA, es_total=False,
            items_devueltos=[
                {'codigo': 'PAPELSP9218', 'cantidad_devuelta': 5},
                {'codigo': 'PAPELSP9830', 'cantidad_devuelta': 4},
            ],
            **_ARGS)
        valor_cruce = sum(
            (l['f470_vlr_neto_prorrateado'] for l in lineas), Decimal(0))
        assert len(lineas) == 2
        assert valor_cruce == Decimal('73150')

    def test_una_referencia_que_no_se_devolvio_no_entra(self):
        lineas = _construir_lineas_nc(
            self.FACTURA, es_total=False,
            items_devueltos=[{'codigo': 'PAPELSP9830', 'cantidad_devuelta': 1}],
            **_ARGS)
        assert [l['f120_referencia'] for l in lineas] == ['PAPELSP9830']

    def test_devolucion_total(self):
        lineas = _construir_lineas_nc(
            self.FACTURA, es_total=True, items_devueltos=[], **_ARGS)
        valor_cruce = sum(
            (l['f470_vlr_neto_prorrateado'] for l in lineas), Decimal(0))
        assert len(lineas) == 2
        assert valor_cruce == Decimal('73150.0')

    def test_la_bodega_de_averias_sigue_aplicando_por_linea(self):
        lineas = _construir_lineas_nc(
            self.FACTURA, es_total=False,
            items_devueltos=[
                {'codigo': 'PAPELSP9218', 'cantidad_devuelta': 1, 'es_averiado': True},
                {'codigo': 'PAPELSP9830', 'cantidad_devuelta': 1},
            ],
            bodega_averias='AV1', **_ARGS)
        por_ref = {l['f120_referencia']: l['f470_id_bodega'] for l in lineas}
        assert por_ref == {'PAPELSP9218': 'AV1', 'PAPELSP9830': 'NB1'}


class TestElRowidLlegaAlPayload:
    """**La otra mitad del arreglo, y la que se puede quedar sin hacer.**

    `_construir_lineas_nc` puede estar perfecta y no servir de nada si quien
    encola el job no manda el `f470_rowid`: el payload llegaría sin clave y todo
    producto de doble unidad caería en `LineaDevueltaAmbigua`. Los tests de
    función pura no ven ese salto — le pasan el item a mano.

    Este recorre el camino real: `crear_devolucion` →
    `confirmar_entrada_fisica` → payload del `SiesaJob` → `_construir_lineas_nc`
    con **ese** payload, sobre una factura de doble unidad.
    """

    @staticmethod
    def _factura(ref):
        """La misma referencia en dos líneas: 1 PQ ($24.250) y 1 UND ($2.425)."""
        return [
            {'f470_rowid': '1001', 'f120_referencia': ref, 'f470_cant_base': 1,
             'f470_id_unidad_medida': 'PQ', 'f150_id': 'NB1', 'f470_vlr_neto': 24250.0},
            {'f470_rowid': '1002', 'f120_referencia': ref, 'f470_cant_base': 1,
             'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1', 'f470_vlr_neto': 2425.0},
        ]

    @patch('app.services.siesa_job_service.disparar_dlq_inmediato')
    @patch('app.services.devolucion_cliente_service.connekta')
    def test_devolver_el_paquete_encola_el_rowid_del_paquete(
            self, mock_connekta, _mock_dlq, app, db, almacen, producto, ub_reserva):
        from app.models.packing import TareaPacking
        from app.models.siesa_job import SiesaJob
        from app.services.devolucion_cliente_service import DevolucionClienteService

        tarea = TareaPacking(
            codigo='PK-DOBLE-UOM', tipo_documento='PEDIDO', estado='DESPACHADO',
            almacen_id=almacen.id, numero_pedido_siesa='PD-DOBLE',
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='500',
            siesa_triggered=True,
        )
        db.session.add(tarea)
        db.session.commit()

        factura = self._factura(producto.codigo_siesa)
        mock_connekta.get_rowids_factura.return_value = factura

        # La recepcionista contó UN PAQUETE — la línea PQ, rowid 1001.
        devolucion = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=tarea.id, tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=almacen.id, recepcionista_id=None,
            lineas=[{
                'producto_id': producto.id, 'codigo_siesa': producto.codigo_siesa,
                'cantidad_facturada': 1, 'cantidad_devuelta': 1, 'es_averiado': False,
                'f470_id_unidad_medida': 'PQ', 'f150_id_bodega': 'NB1',
                'f470_rowid': '1001',
            }],
        )
        DevolucionClienteService.confirmar_entrada_fisica(devolucion.id, recepcionista_id=1)

        job = SiesaJob.query.filter_by(
            referencia_tipo='DevolucionCliente', referencia_id=devolucion.id).first()
        items = job.get_payload()['items_devueltos']
        assert items[0].get('f470_rowid') == '1001', (
            'el payload del job no lleva f470_rowid — la clave del cruce se '
            'pierde en el último salto y toda doble unidad queda ambigua')
        assert items[0].get('f470_id_unidad_medida') == 'PQ'

        # Y con ESE payload, la NC sale con la línea del paquete y nada más.
        lineas = _construir_lineas_nc(
            factura, es_total=False, items_devueltos=items, **_ARGS)
        assert len(lineas) == 1
        assert lineas[0]['f470_rowid_movto'] == '1001'
        assert sum((l['f470_vlr_neto_prorrateado'] for l in lineas),
                   Decimal(0)) == Decimal('24250')
