"""
`alertas_stock` serializaba la lista entera, y con el `outerjoin` nuevo la lista
creció.

Medido: si alguien carga mínimos en masa, la respuesta pasa de 1.000 a 28.000
filas y de 277 ms a 6,9 s. Un tablero que tarda siete segundos deja de mirarse,
y el disparador de reposición se apaga por la vía más tonta.

El tope solo no alcanza: un conteo sin su base no es una medición (CLAUDE.md).
Recortar a 500 y devolver `total_alertas = 500` haría que el jefe de almacén
crea que hay quinientos productos por reponer cuando hay veintiocho mil. Por eso
`total_alertas` sigue siendo el conteo REAL —el mismo que el KPI del tablero— y
la respuesta declara aparte cuántas trajo, con qué tope y si mordió.

Y el recorte no puede ser por el medio: lo que se cae tiene que ser lo menos
urgente. El orden lo pone la consulta (SQL), no un `sort` posterior — un sort
después del `LIMIT` ordena el pedazo que sobrevivió, no el universo.

`routes/dashboard.py` no se toca: serializa este dict tal cual, así que la
declaración viaja adentro de la estructura que ya devolvíamos.
"""
import pytest

from app.services.dashboard_service import (
    DashboardService, LIMITE_ALERTAS_STOCK,
)


def _cargar_productos_bajo_minimo(db, almacen, ub, criticos: int, bajos: int):
    """
    `criticos` productos sin ninguna fila de inventario (agotado total) y
    `bajos` con 1 unidad sobre un mínimo de 10. Inserción en bloque: el punto
    del test es el volumen.
    """
    from app.models.inventario import UbicacionProducto
    from app.models.producto import Producto

    productos = []
    for i in range(criticos):
        productos.append(Producto(codigo=f'TOPE-CRIT-{i:04d}', nombre=f'Agotado {i}',
                                  codigo_siesa=f'TOPE-CRIT-{i:04d}',
                                  stock_minimo=10, clasificacion_abc='A', activo=True))
    for i in range(bajos):
        productos.append(Producto(codigo=f'TOPE-BAJO-{i:04d}', nombre=f'Escaso {i}',
                                  codigo_siesa=f'TOPE-BAJO-{i:04d}',
                                  stock_minimo=10, clasificacion_abc='C', activo=True))
    db.session.add_all(productos)
    db.session.commit()

    db.session.add_all([
        UbicacionProducto(ubicacion_id=ub.id, producto_id=p.id,
                          cantidad=1, reservado=0, bloqueado=0)
        for p in productos if p.codigo.startswith('TOPE-BAJO')
    ])
    db.session.commit()
    return productos


