"""
Los dos filtros de averías estaban verdes probando al escritor que SÍ marcaba.

Un lote anterior cerró dos huecos: el FEFO dejó de asignar `tipo_zona='AVERIAS'`
(`picking_service.calcular_fefo`) y la alerta de mínimos dejó de contarla como
cobertura (`dashboard_service.consulta_productos_bajo_minimo`). Los dos se
probaron construyendo la ubicación **a mano** con `tipo_zona='AVERIAS'` — que es
como la crea `layout_service.crear_ubicacion_averias` y como la clasifica
`ubicaciones_sync_service`.

El otro escritor no la marca así. `devolucion_service.confirmar_ubicacion(...,
es_averiado=True)` crea el bin `AVERIADOS` con `zona='CUARENTENA'` y
`tipo='cuarentena'` y **sin `tipo_zona`**, así que el default del modelo
(`app/models/ubicacion.py:37`, `'GENERAL'`) lo deja como zona vendible. Las dos
consultas nuevas miran `tipo_zona` y solo `tipo_zona`: la mercancía devuelta
como averiada sale al FEFO y tapa el mínimo del tablero, con los dos arreglos
en verde.

Por eso estos tests **no construyen la ubicación**: llaman al escritor real y
verifican sobre lo que el escritor deja en la base. Un test que arma el bin a
mano prueba el caso que ya funcionaba.

Detector en las dos direcciones — sin la mitad de abajo esto solo prueba que
dispara: el FEFO entre ubicaciones legítimas conserva las asignaciones exactas
de antes, un producto con stock sano sigue fuera de la alerta, y el bin de
cuarentena que marca el OTRO escritor se sigue excluyendo.
"""
from datetime import datetime, timedelta

import pytest

from app.services import devolucion_service
from app.services.dashboard_service import DashboardService
from app.services.picking_service import PickingService


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — el bin de averías se crea SIEMPRE llamando al escritor real
# ─────────────────────────────────────────────────────────────────────────────

def _bin_averiados_del_escritor(db, almacen, producto, cantidad,
                                recepcionista_id=None, dias_quieto=0):
    """
    Deja `cantidad` unidades averiadas en el almacén **usando el camino de
    producción**: una TareaDevolucion confirmada con `es_averiado=True`.

    Devuelve la `Ubicacion` que el servicio creó. Nadie le pasa `tipo_zona`:
    ese es justo el punto del test.

    `dias_quieto` envejece la `fecha_ingreso` que el servicio acaba de escribir.
    No fabrica nada: es lo que pasa solo con el tiempo, y es la razón por la que
    la avería **gana** el desempate FEFO — ese stock no se mueve, así que su
    fecha de ingreso termina siendo la más vieja del producto.
    """
    from app.models.devolucion import TareaDevolucion
    from app.models.inventario import UbicacionProducto
    from app.models.ubicacion import Ubicacion

    tarea = TareaDevolucion(
        codigo=f'DEV-{producto.id}-{datetime.utcnow().strftime("%Y%m%d%H%M%S%f")}',
        producto_id=producto.id,
        almacen_id=almacen.id,
        cantidad_diferencia=cantidad,
        estado='PENDIENTE',
        idempotency_key=f'DEV-{producto.id}-{cantidad}-{datetime.utcnow().timestamp()}',
    )
    db.session.add(tarea)
    db.session.commit()

    devolucion_service.confirmar_ubicacion(
        tarea_id=tarea.id,
        ubicacion_codigo=None,
        recepcionista_id=recepcionista_id,
        es_averiado=True,
    )
    ub = Ubicacion.query.filter_by(
        codigo='AVERIADOS', almacen_id=almacen.id
    ).one()
    if dias_quieto:
        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=ub.id, producto_id=producto.id
        ).one()
        reg.fecha_ingreso = datetime.utcnow() - timedelta(days=dias_quieto)
        db.session.commit()
    return ub


def _stock(db, ubicacion, producto, cantidad, dias_atras=0):
    from app.models.inventario import UbicacionProducto
    reg = UbicacionProducto(
        ubicacion_id=ubicacion.id, producto_id=producto.id,
        cantidad=cantidad, reservado=0, bloqueado=0,
        fecha_ingreso=datetime.utcnow() - timedelta(days=dias_atras),
    )
    db.session.add(reg)
    db.session.commit()
    return reg


def _producto(db, codigo, nombre, stock_minimo=None):
    from app.models.producto import Producto
    p = Producto(codigo=codigo, nombre=nombre, codigo_siesa=codigo,
                 stock_minimo=stock_minimo, activo=True)
    db.session.add(p)
    db.session.commit()
    return p


# ─────────────────────────────────────────────────────────────────────────────
# 1. El FEFO no puede asignar lo que dejó el escritor de devoluciones
# ─────────────────────────────────────────────────────────────────────────────

