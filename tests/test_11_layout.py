"""
Tests del módulo de Layout — ubicaciones gestionadas 100% en el WMS.

Cubre: letras de pasillo disponibles (A-Z + AA, AB...), creación de filas en
bloque, numeración de AVERIAS, la regla 1 SKU ↔ 1 ubicación PICKING y sus
guardarraíles, y los guardarraíles de reclasificación.
"""
import pytest
from app.services import layout_service as svc
from app.models.ubicacion import Ubicacion


def test_letras_disponibles_empieza_en_A(db, almacen):
    letras = svc.letras_disponibles(almacen.id, minimo_a_mostrar=5)
    assert letras == ['A', 'B', 'C', 'D', 'E']


def test_letras_disponibles_excluye_usadas(db, almacen):
    svc.crear_fila(almacen.id, 'A', 1, 2, 'PICKING')
    letras = svc.letras_disponibles(almacen.id, minimo_a_mostrar=3)
    assert 'A' not in letras
    assert letras == ['B', 'C', 'D']


def test_letras_disponibles_revela_dobles_al_agotar_simples(db, almacen):
    from app.extensions import db as _db
    # Ocupar las 26 letras simples directamente (sin pasar por crear_fila,
    # más rápido para el test — una ubicación basta para "usar" la letra).
    for n in range(1, 27):
        letra = svc._indice_a_letra(n)
        _db.session.add(Ubicacion(
            codigo=f'PIK-{letra}01-01', almacen_id=almacen.id,
            pasillo=letra, tipo_zona='PICKING', origen='MANUAL', activo=True,
        ))
    _db.session.commit()

    letras = svc.letras_disponibles(almacen.id, minimo_a_mostrar=3)
    assert letras == ['AA', 'AB', 'AC']


def test_crear_fila_genera_codigos_correctos(db, almacen):
    creadas = svc.crear_fila(almacen.id, 'a', 3, 4, 'PICKING')
    codigos = sorted(u.codigo for u in creadas)
    assert codigos == ['PIK-A03-01', 'PIK-A03-02', 'PIK-A03-03', 'PIK-A03-04']
    assert all(u.origen == 'MANUAL' for u in creadas)
    assert all(u.tipo_zona == 'PICKING' for u in creadas)


def test_crear_fila_rechaza_zona_invalida(db, almacen):
    with pytest.raises(ValueError, match='tipo_zona'):
        svc.crear_fila(almacen.id, 'A', 1, 2, 'GENERAL')


def test_crear_fila_rechaza_pasillo_invalido(db, almacen):
    with pytest.raises(ValueError, match='pasillo'):
        svc.crear_fila(almacen.id, 'A1', 1, 2, 'PICKING')


def test_crear_fila_rechaza_codigo_duplicado(db, almacen):
    svc.crear_fila(almacen.id, 'A', 3, 2, 'PICKING')
    with pytest.raises(ValueError, match='ya existe'):
        svc.crear_fila(almacen.id, 'A', 3, 2, 'PICKING')


def test_crear_ubicacion_averias_numera_secuencial(db, almacen):
    a1 = svc.crear_ubicacion_averias(almacen.id)
    a2 = svc.crear_ubicacion_averias(almacen.id)
    assert a1.codigo == 'AVE1'
    assert a2.codigo == 'AVE2'
    assert a1.tipo_zona == 'AVERIAS'
    assert a1.origen == 'MANUAL'


def test_asignar_producto_picking_ok(db, almacen, producto):
    ub = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    resultado = svc.asignar_producto(ub.id, producto.id, 50)
    assert resultado['cantidad_total'] == 50

    ub_refrescada = Ubicacion.query.get(ub.id)
    assert ub_refrescada.producto_asignado_id == producto.id


def test_asignar_producto_picking_rechaza_ubicacion_ocupada(db, almacen, producto, producto2):
    ub = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    with pytest.raises(ValueError, match='ya está asignada'):
        svc.asignar_producto(ub.id, producto2.id, 10)


def test_asignar_producto_picking_rechaza_producto_con_otro_slot(db, almacen, producto):
    ub1 = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    ub2 = svc.crear_fila(almacen.id, 'B', 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub1.id, producto.id, 50)

    with pytest.raises(ValueError, match='ya tiene un slot'):
        svc.asignar_producto(ub2.id, producto.id, 10)


def test_asignar_producto_picking_permite_sumar_al_mismo_slot(db, almacen, producto):
    ub = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)
    resultado = svc.asignar_producto(ub.id, producto.id, 20)
    assert resultado['cantidad_total'] == 70


def test_asignar_producto_reserva_sin_restriccion_1a1(db, almacen, producto):
    ub1 = svc.crear_fila(almacen.id, 'A', 1, 1, 'RESERVA')[0]
    ub2 = svc.crear_fila(almacen.id, 'B', 1, 1, 'RESERVA')[0]
    # Mismo producto en dos ubicaciones RESERVA distintas — no debe fallar.
    svc.asignar_producto(ub1.id, producto.id, 100)
    svc.asignar_producto(ub2.id, producto.id, 200)
    assert Ubicacion.query.get(ub1.id).producto_asignado_id is None
    assert Ubicacion.query.get(ub2.id).producto_asignado_id is None


def test_reclasificar_bloquea_con_stock_activo(db, almacen, producto):
    ub = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    with pytest.raises(ValueError, match='unidades activas'):
        svc.reclasificar_ubicacion(ub.id, tipo_zona='RESERVA')


