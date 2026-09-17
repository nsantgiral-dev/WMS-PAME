"""
Lote real de 14 pedidos reales de Siesa QA (conversacion 2026-09-10):
PD1469-PD1481 (13, cada uno con un motivo de descuento distinto de los 13
del catalogo real, 8 entrega completa + 5 entrega parcial con devolucion+NC)
y PD1484 (credito a 30 dias, cond_pago=C04, manejado normal sin retencion).

Flujo real por pedido: Picking -> Packing -> Despacho (244328/142945/142943)
-> Ruta unica -> Conductor confirma parada (motivo/parcial segun el caso) ->
Liquidar ruta (encola NC/RC/DC) -> confirmar entrada fisica de las 5
devoluciones -> drenar cola real (procesar_jobs_pendientes) hasta vaciar.

Base LOCAL AISLADA (nunca la Postgres de produccion), contra Siesa QA real
(.env.qa). PD1469-1481/1484 ya EXISTEN en Siesa QA (creados por el usuario
hoy) -- este script NO los crea, los consume tal cual estan.

Uso:
    venv/Scripts/python.exe scripts/qa_lote16_pedidos_reales.py
    venv/Scripts/python.exe scripts/qa_lote16_pedidos_reales.py --disparar-real --si-de-verdad
"""
import argparse
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _register_pg_stubs(dbapi_conn, _connection_record):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


ITEMS_COMUN = [
    {'codigo_siesa': 'PAPELSP9830', 'nombre': 'SOBRE DE MANILA CARTA ESP 25X 31', 'cantidad': 4.0},
    {'codigo_siesa': 'BELLESB1382', 'nombre': 'PEINILLA MOJARRA GRANDE', 'cantidad': 4.0},
    {'codigo_siesa': 'PAPELSP9218', 'nombre': 'RESMA DE PAPEL CARTA REPROGRAF', 'cantidad': 4.0},
]

# motivo=None => PD1484, credito 30 dias, sin retencion, entrega completa normal.
# devuelve: {codigo_siesa: unidades_devueltas} solo para los 5 parciales.
CASOS = {
    'PD1469': {'consec': 1469, 'cliente': 'JORGUE CARDOZO FAIBER SANTIAGO',   'motivo': 'RETEFUENTE_2.5', 'devuelve': None},
    'PD1470': {'consec': 1470, 'cliente': 'REYES RIVERA LICETTE',             'motivo': 'RETEFUENTE_1.5', 'devuelve': None},
    'PD1471': {'consec': 1471, 'cliente': 'NIETO CABALLERO SEBASTIAN',        'motivo': 'RETEIVA',        'devuelve': {'PAPELSP9218': 1}},
    'PD1472': {'consec': 1472, 'cliente': 'SANCHEZ ALVAREZ KEVIN OSWALDO',    'motivo': 'ICA_4X1000',     'devuelve': None},
    'PD1473': {'consec': 1473, 'cliente': 'MOYAS ROJAS JORGE IVAN',           'motivo': 'ICA_3X1000',     'devuelve': None},
    'PD1474': {'consec': 1474, 'cliente': 'ROBLES MANRIQUE MAIDY LORENA',     'motivo': 'ICA_6X1000',     'devuelve': {'BELLESB1382': 1}},
    'PD1475': {'consec': 1475, 'cliente': 'BENITEZ LOPEZ KAREN DAYANA',       'motivo': 'ICA_10X1000',    'devuelve': None},
    'PD1476': {'consec': 1476, 'cliente': 'BAICUE RUMIQUE LEIDY DAYANA',      'motivo': 'ICA_11X1000',    'devuelve': None},
    'PD1477': {'consec': 1477, 'cliente': 'FLOREZ LOPEZ JOSE FERNANDO',       'motivo': 'AUTORRETENCION_ICA_NEIVA_3X1000',   'devuelve': {'PAPELSP9830': 1}},
    'PD1478': {'consec': 1478, 'cliente': 'INVERSIONES VALENCIA ASOCIADOS SAS', 'motivo': 'AUTORRETENCION_ICA_NEIVA_3.5X1000', 'devuelve': None},
    'PD1479': {'consec': 1479, 'cliente': 'SAMBONI BENAVIDES ALDIVAR',        'motivo': 'AUTORRETENCION_ICA_NEIVA_4.5X1000', 'devuelve': {'PAPELSP9218': 2}},
    'PD1480': {'consec': 1480, 'cliente': 'MASMELAS CHARRY ALEXANDRA',        'motivo': 'AUTORRETENCION_ICA_NEIVA_8X1000',   'devuelve': None},
    'PD1481': {'consec': 1481, 'cliente': 'SILVA ALMARIO KAROL VANESA',       'motivo': 'AUTORRETENCION_ICA_PITALITO_4X1000', 'devuelve': {'PAPELSP9830': 1, 'BELLESB1382': 1, 'PAPELSP9218': 1}},
    'PD1484': {'consec': 1484, 'cliente': 'GOMEZ BENAVIDES DANNY VIVIANA',    'motivo': None,             'devuelve': None},
}


