"""
Recuperacion puntual de PD1451 -- el despacho YA se hizo de verdad
(RM-1568, FE FEW-1473, confirmado por lectura contra Siesa QA) en un
intento anterior cuya base local se perdio (se borro el archivo antes de
reintentar). Reconstruye el estado local (picking+packing, sin volver a
tocar Siesa) y sigue desde Muelle -> Conductor -> Liquidacion.
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _register_pg_stubs(dbapi_conn, _connection_record):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


ITEMS_COMUNES = [
    {'codigo_siesa': 'PAPELSP9830', 'nombre': 'SOBRE DE MANILA CARTA ESP 25X 31', 'cantidad': 4.0},
    {'codigo_siesa': 'BELLESB1382', 'nombre': 'PEINILLA MOJARRA GRANDE', 'cantidad': 4.0},
    {'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 4.0},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--disparar-real', action='store_true')
    ap.add_argument('--si-de-verdad', action='store_true')
    args = ap.parse_args()

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qareal_pd1451.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-ciclo-real-32-bytes-o-mas-para-hmac')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

    from app import create_app
    from app.extensions import db
    app = create_app()
    with app.app_context():
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)
        db.create_all()

        import app.services.siesa_job_service as _sjs
        _sjs.disparar_dlq_inmediato = lambda *a, **k: None

        from app.services.connekta_gateway import connekta
        from app.services.picking_service import PickingService
        from app.services.packing_service import PackingService
        from app.services.muelle_service import MuelleService
        from app.services.ruta_service import RutaService
        from app.services.liquidacion_service import LiquidacionService
        from app.services.siesa_job_service import procesar_jobs_pendientes
        from app.models.almacen import Almacen
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        from app.models.inventario import UbicacionProducto
        from app.models.pedido_siesa import PedidoSiesa
        from app.models.usuario import Usuario
        from app.models.conductor import Conductor
        from app.models.vehiculo import Vehiculo
        from app.models.bulto import Bulto
        from app.models.packing import TareaPacking
        from app.models.siesa_job import SiesaJob
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho
        from werkzeug.security import generate_password_hash

        if args.disparar_real:
            if not args.si_de_verdad:
                print('[BLOQUEADO]'); sys.exit(1)
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO ***')
        print('modo_simulacion:', connekta.modo_simulacion, 'modo_ensayo:', connekta.modo_ensayo)

        almacen = Almacen(codigo='NB1', nombre='NEIVA BODEGA CD', bodega_siesa_id='NB1', activo=True)
        db.session.add(almacen); db.session.flush()
        ub = Ubicacion(codigo='PIK-QA-01', almacen_id=almacen.id, tipo_zona='PICKING',
                        stock_minimo=0, stock_maximo=99999, secuencia_ruteo=1, activo=True)
        db.session.add(ub); db.session.flush()

        productos = []
        for it in ITEMS_COMUNES:
            p = Producto(codigo=it['codigo_siesa'], nombre=it['nombre'],
                         codigo_siesa=it['codigo_siesa'], unidad_negocio_id='001')
            db.session.add(p); db.session.flush()
            db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=p.id,
                                              cantidad=it['cantidad'] + 100, reservado=0, bloqueado=0))
            productos.append(p)
            db.session.add(PedidoSiesa(
                tipo_docto='PD', consec_docto=1451, centro_op='003', bodega='NB1',
                numero_pedido='PD1451', item_codigo=it['codigo_siesa'],
                item_descripcion=it['nombre'], item_id_siesa=it['codigo_siesa'],
                cliente='GOMEZ CHICO SERGIO', municipio='NEIVA', estado_siesa=3,
                cantidad_pedida=it['cantidad'], cantidad_remisionada=0,
                cantidad_pendiente=it['cantidad'], producto_id=p.id))

        def _actor(email, nombre, rol):
            u = Usuario(nombre=nombre, email=email, password_hash=generate_password_hash('qa123456'),
                        rol=rol, almacen_id=almacen.id, activo=True)
            db.session.add(u); db.session.flush()
            return u
        operario = _actor('operario_qa@wms-pame.local', 'Operario QA', 'operario')
        conductor_u = _actor('conductor_qa@wms-pame.local', 'Conductor QA', 'conductor')
        admin = _actor('admin_qa_pd1451@wms-pame.local', 'Admin QA', 'admin')
        conductor = Conductor(nombre='Conductor QA', cedula='QA-COND-01',
                               usuario_id=conductor_u.id, activo=True, disponible=True)
        db.session.add(conductor); db.session.flush()
        vehiculo = Vehiculo(placa='QA0001', tipo='NHR', activo=True)
        db.session.add(vehiculo); db.session.flush()
        db.session.commit()

        # Picking + Packing locales (sin llamar Siesa)
        pickings = []
        for it, p in zip(ITEMS_COMUNES, productos):
            tareas = PickingService.crear_tareas(
                producto_id=p.id, cantidad=it['cantidad'], almacen_id=almacen.id,
                referencia_documento='PD1451', tipo_documento='PEDIDO')
            for t in tareas:
                PickingService.iniciar_picking(t.id, operario.id)
                PickingService.confirmar_picking(t.id, it['cantidad'], operario.id)
                pickings.append(t.id)
        db.session.commit()

        tarea = PackingService.crear_desde_picking(
            tareas_picking_ids=pickings, numero_pedido_siesa='PD1451',
            almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa='1451')
        PackingService.iniciar(tarea.id, operario.id)
        for item in tarea.items:
            PackingService.escanear_item(tarea.id, item.producto_id, item.cantidad_esperada)
        PackingService.confirmar_packing(tarea.id)
        db.session.commit()

        # Bultos manuales (mismo formato que _crear_bultos de pedido_closer.py)
        db.session.add(Bulto(tarea_id=tarea.id, codigo_barras='PD1451-01', tipo='Caja',
                              numero=1, total=1))
        db.session.commit()

        # Marcar como ya despachado de verdad (RM-1568, FEW-1473 reales)
        tarea = TareaPacking.query.get(tarea.id)
        tarea.estado = 'DESPACHADO'
        tarea.siesa_triggered = True
        tarea.rm_tipo, tarea.rm_consec = 'RM', '1568'
        tarea.fe_tipo, tarea.fe_consec = 'FEW', '1473'
        db.session.commit()
        print(f'tarea {tarea.id} reconciliada: siesa_triggered={tarea.siesa_triggered} '
              f'rm={tarea.rm_tipo}/{tarea.rm_consec} fe={tarea.fe_tipo}/{tarea.fe_consec}')

        # ── Muelle real ──
        print('--- Muelle (real) ---')
        ruta = RutaService.crear_ruta({
            'conductor_id': conductor.id, 'vehiculo_id': vehiculo.id, 'tipo_ruta': 'Urbana'})
        MuelleService.asignar_a_ruta(ruta.id, bultos_ids=[b.id for b in Bulto.query.filter_by(tarea_id=tarea.id).all()])
        for b in Bulto.query.filter_by(tarea_id=tarea.id).all():
            b.codigo_barras = b.codigo_barras.upper()
        db.session.commit()
        for b in Bulto.query.filter_by(tarea_id=tarea.id).all():
            r = MuelleService.cargar_bulto(b.codigo_barras, ruta.id)
            print(f'  escaneado {b.codigo_barras}: {r.get("ok")}')
        RutaService.cerrar_ruta(ruta.id)
        ruta = RutaDespacho.query.get(ruta.id)
        print(f'  ruta {ruta.id} -> {ruta.estado}')

        # ── Conductor entrega (PARCIAL) ──
        print('--- Conductor entrega (PARCIAL) ---')
        lineas = connekta.get_rowids_factura('FEW', '1473')
        bruto = sum(float(l.get('f470_vlr_bruto', 0)) for l in lineas)
        iva = sum(float(l.get('f470_vlr_imp', 0)) for l in lineas)
        neto = sum(float(l.get('f470_vlr_neto', 0)) for l in lineas)
        print(f'  FE FEW-1473: bruto={bruto} iva={iva} neto={neto}')

        total_ped = ITEMS_COMUNES[0]['cantidad']
        entregado = round(total_ped * 0.75)
        monto_cobrado = round(neto * (entregado / total_ped), 2)
        datos = {
            'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO', 'monto_cobrado': monto_cobrado,
            'observaciones': f'Cliente recibio {entregado} de {total_ped} (linea {productos[0].codigo})',
            'items_entregados': [{
                'codigo': productos[0].codigo, 'nombre': productos[0].nombre, 'unidad': 'und',
                'cantidad_pedida': total_ped, 'cantidad_entregada': entregado,
            }],
        }
        print('  confirmar_parada:', datos)
        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, conductor.id, datos)
        db.session.commit()

        print('--- Liquidación ---')
        resumen = LiquidacionService.liquidar_ruta_siesa(ruta.id, admin_id=admin.id)
        print('  resumen:', resumen)

        from app.models.devolucion_cliente import DevolucionCliente
        from app.services.devolucion_cliente_service import DevolucionClienteService
        dev = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo_id).first()
        if dev:
            print(f'  confirmando entrada física de devolución {dev.id}...')
            DevolucionClienteService.confirmar_entrada_fisica(dev.id, recepcionista_id=operario.id)
            db.session.commit()

        for vuelta in range(4):
            for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
                j.proximo_intento = None
            db.session.commit()
            n = procesar_jobs_pendientes()
            print(f'  vuelta {vuelta + 1}: {n} job(s) procesados')
            if n == 0:
                break

        print('\n=== RESULTADO FINAL ===')
        recaudo = RecaudoEntrega.query.get(recaudo_id)
        print('recaudo:', recaudo.to_dict())
        for j in SiesaJob.query.all():
            print(f'  JOB {j.id} {j.tipo} -> {j.estado} (error={j.error_ultimo!r})')


if __name__ == '__main__':
    main()
