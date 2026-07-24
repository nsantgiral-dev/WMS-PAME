"""
Tests del módulo de Layout — ubicaciones gestionadas 100% en el WMS.

Cubre: letras de pasillo disponibles (A-Z + AA, AB...), creación de un Cuerpo
completo en bloque (Pasillo -> Fila -> Cuerpo -> Entrepaños -> Huecos, con
zona fija para todo el Cuerpo — PICKING o RESERVA, no mezclada por nivel),
numeración de AVERIAS, la regla 1 SKU ↔ 1 ubicación PICKING y sus
guardarraíles, y los guardarraíles de reclasificación.

editar_fila()/eliminar_fila() son el mecanismo legado sobre el campo 'estante'
(fila plana previa al rediseño de 5 ejes) — ningún mecanismo nuevo lo escribe,
así que sus tests construyen las ubicaciones directo en la BD.
"""
import pytest
from app.services import layout_service as svc
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto


def test_letras_disponibles_empieza_en_A(db, almacen):
    letras = svc.letras_disponibles(almacen.id, minimo_a_mostrar=5)
    assert letras == ['A', 'B', 'C', 'D', 'E']


def test_letras_disponibles_excluye_usadas(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'PICKING')
    letras = svc.letras_disponibles(almacen.id, minimo_a_mostrar=3)
    assert 'A' not in letras
    assert letras == ['B', 'C', 'D']


def test_letras_disponibles_revela_dobles_al_agotar_simples(db, almacen):
    from app.extensions import db as _db
    # Ocupar las 26 letras simples directamente (sin pasar por crear_cuerpo,
    # más rápido para el test — una ubicación basta para "usar" la letra).
    for n in range(1, 27):
        letra = svc._indice_a_letra(n)
        _db.session.add(Ubicacion(
            codigo=f'PIK-{letra}1-01-01-01', almacen_id=almacen.id,
            pasillo=letra, tipo_zona='PICKING', origen='MANUAL', activo=True,
        ))
    _db.session.commit()

    letras = svc.letras_disponibles(almacen.id, minimo_a_mostrar=3)
    assert letras == ['AA', 'AB', 'AC']


def test_crear_cuerpo_genera_codigos_y_aplica_zona_a_todo_el_cuerpo(db, almacen):
    creadas = svc.crear_cuerpo(almacen.id, 'a', 1, 3, 4, 'PICKING')
    codigos = sorted(u.codigo for u in creadas)
    assert codigos == [
        'PIK-A1-C03-E01-H01', 'PIK-A1-C03-E02-H01', 'PIK-A1-C03-E03-H01', 'PIK-A1-C03-E04-H01',
    ]
    assert all(u.origen == 'MANUAL' for u in creadas)
    # Un Cuerpo es 100% de una sola zona — los 4 entrepaños heredan PICKING, ninguno pasa a RESERVA solo.
    assert all(u.tipo_zona == 'PICKING' for u in creadas)


def test_crear_cuerpo_reserva_aplica_zona_a_todo_el_cuerpo(db, almacen):
    creadas = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 3, 'RESERVA')
    codigos = sorted(u.codigo for u in creadas)
    assert codigos == [
        'RES-A1-C01-E01-H01', 'RES-A1-C01-E02-H01', 'RES-A1-C01-E03-H01',
    ]
    assert all(u.tipo_zona == 'RESERVA' for u in creadas)


def test_crear_cuerpo_rechaza_zona_invalida(db, almacen):
    with pytest.raises(ValueError, match='tipo_zona debe ser una de'):
        svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'AVERIAS')


def test_crear_cuerpo_crea_varios_huecos_por_entrepano(db, almacen):
    creadas = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING', huecos_por_nivel=[3])
    codigos = sorted(u.codigo for u in creadas)
    assert codigos == ['PIK-A1-C01-E01-H01', 'PIK-A1-C01-E01-H02', 'PIK-A1-C01-E01-H03']
    assert all(u.nivel == 1 and u.cuerpo == 1 and u.fila == 1 for u in creadas)