def seed_pedido(db, almacen, ub, productos_cache, numero_pedido, caso):
    from app.models.producto import Producto
    from app.models.inventario import UbicacionProducto
    from app.models.pedido_siesa import PedidoSiesa

    productos = []
    for it in ITEMS_COMUN:
        p = productos_cache.get(it['codigo_siesa'])
        if not p:
            p = Producto.query.filter_by(codigo_siesa=it['codigo_siesa']).first()
            if not p:
                p = Producto(codigo=it['codigo_siesa'], nombre=it['nombre'],
                             codigo_siesa=it['codigo_siesa'], unidad_negocio_id='001')
                db.session.add(p); db.session.flush()
            productos_cache[it['codigo_siesa']] = p
            if not UbicacionProducto.query.filter_by(ubicacion_id=ub.id, producto_id=p.id).first():
                db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=p.id,
                                                  cantidad=1000, reservado=0, bloqueado=0))
        productos.append(p)
        if not PedidoSiesa.query.filter_by(
                tipo_docto='PD', consec_docto=caso['consec'], item_codigo=it['codigo_siesa']).first():
            db.session.add(PedidoSiesa(
                tipo_docto='PD', consec_docto=caso['consec'],
                centro_op='003', bodega='NB1', numero_pedido=numero_pedido,
                item_codigo=it['codigo_siesa'], item_descripcion=it['nombre'],
                item_id_siesa=it['codigo_siesa'], cliente=caso['cliente'], municipio='NEIVA',
                estado_siesa=3, cantidad_pedida=it['cantidad'], cantidad_remisionada=0,
                cantidad_pendiente=it['cantidad'], producto_id=p.id))
    db.session.commit()
    return productos