def test_reclasificar_permite_sin_stock(db, almacen):
    ub = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    resultado = svc.reclasificar_ubicacion(ub.id, tipo_zona='RESERVA')
    assert resultado['ubicacion']['tipo_zona'] == 'RESERVA'


def test_reclasificar_liberar_slot_bloquea_con_stock(db, almacen, producto):
    ub = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    with pytest.raises(ValueError, match='no se puede'):
        svc.reclasificar_ubicacion(ub.id, liberar_slot=True)


def test_editar_fila_cambia_zona_de_todas_las_posiciones(db, almacen):
    svc.crear_fila(almacen.id, 'A', 3, 4, 'PICKING')
    resultado = svc.editar_fila(almacen.id, 'A', 3, tipo_zona='RESERVA')

    assert resultado['total_posiciones'] == 4
    assert len(resultado['actualizadas']) == 4
    assert resultado['bloqueadas'] == {}
    for u in Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', estante='3').all():
        assert u.tipo_zona == 'RESERVA'


def test_editar_fila_no_aborta_lote_si_una_posicion_tiene_stock(db, almacen, producto):
    creadas = svc.crear_fila(almacen.id, 'A', 1, 3, 'PICKING')
    svc.asignar_producto(creadas[0].id, producto.id, 50)  # solo la posición 01 tiene stock

    resultado = svc.editar_fila(almacen.id, 'A', 1, tipo_zona='RESERVA')

    assert resultado['total_posiciones'] == 3
    assert len(resultado['actualizadas']) == 2
    assert list(resultado['bloqueadas'].keys()) == ['PIK-A01-01']
    # Las que sí se pudieron cambiar, quedaron en RESERVA; la bloqueada sigue en PICKING
    assert Ubicacion.query.get(creadas[0].id).tipo_zona == 'PICKING'
    assert Ubicacion.query.get(creadas[1].id).tipo_zona == 'RESERVA'
    assert Ubicacion.query.get(creadas[2].id).tipo_zona == 'RESERVA'


def test_editar_fila_solo_cambia_el_campo_indicado(db, almacen):
    svc.crear_fila(almacen.id, 'A', 1, 2, 'PICKING', capacidad_maxima=100)
    svc.editar_fila(almacen.id, 'A', 1, capacidad_maxima=250)

    for u in Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', estante='1').all():
        assert u.capacidad_maxima == 250
        assert u.tipo_zona == 'PICKING'  # no se tocó porque no se pidió


def test_editar_fila_rechaza_fila_inexistente(db, almacen):
    with pytest.raises(ValueError, match='No hay posiciones'):
        svc.editar_fila(almacen.id, 'Z', 9, tipo_zona='RESERVA')


def test_eliminar_fila_borra_posiciones_nunca_usadas(db, almacen):
    svc.crear_fila(almacen.id, 'A', 5, 3, 'RESERVA')
    resultado = svc.eliminar_fila(almacen.id, 'A', 5)

    assert resultado['total_posiciones'] == 3
    assert len(resultado['eliminadas']) == 3
    assert resultado['bloqueadas'] == {}
    assert Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', estante='5').count() == 0


def test_eliminar_fila_bloquea_con_stock_activo(db, almacen, producto):
    creadas = svc.crear_fila(almacen.id, 'A', 1, 2, 'PICKING')
    svc.asignar_producto(creadas[0].id, producto.id, 50)

    resultado = svc.eliminar_fila(almacen.id, 'A', 1)

    assert len(resultado['eliminadas']) == 1  # la posición 02, sin usar
    assert list(resultado['bloqueadas'].keys()) == ['PIK-A01-01']
    assert 'stock activo' in resultado['bloqueadas']['PIK-A01-01']
    # La bloqueada sigue existiendo
    assert Ubicacion.query.get(creadas[0].id) is not None


def test_eliminar_fila_bloquea_por_historial_aunque_stock_sea_cero(db, almacen, producto):
    creadas = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')
    svc.asignar_producto(creadas[0].id, producto.id, 50)
    # Vaciar el stock a 0 pero el MovimientoInventario del asignar_producto queda como historial
    from app.extensions import db as _db
    from app.models.inventario import UbicacionProducto
    up = UbicacionProducto.query.filter_by(ubicacion_id=creadas[0].id).first()
    up.cantidad = 0
    _db.session.commit()

    resultado = svc.eliminar_fila(almacen.id, 'A', 1)

    assert resultado['eliminadas'] == []
    assert 'movimientos de inventario' in resultado['bloqueadas']['PIK-A01-01']


def test_eliminar_fila_bloquea_con_tarea_picking(db, almacen, producto):
    from app.extensions import db as _db
    from app.models.picking import TareaPicking

    ub = svc.crear_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    _db.session.add(TareaPicking(
        codigo='PICK-TEST-1', producto_id=producto.id, cantidad_solicitada=5,
        ubicacion_id=ub.id, almacen_id=almacen.id, estado='COMPLETADO',
    ))
    _db.session.commit()

    resultado = svc.eliminar_fila(almacen.id, 'A', 1)
    assert resultado['eliminadas'] == []
    assert 'Picking' in resultado['bloqueadas']['PIK-A01-01']


def test_eliminar_fila_rechaza_fila_inexistente(db, almacen):
    with pytest.raises(ValueError, match='No hay posiciones'):
        svc.eliminar_fila(almacen.id, 'Z', 9)
