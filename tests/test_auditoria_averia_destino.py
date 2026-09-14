"""La avería de una auditoría de picking **no puede perder inventario**.

## El defecto que este archivo cierra

`PickingService.auditar_tarea(resultado='AVERIA')` restaba del origen y escribía
un movimiento que decía «mercancía averiada trasladada a zona AVERIAS» **aunque
no hubiera ningún destino**: el decremento y el movimiento estaban FUERA del
`if averia_ub:`. Sin destino eso no es un traslado, es una pérdida de inventario
con un registro que miente — y salía con HTTP 200.

Verificado el 2026-09-14: el defecto existía igual en la base, en `origin/main`
y en la rama. No lo introdujo ningún merge.

Se disparaba justo en el almacén que **todavía no armó su zona de averías en el
layout**, o sea el estado previo a que alguien la arme. No es un borde raro: es
el punto de partida.

## Por qué la rama entera no tenía tests

`auditar_tarea` tiene cinco resultados posibles y, hasta este archivo, **un solo
test** (`test_backorder_siesa.py`, rama `DISCREPANCIA_SIESA`). `AVERIA` —la
única que mueve inventario entre ubicaciones— nunca corrió en una prueba.
`test_fefo_excluye_averias.py` la nombra en su docstring como la fuente del
stock averiado, pero construye el `UbicacionProducto` a mano: probaba el lector,
nunca al escritor.

Acá la avería se produce **con el escritor real**, que es la lección de
`test_bin_averias_del_escritor_vivo.py`: un test que fabrica su propio mundo
coherente no puede ver un escritor roto.
"""
import pytest

from app.models.picking import TareaPicking, EstadoPicking
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.ubicacion import Ubicacion
from app.services.picking_service import PickingService, filtro_ubicacion_averias


def _origen(db, almacen, producto, cantidad=10, bloqueado=3):
    ub = Ubicacion(codigo='PIK-AUD-01', almacen_id=almacen.id, zona='A',
                   tipo='estanteria', tipo_zona='PICKING', activo=True)
    db.session.add(ub)
    db.session.flush()
    reg = UbicacionProducto(ubicacion_id=ub.id, producto_id=producto.id,
                            cantidad=cantidad, reservado=0, bloqueado=bloqueado,
                            lote='L-77')
    db.session.add(reg)
    tarea = TareaPicking(codigo='PICK-AUD-01', producto_id=producto.id,
                         cantidad_solicitada=bloqueado, ubicacion_id=ub.id,
                         almacen_id=almacen.id, estado=EstadoPicking.BLOQUEADO,
                         motivo_bloqueo='MERCANCIA_AVERIADA')
    db.session.add(tarea)
    db.session.commit()
    return ub, reg, tarea


def _en_averias(db, almacen, producto):
    """Unidades del producto en CUALQUIER ubicación de averías del almacén."""
    filas = (db.session.query(UbicacionProducto)
             .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
             .filter(Ubicacion.almacen_id == almacen.id,
                     filtro_ubicacion_averias(),
                     UbicacionProducto.producto_id == producto.id).all())
    return sum(f.cantidad for f in filas)


def _ave_de_layout(db, almacen):
    """Un bin de averías con dirección física, como el que crea el layout."""
    ub = Ubicacion(codigo='AVE-D1-EST01', almacen_id=almacen.id,
                   tipo_zona='AVERIAS', tipo='estiba', pasillo='D', fila=1,
                   activo=True)
    db.session.add(ub)
    db.session.commit()
    return ub


# ══════════════════════════════════════════════════════════════════════════
# Dirección 1 — el defecto. Estos se ponen ROJOS sin el arreglo.
# ══════════════════════════════════════════════════════════════════════════

