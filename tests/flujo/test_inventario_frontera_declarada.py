"""
Lo que `INV-03` **no** puede ver, y lo que la corrida cuesta leer tres veces.

## Por qué existe este archivo

`tests/flujo/test_flujo_inventario.py` prueba las dos direcciones de cada
invariante: que dispara sobre la violación y que no dispara sobre operación
sana. Falta una tercera pregunta, que es la que quema una herramienta de
auditoría:

> **¿el invariante promete vigilar algo que estructuralmente no puede ver?**

Un falso positivo manda a alguien a investigar lo que no es. Un **falso
negativo con promesa** es peor: nadie va a investigar nada, porque el panel
dice que ese riesgo está cubierto. `INV-03` estaba en ese segundo caso — su
`consecuencia` describía, con las palabras del comentario de
`traslado_service._descontar_inventario_wms`, exactamente el descuento cruzado
de bodega que su `JOIN` con `Ubicacion` deja fuera.

## Y el costo, medido con un contador y no con un cronómetro

El flujo `inventario` leía el mismo universo de `movimientos_inventario` **tres
veces por corrida** (`INV-01`, `INV-02` e `INV-09` llaman a `_movimientos()`) y
los saldos vivos **dos**. La consulta no era el problema —`EXPLAIN ANALYZE` da
173 ms sobre 50.000 filas—: materializar 20.000 objetos ORM tres veces sí.

Se mide contando **sentencias SQL emitidas**, no milisegundos: un umbral de
tiempo en CI es un test que un día falla por la máquina y se termina
desactivando. El número de veces que se lee el universo es una propiedad del
código, no del hardware.
"""
import pytest
from sqlalchemy import event

from app.services import auditoria
from app.services.auditoria import inventario as _inv

# La cadena sana ya está construida con los servicios reales en el archivo
# hermano. Rehacerla acá sería una segunda implementación del mismo arnés — y
# la que divergiera sería la que nadie estuviera mirando (Regla 0).
from tests.flujo.test_flujo_inventario import kardex  # noqa: F401


def _res(codigo):
    r = auditoria.auditar('inventario')
    return next(x for x in r['resultados'] if x['codigo'] == codigo)


def _declarado(codigo):
    """El invariante tal como lo publica el registro — que es lo que el panel
    muestra y lo que alguien lee para decidir si investiga."""
    return next(i for i in auditoria.registrados('inventario')
                if i.codigo == codigo)


@pytest.fixture
def descuento_agregado(db, kardex):  # noqa: F811
    """El descuento por almacén tal como lo escribe el fallback de despacho.

    `traslado_service._descontar_inventario_wms` descuenta de **N bins** y
    escribe **un** movimiento por producto con los saldos agregados del almacén
    y `ubicacion_id=None` — el bin víctima no aparece en ninguna parte de la
    fila.

    El bin víctima de este caso está en **otro almacén y no tiene cadena**: se
    cargó una vez y nadie lo pickeó. Es la forma que hace invisible el defecto a
    los tres invariantes a la vez, y no un caso de borde inventado — un bin de
    tienda que solo recibe traslados es exactamente eso.
    """
    from app.models.almacen import Almacen
    from app.models.inventario import MovimientoInventario, UbicacionProducto
    from app.models.ubicacion import Ubicacion

    otro = Almacen(codigo='ALM-NC9', nombre='Neiva Centro',
                   bodega_siesa_id='NC1', activo=True)
    db.session.add(otro)
    db.session.flush()
    bin_victima = Ubicacion(codigo='PIK-NC9-01', almacen_id=otro.id,
                            tipo_zona='PICKING', stock_minimo=0,
                            stock_maximo=999, secuencia_ruteo=1, activo=True)
    db.session.add(bin_victima)
    db.session.flush()
    reg = UbicacionProducto(ubicacion_id=bin_victima.id,
                            producto_id=kardex.producto.id,
                            cantidad=20, reservado=0, bloqueado=0)
    db.session.add(reg)
    db.session.commit()

    # El descuento cruzado: se vacían 9 unidades del bin de la tienda y la
    # salida se firma contra el almacén del CD, sin nombrar el bin.
    reg.cantidad -= 9
    db.session.add(MovimientoInventario(
        producto_id=kardex.producto.id, ubicacion_id=None,
        almacen_id=kardex.almacen_id, tipo='SALIDA_TRASLADO', cantidad=-9,
        saldo_antes=38, saldo_despues=29,
        motivo='Traslado TR-0009 → PUNTO DE VENTA'))
    db.session.commit()

    class _Caso:
        almacen_victima = otro.id
        almacen_firmante = kardex.almacen_id
        ubicacion_victima = bin_victima.id
    return _Caso


