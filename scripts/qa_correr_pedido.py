"""
Corre UN pedido real de Siesa QA de punta a punta por el WMS —
Pedido -> Picking -> Packing (dispara 244328/142945/142943) -> Muelle ->
Ruta -> Conductor (confirmar_parada) -> Liquidacion (RC/NC/DC) —
usando los servicios reales, contra una base LOCAL AISLADA (nunca la
Postgres de produccion) pero contra el Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_correr_pedido.py PD1113 --hasta-despacho
    venv/Scripts/python.exe scripts/qa_correr_pedido.py PD1113 --disparar-real
    venv/Scripts/python.exe scripts/qa_correr_pedido.py PD1113 --seguir

Por defecto arranca en MODO_ENSAYO=true (heredado de .env.qa) — el primer
POST real (244328) queda bloqueado por el propio gateway y solo muestra el
payload que se habria mandado. Con --disparar-real se apaga el ensayo justo
antes de ese POST puntual (y solo para el paso pedido) -- exige --si-de-verdad
como segunda confirmacion.
"""
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_ciclo.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-ciclo-real-32-bytes-o-mas-para-hmac')

from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

from app import create_app
from app.extensions import db


def _register_pg_stubs(dbapi_conn, _connection_record):
    """SQLite no tiene advisory locks de Postgres — mismos stubs no-op que
    `tests/conftest.py` usa para toda la suite."""
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)