class TestSinDestinoNoSePierdeInventario:

    def test_sin_ninguna_ubicacion_averias_el_stock_no_desaparece(
            self, app, db, almacen, producto, usuario_admin):
        """El caso de producción: almacén sin zona de averías armada."""
        ub, reg, tarea = _origen(db, almacen, producto, cantidad=10)
        assert _en_averias(db, almacen, producto) == 0

        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=3)
        db.session.refresh(reg)
        assert reg.cantidad == 7
        assert _en_averias(db, almacen, producto) == 3, (
            'las unidades se restaron del origen y no aparecieron en ninguna '
            'ubicación de averías: se perdieron')

    def test_la_conservacion_se_cumple(
            self, app, db, almacen, producto, usuario_admin):
        """El invariante de verdad, y el único que no se puede satisfacer
        haciendo trampa: nada se crea, nada se destruye."""
        ub, reg, tarea = _origen(db, almacen, producto, cantidad=10)
        antes = reg.cantidad + _en_averias(db, almacen, producto)

        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=4)
        db.session.refresh(reg)
        assert reg.cantidad + _en_averias(db, almacen, producto) == antes

    def test_el_movimiento_no_miente_sobre_el_destino(
            self, app, db, almacen, producto, usuario_admin):
        """El movimiento decía «trasladada a zona AVERIAS» sin que existiera
        ningún destino. Ahora hay dos patas y la positiva apunta a un bin de
        averías real."""
        ub, reg, tarea = _origen(db, almacen, producto)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=3)

        movs = MovimientoInventario.query.filter_by(
            producto_id=producto.id, tipo='AJUSTE_AUDITORIA').all()
        positivos = [m for m in movs if m.cantidad > 0]
        assert positivos, 'el traslado quedó con una sola pata'
        destino = Ubicacion.query.get(positivos[0].ubicacion_id)
        assert destino.tipo_zona == 'AVERIAS'
        assert sum(m.cantidad for m in movs) == 0, (
            'las dos patas del traslado tienen que sumar cero')

    def test_la_degradacion_se_declara(
            self, app, db, almacen, producto, usuario_admin):
        """Un respaldo silencioso es la mitad del defecto original: el que lee
        el kardex tiene que poder ver que no hubo dirección física."""
        ub, reg, tarea = _origen(db, almacen, producto)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=3)
        mov = MovimientoInventario.query.filter_by(
            producto_id=producto.id, tipo='AJUSTE_AUDITORIA').first()
        assert 'degradado' in (mov.motivo or ''), mov.motivo

    def test_no_se_pueden_averiar_mas_unidades_de_las_que_hay(
            self, app, db, almacen, producto, usuario_admin):
        """`cantidad_hallada` no se validaba contra el stock. Con 50 sobre un
        origen de 10, el origen se clampeaba a 0 y el destino acreditaba 50:
        cuarenta unidades inventadas. La conservación se rompía hacia ARRIBA,
        que es la dirección que ningún tablero nota."""
        ub, reg, tarea = _origen(db, almacen, producto, cantidad=10)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=50)
        db.session.refresh(reg)
        assert reg.cantidad == 0
        assert _en_averias(db, almacen, producto) == 10, 'se inventó inventario'


# ══════════════════════════════════════════════════════════════════════════
# Dirección 2 — que NO se coma la operación sana. Verdes hoy y mañana.
# ══════════════════════════════════════════════════════════════════════════