def test_crear_cuerpo_huecos_variables_por_nivel(db, almacen):
    creadas = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 3, 'PICKING', huecos_por_nivel=[3, 2, 4])
    huecos_por_nivel = {}
    for u in creadas:
        huecos_por_nivel.setdefault(u.nivel, []).append(u.hueco)

    assert sorted(huecos_por_nivel[1]) == [1, 2, 3]
    assert sorted(huecos_por_nivel[2]) == [1, 2]
    assert sorted(huecos_por_nivel[3]) == [1, 2, 3, 4]


def test_crear_cuerpo_rechaza_huecos_por_nivel_longitud_incorrecta(db, almacen):
    with pytest.raises(ValueError, match='huecos_por_nivel debe traer un valor'):
        svc.crear_cuerpo(almacen.id, 'A', 1, 1, 3, 'PICKING', huecos_por_nivel=[1, 2])


def test_crear_cuerpo_rechaza_huecos_por_nivel_con_cero(db, almacen):
    with pytest.raises(ValueError, match='al menos 1 hueco'):
        svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'PICKING', huecos_por_nivel=[1, 0])


def test_crear_cuerpo_rechaza_fila_invalida(db, almacen):
    with pytest.raises(ValueError, match='fila debe ser 1 o 2'):
        svc.crear_cuerpo(almacen.id, 'A', 3, 1, 2, 'PICKING')


def test_crear_cuerpo_rechaza_pasillo_invalido(db, almacen):
    with pytest.raises(ValueError, match='pasillo'):
        svc.crear_cuerpo(almacen.id, 'A1', 1, 1, 2, 'PICKING')


