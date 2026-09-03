"""
El watchdog ABC decide por almacén — y contaba picks de todos.

`ABCService.watchdog_anomalias(almacen_id)` recibe UN almacén y todo lo demás
que consulta lo filtra por él: la clase ABC (`ProductoClasificacionABC.almacen_id`),
las ubicaciones con stock (`Ubicacion.almacen_id`), los conteos ya activos
(`SesionConteo.almacen_id`). La única consulta que no lo hacía era la que
decide: el conteo de picks de los últimos 7 días.

Qué costaba: un producto clase C de una tienda cuyos 30 picks ocurrieron todos
en el CD superaba el umbral de C (10) y disparaba un override. El watchdog
creaba una `SesionConteo(tipo='WATCHDOG_ABC')` **en la tienda equivocada**, que
consume el cupo diario de conteo del operario y termina en un ajuste 142951
contra Siesa sobre una ubicación que nadie tenía razón para contar. Y el flujo
de conteo es el de riesgo silencioso (CLAUDE.md): «nadie reclama un ajuste» —
entra al ERP, cuadra el papel contra la realidad equivocada y reaparece meses
después en el siguiente conteo físico.

Detector en las dos direcciones: el primer test exige que NO dispare con picks
ajenos; el segundo exige que SÍ siga disparando con picks propios — un filtro
de más apagaría el watchdog entero y eso no daría error, solo silencio.
"""
from datetime import datetime, timedelta

import pytest

from app.services.abc_service import ABCService


def _almacen(db, codigo, bodega):
    from app.models.almacen import Almacen
    a = Almacen(codigo=codigo, nombre=f'Almacén {codigo}',
                bodega_siesa_id=bodega, activo=True)
    db.session.add(a)
    db.session.commit()
    return a


def _ubicacion(db, almacen, codigo, tipo_zona='PICKING'):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo=codigo, almacen_id=almacen.id,
                  tipo_zona=tipo_zona, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _clasificar(db, producto, almacen, clase):
    from app.models.producto_clasificacion_abc import ProductoClasificacionABC
    db.session.add(ProductoClasificacionABC(
        producto_id=producto.id, almacen_id=almacen.id, clasificacion=clase,
    ))
    db.session.commit()


def _stock(db, ubicacion, producto, cantidad=25):
    from app.models.inventario import UbicacionProducto
    db.session.add(UbicacionProducto(
        ubicacion_id=ubicacion.id, producto_id=producto.id,
        cantidad=cantidad, reservado=0, bloqueado=0,
    ))
    db.session.commit()


def _picks_completados(db, producto, almacen, ubicacion, cuantos, prefijo):
    """N tareas de picking COMPLETADO dentro de la ventana de 7 días."""
    from app.models.picking import TareaPicking
    hace_dos_dias = datetime.utcnow() - timedelta(days=2)
    for i in range(cuantos):
        db.session.add(TareaPicking(
            codigo=f'{prefijo}-{i:03d}',
            producto_id=producto.id,
            cantidad_solicitada=1,
            cantidad_recogida=1,
            ubicacion_id=ubicacion.id,
            almacen_id=almacen.id,
            estado='COMPLETADO',
            fecha_completado=hace_dos_dias,
        ))
    db.session.commit()


@pytest.fixture
def dos_almacenes(db):
    """Tienda (donde el producto es clase C) y CD (donde ocurren los picks)."""
    tienda = _almacen(db, 'ALM-TIENDA', 'NC1')
    cd = _almacen(db, 'ALM-CD', 'NB1')
    return tienda, cd


class TestWatchdogSoloCuentaPicksDeSuAlmacen:

    def test_picks_del_cd_no_disparan_conteo_en_la_tienda(
        self, app, db, producto, dos_almacenes
    ):
        """
        Producto clase C EN LA TIENDA. Sus 30 picks ocurrieron todos EN EL CD.
        El watchdog de la tienda no tiene ninguna evidencia de rotación local:
        no debe crear ni un solo conteo.
        """
        tienda, cd = dos_almacenes
        ub_tienda = _ubicacion(db, tienda, 'PIK-TIENDA-01')
        ub_cd = _ubicacion(db, cd, 'PIK-CD-01')

        _clasificar(db, producto, tienda, 'C')
        _stock(db, ub_tienda, producto)

        # 30 picks, todos en el CD — tres veces el umbral de clase C (10)
        _picks_completados(db, producto, cd, ub_cd, 30, 'PICK-CD')

        overrides = ABCService.watchdog_anomalias(tienda.id)

        from app.models.conteo import SesionConteo
        sesiones = SesionConteo.query.filter_by(tipo='WATCHDOG_ABC').all()

        assert overrides == [], (
            f'El watchdog de la tienda disparó con picks que ocurrieron en el CD: {overrides}'
        )
        assert sesiones == [], (
            'Se creó una SesionConteo WATCHDOG_ABC en el almacén equivocado — '
            'consume el cupo de conteo del operario y termina en un ajuste 142951'
        )

    def test_picks_propios_siguen_disparando_el_watchdog(
        self, app, db, producto, dos_almacenes
    ):
        """
        Dirección contraria: operación sana. Mismo producto clase C, mismos 30
        picks, pero ocurridos EN LA TIENDA. El watchdog debe seguir disparando
        exactamente como antes — un filtro de más lo apagaría en silencio.
        """
        tienda, _cd = dos_almacenes
        ub_tienda = _ubicacion(db, tienda, 'PIK-TIENDA-01')

        _clasificar(db, producto, tienda, 'C')
        _stock(db, ub_tienda, producto)
        _picks_completados(db, producto, tienda, ub_tienda, 30, 'PICK-TIENDA')

        overrides = ABCService.watchdog_anomalias(tienda.id)

        assert len(overrides) == 1, f'El watchdog dejó de ver rotación real: {overrides}'
        assert overrides[0]['producto_id'] == producto.id
        assert overrides[0]['clase_actual'] == 'C'
        assert overrides[0]['picks_7dias'] == 30
        assert overrides[0]['umbral'] == 10

        from app.models.conteo import SesionConteo
        sesiones = SesionConteo.query.filter_by(tipo='WATCHDOG_ABC').all()
        assert len(sesiones) == 1
        assert sesiones[0].almacen_id == tienda.id
        assert sesiones[0].ubicacion_id == ub_tienda.id

    def test_picks_mezclados_cuentan_solo_los_propios(
        self, app, db, producto, dos_almacenes
    ):
        """
        El caso que distingue «filtra» de «no cuenta nada»: 8 picks propios
        (bajo el umbral de 10) + 30 ajenos. Sin el filtro suma 38 y dispara;
        con el filtro son 8 y no dispara. Que el total ajeno sea grande es
        justamente lo que hace visible el error de suma.
        """
        tienda, cd = dos_almacenes
        ub_tienda = _ubicacion(db, tienda, 'PIK-TIENDA-01')
        ub_cd = _ubicacion(db, cd, 'PIK-CD-01')

        _clasificar(db, producto, tienda, 'C')
        _stock(db, ub_tienda, producto)
        _picks_completados(db, producto, tienda, ub_tienda, 8, 'PICK-TIENDA')
        _picks_completados(db, producto, cd, ub_cd, 30, 'PICK-CD')

        overrides = ABCService.watchdog_anomalias(tienda.id)

        assert overrides == [], (
            f'8 picks propios están bajo el umbral 10 — los 30 del CD no deben sumar: {overrides}'
        )