class TestFefoNoAsignaElBinQueCreaDevoluciones:

    def test_el_bin_averiados_le_gana_el_desempate_a_una_zona_vendible(
        self, app, db, almacen, producto, ub_general
    ):
        """
        La avería devuelta y el bucket SIESA-GENERAL quedan en la MISMA
        prioridad de zona mientras el bin siga marcado `tipo_zona='GENERAL'`.
        Ahí desempata la fecha de ingreso — y la avería, que lleva meses
        quieta, gana. El FEFO pide 5 y se los saca a la mercancía dañada
        teniendo 50 unidades buenas al lado.
        """
        ub_averiados = _bin_averiados_del_escritor(
            db, almacen, producto, 40, dias_quieto=200
        )
        _stock(db, ub_general, producto, 50, dias_atras=1)

        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)

        codigos = [a['ubicacion_codigo'] for a in fefo['asignaciones']]
        assert 'AVERIADOS' not in codigos, (
            'El FEFO asignó a un pedido de cliente la mercancía que el '
            'recepcionista marcó como averiada'
        )
        assert [(a['ubicacion_id'], a['cantidad']) for a in fefo['asignaciones']] == [
            (ub_general.id, 5)
        ]

    def test_el_bin_averiados_tampoco_completa_un_faltante(
        self, app, db, almacen, producto, ub_picking
    ):
        """
        Regla 0: el faltante se declara. Con 5 vendibles y 40 averiadas, pedir
        40 tiene que quedar corto — que salga corto lo corrige alguien mañana;
        una caja rota entregada, no.
        """
        _bin_averiados_del_escritor(db, almacen, producto, 40)
        _stock(db, ub_picking, producto, 5, dias_atras=1)

        fefo = PickingService.calcular_fefo(producto.id, 40, almacen.id)

        assert [a['ubicacion_codigo'] for a in fefo['asignaciones']] == ['PIK-01-A']
        assert fefo['completo'] is False
        assert fefo['cantidad_disponible'] == 5
        assert fefo['cantidad_faltante'] == 35

    def test_sin_stock_vendible_el_fefo_no_devuelve_nada(
        self, app, db, almacen, producto
    ):
        """La avería sola no es stock: no hay de dónde sacar, y hay que decirlo."""
        _bin_averiados_del_escritor(db, almacen, producto, 40)

        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)

        assert fefo['asignaciones'] == []
        assert fefo['cantidad_faltante'] == 5

    def test_crear_tareas_no_genera_picking_contra_el_bin_averiados(
        self, app, db, almacen, producto, ub_picking
    ):
        """
        El defecto no termina en una lista: termina en una TareaPicking que un
        operario ejecuta y una caja rota que sale al cliente.

        Se piden 40 con 5 vendibles y 40 averiadas. Hoy `crear_tareas` completa
        el pedido con la avería y devuelve dos tareas; lo correcto es que
        levante «stock insuficiente» y que nadie camine hacia AVERIADOS.
        """
        from app.models.picking import TareaPicking

        ub_averiados = _bin_averiados_del_escritor(db, almacen, producto, 40)
        _stock(db, ub_picking, producto, 5, dias_atras=1)

        with pytest.raises(ValueError, match='Stock insuficiente'):
            PickingService.crear_tareas(
                producto_id=producto.id, cantidad=40, almacen_id=almacen.id,
                referencia_documento='PED-DEV-001', tipo_documento='PEDIDO',
            )

        assert TareaPicking.query.filter_by(ubicacion_id=ub_averiados.id).all() == []


# ─────────────────────────────────────────────────────────────────────────────
# 2. El mínimo del tablero no puede quedar tapado por la avería devuelta
# ─────────────────────────────────────────────────────────────────────────────

class TestAlertaDeMinimosIgnoraElBinQueCreaDevoluciones:

    def test_dos_vendibles_y_cuarenta_devueltas_averiadas_alertan(
        self, app, db, almacen, ub_picking
    ):
        """
        2 vendibles + 40 averiadas de devolución, mínimo 10 → bajo mínimo.
        El stock reportado tiene que ser el vendible (2), no 42.
        """
        prod = _producto(db, 'DEV-AVERIADO', 'Resma mojada', stock_minimo=10)
        _stock(db, ub_picking, prod, 2)
        _bin_averiados_del_escritor(db, almacen, prod, 40)

        resultado = DashboardService.alertas_stock(almacen.id)
        por_codigo = {a['codigo']: a for a in resultado['alertas']}

        assert 'DEV-AVERIADO' in por_codigo, (
            '40 unidades devueltas como averiadas taparon el mínimo sobre 2 '
            'unidades vendibles — la reposición nunca se dispara'
        )
        assert por_codigo['DEV-AVERIADO']['stock_actual'] == 2

    def test_el_kpi_del_tablero_cuenta_lo_mismo_que_la_lista(
        self, app, db, almacen, ub_picking
    ):
        prod = _producto(db, 'DEV-AVERIADO-KPI', 'Resma mojada', stock_minimo=10)
        _stock(db, ub_picking, prod, 2)
        _bin_averiados_del_escritor(db, almacen, prod, 40)

        kpis = DashboardService.kpis_operativos(almacen.id)
        alertas = DashboardService.alertas_stock(almacen.id)

        assert kpis['alertas']['productos_bajo_minimo'] == 1
        assert kpis['alertas']['productos_bajo_minimo'] == alertas['total_alertas']


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dirección contraria — lo sano sigue igual (si no, esto prueba la mitad)
# ─────────────────────────────────────────────────────────────────────────────