class TestAlertasStockDeclaraSuBase:

    def test_la_respuesta_se_corta_pero_el_total_es_el_real(self, app, db, almacen, ub_picking):
        criticos, bajos = 5, LIMITE_ALERTAS_STOCK + 20
        _cargar_productos_bajo_minimo(db, almacen, ub_picking, criticos, bajos)

        resultado = DashboardService.alertas_stock(almacen.id)

        assert len(resultado['alertas']) == LIMITE_ALERTAS_STOCK
        assert resultado['alertas_mostradas'] == LIMITE_ALERTAS_STOCK
        assert resultado['limite'] == LIMITE_ALERTAS_STOCK
        assert resultado['truncado'] is True
        assert resultado['total_alertas'] == criticos + bajos, (
            'El total tiene que ser la base real, no lo que sobrevivió al tope: '
            'un conteo sin su base no es una medición'
        )

    def test_el_kpi_del_tablero_sigue_coincidiendo_con_el_total(self, app, db, almacen, ub_picking):
        """
        `productos_bajo_minimo` y `total_alertas` salen de la misma consulta y
        tienen que dar el mismo número. Si el tope contaminara el total, el
        tablero y la lista empezarían a mentir cada uno por su lado.
        """
        _cargar_productos_bajo_minimo(db, almacen, ub_picking, 5, LIMITE_ALERTAS_STOCK + 20)

        kpis = DashboardService.kpis_operativos(almacen.id)
        alertas = DashboardService.alertas_stock(almacen.id)

        assert kpis['alertas']['productos_bajo_minimo'] == alertas['total_alertas']
        assert kpis['alertas']['productos_bajo_minimo'] > alertas['alertas_mostradas']

    def test_lo_que_se_cae_es_lo_menos_urgente(self, app, db, almacen, ub_picking):
        """
        Los CRÍTICOS (agotado total) no se pueden perder en el recorte. Con el
        orden en Python después del `LIMIT`, se perderían: el `LIMIT` habría
        elegido 500 filas cualesquiera y recién ahí se ordenaban.
        """
        criticos = 5
        _cargar_productos_bajo_minimo(db, almacen, ub_picking, criticos,
                                      LIMITE_ALERTAS_STOCK + 20)

        resultado = DashboardService.alertas_stock(almacen.id)
        codigos = {a['codigo'] for a in resultado['alertas']}

        for i in range(criticos):
            assert f'TOPE-CRIT-{i:04d}' in codigos, (
                'El tope se llevó un agotado total — justo lo que hay que '
                'reponer primero'
            )
        assert [a['urgencia'] for a in resultado['alertas'][:criticos]] == ['CRITICO'] * criticos

    def test_sin_volumen_no_declara_truncado_y_muestra_todo(self, app, db, almacen, ub_picking):
        """
        Dirección contraria: la operación normal no cambia. Tres alertas siguen
        siendo tres alertas, `truncado` en False y la lista completa — si no,
        se habría cambiado un problema de volumen por uno de datos faltantes.
        """
        _cargar_productos_bajo_minimo(db, almacen, ub_picking, 1, 2)

        resultado = DashboardService.alertas_stock(almacen.id)

        assert resultado['total_alertas'] == 3
        assert resultado['alertas_mostradas'] == 3
        assert len(resultado['alertas']) == 3
        assert resultado['truncado'] is False

    def test_el_orden_dentro_de_la_pagina_es_estable_entre_llamadas(self, app, db, almacen, ub_picking):
        """
        Sin un desempate determinista, dos llamadas seguidas devuelven páginas
        distintas y el jefe de almacén ve aparecer y desaparecer productos sin
        que nada haya cambiado.
        """
        _cargar_productos_bajo_minimo(db, almacen, ub_picking, 5, LIMITE_ALERTAS_STOCK + 20)

        primera = [a['codigo'] for a in DashboardService.alertas_stock(almacen.id)['alertas']]
        segunda = [a['codigo'] for a in DashboardService.alertas_stock(almacen.id)['alertas']]

        assert primera == segunda


class TestProductividadOperariosNoRevienta:
    """
    `productividad_operarios` usaba `hoy` sin definirla — `NameError` en cada
    llamada. El comentario del lote anterior la declaró «variable muerta» y la
    quitó; el `conteos_hoy_por_op` de más abajo sí la usaba.

    Por qué nadie lo vio: `/api/dashboard/productividad` devolvía 500, y
    `resumen_completo` envuelve cada bloque en un `_safe()` que registra y
    devuelve `None` — el panel de productividad salía vacío, que es
    indistinguible de «no hubo trabajo hoy». Lo que no da error, deja pasar.
    """

    def test_devuelve_la_productividad_en_vez_de_reventar(self, app, db, almacen, usuario):
        resultado = DashboardService.productividad_operarios(almacen.id, dias=7)

        assert resultado['almacen_id'] == almacen.id
        assert resultado['periodo_dias'] == 7
        ids = [o['operario_id'] for o in resultado['operarios']]
        assert usuario.id in ids

    def test_cuenta_el_conteo_de_hoy_por_el_dia_operativo(
        self, app, db, almacen, usuario, ub_picking, producto
    ):
        """
        El cupo diario del operario es una de las cuatro fechas que la Regla 5
        de CLAUDE.md lista como reiniciadas a las 7 p.m. Colombia. Una sesión
        abierta hace un rato tiene que contar como de hoy.
        """
        from datetime import datetime, timedelta
        from app.models.conteo import SesionConteo

        db.session.add(SesionConteo(
            codigo='CNT-HOY-001', almacen_id=almacen.id, operario_id=usuario.id,
            ubicacion_id=ub_picking.id, producto_id=producto.id,
            estado='PENDIENTE', fecha_inicio=datetime.utcnow() - timedelta(minutes=30),
        ))
        db.session.commit()

        resultado = DashboardService.productividad_operarios(almacen.id, dias=7)
        fila = next(o for o in resultado['operarios'] if o['operario_id'] == usuario.id)

        assert fila['conteos_hoy'] == 1