def test_crear_cuerpo_rechaza_codigo_duplicado(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', 1, 3, 2, 'PICKING')
    with pytest.raises(ValueError, match='ya existe'):
        svc.crear_cuerpo(almacen.id, 'A', 1, 3, 2, 'PICKING')


def test_crear_ubicacion_averias_numera_secuencial(db, almacen):
    a1 = svc.crear_ubicacion_averias(almacen.id)
    a2 = svc.crear_ubicacion_averias(almacen.id)
    assert a1.codigo == 'AVE1'
    assert a2.codigo == 'AVE2'
    assert a1.tipo_zona == 'AVERIAS'
    assert a1.origen == 'MANUAL'


def test_asignar_producto_picking_ok(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    resultado = svc.asignar_producto(ub.id, producto.id, 50)
    assert resultado['cantidad_total'] == 50

    ub_refrescada = Ubicacion.query.get(ub.id)
    assert ub_refrescada.producto_asignado_id == producto.id


def test_asignar_producto_picking_rechaza_ubicacion_ocupada(db, almacen, producto, producto2):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    with pytest.raises(ValueError, match='ya está asignada'):
        svc.asignar_producto(ub.id, producto2.id, 10)


def test_asignar_producto_picking_rechaza_producto_con_otro_slot(db, almacen, producto):
    ub1 = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    ub2 = svc.crear_cuerpo(almacen.id, 'B', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub1.id, producto.id, 50)

    with pytest.raises(ValueError, match='ya tiene un slot'):
        svc.asignar_producto(ub2.id, producto.id, 10)


def test_asignar_producto_picking_permite_sumar_al_mismo_slot(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)
    resultado = svc.asignar_producto(ub.id, producto.id, 20)
    assert resultado['cantidad_total'] == 70


def test_asignar_producto_picking_permite_capacidad_maxima(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50, capacidad_maxima=80)

    assert Ubicacion.query.get(ub.id).capacidad_maxima == 80


def test_asignar_producto_reserva_rechaza_capacidad_maxima(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'RESERVA')[0]
    with pytest.raises(ValueError, match='capacidad_maxima solo aplica a Huecos PICKING'):
        svc.asignar_producto(ub.id, producto.id, 50, capacidad_maxima=80)


def test_asignar_producto_reserva_sin_restriccion_1a1(db, almacen, producto):
    ub1 = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'RESERVA')[0]
    ub2 = svc.crear_cuerpo(almacen.id, 'B', 1, 1, 1, 'RESERVA')[0]
    assert ub1.tipo_zona == 'RESERVA' and ub2.tipo_zona == 'RESERVA'
    # Mismo producto en dos ubicaciones RESERVA distintas — no debe fallar.
    svc.asignar_producto(ub1.id, producto.id, 100)
    svc.asignar_producto(ub2.id, producto.id, 200)
    assert Ubicacion.query.get(ub1.id).producto_asignado_id is None
    assert Ubicacion.query.get(ub2.id).producto_asignado_id is None


# ── Fusión Layout↔Picking (solo NB1/CO003) ──────────────────────────────────
# Al asignar un SKU a un hueco real en el almacén NB1/CO003, se resta esa
# cantidad de SIESA-GENERAL (el bucket sin ubicación física del sync de Siesa)
# para que picking/conteo/traslados —que ya leen UbicacionProducto sin filtrar
# por tipo_zona— empiecen a apuntar al hueco real. Nunca bloquea: las
# cantidades por ubicación son informativas, no la fuente oficial de stock.

def _crear_general_con_stock(almacen_id, producto_id, cantidad):
    from app.extensions import db as _db
    from app.models.inventario import UbicacionProducto
    general = Ubicacion(
        codigo=Ubicacion.CODIGO_GENERAL, almacen_id=almacen_id,
        zona='GENERAL', tipo='estanteria', activo=True,
    )
    _db.session.add(general)
    _db.session.flush()
    _db.session.add(UbicacionProducto(
        ubicacion_id=general.id, producto_id=producto_id, cantidad=cantidad,
    ))
    _db.session.commit()
    return general


def test_asignar_producto_traspasa_desde_siesa_general_en_nb1_co003(db, almacen, producto):
    almacen.centro_op_siesa = '003'  # bodega_siesa_id ya es 'NB1' en el fixture
    db.session.commit()
    general = _crear_general_con_stock(almacen.id, producto.id, 200)

    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    assert UbicacionProducto.query.filter_by(
        ubicacion_id=general.id, producto_id=producto.id
    ).first().cantidad == 150
    assert UbicacionProducto.query.filter_by(
        ubicacion_id=ub.id, producto_id=producto.id
    ).first().cantidad == 50


def test_asignar_producto_traspaso_no_bloquea_si_general_insuficiente(db, almacen, producto):
    almacen.centro_op_siesa = '003'
    db.session.commit()
    general = _crear_general_con_stock(almacen.id, producto.id, 10)

    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    resultado = svc.asignar_producto(ub.id, producto.id, 50)

    # No bloquea aunque pida más de lo que hay en SIESA-GENERAL: la cantidad
    # asignada al hueco es la informada por el jefe de bodega (50), no la
    # limitada por el traspaso; SIESA-GENERAL solo llega a 0, nunca negativo.
    assert resultado['cantidad_total'] == 50
    assert UbicacionProducto.query.filter_by(
        ubicacion_id=general.id, producto_id=producto.id
    ).first().cantidad == 0


def test_asignar_producto_no_traspasa_fuera_de_nb1_co003(db, almacen, producto):
    almacen.centro_op_siesa = '002'  # NB1 pero CO distinto — fuera de alcance
    db.session.commit()
    general = _crear_general_con_stock(almacen.id, producto.id, 200)

    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    assert UbicacionProducto.query.filter_by(
        ubicacion_id=general.id, producto_id=producto.id
    ).first().cantidad == 200  # sin tocar


def test_reclasificar_bloquea_con_stock_activo(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    with pytest.raises(ValueError, match='unidades activas'):
        svc.reclasificar_ubicacion(ub.id, tipo_zona='RESERVA')


def test_reclasificar_permite_sin_stock(db, almacen):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    resultado = svc.reclasificar_ubicacion(ub.id, tipo_zona='RESERVA')
    assert resultado['ubicacion']['tipo_zona'] == 'RESERVA'


def test_reclasificar_liberar_slot_bloquea_con_stock(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    with pytest.raises(ValueError, match='no se puede'):
        svc.reclasificar_ubicacion(ub.id, liberar_slot=True)


def test_eliminar_ubicacion_individual_borra_nunca_usada(db, almacen):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    resultado = svc.eliminar_ubicacion(ub.id)
    assert resultado['codigo'] == ub.codigo
    assert Ubicacion.query.get(ub.id) is None


def test_eliminar_ubicacion_individual_bloquea_con_stock(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 50)

    with pytest.raises(ValueError, match='stock activo'):
        svc.eliminar_ubicacion(ub.id)
    assert Ubicacion.query.get(ub.id) is not None


# ── editar_fila / eliminar_fila — mecanismo legado sobre 'estante' ───────────
# Ningún mecanismo nuevo escribe 'estante'; se prueba construyendo directo,
# igual que quedaron las ubicaciones creadas antes del rediseño de 5 ejes.

def _crear_legacy(almacen_id, pasillo, fila_legacy, cantidad, tipo_zona, capacidad_maxima=None):
    from app.extensions import db as _db
    prefijo = {'PICKING': 'PIK', 'RESERVA': 'RES'}[tipo_zona]
    creadas = []
    for pos in range(1, cantidad + 1):
        ub = Ubicacion(
            codigo=f'{prefijo}-{pasillo}{fila_legacy:02d}-{pos:02d}',
            almacen_id=almacen_id, pasillo=pasillo, estante=str(fila_legacy),
            tipo_zona=tipo_zona, tipo='estanteria', capacidad_maxima=capacidad_maxima,
            origen='MANUAL', activo=True,
        )
        _db.session.add(ub)
        creadas.append(ub)
    _db.session.commit()
    return creadas


def test_editar_fila_cambia_zona_de_todas_las_posiciones(db, almacen):
    _crear_legacy(almacen.id, 'A', 3, 4, 'PICKING')
    resultado = svc.editar_fila(almacen.id, 'A', 3, tipo_zona='RESERVA')

    assert resultado['total_posiciones'] == 4
    assert len(resultado['actualizadas']) == 4
    assert resultado['bloqueadas'] == {}
    for u in Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', estante='3').all():
        assert u.tipo_zona == 'RESERVA'


def test_editar_fila_no_aborta_lote_si_una_posicion_tiene_stock(db, almacen, producto):
    creadas = _crear_legacy(almacen.id, 'A', 1, 3, 'PICKING')
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
    _crear_legacy(almacen.id, 'A', 1, 2, 'PICKING', capacidad_maxima=100)
    svc.editar_fila(almacen.id, 'A', 1, capacidad_maxima=250)

    for u in Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', estante='1').all():
        assert u.capacidad_maxima == 250
        assert u.tipo_zona == 'PICKING'  # no se tocó porque no se pidió


def test_editar_fila_rechaza_fila_inexistente(db, almacen):
    with pytest.raises(ValueError, match='No hay posiciones'):
        svc.editar_fila(almacen.id, 'Z', 9, tipo_zona='RESERVA')


def test_eliminar_fila_borra_posiciones_nunca_usadas(db, almacen):
    _crear_legacy(almacen.id, 'A', 5, 3, 'RESERVA')
    resultado = svc.eliminar_fila(almacen.id, 'A', 5)

    assert resultado['total_posiciones'] == 3
    assert len(resultado['eliminadas']) == 3
    assert resultado['bloqueadas'] == {}
    assert Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', estante='5').count() == 0


def test_eliminar_fila_bloquea_con_stock_activo(db, almacen, producto):
    creadas = _crear_legacy(almacen.id, 'A', 1, 2, 'PICKING')
    svc.asignar_producto(creadas[0].id, producto.id, 50)

    resultado = svc.eliminar_fila(almacen.id, 'A', 1)

    assert len(resultado['eliminadas']) == 1  # la posición 02, sin usar
    assert list(resultado['bloqueadas'].keys()) == ['PIK-A01-01']
    assert 'stock activo' in resultado['bloqueadas']['PIK-A01-01']
    # La bloqueada sigue existiendo
    assert Ubicacion.query.get(creadas[0].id) is not None


def test_eliminar_fila_bloquea_por_historial_aunque_stock_sea_cero(db, almacen, producto):
    creadas = _crear_legacy(almacen.id, 'A', 1, 1, 'PICKING')
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

    ub = _crear_legacy(almacen.id, 'A', 1, 1, 'PICKING')[0]
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


# ── Operaciones a nivel de Cuerpo completo (esquema de 5 ejes) ─────────────
# editar_cuerpo() / eliminar_cuerpo() / reclasificar_cuerpo() — todas actúan
# sobre TODOS los entrepaños/huecos de un Pasillo+Fila+Cuerpo a la vez.

def test_editar_cuerpo_remodula_cantidad_de_entrepanos_y_huecos(db, almacen, producto):
    viejas = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'PICKING')  # 2 entrepaños, 1 hueco c/u
    svc.asignar_producto(viejas[0].id, producto.id, 20)  # E01-H01 con SKU asignado

    creadas = svc.editar_cuerpo(almacen.id, 'A', 1, 1, cantidad_entrepanos=3,
                                 huecos_por_nivel=[1, 2, 1])

    codigos_nuevos = sorted(u.codigo for u in creadas)
    assert codigos_nuevos == [
        'PIK-A1-C01-E01-H01', 'PIK-A1-C01-E02-H01', 'PIK-A1-C01-E02-H02', 'PIK-A1-C01-E03-H01',
    ]
    assert all(u.tipo_zona == 'PICKING' for u in creadas)  # conserva la zona original
    # E01-H01 reaparece (mismo código, sigue en la nueva estructura) pero es
    # un registro nuevo — el SKU que tenía asignado antes de remodular ya no está.
    nuevo_e01h01 = next(u for u in creadas if u.codigo == 'PIK-A1-C01-E01-H01')
    assert nuevo_e01h01.producto_asignado_id is None
    assert UbicacionProducto.query.filter_by(ubicacion_id=nuevo_e01h01.id).count() == 0


def test_editar_cuerpo_devuelve_stock_a_siesa_general_antes_de_borrar(db, almacen, producto):
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    svc.asignar_producto(ub.id, producto.id, 40)

    svc.editar_cuerpo(almacen.id, 'A', 1, 1, cantidad_entrepanos=2)

    general = Ubicacion.query.filter_by(
        codigo=Ubicacion.CODIGO_GENERAL, almacen_id=almacen.id
    ).first()
    assert general is not None  # se crea si no existía
    reg = UbicacionProducto.query.filter_by(
        ubicacion_id=general.id, producto_id=producto.id
    ).first()
    assert reg.cantidad == 40  # ninguna unidad se perdió

    from app.models.inventario import MovimientoInventario
    mov = MovimientoInventario.query.filter_by(
        tipo='REMODULACION_CUERPO', producto_id=producto.id
    ).first()
    assert mov is not None and mov.cantidad == -40


def test_editar_cuerpo_bloquea_si_hay_historial_real(db, almacen, producto):
    from app.models.picking import TareaPicking
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    db.session.add(TareaPicking(
        codigo='PICK-TEST-2', producto_id=producto.id, cantidad_solicitada=5,
        ubicacion_id=ub.id, almacen_id=almacen.id, estado='COMPLETADO',
    ))
    db.session.commit()

    with pytest.raises(ValueError, match='historial'):
        svc.editar_cuerpo(almacen.id, 'A', 1, 1, cantidad_entrepanos=3)

    # No se tocó nada — el hueco original sigue intacto
    assert Ubicacion.query.filter_by(codigo='PIK-A1-C01-E01-H01').first() is not None


def test_editar_cuerpo_rechaza_cuerpo_inexistente(db, almacen):
    with pytest.raises(ValueError, match='No existe el cuerpo'):
        svc.editar_cuerpo(almacen.id, 'Z', 1, 9, cantidad_entrepanos=2)


def test_eliminar_cuerpo_borra_todo_si_nunca_se_uso(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'PICKING', huecos_por_nivel=[2, 1])

    resultado = svc.eliminar_cuerpo(almacen.id, 'A', 1, 1)

    assert resultado['total'] == 3
    assert Ubicacion.query.filter_by(
        almacen_id=almacen.id, pasillo='A', fila=1, cuerpo=1
    ).count() == 0


def test_eliminar_cuerpo_todo_o_nada_bloquea_completo_si_un_hueco_tiene_stock(db, almacen, producto):
    creadas = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'PICKING')
    svc.asignar_producto(creadas[0].id, producto.id, 10)  # solo el primer hueco tiene stock

    with pytest.raises(ValueError, match='No se puede eliminar el cuerpo'):
        svc.eliminar_cuerpo(almacen.id, 'A', 1, 1)

    # Ninguno de los dos huecos se borró — ni siquiera el que estaba limpio
    assert Ubicacion.query.filter_by(
        almacen_id=almacen.id, pasillo='A', fila=1, cuerpo=1
    ).count() == 2


def test_eliminar_cuerpo_rechaza_cuerpo_inexistente(db, almacen):
    with pytest.raises(ValueError, match='No existe el cuerpo'):
        svc.eliminar_cuerpo(almacen.id, 'Z', 1, 9)


def test_reclasificar_cuerpo_cambia_zona_de_todos_los_huecos(db, almacen):
    svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'RESERVA')

    resultado = svc.reclasificar_cuerpo(almacen.id, 'A', 1, 1, tipo_zona='PICKING')

    assert len(resultado['actualizadas']) == 2
    assert resultado['bloqueadas'] == {}
    ubs = Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', fila=1, cuerpo=1).all()
    assert all(u.tipo_zona == 'PICKING' for u in ubs)


