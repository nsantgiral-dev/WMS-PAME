"""Remodular el layout **no puede devolver mercancía dañada a la venta**.

## El defecto que este archivo cierra

`editar_cuerpo` traspasa el stock de cada hueco a `SIESA-GENERAL` antes de
borrarlo, y ese bucket nace con `tipo_zona='GENERAL'` — vendible. El guard que
había, `_motivo_historial_operativo_real`, pregunta *«¿alguien trabajó acá?»*:
tareas de picking, de reposición, conteos, movimientos. Un bin de averías que
solo recibió una avería de auditoría **no tenía nada de eso**, porque hasta el
2026-09-14 el movimiento se escribía contra el hueco de ORIGEN.

Resultado: el guard lo veía limpio, la remodulación vertía las unidades
averiadas a `SIESA-GENERAL`, y el FEFO las asignaba a un pedido de cliente. Sin
error, sin aviso, y con un movimiento cuyo texto es cierto y no menciona la
avería.

## Y la puerta de al lado, que es la que de verdad importa

Arreglar solo `editar_cuerpo` **no cerraba nada**: `reclasificar_cuerpo` bloquea
el hueco con stock y sigue con los demás —comportamiento deliberado y probado—,
pero eso deja el cuerpo con huecos de dos zonas. Después `editar_cuerpo` lee la
zona de `ubicaciones[0]` y reconstruye **todo el cuerpo** en esa zona. Dos clics
de admin y el cuerpo de averías ya se llama PICKING, así que un guard que
pregunte «¿este cuerpo es AVERIAS?» responde que no.

Por eso el arreglo son tres piezas y este archivo las prueba las tres.

La mercancía averiada se produce **con el escritor real** (`auditar_tarea`), no
con un `UbicacionProducto` a mano: un test que fabrica su propio mundo coherente
no puede ver un escritor roto.
"""
import pytest

from app.models.picking import TareaPicking, EstadoPicking
from app.models.inventario import UbicacionProducto
from app.models.ubicacion import Ubicacion
from app.services import layout_service as svc
from app.services.picking_service import PickingService


def _cuerpo(almacen, zona='AVERIAS', pasillo='D', huecos=(1, 1)):
    return svc.crear_cuerpo(
        almacen_id=almacen.id, pasillo=pasillo, fila=1, cuerpo=1,
        cantidad_entrepanos=len(huecos), huecos_por_nivel=list(huecos),
        tipo_zona=zona, tipo_mueble='estanteria')


def _averiar(db, almacen, producto, usuario_admin, cantidad=10):
    """Produce mercancía averiada por el camino real: auditoría de picking."""
    origen = Ubicacion(codigo='PIK-ORIG-01', almacen_id=almacen.id, zona='A',
                       tipo='estanteria', tipo_zona='PICKING', activo=True)
    db.session.add(origen)
    db.session.flush()
    db.session.add(UbicacionProducto(ubicacion_id=origen.id,
                                     producto_id=producto.id,
                                     cantidad=cantidad, bloqueado=cantidad))
    tarea = TareaPicking(codigo='PICK-AVE-01', producto_id=producto.id,
                         cantidad_solicitada=cantidad, ubicacion_id=origen.id,
                         almacen_id=almacen.id, estado=EstadoPicking.BLOQUEADO,
                         motivo_bloqueo='MERCANCIA_AVERIADA')
    db.session.add(tarea)
    db.session.commit()
    PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                 resultado='AVERIA', cantidad_hallada=cantidad)
    db.session.commit()


def _en_general(db, almacen, producto):
    ub = Ubicacion.query.filter_by(codigo=Ubicacion.CODIGO_GENERAL,
                                   almacen_id=almacen.id).first()
    if ub is None:
        return 0
    reg = UbicacionProducto.query.filter_by(ubicacion_id=ub.id,
                                            producto_id=producto.id).first()
    return reg.cantidad if reg else 0


# ══════════════════════════════════════════════════════════════════════════
# Dirección 1 — bloquea lo que debe
# ══════════════════════════════════════════════════════════════════════════

