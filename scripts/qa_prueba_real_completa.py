"""
Ciclo REAL completo contra Siesa QA, con MUELLE real (no atajo):
Pedido -> Picking -> Packing (244328/142945/142943) -> Muelle real
(RutaService.crear_ruta -> MuelleService.asignar_a_ruta -> cargar_bulto ->
RutaService.cerrar_ruta) -> Conductor (confirmar_parada) -> Liquidacion
(RC/NC/DC via procesar_jobs_pendientes, el mismo camino real del DLQ).

Base LOCAL AISLADA por pedido (nunca la Postgres de produccion), Siesa QA
real (.env.qa). disparar_dlq_inmediato desactivado.

Uso:
    venv/Scripts/python.exe scripts/qa_prueba_real_completa.py PD1450 --disparar-real --si-de-verdad
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


# Los 3 items reales que trae cada uno de estos 5 pedidos (verificado por
# lectura, 2026-09-04) — 4 und de cada uno, 12 und / 3 productos por pedido.
ITEMS_COMUNES = [
    {'codigo_siesa': 'PAPELSP9830', 'nombre': 'SOBRE DE MANILA CARTA ESP 25X 31', 'cantidad': 4.0},
    {'codigo_siesa': 'BELLESB1382', 'nombre': 'PEINILLA MOJARRA GRANDE', 'cantidad': 4.0},
    {'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 4.0},
]

CASOS = {
    'PD1450': {'consec_docto': 1450, 'cliente': 'BENITEZ LOPEZ KAREN DAYANA',
               'entrega_kind': 'COMPLETO'},
    'PD1451': {'consec_docto': 1451, 'cliente': 'GOMEZ CHICO SERGIO',
               'entrega_kind': 'PARCIAL', 'entregado_frac': 0.75},
    'PD1454': {'consec_docto': 1454, 'cliente': 'ROA RIOS OSCAR DANIEL',
               'entrega_kind': 'MOTIVO', 'motivo': 'RETEFUENTE_2.5'},
    'PD1455': {'consec_docto': 1455, 'cliente': 'MONTES OYOLA NICOLAS',
               'entrega_kind': 'MOTIVO', 'motivo': 'RETEIVA'},
    'PD1456': {'consec_docto': 1456, 'cliente': 'PERDOMO ROJAS GABRIELA',
               'entrega_kind': 'MOTIVO', 'motivo': 'ICA_4X1000'},
}


def seed(db, caso, numero_pedido, admin_email):
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
        db.session.add(almacen); db.session.flush()

    ub = Ubicacion.query.filter_by(codigo='PIK-QA-01', almacen_id=almacen.id).first()
    if not ub:
        ub = Ubicacion(codigo='PIK-QA-01', almacen_id=almacen.id, tipo_zona='PICKING',
                        stock_minimo=0, stock_maximo=99999, secuencia_ruteo=1, activo=True)
        db.session.add(ub); db.session.flush()

    productos = []
    for it in ITEMS_COMUNES:
        p = Producto.query.filter_by(codigo_siesa=it['codigo_siesa']).first()
        if not p:
            p = Producto(codigo=it['codigo_siesa'], nombre=it['nombre'],
                         codigo_siesa=it['codigo_siesa'], unidad_negocio_id='001')
            db.session.add(p); db.session.flush()
        if not UbicacionProducto.query.filter_by(ubicacion_id=ub.id, producto_id=p.id).first():
            db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=p.id,
                                              cantidad=it['cantidad'] + 100, reservado=0, bloqueado=0))
        productos.append(p)
        if not PedidoSiesa.query.filter_by(
                tipo_docto='PD', consec_docto=caso['consec_docto'],
                item_codigo=it['codigo_siesa']).first():
            db.session.add(PedidoSiesa(
                tipo_docto='PD', consec_docto=caso['consec_docto'],
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
            db.session.add(u); db.session.flush()
        return u

    operario = _actor('operario_qa@wms-pame.local', 'Operario QA', 'operario')
    conductor_u = _actor('conductor_qa@wms-pame.local', 'Conductor QA', 'conductor')
    admin = _actor(admin_email, 'Admin QA', 'admin')

    conductor = Conductor.query.filter_by(cedula='QA-COND-01').first()
    if not conductor:
        conductor = Conductor(nombre='Conductor QA', cedula='QA-COND-01',
                               usuario_id=conductor_u.id, activo=True, disponible=True)
        db.session.add(conductor); db.session.flush()

    vehiculo = Vehiculo.query.filter_by(placa='QA0001').first()
    if not vehiculo:
        vehiculo = Vehiculo(placa='QA0001', tipo='NHR', activo=True)
        db.session.add(vehiculo); db.session.flush()
    db.session.commit()
    return almacen, productos, operario.id, conductor.id, vehiculo.id, admin.id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pedido', choices=list(CASOS.keys()))
    ap.add_argument('--disparar-real', action='store_true')
    ap.add_argument('--si-de-verdad', action='store_true')
    args = ap.parse_args()
    caso = CASOS[args.pedido]

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', f'qareal_{args.pedido.lower()}.db')
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
        from app.services.despacho_parcial_service import DespachoParialService
        from app.services.liquidacion_service import LiquidacionService, monto_de_retencion
        from app.services.siesa_job_service import _ejecutar_job, procesar_jobs_pendientes
        from app.models.bulto import Bulto
        from app.models.packing import TareaPacking
        from app.models.siesa_job import SiesaJob
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import EstadoRutaDespacho, RutaDespacho

        print(f'=== {args.pedido} ({caso["entrega_kind"]}) === '
              f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales'); sys.exit(1)
        if args.disparar_real and not args.si_de_verdad:
            print('[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
        if args.disparar_real:
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO — TODO lo que sigue es REAL ***')

        numero_pedido = args.pedido
        almacen, productos, operario_id, conductor_id, vehiculo_id, admin_id = seed(
            db, caso, numero_pedido, f'admin_qa_{args.pedido.lower()}@wms-pame.local')

        # ── 1) Picking ──
        print('--- Picking ---')
        pickings = []
        for it, p in zip(ITEMS_COMUNES, productos):
            tareas = PickingService.crear_tareas(
                producto_id=p.id, cantidad=it['cantidad'], almacen_id=almacen.id,
                referencia_documento=numero_pedido, tipo_documento='PEDIDO')
            for t in tareas:
                PickingService.iniciar_picking(t.id, operario_id)
                PickingService.confirmar_picking(t.id, it['cantidad'], operario_id)
                pickings.append(t.id)
        db.session.commit()

        # ── 2) Packing ──
        print('--- Packing ---')
        tarea = PackingService.crear_desde_picking(
            tareas_picking_ids=pickings, numero_pedido_siesa=numero_pedido,
            almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa=str(caso['consec_docto']))
        PackingService.iniciar(tarea.id, operario_id)
        for item in tarea.items:
            PackingService.escanear_item(tarea.id, item.producto_id, item.cantidad_esperada)
        PackingService.confirmar_packing(tarea.id)
        PackingService.cerrar_packing(tarea.id, [{'tipo': 'Caja', 'cantidad': 1}], operario_id)
        db.session.commit()

        # ── 3) Despacho real (244328 -> 142945 -> 142943) ──
        print('--- Despacho ---')
        job = SiesaJob.query.filter_by(
            tipo='DESPACHO_F470', referencia_tipo='TareaPacking', referencia_id=tarea.id).first()
        _ejecutar_job(job)
        db.session.commit()
        tarea = TareaPacking.query.get(tarea.id)

        if not tarea.siesa_triggered:
            rm = connekta.get_remision_desde_pedido('PD', caso['consec_docto'])
            if rm and rm.get('consec') and not (tarea.rm_tipo and tarea.rm_consec):
                tarea.rm_tipo, tarea.rm_consec = rm.get('tipo', 'RM'), str(rm['consec'])
                db.session.commit()
            if tarea.rm_tipo and tarea.rm_consec:
                resultado = DespachoParialService.despachar_parcial(tarea, {})
                print('  reintento post-reconciliación:', resultado)
                db.session.commit()
                tarea = TareaPacking.query.get(tarea.id)

        print(f'  siesa_triggered={tarea.siesa_triggered} rm={tarea.rm_tipo}/{tarea.rm_consec} '
              f'fe={tarea.fe_tipo}/{tarea.fe_consec}')
        if not tarea.siesa_triggered:
            print('[ERROR] despacho no completó — abortando'); sys.exit(1)

        # ── 4) Muelle real ──
        print('--- Muelle (real) ---')
        ruta = RutaService.crear_ruta({
            'conductor_id': conductor_id, 'vehiculo_id': vehiculo_id, 'tipo_ruta': 'Urbana'})
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

        # ── 5) Conductor entrega ──
        print('--- Conductor entrega al cliente ---')
        if not tarea.fe_tipo or not tarea.fe_consec:
            from app.services.fe_resolver import resolver_fe
            fe_tipo, fe_consec = resolver_fe(tarea)
        else:
            fe_tipo, fe_consec = tarea.fe_tipo, tarea.fe_consec
        lineas = connekta.get_rowids_factura(fe_tipo, fe_consec)
        bruto = sum(float(l.get('f470_vlr_bruto', 0)) for l in lineas)
        iva = sum(float(l.get('f470_vlr_imp', 0)) for l in lineas)
        neto = sum(float(l.get('f470_vlr_neto', 0)) for l in lineas)
        print(f'  FE {fe_tipo}-{fe_consec}: bruto={bruto} iva={iva} neto={neto}')

        kind = caso['entrega_kind']
        if kind == 'COMPLETO':
            datos = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': neto}
        elif kind == 'PARCIAL':
            total_ped = ITEMS_COMUNES[0]['cantidad']
            entregado = round(total_ped * caso.get('entregado_frac', 0.75))
            monto_cobrado = round(neto * (entregado / total_ped), 2)
            datos = {
                'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO', 'monto_cobrado': monto_cobrado,
                'observaciones': f'Cliente recibio {entregado} de {total_ped} (linea {productos[0].codigo})',
                'items_entregados': [{
                    'codigo': productos[0].codigo, 'nombre': productos[0].nombre, 'unidad': 'und',
                    'cantidad_pedida': total_ped, 'cantidad_entregada': entregado,
                }],
            }
        elif kind == 'MOTIVO':
            motivo = caso['motivo']
            retencion = monto_de_retencion(motivo, bruto, iva)
            monto_cobrado = round(neto - retencion, 2)
            datos = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': monto_cobrado,
                     'motivo_descuento': motivo, 'monto_descuento': retencion}

        print('  confirmar_parada:', datos)
        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, conductor_id, datos)
        db.session.commit()

        # ── 6) Liquidación (el conductor entrega cuentas al administrador) ──
        print('--- Liquidación ---')
        resumen = LiquidacionService.liquidar_ruta_siesa(ruta.id, admin_id=admin_id)
        print('  resumen:', resumen)

        if kind == 'PARCIAL':
            from app.models.devolucion_cliente import DevolucionCliente
            from app.services.devolucion_cliente_service import DevolucionClienteService
            dev = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo_id).first()
            if dev:
                print(f'  confirmando entrada física de devolución {dev.id}...')
                DevolucionClienteService.confirmar_entrada_fisica(dev.id, recepcionista_id=operario_id)
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