def despachar_pedido(db, connekta, almacen, ub, productos_cache, numero_pedido, caso, operario_id):
    from app.services.picking_service import PickingService
    from app.services.packing_service import PackingService
    from app.services.despacho_parcial_service import DespachoParialService
    from app.services.siesa_job_service import _ejecutar_job
    from app.models.siesa_job import SiesaJob
    from app.models.packing import TareaPacking
    from app.models.producto import Producto
    from app.services.fe_resolver import resolver_fe, FENoEncontrada

    # Idempotencia: si esta corrida ya despacho este pedido antes (ej. quedo
    # a medias por un crash posterior), NO volver a postear 244328/142945/
    # 142943 -- reusar la tarea ya disparada y solo reconciliar la FE si hace
    # falta (resolver_fe es de solo lectura, no dispara nada nuevo).
    existente = TareaPacking.query.filter_by(
        numero_pedido_siesa=numero_pedido, siesa_triggered=True).first()
    if existente:
        print(f'  {numero_pedido}: ya estaba despachado (tarea={existente.id}, '
              f'rm={existente.rm_tipo}/{existente.rm_consec}) -- reusando, sin re-postear')
        if not existente.fe_tipo or not existente.fe_consec:
            try:
                resolver_fe(existente, gateway=connekta)
            except FENoEncontrada as e:
                print(f'  [AVISO] {numero_pedido}: FE aun no resoluble: {e}')
        productos = [Producto.query.filter_by(codigo_siesa=it['codigo_siesa']).first()
                     for it in ITEMS_COMUN]
        return existente, productos

    # Idempotencia parte 2: la tarea de packing puede existir ya (picking +
    # packing corrieron) pero el despacho haber fallado a medias (ej. rechazo
    # real de Siesa como "No hay suficiente cantidad disponible" en 244328).
    # En ese caso NO recrear picking/packing (PackingService.crear_desde_picking
    # rechaza un segundo intento sobre el mismo pedido) -- reusar la tarea y
    # solo reintentar el job de despacho.
    tarea_previa = TareaPacking.query.filter_by(numero_pedido_siesa=numero_pedido).first()
    if tarea_previa:
        print(f'  {numero_pedido}: tarea de packing ya existe (id={tarea_previa.id}, '
              f'siesa_triggered=False) -- reintentando solo el despacho')
        tarea = tarea_previa
        job = SiesaJob.query.filter_by(
            tipo='DESPACHO_F470', referencia_tipo='TareaPacking', referencia_id=tarea.id).first()
        if not job:
            raise RuntimeError(f'{numero_pedido}: tarea existe pero no tiene DESPACHO_F470 encolado')
        productos = [Producto.query.filter_by(codigo_siesa=it['codigo_siesa']).first()
                     for it in ITEMS_COMUN]
    else:
        productos = seed_pedido(db, almacen, ub, productos_cache, numero_pedido, caso)

        pickings = []
        for it, p in zip(ITEMS_COMUN, productos):
            tareas = PickingService.crear_tareas(
                producto_id=p.id, cantidad=it['cantidad'], almacen_id=almacen.id,
                referencia_documento=numero_pedido, tipo_documento='PEDIDO')
            for t in tareas:
                PickingService.iniciar_picking(t.id, operario_id)
                PickingService.confirmar_picking(t.id, it['cantidad'], operario_id)
                pickings.append(t.id)
        db.session.commit()

        tarea = PackingService.crear_desde_picking(
            tareas_picking_ids=pickings, numero_pedido_siesa=numero_pedido,
            almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa=str(caso['consec']))
        PackingService.iniciar(tarea.id, operario_id)
        for item in tarea.items:
            PackingService.escanear_item(tarea.id, item.producto_id, item.cantidad_esperada)
        PackingService.confirmar_packing(tarea.id)
        PackingService.cerrar_packing(tarea.id, [{'tipo': 'Caja', 'cantidad': 1}], operario_id)
        db.session.commit()

        job = SiesaJob.query.filter_by(
            tipo='DESPACHO_F470', referencia_tipo='TareaPacking', referencia_id=tarea.id).first()
        if not job:
            raise RuntimeError(f'{numero_pedido}: no se encolo DESPACHO_F470')

    _ejecutar_job(job)
    db.session.commit()

    tarea = TareaPacking.query.get(tarea.id)
    if not tarea.siesa_triggered:
        rm = connekta.get_remision_desde_pedido('PD', caso['consec'])
        if rm and rm.get('consec') and not (tarea.rm_tipo and tarea.rm_consec):
            tarea.rm_tipo, tarea.rm_consec = rm.get('tipo', 'RM'), str(rm['consec'])
            db.session.commit()
        if tarea.rm_tipo and tarea.rm_consec:
            DespachoParialService.despachar_parcial(tarea, {})
            db.session.commit()
            tarea = TareaPacking.query.get(tarea.id)

    print(f'  {numero_pedido}: siesa_triggered={tarea.siesa_triggered} '
          f'rm={tarea.rm_tipo}/{tarea.rm_consec} fe={tarea.fe_tipo}/{tarea.fe_consec}')
    if not tarea.siesa_triggered:
        raise RuntimeError(f'{numero_pedido}: despacho NO se disparo correctamente')

    if not tarea.fe_tipo or not tarea.fe_consec:
        try:
            resolver_fe(tarea, gateway=connekta)
        except FENoEncontrada as e:
            print(f'  [AVISO] {numero_pedido}: FE aun no resoluble tras despacho: {e}')
    return tarea, productos


