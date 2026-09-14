"""
Traslado inter-bodega REAL contra Siesa QA, de punta a punta:
Solicitud -> Enviar -> Aprobar (crea picking) -> Picking confirmado ->
RIT real (174646) -> Packing confirmado -> Compromisos reales (174720) ->
Despacho real (174930, STS desde RIT) -> Recepcion real (173079, ETS).

Base LOCAL AISLADA por par de bodegas (nunca la Postgres de produccion),
Siesa QA real (.env.qa). disparar_dlq_inmediato desactivado.

Item usado: PAPELSP6948 (stock real verificado > 400 und en las 4 bodegas
principales, 2026-09-04). Cantidad por traslado: 5 unidades.

Uso:
    venv/Scripts/python.exe scripts/qa_traslado_real.py NB1 NS1
    venv/Scripts/python.exe scripts/qa_traslado_real.py NB1 NS1 --disparar-real --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CANTIDAD = 5.0
CODIGO_SIESA_ITEM = 'PAPELSP6948'
NOMBRE_ITEM = 'MARCADOR SHARPIE 0TANK GRUESO ROJO'


def _register_pg_stubs(dbapi_conn, _connection_record):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('origen', choices=['NB1', 'NS1', 'NC1', 'PC1'])
    ap.add_argument('destino', choices=['NB1', 'NS1', 'NC1', 'PC1'])
    ap.add_argument('--disparar-real', action='store_true')
    ap.add_argument('--si-de-verdad', action='store_true')
    args = ap.parse_args()
    if args.origen == args.destino:
        print('[ERROR] origen y destino no pueden ser iguales'); sys.exit(1)

    tag = f'{args.origen.lower()}_{args.destino.lower()}'
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', f'qatras_{tag}.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-traslado-real-32-bytes-o-mas')
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
        from app.services.traslado_service import TrasladoService
        from app.services.picking_service import PickingService
        from app.models.almacen import Almacen
        from app.models.producto import Producto
        from app.models.usuario import Usuario
        from app.models.picking import TareaPicking, EstadoPicking
        from app.models.traslado import SolicitudTraslado, EstadoTraslado
        from werkzeug.security import generate_password_hash

        print(f'=== Traslado {args.origen} -> {args.destino} === '
              f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales'); sys.exit(1)
        print(f'bodega_transito={connekta.bodega_transito!r} '
              f'motivo_traslado={connekta.motivo_traslado!r} '
              f'unidad_negocio={connekta.unidad_negocio!r}')

        if args.disparar_real:
            if not args.si_de_verdad:
                print('[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO — TODO lo que sigue es REAL ***')

        # ── Seed mínimo: 2 Almacenes "tienda" (sin UbicacionProducto local
        # a propósito -> TrasladoService cae al camino de picking directo,
        # sin exigir FEFO local) + producto + actores ──
        for bod in (args.origen, args.destino):
            if not Almacen.query.filter_by(bodega_siesa_id=bod).first():
                db.session.add(Almacen(codigo=bod, nombre=f'Bodega {bod}',
                                        bodega_siesa_id=bod, activo=True))
        db.session.commit()

        producto = Producto.query.filter_by(codigo_siesa=CODIGO_SIESA_ITEM).first()
        if not producto:
            producto = Producto(codigo=CODIGO_SIESA_ITEM, nombre=NOMBRE_ITEM,
                                 codigo_siesa=CODIGO_SIESA_ITEM, unidad_negocio_id='99')
            db.session.add(producto)
            db.session.commit()

        def _actor(email, nombre, rol):
            u = Usuario.query.filter_by(email=email).first()
            if not u:
                u = Usuario(nombre=nombre, email=email,
                            password_hash=generate_password_hash('qa123456'),
                            rol=rol, activo=True)
                db.session.add(u); db.session.commit()
            return u

        solicitante = _actor('solicitante_qa@wms-pame.local', 'Solicitante QA', 'admin')
        aprobador = _actor('aprobador_qa@wms-pame.local', 'Aprobador QA', 'admin')
        operario = _actor('operario_traslado_qa@wms-pame.local', 'Operario Traslado QA', 'operario')

        # ── 1) Crear + enviar solicitud ──
        print('--- Solicitud ---')
        sol = TrasladoService.crear_solicitud(
            solicitante_id=solicitante.id, bodega_destino=args.destino,
            nombre_punto_venta=args.destino,
            items=[{'producto_id': producto.id, 'cantidad_solicitada': CANTIDAD}],
            bodega_origen=args.origen)
        TrasladoService.enviar_solicitud(sol.id)
        print(f'  {sol.codigo} ENVIADA, modo_transferencia={sol.modo_transferencia}')

        # ── 2) Aprobar (crea TareaPicking) ──
        print('--- Aprobar ---')
        from app.models.traslado import ItemSolicitudTraslado
        items_ids = [it.id for it in ItemSolicitudTraslado.query.filter_by(solicitud_id=sol.id).all()]
        sol = TrasladoService.aprobar_solicitud(
            sol.id, aprobador.id,
            items_aprobados=[{'id': i, 'cantidad_aprobada': CANTIDAD} for i in items_ids],
            operario_id=operario.id)
        print(f'  estado={sol.estado}')

        # ── 3) Picking (operario) — el ÚLTIMO confirmar_picking dispara solo
        # el RIT 174646 real y avanza EN_PICKING → EN_PACKING (ver
        # PickingService.confirmar_picking, auto-trigger para TRASLADO) — no
        # se llama confirmar_picking_traslado a mano, sería un segundo POST.
        print('--- Picking (dispara RIT 174646 real al completar) ---')
        tareas = TareaPicking.query.filter_by(
            referencia_documento=sol.codigo, tipo_documento='TRASLADO').all()
        for t in tareas:
            PickingService.iniciar_picking(t.id, operario.id)
            PickingService.confirmar_picking(t.id, CANTIDAD, operario.id)
            print(f'  picking tarea {t.id} confirmada')
        db.session.commit()
        sol = SolicitudTraslado.query.get(sol.id)
        print(f'  estado={sol.estado} rit_consec={sol.siesa_requisicion_consec} '
              f'siesa_error={sol.siesa_error!r}')

        # ── 5) Confirmar packing traslado -> Compromisos 174720 real ──
        print('--- Confirmar packing traslado (Compromisos 174720) ---')
        sol = TrasladoService.confirmar_packing_traslado(sol.id, operario.id)
        print(f'  estado={sol.estado} compromisos_ok={sol.siesa_compromisos_ok} '
              f'siesa_error={sol.siesa_error!r}')

        # ── 6) Despachar -> STS 174930/173076 real ──
        print('--- Despachar (STS) ---')
        sol = TrasladoService.despachar(sol.id)
        print(f'  estado={sol.estado} salida_consec={sol.siesa_salida_consec} '
              f'siesa_error={sol.siesa_error!r}')

        # ── 7) Confirmar recepción -> ETS 173079 real ──
        print('--- Confirmar recepción (ETS 173079) ---')
        sol = TrasladoService.confirmar_recepcion(
            sol.id, aprobador.id,
            items_recibidos=[{'id': i, 'cantidad_recibida': CANTIDAD} for i in items_ids])
        print(f'  estado={sol.estado} entrada_consec={sol.siesa_entrada_consec} '
              f'siesa_error={sol.siesa_error!r}')

        print('\n=== RESULTADO FINAL ===')
        sol = SolicitudTraslado.query.get(sol.id)
        print(f'codigo={sol.codigo} estado={sol.estado}')
        print(f'  RIT (174646)       : {sol.siesa_requisicion_consec}')
        print(f'  Compromisos (174720): {sol.siesa_compromisos_ok}')
        print(f'  Salida STS (174930) : {sol.siesa_salida_consec}')
        print(f'  Entrada ETS (173079): {sol.siesa_entrada_consec}')
        print(f'  siesa_error final   : {sol.siesa_error!r}')


if __name__ == '__main__':
    main()
