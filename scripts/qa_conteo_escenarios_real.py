"""
Conteo cíclico REAL contra Siesa QA: cuatro ajustes 142951 que dejan el
inventario de Siesa QA como estaba (tope duro: 5 POST por corrida).

  1. SOBRANTE   PAPELSP9830 NB1: CC1 = CC2 = Siesa + 2  → AJ-ENT 2 (clase 63, motivo 01), sale solo
  2. FALTANTE   PAPELSP9830 NB1: CC1 = CC2 = Siesa − 2  → AJ-SAL 2 (clase 63, motivo 02), sale solo
  3. ENTRADA    PAPELSP7879 NB1 (Siesa SIN fila): WMS 1, CC1 = CC2 = 1 → la firma el admin,
                costo de otras bodegas → ADI clase 61 / concepto 601 / motivo 01 con f470_costo_prom_uni
  4. NETEO      PAPELSP7879 NB1: CC1 = CC2 = 0 → AJ-SAL 1 (clase 63), deja Siesa como estaba

Cada caso va por los servicios del WMS (conteo manual → CC1 → recuento propio
→ CC2 de otra persona → ajuste automático o aprobación → job AJUSTE_CONTEO
ejecutado con el gateway REAL). Antes y después de cada POST se lee InvFecha
(cantidad y costo), se espera la Regla 20, y se compara Siesa QA con el WMS
local del SKU.

Guardas:
- `.env.qa` se carga para las credenciales y DESPUÉS se fuerza una base SQLite
  local desechable (el `.env.qa` trae el DATABASE_URL de QA: nunca se usa).
- Antes de cualquier POST se verifica que la URL sea serviciosqa.siesacloud.com;
  si no, se aborta. Más de 5 POST → se aborta.
- MODO_ENSAYO se apaga solo dentro del proceso y solo con --si-de-verdad.
- Un caso que falla NO se reintenta: se imprime el mensaje exacto de Siesa y
  se detiene la corrida.

Uso:
    venv/bin/python scripts/qa_conteo_escenarios_real.py                   # ensayo (sin POST)
    venv/bin/python scripts/qa_conteo_escenarios_real.py --si-de-verdad    # POST reales a QA
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

HOST_QA = 'serviciosqa.siesacloud.com'
MAX_POST = 5
ESPERA_REGLA_20 = 15


def _stubs(dbapi_conn, _rec):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--si-de-verdad', action='store_true')
    ap.add_argument('--casos', default='1,2,3,4')
    args = ap.parse_args()
    casos = {int(x) for x in args.casos.split(',') if x}

    from dotenv import load_dotenv
    env_qa = os.path.join(REPO, '.env.qa')
    if not os.path.exists(env_qa):
        env_qa = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(REPO))), '.env.qa')
    load_dotenv(env_qa, override=True)
    # DESPUÉS de cargar .env.qa: la base es local y desechable, siempre.
    db_path = os.path.join(tempfile.mkdtemp(prefix='qa_conteo_esc_'), 'conteo.db')
    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-escenarios-real-32-bytes-x')
    for k in ('RAILWAY_ENVIRONMENT_NAME', 'RAILWAY_SERVICE_ID', 'SIESA_VENTANA'):
        os.environ.pop(k, None)
    os.environ['MODO_ENSAYO'] = 'true'          # en el proceso; el archivo no se toca

    from app import create_app
    from app.extensions import db
    app = create_app()
    assert app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:///'), 'base no local: aborto'
    with app.app_context():
        from sqlalchemy import event
        event.listen(db.engine, 'connect', _stubs)
        db.create_all()
        import app.services.siesa_job_service as sjs
        sjs.disparar_dlq_inmediato = lambda *a, **k: None
        from app.models.almacen import Almacen
        from app.models.conteo import SesionConteo
        from app.models.inventario import UbicacionProducto
        from app.models.producto import Producto
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        from app.models.ubicacion import Ubicacion
        from app.models.usuario import Usuario
        from app.services.connekta_gateway import ConnektaGateway, connekta
        from app.services.conteo_service import ConteoService
        from werkzeug.security import generate_password_hash

        url = getattr(connekta, 'base_url', '') or ''
        print(f'Connekta: {url}  simulación={connekta.modo_simulacion}  base={db_path}')
        if HOST_QA not in url or connekta.modo_simulacion:
            print('[ABORTO] no es Siesa QA o no hay credenciales'); sys.exit(2)
        connekta.motivo_entrada_inventario = '01'        # concepto 0601 motivo 01 (QA 2026-09-29)

        posts = []
        _post_real = ConnektaGateway._post

        def _post_guardado(self, id_conector, nombre, payload, url=None, extra_params=None):
            base = getattr(self, 'base_url', '') or ''
            if HOST_QA not in base or (url and HOST_QA not in url):
                raise SystemExit(f'[ABORTO] POST fuera de Siesa QA: {base} {url}')
            if len(posts) >= MAX_POST:
                raise SystemExit('[ABORTO] tope de 5 POST alcanzado')
            posts.append(json.loads(json.dumps(payload)))
            return _post_real(self, id_conector, nombre, payload, url=url, extra_params=extra_params)
        ConnektaGateway._post = _post_guardado
        if args.si_de_verdad:
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO en este proceso: los 142951 van a Siesa QA ***')

        alm = Almacen(codigo='NB1', nombre='Neiva Bodega CD', bodega_siesa_id='NB1',
                      centro_op_siesa='003', activo=True)
        db.session.add(alm)
        db.session.flush()
        general = Ubicacion(codigo='SIESA-GENERAL', almacen_id=alm.id, tipo_zona='GENERAL',
                            stock_minimo=0, stock_maximo=999999, secuencia_ruteo=1, activo=True)
        cross = Ubicacion(codigo='CROSS-DOCK', almacen_id=alm.id, tipo_zona='GENERAL',
                          stock_minimo=0, stock_maximo=999999, secuencia_ruteo=2, activo=True)
        db.session.add_all([general, cross])

        def persona(email, rol, **kw):
            u = Usuario(nombre=email.split('@')[0], email=email, rol=rol, activo=True,
                        password_hash=generate_password_hash('x'), almacen_id=alm.id,
                        ultima_senal_at=datetime.utcnow(), **kw)
            db.session.add(u)
            db.session.flush()
            return u
        ana = persona('ana_qa_esc@wms.local', 'operario', puede_picar=True)
        beto = persona('beto_qa_esc@wms.local', 'operario', puede_picar=True)
        admin = persona('admin_qa_esc@wms.local', 'admin')
        db.session.commit()

        def foto(ref):
            f = ConteoService.consultar_foto_siesa(ref, bodega='NB1')
            return None if f is None else {k: f.get(k) for k in
                                           ('existencia', 'cant_pos', 'salida_sin_conf', 'costo_prom_uni')}

        def producto(ref, wms, lugar):
            p = Producto.query.filter_by(codigo=ref).first()
            if p is None:
                p = Producto(codigo=ref, nombre=ref, codigo_siesa=ref, unidad_negocio_id='001', activo=True)
                db.session.add(p)
                db.session.flush()
            for f in UbicacionProducto.query.filter_by(producto_id=p.id).all():
                f.cantidad = 0
            fila = UbicacionProducto.query.filter_by(producto_id=p.id, ubicacion_id=lugar.id).first()
            if fila is None:
                db.session.add(UbicacionProducto(ubicacion_id=lugar.id, producto_id=p.id,
                                                 cantidad=wms, reservado=0, bloqueado=0))
            else:
                fila.cantidad = wms
            db.session.commit()
            return p

        def contar(sid, u, n):
            ConteoService.obtener_tarea_operario(sid, u.id)
            return ConteoService.registrar_conteo(sid, u.id, n, cero_confirmado=(n == 0))

        def ciclo(caso, ref, contado_de, wms_de, lugar, aprueba=None):
            print(f'\n=== Caso {caso}: {ref} ===')
            antes = foto(ref)
            print(f'  Siesa ANTES: {antes}')
            if antes is None:
                print('  [ALTO] Siesa no dio foto: no se sigue'); return False
            contado = contado_de(antes)
            p = producto(ref, wms_de(antes), lugar)
            r = ConteoService.crear_conteo_manual(alm.id, ref)
            raiz = SesionConteo.query.filter(SesionConteo.codigo.in_(r['codigos'])).one()
            r1 = contar(raiz.id, ana, contado)
            if r1['resultado'] == 'RECONTAR_TU':
                r1 = ConteoService.registrar_conteo(raiz.id, ana.id, contado, cero_confirmado=(contado == 0))
            print(f'  CC1 ({contado}): {r1["resultado"]}')
            if r1['resultado'] == 'SEGUNDO_CONTEO':
                cc2 = db.session.get(SesionConteo, r1['segundo_conteo_id'])
                quien = db.session.get(Usuario, cc2.operario_id) if cc2.operario_id else beto
                r2 = contar(cc2.id, quien, contado)
                print(f'  CC2 ({contado}, {quien.nombre}): {r2["resultado"]}')
            elif r1['resultado'] != 'DENTRO_TOLERANCIA':
                print('  [ALTO] resultado inesperado del CC1'); return False
            db.session.expire_all()
            raiz = db.session.get(SesionConteo, raiz.id)
            print(f'  raíz {raiz.estado} teórico={raiz.teorico_siesa} sin_fila={raiz.sin_fila_en_siesa} '
                  f'por_tolerancia={raiz.ajuste_por_tolerancia}')
            if aprueba is not None and raiz.estado == 'DESCUADRE':
                ConteoService.confirmar_ajuste(raiz.id, aprueba.id)
                db.session.commit()
                print(f'  aprobado por {aprueba.nombre}')
            jobs = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=raiz.id,
                                            estado=EstadoSiesaJob.PENDIENTE).all()
            if len(jobs) != 1:
                print(f'  [ALTO] jobs de ajuste: {len(jobs)} (bloqueo: '
                      f'{ConteoService.motivo_bloqueo_ajuste(raiz)})'); return False
            job = jobs[0]
            pj = json.loads(job.payload)
            print(f'  job: {pj["motivo_codigo"]} {pj["cantidad"]} costo={pj.get("costo_unitario")} '
                  f'({pj.get("costo_fuente")})')
            n_antes = len(posts)
            try:
                res = sjs._ejecutar_job(job)
            except SystemExit:
                raise
            except Exception as e:
                print(f'  [SIESA RECHAZÓ] {type(e).__name__}: {e}')
                return False
            job.estado = EstadoSiesaJob.COMPLETADO
            db.session.commit()
            if len(posts) > n_antes:
                pl = posts[-1]
                d, m = pl['Documentos'][0], pl['Movimientos'][0]
                print(f'  142951: tipo={d["f350_id_tipo_docto"]} clase={d["f350_id_clase_docto"]} '
                      f'concepto={m["f470_id_concepto"]} motivo={m["f470_id_motivo"]} '
                      f'cant={m["f470_cant_base"]} costo={m["f470_costo_prom_uni"]} bodega={m["f470_id_bodega"]} '
                      f'co={d["f350_id_co"]} notas={d["f350_notas"]}')
            print(f'  respuesta Siesa: {res}')
            time.sleep(ESPERA_REGLA_20)
            despues = foto(ref)
            db.session.expire_all()
            wms = ConteoService.existencia_wms_del_sku(p.id, alm.id)
            raiz = db.session.get(SesionConteo, raiz.id)
            esperado = antes['existencia'] + (pj['cantidad'] if pj['motivo_codigo'] == 'AJ-ENT' else -pj['cantidad'])
            print(f'  Siesa DESPUÉS: {despues}  (esperado existencia {esperado})')
            print(f'  WMS local DESPUÉS: {wms}   sesión {raiz.estado}')
            print(f'  Siesa == esperado: {despues and despues["existencia"] == esperado}   '
                  f'WMS == Siesa: {despues and wms == despues["existencia"]}')
            return True

        seguir = True
        if 1 in casos and seguir:
            seguir = ciclo(1, 'PAPELSP9830', lambda f: int(f['existencia'] - f['cant_pos']) + 2,
                           lambda f: int(f['existencia']), general)
        if 2 in casos and seguir:
            seguir = ciclo(2, 'PAPELSP9830', lambda f: int(f['existencia'] - f['cant_pos']) - 2,
                           lambda f: int(f['existencia']), general)
        if 3 in casos and seguir:
            seguir = ciclo(3, 'PAPELSP7879', lambda f: 1, lambda f: 1, cross, aprueba=admin)
        if 4 in casos and seguir:
            seguir = ciclo(4, 'PAPELSP7879', lambda f: 0, lambda f: int(f['existencia']), general)
        print(f'\nPOST enviados: {len(posts)} (tope {MAX_POST})')


if __name__ == '__main__':
    main()