class TestLaCascadaNoSeComeLaPreferencia:

    def test_con_bin_del_layout_gana_el_layout(
            self, app, db, almacen, producto, usuario_admin):
        """La dirección física es la preferencia y no se negocia: es lo que
        permite ir a buscar la caja. Un arreglo que mande todo al bin de
        respaldo pasa todos los tests de arriba y borra esa trazabilidad."""
        ave = _ave_de_layout(db, almacen)
        ub, reg, tarea = _origen(db, almacen, producto)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=3)

        en_ave = UbicacionProducto.query.filter_by(
            ubicacion_id=ave.id, producto_id=producto.id).first()
        assert en_ave is not None and en_ave.cantidad == 3
        mov = MovimientoInventario.query.filter_by(
            producto_id=producto.id, tipo='AJUSTE_AUDITORIA').first()
        assert 'degradado' not in (mov.motivo or ''), (
            'había bin de layout: no es una degradación')

    def test_no_se_crea_el_bin_de_respaldo_si_hay_uno_de_layout(
            self, app, db, almacen, producto, usuario_admin):
        """Si no, cada auditoría ensucia el layout con un bin sin dirección."""
        _ave_de_layout(db, almacen)
        ub, reg, tarea = _origen(db, almacen, producto)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=3)
        assert Ubicacion.query.filter_by(
            codigo='AVERIADOS', almacen_id=almacen.id).count() == 0

    def test_el_lote_viaja_con_la_mercancia(
            self, app, db, almacen, producto, usuario_admin):
        """Se perdía en el traslado. Es lo que permite saber de qué entrada
        venía la caja rota — y por tanto a quién reclamarle, si aplica."""
        ave = _ave_de_layout(db, almacen)
        ub, reg, tarea = _origen(db, almacen, producto)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=3)
        en_ave = UbicacionProducto.query.filter_by(
            ubicacion_id=ave.id, producto_id=producto.id).first()
        assert en_ave.lote == 'L-77'

    def test_el_bloqueado_queda_en_cero(
            self, app, db, almacen, producto, usuario_admin):
        """Mismo invariante que `test_backorder_siesa.py` para la otra rama.
        Es el que atrapa a quien meta un `rollback()` a media transacción: se
        perdería el descongelado y quedaría una tarea CANCELADA con el
        inventario todavía congelado."""
        ub, reg, tarea = _origen(db, almacen, producto, bloqueado=3)
        cerrada = PickingService.auditar_tarea(
            tarea.id, admin_id=usuario_admin.id, resultado='AVERIA',
            cantidad_hallada=3)
        db.session.refresh(reg)
        assert cerrada.estado == EstadoPicking.CANCELADO
        assert reg.bloqueado == 0

    def test_las_otras_ramas_no_tocan_averias(
            self, app, db, almacen, producto, usuario_admin):
        """Estas dos ramas tampoco tenían test. Se escriben ahora porque el
        refactor las rozó y no había ninguna red."""
        for resultado in ('NO_ENCONTRADO', 'ENCONTRADO_PARCIAL'):
            ub, reg, tarea = _origen(db, almacen, producto)
            tarea.codigo = f'PICK-AUD-{resultado}'
            ub.codigo = f'PIK-AUD-{resultado}'
            db.session.commit()
            PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                         resultado=resultado, cantidad_hallada=1)
            assert _en_averias(db, almacen, producto) == 0, resultado
            assert Ubicacion.query.filter_by(
                codigo='AVERIADOS', almacen_id=almacen.id).count() == 0, resultado

    def test_averia_con_cantidad_cero_no_crea_nada(
            self, app, db, almacen, producto, usuario_admin):
        """El `<select>` del dashboard deja mandar cantidad 0."""
        ub, reg, tarea = _origen(db, almacen, producto)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=0)
        db.session.refresh(reg)
        assert reg.cantidad == 10
        assert Ubicacion.query.filter_by(
            codigo='AVERIADOS', almacen_id=almacen.id).count() == 0

    def test_el_stock_averiado_no_vuelve_al_fefo(
            self, app, db, almacen, producto, usuario_admin):
        """Cierra el círculo con `test_fefo_excluye_averias.py`, que hoy simula
        el stock a mano: acá la avería la produce el escritor real."""
        _ave_de_layout(db, almacen)
        ub, reg, tarea = _origen(db, almacen, producto, cantidad=10)
        PickingService.auditar_tarea(tarea.id, admin_id=usuario_admin.id,
                                     resultado='AVERIA', cantidad_hallada=10)
        plan = PickingService.calcular_fefo(producto.id, 10, almacen.id)
        codigos = {a['ubicacion_codigo'] for a in (plan.get('asignaciones') or [])}
        assert 'AVE-D1-EST01' not in codigos
