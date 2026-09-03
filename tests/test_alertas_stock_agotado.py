"""
La alerta de stock prometía «bajo mínimo **o sin stock**» y no veía el agotado.

Dos defectos en la misma consulta, y la consulta estaba escrita dos veces
(`dashboard_service.alertas_stock` y el bloque de `kpis_operativos`):

1. `JOIN` interno contra la suma de `UbicacionProducto`. Un producto sin
   ninguna fila de inventario en el almacén no tiene qué juntar, así que
   desaparecía de la consulta. El agotado total — el caso exacto que el
   docstring promete mostrar — era el único que no salía nunca. Un producto en
   cero no está «sin novedad»: es el que ya no se puede vender.

2. La suma incluía las ubicaciones `tipo_zona='AVERIAS'` (mercancía dañada / en
   revisión, `app/models/ubicacion.py:39`). Cuarenta unidades averiadas tapaban
   un mínimo de diez sobre dos unidades vendibles. Inventario que no se puede
   despachar apagando la alerta de reponerlo.

Alimenta `productos_bajo_minimo` y la lista de `alertas_stock` — el disparador
de reposición/compra que mira el jefe de almacén. Lo que costaba no es un KPI
feo: es no comprar lo agotado porque el tablero dice que está cubierto.

Detector en las dos direcciones: los tres productos rotos deben aparecer, y el
producto con stock sano debe seguir SIN aparecer. Una consulta que devuelve
todo también «encuentra» el agotado y no sirve para nada.
"""
import pytest

from app.services.dashboard_service import DashboardService


def _producto(db, codigo, nombre, stock_minimo=10, activo=True):
    from app.models.producto import Producto
    p = Producto(codigo=codigo, nombre=nombre, codigo_siesa=codigo,
                 stock_minimo=stock_minimo, activo=activo)
    db.session.add(p)
    db.session.commit()
    return p


def _ubicacion(db, almacen, codigo, tipo_zona):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo=codigo, almacen_id=almacen.id,
                  tipo_zona=tipo_zona, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _stock(db, ubicacion, producto, cantidad):
    from app.models.inventario import UbicacionProducto
    db.session.add(UbicacionProducto(
        ubicacion_id=ubicacion.id, producto_id=producto.id,
        cantidad=cantidad, reservado=0, bloqueado=0,
    ))
    db.session.commit()


@pytest.fixture
def escenario_minimo_10(db, almacen):
    """
    Cuatro productos con stock_minimo=10 en el mismo almacén:

      AGOTADO   0 unidades, sin ninguna fila de UbicacionProducto  → debe alertar
      AVERIADO  2 en PICKING + 40 en AVERIAS (vendible = 2)        → debe alertar
      BAJO      3 en PICKING                                       → debe alertar
      SANO      50 en PICKING                                      → NO debe alertar
    """
    pik = _ubicacion(db, almacen, 'PIK-ALERTA-01', 'PICKING')
    ave = _ubicacion(db, almacen, 'AVE1', 'AVERIAS')

    agotado = _producto(db, 'ALR-AGOTADO', 'Resma agotada')

    averiado = _producto(db, 'ALR-AVERIADO', 'Resma mojada')
    _stock(db, pik, averiado, 2)
    _stock(db, ave, averiado, 40)

    bajo = _producto(db, 'ALR-BAJO', 'Resma escasa')
    _stock(db, pik, bajo, 3)

    sano = _producto(db, 'ALR-SANO', 'Resma normal')
    _stock(db, pik, sano, 50)

    return {'agotado': agotado, 'averiado': averiado, 'bajo': bajo, 'sano': sano}


