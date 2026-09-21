"""Copia `almacenes` y `usuarios` de producción a la base de QA.

Producción SOLO SE LEE (sesión read-only). Se escribe únicamente en QA.
Conserva ids y hashes de contraseña: los usuarios entran con la misma clave.

    SRC_URL='<prod>' DST_URL='<qa, DATABASE_PUBLIC_URL>' \
        venv/Scripts/python scripts/qa_copiar_usuarios.py            # simulacro
    ... scripts/qa_copiar_usuarios.py --ejecutar --host <host-destino>

Con `--unidades` copia en cambio la unidad de negocio: el mapeo
`siesa_mapeo_unidades` (upsert) y `productos.unidad_negocio_id` (solo donde QA la
tiene vacía). El sync de QA inserta el mapeo con la unidad en blanco, y
`aprobar_solicitud` rechaza cualquier producto sin unidad de negocio.

Sin --ejecutar no escribe nada. Con --ejecutar hay que escribir el host de
destino (mismo criterio que scripts de borrado: repetir un comando del
historial no debe bastar).
"""
import argparse
import os
import sys

from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.engine import make_url

TABLAS = ['almacenes', 'usuarios']  # orden por FK: usuarios.almacen_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ejecutar', action='store_true')
    ap.add_argument('--host', default='', help='host del destino, para confirmar')
    ap.add_argument('--unidades', action='store_true',
                    help='copiar unidad de negocio (mapeo + productos) en vez de usuarios')
    args = ap.parse_args()

    src_url, dst_url = os.environ.get('SRC_URL'), os.environ.get('DST_URL')
    if not src_url or not dst_url:
        sys.exit('Faltan SRC_URL y/o DST_URL.')
    src_u, dst_u = make_url(src_url), make_url(dst_url)
    if (src_u.host, src_u.port) == (dst_u.host, dst_u.port):
        sys.exit('ABORTADO: origen y destino son el mismo servidor.')
    print(f'origen : {src_u.host}:{src_u.port}/{src_u.database} (solo lectura)')
    print(f'destino: {dst_u.host}:{dst_u.port}/{dst_u.database}')

    src = create_engine(src_url, connect_args={'options': '-c default_transaction_read_only=on'})
    dst = create_engine(dst_url)

    if args.unidades:
        return copiar_unidades(src, dst, dst_u, args)

    with src.connect() as s, dst.begin() as d:
        for nombre in TABLAS:
            t_src = Table(nombre, MetaData(), autoload_with=s)
            t_dst = Table(nombre, MetaData(), autoload_with=d)
            filas = [dict(r._mapping) for r in s.execute(t_src.select())]
            existentes = d.execute(text(f'SELECT count(*) FROM {nombre}')).scalar()
            cols_comunes = {c.name for c in t_dst.columns}
            filas = [{k: v for k, v in f.items() if k in cols_comunes} for f in filas]
            print(f'{nombre}: {len(filas)} filas en origen, {existentes} en destino')
            if existentes:
                sys.exit(f'ABORTADO: {nombre} ya tiene filas en destino.')
            if not args.ejecutar:
                continue
            if args.host != dst_u.host:
                sys.exit(f'Para escribir pasa --host {dst_u.host}')
            if filas:
                d.execute(t_dst.insert(), filas)
                d.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{nombre}','id'), "
                    f"(SELECT COALESCE(MAX(id),1) FROM {nombre}))"))
        if not args.ejecutar:
            d.rollback() if hasattr(d, 'rollback') else None
            print('Simulacro: no se escribió nada. Usa --ejecutar --host <destino>.')
        else:
            print('Listo.')


def copiar_unidades(src, dst, dst_u, args):
    with src.connect() as s, dst.begin() as d:
        mapeo = [dict(r._mapping) for r in s.execute(text(
            "SELECT tipo_inv_siesa, unidad_negocio_id, descripcion FROM siesa_mapeo_unidades"))]
        prods = [dict(c=r[0], u=r[1]) for r in s.execute(text(
            "SELECT codigo_siesa, unidad_negocio_id FROM productos "
            "WHERE coalesce(unidad_negocio_id,'') <> '' AND codigo_siesa IS NOT NULL"))]
        vacios = d.execute(text(
            "SELECT count(*) FROM productos WHERE coalesce(unidad_negocio_id,'') = ''")).scalar()
        print(f'mapeo en origen: {len(mapeo)} filas | productos con unidad en origen: {len(prods)}')
        print(f'productos con unidad vacía en destino: {vacios}')
        if not args.ejecutar:
            d.rollback() if hasattr(d, 'rollback') else None
            print('Simulacro: no se escribió nada. Usa --unidades --ejecutar --host <destino>.')
            return
        if args.host != dst_u.host:
            sys.exit(f'Para escribir pasa --host {dst_u.host}')
        for m in mapeo:
            r = d.execute(text(
                "UPDATE siesa_mapeo_unidades SET unidad_negocio_id=:unidad_negocio_id, "
                "descripcion=:descripcion WHERE tipo_inv_siesa=:tipo_inv_siesa"), m)
            if not r.rowcount:
                d.execute(text(
                    "INSERT INTO siesa_mapeo_unidades (tipo_inv_siesa, unidad_negocio_id, descripcion) "
                    "VALUES (:tipo_inv_siesa, :unidad_negocio_id, :descripcion)"), m)
        # Solo donde el destino la tiene vacía: no pisa lo que un admin de QA fijó.
        # UNA sentencia con las dos listas: 26.000 UPDATE sueltos por la red
        # tardaban más de 20 minutos con la transacción abierta, bloqueando filas
        # de `productos` que el sync de QA también escribe.
        res = d.execute(text(
            "UPDATE productos SET unidad_negocio_id = dat.u "
            "FROM unnest(CAST(:cods AS text[]), CAST(:unis AS text[])) AS dat(c, u) "
            "WHERE productos.codigo_siesa = dat.c "
            "AND coalesce(productos.unidad_negocio_id, '') = ''"),
            {'cods': [p['c'] for p in prods], 'unis': [p['u'] for p in prods]})
        quedan = d.execute(text(
            "SELECT count(*) FROM productos WHERE coalesce(unidad_negocio_id,'') = ''")).scalar()
        print(f'Listo. Productos actualizados: {res.rowcount} | siguen vacíos: {quedan}')


if __name__ == '__main__':
    main()