class TestLoVendibleSigueIntacto:

    def test_el_fefo_entre_dos_ubicaciones_legitimas_no_cambia(
        self, app, db, almacen, producto, ub_picking, ub_general
    ):
        """
        Las asignaciones EXACTAS de antes: ubicación real primero, GENERAL de
        respaldo, con las mismas cantidades. El FEFO es camino caliente — un
        criterio movido de más rompe la operación entera.
        """
        _stock(db, ub_general, producto, 4000, dias_atras=120)
        _stock(db, ub_picking, producto, 10, dias_atras=0)

        fefo = PickingService.calcular_fefo(producto.id, 15, almacen.id)

        assert [(a['ubicacion_id'], a['cantidad']) for a in fefo['asignaciones']] == [
            (ub_picking.id, 10), (ub_general.id, 5)
        ]
        assert fefo['completo'] is True

    def test_entre_dos_zonas_vendibles_sigue_ganando_la_mas_antigua(
        self, app, db, almacen, producto, ub_picking, ub_reserva
    ):
        _stock(db, ub_picking, producto, 20, dias_atras=1)
        _stock(db, ub_reserva, producto, 20, dias_atras=90)

        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)

        assert [(a['ubicacion_id'], a['cantidad']) for a in fefo['asignaciones']] == [
            (ub_reserva.id, 5)
        ]

    def test_el_producto_con_stock_sano_sigue_fuera_de_la_alerta(
        self, app, db, almacen, ub_picking
    ):
        """50 vendibles sobre un mínimo de 10 no son una alerta. Una consulta
        que devuelve el catálogo entero también «encuentra» el agotado."""
        prod = _producto(db, 'DEV-SANO', 'Resma normal', stock_minimo=10)
        _stock(db, ub_picking, prod, 50)

        resultado = DashboardService.alertas_stock(almacen.id)

        assert 'DEV-SANO' not in {a['codigo'] for a in resultado['alertas']}

    def test_la_zona_averias_del_otro_escritor_se_sigue_excluyendo(
        self, app, db, almacen, producto, ub_picking
    ):
        """
        `layout_service.crear_ubicacion_averias` / `ubicaciones_sync_service`
        marcan `tipo_zona='AVERIAS'`. Ese camino ya funcionaba y tiene que
        seguir funcionando: la política única no puede cerrar un hueco abriendo
        el que ya estaba tapado.
        """
        from app.models.ubicacion import Ubicacion
        ave = Ubicacion(codigo='AVE1', almacen_id=almacen.id,
                        tipo_zona='AVERIAS', activo=True)
        db.session.add(ave)
        db.session.commit()

        _stock(db, ave, producto, 100, dias_atras=200)
        _stock(db, ub_picking, producto, 5, dias_atras=1)

        fefo = PickingService.calcular_fefo(producto.id, 5, almacen.id)

        assert [a['ubicacion_id'] for a in fefo['asignaciones']] == [ub_picking.id]

    def test_la_devolucion_no_averiada_sigue_siendo_stock_vendible(
        self, app, db, almacen, producto):
        """
        Dirección cara en el sentido opuesto: marcar de más esconde stock bueno.
        Una devolución normal (`es_averiado=False`) entra a una ubicación de
        estantería y **tiene** que quedar disponible para el FEFO.
        """
        from app.models.devolucion import TareaDevolucion

        tarea = TareaDevolucion(
            codigo='DEV-NORMAL-001', producto_id=producto.id,
            almacen_id=almacen.id, cantidad_diferencia=12,
            estado='PENDIENTE', idempotency_key='DEV-NORMAL-001',
        )
        db.session.add(tarea)
        db.session.commit()

        devolucion_service.confirmar_ubicacion(
            tarea_id=tarea.id, ubicacion_codigo='EST-DEV-01',
            recepcionista_id=None, es_averiado=False,
        )

        fefo = PickingService.calcular_fefo(producto.id, 12, almacen.id)

        assert [a['ubicacion_codigo'] for a in fefo['asignaciones']] == ['EST-DEV-01']
        assert fefo['completo'] is True
