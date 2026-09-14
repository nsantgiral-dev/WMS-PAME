"""
Ruta gestionada por el conductor y ausente en Liquidación — reportado el
2026-08-31: Víctor gestionó una ruta completa (ya no aparecía en «Rutas
activas» de su pantalla) y el panel de Liquidación mostraba «Sin rutas por
liquidar», aunque el desglose agregado sí contaba sus entregas.

## La causa

`RutaService.crear_ruta()` — el alta ad-hoc de una ruta en el muelle, sin
`RutaMaestra` — nunca asignaba `fecha_programada` (a diferencia de
`programar_viaje()`, que la exige). El campo es `nullable=True`, así que
quedaba en `NULL` para siempre: ni `cerrar_ruta()` ni `entregar_ruta()` la
tocan después.

`GET /api/rutas/liquidacion/dashboard` filtra por
`fecha_programada BETWEEN fecha_desde AND fecha_hasta` — y en SQL,
`NULL >= X` y `NULL <= Y` son ambos `NULL` (no verdadero). La ruta quedaba
excluida de la lista para CUALQUIER rango de fechas que se pidiera, aunque
ya estuviera `ENTREGADA` con recaudos reales. Mismo defecto en
`RutaService.listar_rutas()` (panel admin de Rutas).

## La corrección

`crear_ruta()` ahora asigna `fecha_programada=dia_operativo()` (Bogotá).
Y como red de seguridad para lo que ya haya quedado huérfano en producción
antes de este fix, las dos consultas ahora tratan
`fecha_programada IS NULL` como "siempre visible", no como "nunca".
"""
import uuid

from app.models.recaudo_entrega import EstadoEntrega


def _conductor(db):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    u = Usuario(nombre='Victor Cuellar', email=f'victor-{uuid.uuid4().hex[:6]}@test.com',
                rol='conductor', activo=True)
    u.set_password('test123')
    db.session.add(u)
    db.session.flush()
    c = Conductor(nombre='Victor Cuellar', cedula=f'V-{uuid.uuid4().hex[:8]}',
                  usuario_id=u.id, activo=True, disponible=True)
    db.session.add(c)
    db.session.commit()
    return c


def _vehiculo(db):
    from app.models.vehiculo import Vehiculo
    v = Vehiculo(placa=f'BDT{uuid.uuid4().hex[:3].upper()}', tipo='Turbo', activo=True)
    db.session.add(v)
    db.session.commit()
    return v


def _tarea_con_bulto(db, almacen, ruta_id=None, estado_bulto='PENDIENTE'):
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    tarea = TareaPacking(
        codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO', almacen_id=almacen.id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=int(uuid.uuid4().int % 100000),
        numero_pedido_siesa=f'PED-{uuid.uuid4().hex[:6]}',
    )
    db.session.add(tarea)
    db.session.flush()
    bulto = Bulto(tarea_id=tarea.id, tipo='Caja', numero=1, total=1,
                  codigo_barras=f'BLT-{uuid.uuid4().hex[:8]}',
                  ruta_despacho_id=ruta_id, estado=estado_bulto)
    db.session.add(bulto)
    db.session.commit()
    return tarea, bulto


class TestCrearRutaAsignaFechaProgramada:
    """El alta ad-hoc del muelle (`RutaService.crear_ruta`) ya no deja
    `fecha_programada` en NULL."""

    def test_crear_ruta_no_deja_fecha_en_null(self, db, app):
        from app.services.ruta_service import RutaService
        from app.utils.fecha import dia_operativo

        conductor = _conductor(db)
        vehiculo = _vehiculo(db)
        with app.app_context():
            ruta = RutaService.crear_ruta({
                'conductor_id': conductor.id, 'vehiculo_id': vehiculo.id,
                'tipo_ruta': 'Urbana',
            })
        assert ruta.fecha_programada == dia_operativo()


class TestRutaSinFechaProgramadaApareceEnLiquidacion:
    """El caso reportado por Víctor, de punta a punta: crear_ruta → cargar
    bulto → cerrar (EN_TRANSITO) → confirmar parada → entregar (ENTREGADA)
    → SÍ aparece en el dashboard de Liquidación con la fecha de hoy."""

    def _ruta_gestionada_completa(self, db, almacen):
        from app.services.liquidacion_service import LiquidacionService  # noqa: F401
        from app.services.ruta_service import RutaService

        conductor = _conductor(db)
        vehiculo = _vehiculo(db)
        ruta = RutaService.crear_ruta({
            'conductor_id': conductor.id, 'vehiculo_id': vehiculo.id,
            'tipo_ruta': 'Urbana',
        })
        tarea, bulto = _tarea_con_bulto(db, almacen, ruta_id=ruta.id,
                                        estado_bulto='CARGADO')
        RutaService.cerrar_ruta(ruta.id)
        RutaService.confirmar_parada(ruta.id, tarea.id, conductor.usuario_id, {
            'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
            'monto_cobrado': 50000,
        })
        RutaService.entregar_ruta(ruta.id, {}, conductor.usuario_id)
        return ruta.id

    def test_aparece_en_el_dashboard_hoy(self, db, almacen, app, client, jwt_token_admin):
        ruta_id = self._ruta_gestionada_completa(db, almacen)
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo().isoformat()

        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp = client.get(
            f'/api/rutas/liquidacion/dashboard?fecha_desde={hoy}&fecha_hasta={hoy}',
            headers=h)
        assert resp.status_code == 200
        ids = [r['id'] for r in resp.get_json()['rutas']]
        assert ruta_id in ids, (
            'la ruta gestionada por completo (ENTREGADA + recaudo) no '
            'aparece en el dashboard de Liquidación del día — es el bug '
            'reportado: fecha_programada quedó en NULL y el filtro por '
            'rango de fechas la descartó para cualquier fecha')


class TestFechaProgramadaNulaEsRedDeSeguridad:
    """Aunque `crear_ruta()` ya la asigna, una ruta que de todos modos quede
    con `fecha_programada=None` (dato huérfano ya existente en producción,
    u otro camino de creación futuro que se le olvide) tiene que seguir
    siendo visible — no volver a caer en el mismo agujero."""

    def test_ruta_huerfana_sin_fecha_aparece_en_dashboard(
            self, db, almacen, app, client, jwt_token_admin):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho

        conductor = _conductor(db)
        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana',
                            estado='ENTREGADA', fecha_programada=None)
        db.session.add(ruta)
        db.session.flush()
        tarea, _ = _tarea_con_bulto(db, almacen, ruta_id=ruta.id)
        db.session.add(RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega=EstadoEntrega.ENTREGADO, forma_pago='EFECTIVO',
            monto_cobrado=50000))
        db.session.commit()

        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        resp = client.get(
            '/api/rutas/liquidacion/dashboard?fecha_desde=2020-01-01&fecha_hasta=2020-01-01',
            headers=h)
        assert resp.status_code == 200
        ids = [r['id'] for r in resp.get_json()['rutas']]
        assert ruta.id in ids, (
            'una ruta con fecha_programada=NULL sigue invisible para '
            'CUALQUIER rango de fechas — la red de seguridad no está '
            'funcionando')

    def test_ruta_huerfana_sin_fecha_aparece_en_listar_rutas(self, db, almacen):
        from app.models.ruta_despacho import RutaDespacho
        from app.services.ruta_service import RutaService

        conductor = _conductor(db)
        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana',
                            estado='ENTREGADA', fecha_programada=None)
        db.session.add(ruta)
        db.session.commit()

        pag = RutaService.listar_rutas()
        assert ruta.id in [r.id for r in pag.items]