class TestNoVierteAveriasAlPoolVendible:

    def test_remodular_un_cuerpo_de_averias_con_stock_se_bloquea(
            self, app, db, almacen, producto, usuario_admin):
        _cuerpo(almacen)
        _averiar(db, almacen, producto, usuario_admin, cantidad=10)

        with pytest.raises(ValueError) as e:
            svc.editar_cuerpo(almacen_id=almacen.id, pasillo='D', fila=1,
                              cuerpo=1, cantidad_entrepanos=3,
                              huecos_por_nivel=[1, 1, 1])
        # Bloquea por cualquiera de los dos guards y da igual cuál: desde que
        # la auditoría escribe también la pata positiva, el bin destino SÍ tiene
        # historial, así que el guard viejo alcanza para la avería nueva. Lo que
        # se afirma es la propiedad, no por qué camino se cumplió.
        assert 'remodular' in str(e.value).lower()
        assert _en_general(db, almacen, producto) == 0, (
            'la mercancía averiada llegó al bucket vendible')

    def test_bloquea_stock_averiado_SIN_historial(
            self, app, db, almacen, producto, usuario_admin):
        """El caso que el guard viejo no puede ver, y la razón de ser del nuevo.

        Toda la mercancía averiada que ya estaba en un bin **antes** del
        2026-09-14 no tiene movimiento a nombre de ese bin: la auditoría lo
        escribía contra el hueco de origen. Para ese stock —el que está hoy en
        producción— el guard de historial responde «limpio».

        Se simula escribiendo el `UbicacionProducto` directo, que es exactamente
        la forma del dato preexistente.
        """
        _cuerpo(almacen)
        ave = Ubicacion.query.filter(Ubicacion.codigo.like('AVE-%')).first()
        db.session.add(UbicacionProducto(ubicacion_id=ave.id,
                                         producto_id=producto.id, cantidad=6))
        db.session.commit()

        assert svc._motivo_historial_operativo_real(ave.id) is None, (
            'el guard viejo no puede ver este caso — por eso hace falta el nuevo')
        assert svc._motivo_stock_no_reubicable(ave.id) is not None

        with pytest.raises(ValueError):
            svc.editar_cuerpo(almacen_id=almacen.id, pasillo='D', fila=1,
                              cuerpo=1, cantidad_entrepanos=3,
                              huecos_por_nivel=[1, 1, 1])
        assert _en_general(db, almacen, producto) == 0

    def test_reclasificar_no_deja_el_cuerpo_a_medias(
            self, app, db, almacen, producto, usuario_admin):
        """La puerta de al lado: si se reclasifican solo los huecos vacíos, el
        cuerpo queda mixto y `editar_cuerpo` lo convierte entero."""
        _cuerpo(almacen, huecos=(1, 1, 1))
        _averiar(db, almacen, producto, usuario_admin, cantidad=7)

        with pytest.raises(ValueError):
            svc.reclasificar_cuerpo(almacen_id=almacen.id, pasillo='D', fila=1,
                                    cuerpo=1, tipo_zona='PICKING')
        zonas = {u.tipo_zona for u in Ubicacion.query.filter(
            Ubicacion.codigo.like('AVE-%')).all()}
        assert zonas == {'AVERIAS'}, f'quedó mixto: {zonas}'

    def test_un_cuerpo_de_zona_mixta_no_se_remodula(
            self, app, db, almacen, producto, usuario_admin):
        """El invariante que `ubicaciones[0].tipo_zona` daba por supuesto."""
        _cuerpo(almacen, huecos=(1, 1))
        huecos = Ubicacion.query.filter(Ubicacion.codigo.like('AVE-%')).all()
        huecos[0].tipo_zona = 'PICKING'      # simula el cuerpo ya corrompido
        db.session.commit()

        with pytest.raises(ValueError) as e:
            svc.editar_cuerpo(almacen_id=almacen.id, pasillo='D', fila=1,
                              cuerpo=1, cantidad_entrepanos=2,
                              huecos_por_nivel=[1, 1])
        assert 'más de una zona' in str(e.value)


# ══════════════════════════════════════════════════════════════════════════
# Dirección 2 — no se come la operación sana
# ══════════════════════════════════════════════════════════════════════════

class TestLaOperacionSanaSigueFuncionando:

    def test_cuerpo_de_averias_VACIO_se_remodula_normal(
            self, app, db, almacen, producto, usuario_admin):
        """El caso que rompería un arreglo mal escrito (`if AVERIAS: raise`)."""
        _cuerpo(almacen, huecos=(1, 1))
        r = svc.editar_cuerpo(almacen_id=almacen.id, pasillo='D', fila=1,
                              cuerpo=1, cantidad_entrepanos=3,
                              huecos_por_nivel=[1, 1, 1])
        assert len(r) == 3

    def test_picking_con_stock_sigue_vertiendo_a_general(
            self, app, db, almacen, producto, usuario_admin):
        """El comportamiento existente no se toca: el stock vendible sí se
        traspasa, que es como se remodula un pasillo sin perder unidades."""
        _cuerpo(almacen, zona='PICKING', pasillo='F', huecos=(1,))
        hueco = Ubicacion.query.filter(Ubicacion.codigo.like('PIK-F1%')).first()
        db.session.add(UbicacionProducto(ubicacion_id=hueco.id,
                                         producto_id=producto.id, cantidad=4))
        db.session.commit()

        svc.editar_cuerpo(almacen_id=almacen.id, pasillo='F', fila=1, cuerpo=1,
                          cantidad_entrepanos=2, huecos_por_nivel=[1, 1])
        assert _en_general(db, almacen, producto) == 4

    def test_reclasificar_sin_stock_no_aborta(
            self, app, db, almacen, producto, usuario_admin):
        """No romper el contrato probado: un cuerpo sin mercancía no vendible
        se reclasifica sin chistar."""
        _cuerpo(almacen, huecos=(1, 1))
        r = svc.reclasificar_cuerpo(almacen_id=almacen.id, pasillo='D', fila=1,
                                    cuerpo=1, tipo_zona='PICKING')
        assert len(r['actualizadas']) == 2