class TestINV03NoPuedeVerElDescuentoAgregado:
    """La mitad que faltaba: lo que el invariante NO alcanza, medido."""

    def test_los_tres_invariantes_quedan_en_cero(self, db, descuento_agregado):
        """`INV-01: 0 · INV-02: 0 · INV-03: 0` — el defecto es invisible.

        No es un test que pida que se arregle: es la medición de un hueco real,
        y lo que hace falta es que el reporte lo declare en vez de taparlo.
        """
        assert _res('INV-01')['total'] == 0
        assert _res('INV-02')['total'] == 0
        assert _res('INV-03')['total'] == 0

    def test_la_ceguera_es_estructural_y_se_ve_en_el_codigo(self):
        """Por AST, no por texto: `INV-03` hace un `JOIN` con `Ubicacion` sobre
        `ubicacion_id`, y un INNER JOIN deja fuera los NULL.

        Se comprueba sobre el árbol del módulo —no sobre su fuente como
        cadena— porque los detectores de texto se atraparon en sus propios
        docstrings siete veces en una semana.
        """
        import ast
        import inspect

        arbol = ast.parse(inspect.getsource(
            _inv.el_movimiento_se_firma_contra_el_almacen_de_su_bin))
        joins = [n for n in ast.walk(arbol) if isinstance(n, ast.Call)
                 and getattr(n.func, 'attr', None) == 'join']
        assert joins, 'INV-03 dejó de unir contra Ubicacion'
        assert all('ubicacion_id' in ast.unparse(n) for n in joins)


class TestElHuecoSeDeclaraEnINV09:
    """Un hueco que nadie cuenta no se distingue de un hueco que no existe."""

    def test_inv09_cuenta_el_movimiento_sin_ubicacion(self, db,
                                                      descuento_agregado):
        d = _res('INV-09')['hallazgos'][0]['datos']
        assert d['movimientos_sin_ubicacion'] == 1

    def test_inv09_nombra_a_los_invariantes_que_quedan_ciegos(
            self, db, descuento_agregado):
        """El conteo solo no alcanza: hay que decir **qué** deja de mirarse.

        `movimientos_sin_ubicacion` ya existía y no significaba nada para quien
        lee el panel. Lo que convierte ese número en información es la lista de
        invariantes que ese movimiento esquiva.
        """
        d = _res('INV-09')['hallazgos'][0]['datos']
        assert 'INV-03' in d['sin_ubicacion_fuera_de']
        assert 'INV-02' in d['sin_ubicacion_fuera_de']

    def test_el_detalle_legible_menciona_el_hueco(self, db,
                                                  descuento_agregado):
        """El hallazgo tiene que poder leerse solo. Quien mira el panel no abre
        `datos`."""
        h = _res('INV-09')['hallazgos'][0]
        assert 'INV-03' in h['detalle']


class TestLaConsecuenciaDeINV03DiceLaVerdad:
    """`consecuencia` es lo único que el panel muestra sin abrir el código.

    Se comprueba sobre el objeto registrado —el mismo que serializa
    `auditar()`— y no sobre el fuente del módulo: lo que puede engañar a un
    humano es el texto publicado, no la línea que lo escribe.
    """

    def test_no_promete_el_descuento_agregado_por_almacen(self):
        """Las dos afirmaciones que venían copiadas del comentario del arreglo
        de `traslado_service`. Describían con precisión un riesgo que este
        invariante no alcanza, y por eso el panel lo daba por vigilado."""
        c = _declarado('INV-03').consecuencia
        assert 'el total de la red no cambia' not in c
        assert 'ningún cuadre por sumas' not in c

    def test_declara_que_solo_ve_movimientos_que_nombran_un_bin(self):
        c = _declarado('INV-03').consecuencia
        assert 'bin' in c

    def test_manda_a_inv09_por_lo_que_no_cubre(self):
        """Un límite declarado sin decir dónde se cuenta lo que queda fuera
        sigue siendo un silencio."""
        assert 'INV-09' in _declarado('INV-03').consecuencia