class TestAlertasStockVeElAgotadoYNoCuentaAverias:

    def test_los_tres_bajo_minimo_aparecen_en_alertas_stock(
        self, app, db, almacen, escenario_minimo_10
    ):
        e = escenario_minimo_10
        resultado = DashboardService.alertas_stock(almacen.id)
        por_codigo = {a['codigo']: a for a in resultado['alertas']}

        assert 'ALR-AGOTADO' in por_codigo, (
            'El agotado total no aparece — es justo lo que el docstring promete '
            '(«bajo mínimo o sin stock») y lo que hay que reponer primero'
        )
        assert 'ALR-AVERIADO' in por_codigo, (
            '40 unidades en AVERIAS taparon el mínimo sobre 2 unidades vendibles'
        )
        assert 'ALR-BAJO' in por_codigo

        assert por_codigo['ALR-AGOTADO']['stock_actual'] == 0
        assert por_codigo['ALR-AGOTADO']['urgencia'] == 'CRITICO'
        assert por_codigo['ALR-AVERIADO']['stock_actual'] == 2, (
            'El stock reportado debe ser el vendible (2), no 42'
        )
        assert por_codigo['ALR-BAJO']['stock_actual'] == 3

        _ = e  # el escenario ya está montado por la fixture

    def test_el_producto_con_stock_sano_no_entra_en_la_alerta(
        self, app, db, almacen, escenario_minimo_10
    ):
        """Dirección contraria: 50 unidades vendibles sobre un mínimo de 10 no
        son una alerta. Una consulta que las incluyera devolvería el catálogo
        entero y el jefe de almacén dejaría de mirar el tablero."""
        resultado = DashboardService.alertas_stock(almacen.id)
        codigos = {a['codigo'] for a in resultado['alertas']}
        assert 'ALR-SANO' not in codigos

    def test_kpi_productos_bajo_minimo_cuenta_los_mismos_tres(
        self, app, db, almacen, escenario_minimo_10
    ):
        """
        `kpis_operativos` tenía su propia copia de la consulta. El contador del
        tablero y la lista de alertas tienen que dar el mismo número: si
        divergen, uno de los dos miente y nadie sabe cuál.
        """
        kpis = DashboardService.kpis_operativos(almacen.id)
        alertas = DashboardService.alertas_stock(almacen.id)

        assert kpis['alertas']['productos_bajo_minimo'] == 3
        assert kpis['alertas']['productos_bajo_minimo'] == alertas['total_alertas']

    def test_el_stock_de_otro_almacen_no_cubre_el_minimo_de_este(
        self, app, db, almacen, escenario_minimo_10
    ):
        """
        El agotado deja de serlo solo con stock vendible DE ESTE almacén.
        Al abrir el join hacia afuera, la pregunta «¿cuánto hay acá?» tiene que
        seguir acotada al almacén — si no, se cambia un defecto (no ver el
        agotado) por el contrario (ver agotados que no lo son).
        """
        from app.models.almacen import Almacen
        otro = Almacen(codigo='ALM-OTRO', nombre='Otro almacén',
                       bodega_siesa_id='NC1', activo=True)
        db.session.add(otro)
        db.session.commit()
        ub_otro = _ubicacion(db, otro, 'PIK-OTRO-01', 'PICKING')
        _stock(db, ub_otro, escenario_minimo_10['agotado'], 500)

        resultado = DashboardService.alertas_stock(almacen.id)
        por_codigo = {a['codigo']: a for a in resultado['alertas']}

        assert 'ALR-AGOTADO' in por_codigo
        assert por_codigo['ALR-AGOTADO']['stock_actual'] == 0

    def test_producto_inactivo_no_alerta_aunque_este_agotado(
        self, app, db, almacen, escenario_minimo_10
    ):
        """
        Con el join interno, un producto inactivo y sin filas de inventario
        quedaba fuera por dos razones a la vez. Al abrir el join queda solo el
        filtro `activo == True`, y hay que ejercerlo: un catálogo dado de baja
        no es una lista de compras.
        """
        _producto(db, 'ALR-INACTIVO', 'Descontinuado', activo=False)

        resultado = DashboardService.alertas_stock(almacen.id)
        codigos = {a['codigo'] for a in resultado['alertas']}
        assert 'ALR-INACTIVO' not in codigos
