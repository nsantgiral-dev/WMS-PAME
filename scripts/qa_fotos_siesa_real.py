#!/usr/bin/env python
"""
Verificación EN VIVO de las fotos diarias de Siesa contra Siesa QA. **Solo GET.**

Corre la foto de ventas de un CO y un día, la de cartera y el costo de una
bodega contra Siesa QA real (`.env.qa`), sobre una base SQLite desechable, y
reporta filas, páginas y completitud. No escribe nada en Siesa:

- `MODO_ENSAYO=true` forzado en el proceso (los POST quedan bloqueados en el
  gateway) **y** `ConnektaGateway._post` reemplazado por uno que revienta — dos
  cerrojos, porque un script de verificación que puede escribir no es un
  script de verificación;
- `DATABASE_URL` forzado a SQLite: se niega a correr contra cualquier otra
  cosa (el `.env.qa` trae la base de Railway y NO se usa);
- las credenciales se cargan en el proceso y no se imprimen.

Siesa no opera después de ~8 p. m. Bogotá (Regla 14): fuera de 7:00–19:30 el
script avisa y sale.

Uso:
    venv/bin/python scripts/qa_fotos_siesa_real.py --co 003 --dia 2026-09-22
    venv/bin/python scripts/qa_fotos_siesa_real.py --co 003 --dia 2026-09-22 \\
        --bodega-costo NB1 --sin-cartera
"""
import argparse
import os
import sys
import tempfile
from datetime import date

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)


def _cargar_entorno(db_path: str):
    from dotenv import dotenv_values
    ruta = os.path.join(RAIZ, '.env.qa')
    if not os.path.exists(ruta):
        # En un worktree el .env.qa vive en el repo principal.
        ruta = os.path.join(RAIZ.split('/.claude/worktrees/')[0], '.env.qa')
    for k, v in dotenv_values(ruta).items():
        if k == 'DATABASE_URL' or v is None:
            continue
        os.environ[k] = v
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    os.environ['MODO_ENSAYO'] = 'true'
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-fotos-siesa-32-bytes-o-mas-xxxxxxx')
    if not os.environ['DATABASE_URL'].startswith('sqlite:///'):
        raise SystemExit('DATABASE_URL no es SQLite: este script no corre contra otra base')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--co', default='003')
    p.add_argument('--dia', required=True, help='YYYY-MM-DD, el día del documento')
    p.add_argument('--bodega-costo', default='NB1',
                   help='bodega a costear por InvFecha ("" para saltar)')
    p.add_argument('--sin-cartera', action='store_true')
    p.add_argument('--con-stock', default='', metavar='BODEGA',
                   help='descarga stock_siesa con la consulta de producción '
                        '(solo GET, a la base local) y fotografía esa bodega')
    p.add_argument('--db', default=None, help='ruta del SQLite (por defecto, temporal)')
    args = p.parse_args()

    db_path = args.db or os.path.join(tempfile.mkdtemp(prefix='qa_fotos_'), 'fotos.db')
    _cargar_entorno(db_path)

    from app import create_app
    from app.extensions import db
    from app.services import fotos_siesa_service as fotos
    from app.services.connekta_gateway import ConnektaGateway, connekta

    def _sin_post(self, *a, **k):
        raise RuntimeError('qa_fotos_siesa_real: un POST no debería ocurrir nunca acá')
    ConnektaGateway._post = _sin_post

    app = create_app()
    with app.app_context():
        db.create_all()
        assert connekta.modo_ensayo, 'MODO_ENSAYO no quedó activo'
        if connekta.modo_simulacion:
            raise SystemExit('Sin credenciales de Connekta (.env.qa): nada que verificar')
        if not fotos.ventana_abierta():
            raise SystemExit('Fuera de la ventana de Siesa (7:00–19:30 Bogotá). Regla 14.')

        dia = date.fromisoformat(args.dia)
        print(f'\n  FOTOS DE SIESA — verificación en vivo (solo GET)')
        print(f'  base local: {db_path}\n')

        v = fotos.fotografiar_ventas(args.co, dia, connekta)
        filas = fotos.filas_vigentes('VENTAS', args.co, dia)
        print(f'  VENTAS CO {args.co} {dia}: completa={v["completa"]} filas={v["filas"]} '
              f'páginas={v["paginas"]} motivo={v["motivo"]}')
        if filas is not None:
            docs = {(f.tipo_docto, f.consec_docto) for f in filas}
            neto = sum((f.vlr_neto or 0) for f in filas)
            anul = sum(1 for f in filas if f.estado_docto == 9)
            print(f'    documentos={len(docs)} neto=${neto:,.2f} líneas_anuladas={anul} '
                  f'pedidos_con_clave={len({f.pedido_clave for f in filas if f.pedido_clave})}')

        if not args.sin_cartera:
            c = fotos.fotografiar_cartera(fotos.dia_operativo(), connekta)
            print(f'  CARTERA 1305 abierta: completa={c["completa"]} filas={c["filas"]} '
                  f'páginas={c["paginas"]} motivo={c["motivo"]}')
            filas = fotos.filas_vigentes('CARTERA', 'TODAS', fotos.dia_operativo())
            if filas is not None:
                saldo = sum((f.saldo or 0) for f in filas)
                vencidas = sum(1 for f in filas if (f.dias_vencido or 0) > 0)
                print(f'    saldo=${saldo:,.2f} documentos_vencidos={vencidas}')

        if args.con_stock:
            from app.services import inventario_siesa_service as inv
            datos = inv._descargar_una_pasada_custom()
            if datos is None:
                print('  STOCK: la descarga de stock_siesa falló — no hay foto que tomar')
            else:
                inv._guardar_stock_en_bd(datos)
                s = fotos.fotografiar_stock(args.con_stock, fotos.dia_operativo(),
                                            connekta, costo=True)
                import json as _json
                from app.models.fotos_siesa import FotoCorrida
                det = _json.loads(FotoCorrida.query.filter_by(
                    run_id=s['run_id'], tipo='STOCK').one().detalle or '{}')
                print(f'  STOCK {args.con_stock}: completa={s["completa"]} '
                      f'filas={s["filas"]} motivo={s["motivo"]} '
                      f'bodegas_descargadas={len(datos)} costo={det.get("costo")} '
                      f'rezagadas={det.get("filas_rezagadas")}')
            args.bodega_costo = ''   # el costo ya se trajo con la foto

        if args.bodega_costo:
            costos, lectura = fotos._costos_de_bodega(connekta, args.bodega_costo)
            con_costo = sum(1 for cu, _ in costos.values() if cu is not None)
            print(f'  COSTO InvFecha {args.bodega_costo}: completo={lectura.completa} '
                  f'filas={len(lectura.filas)} páginas={lectura.paginas} '
                  f'referencias={len(costos)} con_unitario={con_costo} '
                  f'motivo={lectura.motivo}')
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