# ── El costo de la corrida ───────────────────────────────────────────────

class _Espia:
    """Cuenta las sentencias SQL de una corrida, por tabla.

    Cuenta **ejecuciones**, no tiempo: el tiempo depende de la máquina y un
    umbral así termina desactivado; el número de lecturas del universo es una
    propiedad del código.
    """

    def __init__(self):
        self.sentencias = []

    def __call__(self, _conn, _cur, statement, _params, _ctx, _many):
        self.sentencias.append(' '.join(statement.split()).lower())

    @property
    def lecturas_del_universo(self):
        """Las que traen las filas de `movimientos_inventario`.

        Se excluyen el `count()` de los movimientos sin fecha y la consulta de
        `INV-03`, que une contra `ubicaciones` — las dos leen la misma tabla y
        ninguna materializa el universo.
        """
        return [s for s in self.sentencias
                if 'from movimientos_inventario' in s
                and 'count(' not in s
                and 'ubicaciones' not in s]

    @property
    def lecturas_de_saldos(self):
        return [s for s in self.sentencias
                if 'from ubicaciones_productos' in s]


@pytest.fixture
def espia(db):
    e = _Espia()
    event.listen(db.engine, 'before_cursor_execute', e)
    yield e
    event.remove(db.engine, 'before_cursor_execute', e)


class TestElUniversoSeLeeUnaVezPorCorrida:

    def test_los_movimientos_se_leen_una_sola_vez(self, db, kardex, espia):  # noqa: F811
        """Tres invariantes piden el mismo universo. Se lee una vez."""
        auditoria.auditar('inventario')
        assert len(espia.lecturas_del_universo) == 1, \
            espia.lecturas_del_universo

    def test_los_saldos_vivos_se_leen_una_sola_vez(self, db, kardex, espia):  # noqa: F811
        auditoria.auditar('inventario')
        assert len(espia.lecturas_de_saldos) == 1, espia.lecturas_de_saldos


class TestElCacheNoSobreviveAUnCambio:
    """**Un auditor que mira datos viejos es peor que uno lento.**

    La memoización tiene que morir con la transacción que la llenó. Si
    sobreviviera a una escritura, el panel devolvería el reporte de antes del
    defecto justo cuando el defecto acaba de entrar.
    """

    def test_una_corrida_posterior_ve_el_cambio(self, db, kardex):  # noqa: F811
        assert _res('INV-01')['total'] == 0

        kardex.registro.cantidad -= 4
        db.session.commit()

        assert _res('INV-01')['total'] == 1
        assert _res('INV-01')['hallazgos'][0]['datos']['diferencia'] == -4

    def test_un_movimiento_nuevo_entra_en_la_ventana(self, db, kardex):  # noqa: F811
        from app.models.inventario import MovimientoInventario

        antes = _res('INV-09')['hallazgos'][0]['datos']['movimientos_leidos']
        db.session.add(MovimientoInventario(
            producto_id=kardex.producto.id, ubicacion_id=None,
            almacen_id=kardex.almacen_id, tipo='SALIDA_TRASLADO', cantidad=-1,
            saldo_antes=38, saldo_despues=37, motivo='agregado por almacén'))
        db.session.commit()

        d = _res('INV-09')['hallazgos'][0]['datos']
        assert d['movimientos_leidos'] == antes + 1
        assert d['movimientos_sin_ubicacion'] == 1

    def test_un_cambio_sin_commit_tampoco_se_esconde(self, db, kardex):  # noqa: F811
        """El caso que la identidad de la transacción no cubre sola: un `flush`
        cambia los datos **dentro** de la misma transacción.

        Pasa en los tests y en cualquier corrida que audite a mitad de una
        operación. El caché se olvida en cada `flush`, no solo en cada commit.
        """
        assert _res('INV-01')['total'] == 0

        kardex.registro.cantidad -= 3
        db.session.flush()

        assert _res('INV-01')['total'] == 1