# ── Los 7 casos reales elegidos (conversacion 2026-09-04) ──────────────────
# item_codigo = codigo_siesa real (get_pedidos_aprobados), cantidad = lo
# pendiente real hoy. Stock NB1 verificado > 0 para todos (script
# qa_detalle_pedidos.py) salvo donde se anota lo contrario.
CASOS = {
    'PD1113': {'tipo_docto': 'PD', 'consec_docto': 1113, 'cliente': 'CUANTIAS MENORES',
               'items': [{'codigo_siesa': 'IMPRESI03', 'nombre': 'ACETATO FYC 70 X50 C-8', 'cantidad': 10.0}],
               'entrega': {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO'}},
    'PD1125': {'tipo_docto': 'PD', 'consec_docto': 1125, 'cliente': 'BENITEZ LOPEZ KAREN DAYANA',
               'items': [{'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 440.0}],
               'entrega': {'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO'}},
    'PD1134': {'tipo_docto': 'PD', 'consec_docto': 1134, 'cliente': 'BENITEZ LOPEZ KAREN DAYANA',
               'items': [{'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 5.0}],
               'entrega': {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'CLIENTE_CERRADO'}},
    'PD1147': {'tipo_docto': 'PD', 'consec_docto': 1147, 'cliente': 'BENITEZ LOPEZ KAREN DAYANA',
               'items': [{'codigo_siesa': 'PAPELSP6948', 'nombre': 'MARCADOR SHARPIE 0TANK GRUESO ROJO', 'cantidad': 12.0},
                         {'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 1.0}],
               'entrega': {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'motivo_descuento': 'RETEFUENTE_2.5'}},
    'PD1141': {'tipo_docto': 'PD', 'consec_docto': 1141, 'cliente': 'PULIDO CASTRO LAURA SOFIA',
               'items': [{'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 2.0}],
               'entrega': {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'motivo_descuento': 'RETEIVA'}},
    'PD1157': {'tipo_docto': 'PD', 'consec_docto': 1157, 'cliente': 'BENITEZ LOPEZ KAREN DAYANA',
               'items': [{'codigo_siesa': 'PAPELSP6948', 'nombre': 'MARCADOR SHARPIE 0TANK GRUESO ROJO', 'cantidad': 24.0}],
               'entrega': {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'motivo_descuento': 'ICA_4X1000'}},
    'PD1146': {'tipo_docto': 'PD', 'consec_docto': 1146, 'cliente': 'CASTRO CUBILLOS PAULA ANDREA',
               'items': [{'codigo_siesa': 'PAPELSP6948', 'nombre': 'MARCADOR SHARPIE 0TANK GRUESO ROJO', 'cantidad': 36.0},
                         {'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 5.0}],
               'entrega': {'estado_entrega': 'ENTREGADO', 'forma_pago': 'TRANSFERENCIA'}},
}


def seed(db, caso, numero_pedido):
    from app.models.almacen import Almacen
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    from app.models.inventario import UbicacionProducto
    from app.models.pedido_siesa import PedidoSiesa
    from app.models.usuario import Usuario
    from app.models.conductor import Conductor
    from app.models.vehiculo import Vehiculo
    from werkzeug.security import generate_password_hash

    almacen = Almacen.query.filter_by(codigo='NB1').first()
    if not almacen:
        almacen = Almacen(codigo='NB1', nombre='NEIVA BODEGA CD', bodega_siesa_id='NB1', activo=True)
        db.session.add(almacen)
        db.session.flush()

    ub = Ubicacion.query.filter_by(codigo='PIK-QA-01', almacen_id=almacen.id).first()
    if not ub:
        ub = Ubicacion(codigo='PIK-QA-01', almacen_id=almacen.id, tipo_zona='PICKING',
                        stock_minimo=0, stock_maximo=9999, secuencia_ruteo=1, activo=True)
        db.session.add(ub)
        db.session.flush()

    productos = []
    for it in caso['items']:
        p = Producto.query.filter_by(codigo_siesa=it['codigo_siesa']).first()
        if not p:
            p = Producto(codigo=it['codigo_siesa'], nombre=it['nombre'],
                         codigo_siesa=it['codigo_siesa'], unidad_negocio_id='001')
            db.session.add(p)
            db.session.flush()
        up = UbicacionProducto.query.filter_by(ubicacion_id=ub.id, producto_id=p.id).first()
        if not up:
            db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=p.id,
                                              cantidad=it['cantidad'] + 100, reservado=0, bloqueado=0))
        productos.append(p)

        if not PedidoSiesa.query.filter_by(
                tipo_docto=caso['tipo_docto'], consec_docto=caso['consec_docto'],
                item_codigo=it['codigo_siesa']).first():
            db.session.add(PedidoSiesa(
                tipo_docto=caso['tipo_docto'], consec_docto=caso['consec_docto'],
                centro_op='003', bodega='NB1', numero_pedido=numero_pedido,
                item_codigo=it['codigo_siesa'], item_descripcion=it['nombre'],
                item_id_siesa=it['codigo_siesa'], cliente=caso['cliente'], municipio='NEIVA',
                estado_siesa=3, cantidad_pedida=it['cantidad'], cantidad_remisionada=0,
                cantidad_pendiente=it['cantidad'], producto_id=p.id))

    def _actor(email, nombre, rol):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(nombre=nombre, email=email, password_hash=generate_password_hash('qa123456'),
                        rol=rol, almacen_id=almacen.id, activo=True)
            db.session.add(u)
            db.session.flush()
        return u

    operario = _actor('operario_qa@wms-pame.local', 'Operario QA', 'operario')
    conductor_u = _actor('conductor_qa@wms-pame.local', 'Conductor QA', 'conductor')
    admin = _actor('admin_qa@wms-pame.local', 'Admin QA', 'admin')

    conductor = Conductor.query.filter_by(cedula='QA-COND-01').first()
    if not conductor:
        conductor = Conductor(nombre='Conductor QA', cedula='QA-COND-01',
                               usuario_id=conductor_u.id, activo=True, disponible=True)
        db.session.add(conductor)
        db.session.flush()

    vehiculo = Vehiculo.query.filter_by(placa='QA0001').first()
    if not vehiculo:
        vehiculo = Vehiculo(placa='QA0001', tipo='NHR', activo=True)
        db.session.add(vehiculo)
        db.session.flush()

    db.session.commit()
    return almacen, productos, operario.id, conductor.id, vehiculo.id, admin.id


def hacer_picking_y_packing(db, caso, numero_pedido, almacen_id, productos, operario_id):
    from app.services.picking_service import PickingService
    from app.services.packing_service import PackingService

    pickings = []
    for it, p in zip(caso['items'], productos):
        tareas = PickingService.crear_tareas(
            producto_id=p.id, cantidad=it['cantidad'], almacen_id=almacen_id,
            referencia_documento=numero_pedido, tipo_documento='PEDIDO')
        for t in tareas:
            PickingService.iniciar_picking(t.id, operario_id)
            PickingService.confirmar_picking(t.id, it['cantidad'], operario_id)
            pickings.append(t.id)
    db.session.commit()

    tarea = PackingService.crear_desde_picking(
        tareas_picking_ids=pickings, numero_pedido_siesa=numero_pedido,
        almacen_id=almacen_id, tipo_docto_pedido_siesa=caso['tipo_docto'],
        consec_docto_pedido_siesa=str(caso['consec_docto']))
    PackingService.iniciar(tarea.id, operario_id)
    for item in tarea.items:
        PackingService.escanear_item(tarea.id, item.producto_id, item.cantidad_esperada)
    PackingService.confirmar_packing(tarea.id)
    PackingService.cerrar_packing(tarea.id, [{'tipo': 'Caja', 'cantidad': 1}], operario_id)
    db.session.commit()
    return tarea.id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pedido', choices=list(CASOS.keys()))
    ap.add_argument('--disparar-real', action='store_true',
                     help='Apaga MODO_ENSAYO justo antes del POST de despacho (244328/142945/142943).')
    ap.add_argument('--si-de-verdad', action='store_true',
                     help='Segunda confirmacion explicita, requerida junto con --disparar-real.')
    args = ap.parse_args()

    caso = CASOS[args.pedido]
    app = create_app()
    with app.app_context():
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)
        db.create_all()

        # `disparar_dlq_inmediato()` lanza un hilo daemon real, incondicional,
        # apenas se encola un job (ver pedido_closer.py) -- carrera real
        # contra la llamada explícita de este script a `_ejecutar_job`,
        # confirmada en vivo con PD1113 (2026-09-04): el hilo de fondo mandó
        # 244328+142945 reales antes que la llamada manual, que llegó
        # fracción de segundo después y Siesa la rechazó ("pedido debe estar
        # comprometido"). Sin documento duplicado -- Siesa se protegió sola
        # -- pero el control de "un POST a la vez, confirmado" que pidió el
        # usuario exige que SOLO la llamada explícita de abajo dispare algo.
        # Mismo mock que usa toda la suite de tests (`_sin_hilos_dlq_reales`,
        # tests/conftest.py).
        import app.services.siesa_job_service as _sjs
        _sjs.disparar_dlq_inmediato = lambda *a, **k: None

        from app.services.connekta_gateway import connekta
        from app.models.packing import TareaPacking
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _ejecutar_job

        print(f'=== {args.pedido} === modo_simulacion={connekta.modo_simulacion} '
              f'modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales reales, abortando'); sys.exit(1)

        almacen, productos, operario_id, conductor_id, vehiculo_id, admin_id = seed(
            db, caso, args.pedido)
        tarea_id = hacer_picking_y_packing(
            db, caso, args.pedido, almacen.id, productos, operario_id)
        print(f'Packing confirmado, tarea_id={tarea_id} — encolando DESPACHO_F470...')

        job = SiesaJob.query.filter_by(
            tipo='DESPACHO_F470', referencia_tipo='TareaPacking',
            referencia_id=tarea_id).first()
        if not job:
            print('[ERROR] no se encolo DESPACHO_F470'); sys.exit(1)

        if args.disparar_real:
            if not args.si_de_verdad:
                print('[BLOQUEADO] --disparar-real exige tambien --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO — el siguiente POST es REAL contra Siesa QA ***')

        _ejecutar_job(job)
        db.session.commit()

        job = SiesaJob.query.get(job.id)
        print(f'\nJob DESPACHO_F470 -> estado={job.estado}')
        print('resultado:', json.dumps(job.get_resultado() if hasattr(job, 'get_resultado') else {}, default=str, indent=2)[:2000])

        tarea = TareaPacking.query.get(tarea_id)
        print(f'\ntarea.siesa_triggered = {tarea.siesa_triggered}')
        print(f'tarea.rm_tipo/rm_consec = {tarea.rm_tipo}/{tarea.rm_consec}')
        print(f'tarea.fe_tipo/fe_consec = {tarea.fe_tipo}/{tarea.fe_consec}')


if __name__ == '__main__':
    main()