def obtener_factura_real(connekta, tipo_docto_fe, consec_fe):
    lineas = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
    bruto = sum(float(l.get('f470_vlr_bruto', 0)) for l in lineas)
    iva = sum(float(l.get('f470_vlr_imp', 0)) for l in lineas)
    neto = sum(float(l.get('f470_vlr_neto', 0)) for l in lineas)
    return bruto, iva, neto


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--disparar-real', action='store_true')
    ap.add_argument('--si-de-verdad', action='store_true')
    ap.add_argument('--desde', default=None, help='reanudar desde este PD (ej. PD1474)')
    ap.add_argument('--solo', default=None, help='CSV de pedidos a correr, ej. PD1469,PD1470 (smoke test)')
    args = ap.parse_args()

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_lote16.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-lote16-real-32-bytes-o-mas-para-hmac')
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
        from app.services.liquidacion_service import LiquidacionService, monto_de_retencion
        from app.services.ruta_service import RutaService
        from app.services.siesa_job_service import procesar_jobs_pendientes
        from app.models.almacen import Almacen
        from app.models.ubicacion import Ubicacion
        from app.models.usuario import Usuario
        from app.models.conductor import Conductor
        from app.models.vehiculo import Vehiculo
        from app.models.ruta_despacho import RutaDespacho
        from app.models.bulto import Bulto
        from app.models.siesa_job import SiesaJob
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.devolucion_cliente import DevolucionCliente, EstadoDevolucionCliente
        from app.services.devolucion_cliente_service import DevolucionClienteService
        from werkzeug.security import generate_password_hash

        print(f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales reales'); sys.exit(1)
        if args.disparar_real and not args.si_de_verdad:
            print('[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
        if args.disparar_real:
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO — TODO lo que sigue es REAL contra Siesa QA ***')

        almacen = Almacen.query.filter_by(codigo='NB1').first()
        if not almacen:
            almacen = Almacen(codigo='NB1', nombre='NEIVA BODEGA CD', bodega_siesa_id='NB1', activo=True)
            db.session.add(almacen); db.session.flush()
        ub = Ubicacion.query.filter_by(codigo='PIK-QA-01', almacen_id=almacen.id).first()
        if not ub:
            ub = Ubicacion(codigo='PIK-QA-01', almacen_id=almacen.id, tipo_zona='PICKING',
                            stock_minimo=0, stock_maximo=999999, secuencia_ruteo=1, activo=True)
            db.session.add(ub); db.session.flush()

        def _actor(email, nombre, rol):
            u = Usuario.query.filter_by(email=email).first()
            if not u:
                u = Usuario(nombre=nombre, email=email, password_hash=generate_password_hash('qa123456'),
                            rol=rol, almacen_id=almacen.id, activo=True)
                db.session.add(u); db.session.flush()
            return u

        operario = _actor('operario_qa@wms-pame.local', 'Operario QA', 'operario')
        conductor_u = _actor('conductor_qa@wms-pame.local', 'Conductor QA', 'conductor')
        admin = _actor('admin_qa_lote16@wms-pame.local', 'Admin QA', 'admin')
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

        productos_cache = {}
        tareas_por_pedido = {}

        pedidos_lista = list(CASOS.keys())
        if args.solo:
            solo = [p.strip() for p in args.solo.split(',')]
            pedidos_lista = [p for p in pedidos_lista if p in solo]
            print(f'--- Smoke test: solo {pedidos_lista} ---')
        elif args.desde:
            i = pedidos_lista.index(args.desde)
            pedidos_lista = pedidos_lista[i:]
            print(f'--- Reanudando desde {args.desde} ---')

        print('\n=== FASE 1: Despacho real (244328/142945/142943) por pedido ===')
        for numero_pedido in pedidos_lista:
            caso = CASOS[numero_pedido]
            print(f'--- {numero_pedido} ({caso["cliente"]}) ---')
            tarea, productos = despachar_pedido(
                db, connekta, almacen, ub, productos_cache, numero_pedido, caso, operario.id)
            tareas_por_pedido[numero_pedido] = (tarea, productos)
            if args.disparar_real:
                time.sleep(12)  # Regla 20 — dejar indexar antes de la siguiente consulta

        print('\n=== FASE 2: Ruta unica + confirmar cada parada ===')
        ruta = RutaDespacho.query.filter_by(estado='EN_TRANSITO', conductor_id=conductor.id).first()
        if not ruta:
            ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado='EN_TRANSITO')
            db.session.add(ruta); db.session.flush()
        for numero_pedido in pedidos_lista:
            tarea, _ = tareas_por_pedido[numero_pedido]
            for b in Bulto.query.filter_by(tarea_id=tarea.id).all():
                b.ruta_despacho_id = ruta.id
        db.session.commit()

        from app.services.fe_resolver import resolver_fe, FENoEncontrada
        pendientes_fe = []
        for numero_pedido in pedidos_lista:
            caso = CASOS[numero_pedido]
            tarea, productos = tareas_por_pedido[numero_pedido]
            if not tarea.fe_tipo or not tarea.fe_consec:
                try:
                    resolver_fe(tarea, gateway=connekta)
                except FENoEncontrada as e:
                    print(f'  [SALTADO] {numero_pedido}: sin FE resoluble aun ({e}) — se omite de esta corrida')
                    pendientes_fe.append(numero_pedido)
                    continue
            bruto, iva, neto = obtener_factura_real(connekta, tarea.fe_tipo, tarea.fe_consec)
            print(f'  {numero_pedido} FE {tarea.fe_tipo}-{tarea.fe_consec}: bruto={bruto} iva={iva} neto={neto}')

            devuelve = caso['devuelve']
            motivo = caso['motivo']
            if devuelve:
                total_ped = sum(it['cantidad'] for it in ITEMS_COMUN)
                total_dev = sum(devuelve.values())
                entregado_frac = (total_ped - total_dev) / total_ped
                bruto_p, iva_p, neto_p = bruto * entregado_frac, iva * entregado_frac, neto * entregado_frac
                retencion = monto_de_retencion(motivo, bruto_p, iva_p) if motivo else 0.0
                monto_cobrado = round(neto_p - retencion, 2)
                items_entregados = []
                for it in ITEMS_COMUN:
                    dev = devuelve.get(it['codigo_siesa'], 0)
                    items_entregados.append({
                        'codigo': it['codigo_siesa'], 'nombre': it['nombre'], 'unidad': 'und',
                        'cantidad_pedida': int(it['cantidad']),
                        'cantidad_entregada': int(it['cantidad'] - dev),
                    })
                datos = {
                    'estado_entrega': 'PARCIAL', 'forma_pago': 'EFECTIVO',
                    'monto_cobrado': monto_cobrado,
                    'motivo_descuento': motivo, 'monto_descuento': retencion,
                    'observaciones': f'Cliente devolvio {total_dev} de {int(total_ped)} unidades',
                    'items_entregados': items_entregados,
                }
            elif motivo:
                retencion = monto_de_retencion(motivo, bruto, iva)
                monto_cobrado = round(neto - retencion, 2)
                datos = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                         'monto_cobrado': monto_cobrado, 'motivo_descuento': motivo,
                         'monto_descuento': retencion}
            else:
                datos = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                         'monto_cobrado': neto}

            print(f'  {numero_pedido} confirmar_parada:', {k: v for k, v in datos.items() if k != 'items_entregados'})
            recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, conductor.id, datos)
            db.session.commit()
            tareas_por_pedido[numero_pedido] = (tarea, productos, recaudo_id)

        if pendientes_fe:
            print(f'\n[AVISO] pedidos omitidos por FE aun no indexada: {pendientes_fe} '
                  f'-- reintentar con --solo {",".join(pendientes_fe)} despues de unos minutos')
        pedidos_lista = [p for p in pedidos_lista if p not in pendientes_fe]

        print('\n=== FASE 3: Liquidar ruta (encola RC/DC, arma devoluciones pendientes) ===')
        resumen = LiquidacionService.liquidar_ruta_siesa(ruta.id, admin_id=admin.id)
        print('  resumen:', resumen)
        db.session.commit()

        print('\n=== FASE 4: Confirmar entrada fisica de las devoluciones (dispara NC) ===')
        for numero_pedido in pedidos_lista:
            if not CASOS[numero_pedido]['devuelve']:
                continue
            _, _, recaudo_id = tareas_por_pedido[numero_pedido]
            dev = DevolucionCliente.query.filter_by(
                recaudo_entrega_id=recaudo_id, estado=EstadoDevolucionCliente.ABIERTA).first()
            if dev:
                print(f'  {numero_pedido}: confirmando entrada fisica devolucion {dev.id}...')
                DevolucionClienteService.confirmar_entrada_fisica(dev.id, recepcionista_id=operario.id)
                db.session.commit()
            else:
                print(f'  {numero_pedido}: [AVISO] no se encontro devolucion ABIERTA para recaudo {recaudo_id}')

        print('\n=== FASE 5: Drenar cola real (NC -> RC -> DC/NI) ===')
        for vuelta in range(12):
            for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
                j.proximo_intento = None
            db.session.commit()
            n = procesar_jobs_pendientes()
            print(f'  vuelta {vuelta + 1}: {n} job(s) procesados')
            if n == 0:
                break
            if args.disparar_real:
                time.sleep(5)

        print('\n=== RESULTADO FINAL ===')
        for numero_pedido in pedidos_lista:
            tarea, _, recaudo_id = tareas_por_pedido[numero_pedido]
            recaudo = RecaudoEntrega.query.get(recaudo_id)
            print(f'\n--- {numero_pedido} ---')
            print('  recaudo:', recaudo.to_dict())
        print('\n--- Jobs Siesa ---')
        for j in SiesaJob.query.all():
            print(f'  JOB {j.id} {j.tipo} ({j.referencia_tipo}#{j.referencia_id}) -> {j.estado} (error={j.error_ultimo!r})')


if __name__ == '__main__':
    main()
