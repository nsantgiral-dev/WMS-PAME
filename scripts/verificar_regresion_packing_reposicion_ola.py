"""
Regresion real (app Flask real + rutas HTTP reales + DB SQLite aislada en
memoria, sin mocks de negocio) para los cambios del 2026-09-09 en Packing,
Reposicion y Ola Predictiva. A diferencia de los `qa_*_real.py` de esta
carpeta, NO pega contra Siesa QA -- estas tres cosas no dependen de Siesa,
asi que se verifican herméticamente (igual patron que `tests/conftest.py`):
SQLite en memoria, CONNEKTA_IKEY/ITOKEN vacios (fuerza modo simulacion),
sin scheduler.

Cubre:

1. `packing_service.escanear_item()` — `verificado` ahora exige
   `cantidad_real >= cantidad_esperada`, no solo `is not None` (que marcaba
   el item como listo tras el PRIMER escaneo aunque faltaran unidades).
   Probado via la ruta real `/api/packing/<id>/escanear`.
2. `reposicion.js` — se le quito el badge "Pendiente Siesa" (nunca se
   resolvia: reposicion RESERVA→PICKING es 100% WMS desde el commit
   9447464, `siesa_enviado` nunca pasa a `True`). Se confirma que el
   contrato de `/api/reposicion/pendientes` no cambio — es un cambio
   puramente de frontend.
3. `ola_predictiva_service.py` — se elimino `_stock_disponible_picking()`
   (funcion muerta, cero callers). Se ejerce el resto del modulo via la
   ruta real `/api/reposicion/pre-verificar-ola`, con y sin deficit.

`siesa_job_service.py` (comentario agregado en una rama ya muerta, sin
comportamiento nuevo) no tiene nada que probar aqui a proposito.

Uso:
    venv/Scripts/python.exe scripts/verificar_regresion_packing_reposicion_ola.py
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ['SECRET_KEY'] = 'test-secret-key-de-32-bytes-o-mas-para-hmac-sha256'
os.environ.pop('CONNEKTA_IKEY', None)
os.environ.pop('CONNEKTA_ITOKEN', None)
os.environ['CONNEKTA_MODO_SIMULACION'] = 'true'

from app import create_app
from app.extensions import db

FALLAS = []


def check(nombre, cond, detalle=''):
    estado = 'PASS' if cond else 'FAIL'
    print(f'[{estado}] {nombre}' + (f' -- {detalle}' if detalle and not cond else ''))
    if not cond:
        FALLAS.append(nombre)


def main():
    app = create_app()
    app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'JWT_ACCESS_TOKEN_EXPIRES': False,
    })

    with app.app_context():
        from sqlalchemy import event as _sa_event

        def _pg_stubs(dbapi_conn, _rec):
            dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
            dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
            dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
            dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)

        _sa_event.listen(db.engine, 'connect', _pg_stubs)
        db.create_all()

        from app.models.almacen import Almacen
        from app.models.producto import Producto
        from app.models.usuario import Usuario
        from app.models.ubicacion import Ubicacion
        from app.models.inventario import UbicacionProducto
        from app.models.packing import TareaPacking, ItemPacking
        from app.models.tarea_reposicion import TareaReposicion, EstadoReposicion
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        almacen = Almacen(codigo='ALM-TEST', nombre='Almacen Test',
                           bodega_siesa_id='NB1', activo=True)
        producto = Producto(codigo='PROD-001', nombre='Resma Carta 500h',
                             codigo_siesa='PROD-001', activo=True)
        db.session.add_all([almacen, producto])
        db.session.commit()

        admin = Usuario(nombre='Admin Test', email='admin@test.com',
                         password_hash=generate_password_hash('test123'),
                         rol='admin', almacen_id=almacen.id, activo=True)
        db.session.add(admin)
        db.session.commit()
        token = create_access_token(identity=str(admin.id))
        headers = {'Authorization': f'Bearer {token}'}

        client = app.test_client()

        # ------------------------------------------------------------------
        # 1) PACKING — escanear_item via la ruta real /api/packing/<id>/escanear
        # ------------------------------------------------------------------
        print('\n=== 1. Packing: escanear_item (fix verificado) ===')
        tarea = TareaPacking(codigo='PCK-TEST-001', almacen_id=almacen.id,
                              estado='EN_PROCESO')
        db.session.add(tarea)
        db.session.commit()
        item = ItemPacking(tarea_id=tarea.id, producto_id=producto.id,
                            cantidad_esperada=10, cantidad_real=0, verificado=False)
        db.session.add(item)
        db.session.commit()

        # Escaneo PARCIAL (7 de 10) — antes del fix esto marcaba verificado=True
        r1 = client.post(f'/api/packing/{tarea.id}/escanear', headers=headers,
                          json={'producto_id': producto.id, 'cantidad_real': 7})
        check('escaneo parcial responde 200', r1.status_code == 200,
              f'status={r1.status_code} body={r1.get_json()}')
        j1 = r1.get_json() or {}
        item_dict = j1.get('item', {})
        check('escaneo parcial (7/10) NO queda verificado (bug corregido)',
              item_dict.get('verificado') is False,
              f'verificado={item_dict.get("verificado")!r}')
        check('items_pendientes=1 tras escaneo parcial',
              j1.get('items_pendientes') == 1, f'items_pendientes={j1.get("items_pendientes")}')

        # Escaneo COMPLETO (10 de 10)
        r2 = client.post(f'/api/packing/{tarea.id}/escanear', headers=headers,
                          json={'producto_id': producto.id, 'cantidad_real': 10})
        check('escaneo completo responde 200', r2.status_code == 200,
              f'status={r2.status_code} body={r2.get_json()}')
        j2 = r2.get_json() or {}
        check('escaneo completo (10/10) SI queda verificado',
              j2.get('item', {}).get('verificado') is True,
              f'verificado={j2.get("item", {}).get("verificado")!r}')
        check('items_pendientes=0 tras escaneo completo',
              j2.get('items_pendientes') == 0, f'items_pendientes={j2.get("items_pendientes")}')

        # Guard existente (no tocado): no debe dejar exceder cantidad_esperada
        r3 = client.post(f'/api/packing/{tarea.id}/escanear', headers=headers,
                          json={'producto_id': producto.id, 'cantidad_real': 11})
        check('exceso de cantidad sigue bloqueado (guard preexistente intacto)',
              r3.status_code == 400, f'status={r3.status_code} body={r3.get_json()}')

        # ------------------------------------------------------------------
        # 2) REPOSICION — contrato de /api/reposicion/pendientes sin cambios
        # ------------------------------------------------------------------
        print('\n=== 2. Reposicion: contrato de la API sin cambios (solo se quito el badge en JS) ===')
        ub_res = Ubicacion(codigo='RES-01-A', almacen_id=almacen.id, tipo_zona='RESERVA', activo=True)
        ub_pik = Ubicacion(codigo='PIK-01-A', almacen_id=almacen.id, tipo_zona='PICKING',
                            stock_minimo=5, stock_maximo=200, activo=True)
        db.session.add_all([ub_res, ub_pik])
        db.session.commit()

        tarea_rep = TareaReposicion(
            codigo='REP-TEST-001', producto_id=producto.id, almacen_id=almacen.id,
            cantidad_unidades=50, ubicacion_reserva_id=ub_res.id,
            ubicacion_picking_id=ub_pik.id, estado=EstadoReposicion.COMPLETADA,
            unidades_movidas=50,
        )
        db.session.add(tarea_rep)
        db.session.commit()
        check('siesa_enviado por defecto sigue False (reposicion 100% WMS)',
              tarea_rep.siesa_enviado is False, f'siesa_enviado={tarea_rep.siesa_enviado!r}')

        r4 = client.get('/api/reposicion/pendientes?estado=COMPLETADA', headers=headers)
        check('GET /api/reposicion/pendientes responde 200', r4.status_code == 200,
              f'status={r4.status_code} body={r4.get_json()}')
        j4 = r4.get_json() or {}
        tareas_json = j4.get('tareas', [])
        check('la tarea creada aparece en el listado', len(tareas_json) == 1,
              f'tareas={tareas_json}')
        if tareas_json:
            check('el campo siesa_enviado sigue en el JSON (contrato backend intacto)',
                  'siesa_enviado' in tareas_json[0], f'keys={list(tareas_json[0].keys())}')

        # ------------------------------------------------------------------
        # 3) OLA PREDICTIVA — /api/reposicion/pre-verificar-ola tras borrar
        #    _stock_disponible_picking() (funcion muerta, sin callers)
        # ------------------------------------------------------------------
        print('\n=== 3. Ola predictiva: pre-verificar-ola sigue funcionando tras borrar la funcion muerta ===')
        up = UbicacionProducto(producto_id=producto.id, ubicacion_id=ub_pik.id,
                                cantidad=20, reservado=0)
        db.session.add(up)
        db.session.commit()

        # Caso A: demanda cabe en el stock disponible (20) -> sin deficit
        r5 = client.post('/api/reposicion/pre-verificar-ola', headers=headers,
                          json={'almacen_id': almacen.id,
                                'items': [{'producto_id': producto.id, 'cantidad': 15}]})
        check('pre-verificar-ola (sin deficit) responde 200', r5.status_code == 200,
              f'status={r5.status_code} body={r5.get_json()}')
        j5 = r5.get_json() or {}
        check('sin deficit cuando la demanda cabe en el disponible',
              j5.get('hay_deficit') is False, f'resp={j5}')

        # Caso B: demanda supera el disponible -> debe generar reposicion preventiva
        r6 = client.post('/api/reposicion/pre-verificar-ola', headers=headers,
                          json={'almacen_id': almacen.id,
                                'items': [{'producto_id': producto.id, 'cantidad': 100}]})
        check('pre-verificar-ola (con deficit) responde 200', r6.status_code == 200,
              f'status={r6.status_code} body={r6.get_json()}')
        j6 = r6.get_json() or {}
        check('detecta deficit cuando la demanda supera el disponible',
              j6.get('hay_deficit') is True, f'resp={j6}')

        import app.services.ola_predictiva_service as ops
        check('la funcion muerta ya no existe en el modulo',
              not hasattr(ops, '_stock_disponible_picking'))

    print('\n' + '=' * 60)
    if FALLAS:
        print(f'RESULTADO: {len(FALLAS)} verificacion(es) fallaron: {FALLAS}')
        sys.exit(1)
    else:
        print('RESULTADO: todas las verificaciones reales pasaron.')
        sys.exit(0)


if __name__ == '__main__':
    main()