def test_reclasificar_cuerpo_desactiva_completo_preserva_historial(db, almacen, producto):
    from app.models.picking import TareaPicking
    ub = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 1, 'PICKING')[0]
    db.session.add(TareaPicking(
        codigo='PICK-TEST-3', producto_id=producto.id, cantidad_solicitada=5,
        ubicacion_id=ub.id, almacen_id=almacen.id, estado='COMPLETADO',
    ))
    db.session.commit()

    resultado = svc.reclasificar_cuerpo(almacen.id, 'A', 1, 1, activo=False)

    assert resultado['actualizadas'] == ['PIK-A1-C01-E01-H01']
    assert Ubicacion.query.get(ub.id).activo is False
    # El historial sigue existiendo — desactivar no borra nada
    assert TareaPicking.query.filter_by(codigo='PICK-TEST-3').first() is not None


def test_reclasificar_cuerpo_bloquea_por_hueco_sin_abortar_el_resto(db, almacen, producto):
    creadas = svc.crear_cuerpo(almacen.id, 'A', 1, 1, 2, 'RESERVA')
    svc.asignar_producto(creadas[0].id, producto.id, 15)  # solo el primer hueco tiene stock

    resultado = svc.reclasificar_cuerpo(almacen.id, 'A', 1, 1, tipo_zona='PICKING')

    assert creadas[1].codigo in resultado['actualizadas']  # el limpio sí se reclasifica
    assert creadas[0].codigo in resultado['bloqueadas']    # el que tiene stock, no
    assert Ubicacion.query.get(creadas[0].id).tipo_zona == 'RESERVA'  # no cambió
    assert Ubicacion.query.get(creadas[1].id).tipo_zona == 'PICKING'  # sí cambió


def test_reclasificar_cuerpo_rechaza_cuerpo_inexistente(db, almacen):
    with pytest.raises(ValueError, match='No existe el cuerpo'):
        svc.reclasificar_cuerpo(almacen.id, 'Z', 1, 9, tipo_zona='PICKING')
