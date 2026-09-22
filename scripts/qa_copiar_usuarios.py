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

Con `--flota` copia los **maestros de flota**: vehículos, conductores, las
plantillas de inspección con sus ítems, y las fichas técnicas. Sin eso QA tiene
el esquema de flota y ninguna fila — medido el 2026-09-21: 0 vehículos, 0
conductores, 0 plantillas—, así que las pantallas salen vacías y los dos crons
del módulo (`FLOTA_AVISOS`, `FLOTA_PREVENTIVO`) corren sobre la nada.

**No copia `flota_documento_vehiculo`, y es a propósito.** Los 11 documentos de
producción tienen `foto_id` y `flota_foto` guarda solo un `storage_ref`: los
bytes viven en el volumen (`FLOTA_FOTOS_DIR`). Copiar las filas traería a QA
documentos que afirman tener una foto que ahí no existe. Mientras el archivo no
viaje con la fila, se deja fuera — y eso deja al barrido de vencimientos sin
nada que barrer, que es una carencia declarada y no un hueco silencioso.

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

#: Maestros de flota, **en orden de clave foránea**:
#:   vehiculos                   — sin dependencias
#:   conductores                 — usuario_id -> usuarios (ya copiados, ids intactos)
#:   flota_plantilla_inspeccion  — sin dependencias
#:   flota_item_inspeccion       — plantilla_id -> flota_plantilla_inspeccion
#:   flota_ficha_tecnica         — vehiculo_id  -> vehiculos
#: Verificado contra `information_schema` el 2026-09-21; si aparece una FK
#: nueva, el INSERT falla ruidoso y hay que reordenar acá.
TABLAS_FLOTA = ['vehiculos', 'conductores', 'flota_plantilla_inspeccion',
                'flota_item_inspeccion', 'flota_ficha_tecnica']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ejecutar', action='store_true')
    ap.add_argument('--host', default='', help='host del destino, para confirmar')
    ap.add_argument('--unidades', action='store_true',
                    help='copiar unidad de negocio (mapeo + productos) en vez de usuarios')
    ap.add_argument('--flota', action='store_true',
                    help='copiar los maestros de flota en vez de usuarios')
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

    tablas = TABLAS_FLOTA if args.flota else TABLAS
    with src.connect() as s, dst.begin() as d:
        for nombre in tablas:
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
                # La secuencia se reinicia solo si la tabla TIENE una.
                #
                # Antes esto asumía una columna `id` serial en todas. Vale para
                # `almacenes` y `usuarios`, y no para `flota_ficha_tecnica`,
                # cuya clave primaria es `vehiculo_id` — ahí
                # `pg_get_serial_sequence(...,'id')` revienta con «column "id"
                # does not exist» **después** de insertar. La transacción lo
                # revirtió entero, así que el costo fue una corrida perdida y
                # no medio maestro sembrado; el arreglo es no suponer el
                # esquema, preguntarlo.
                fila = d.execute(text(
                    "SELECT a.attname, pg_get_serial_sequence(:t, a.attname) "
                    "FROM pg_attribute a "
                    "WHERE a.attrelid = CAST(:t AS regclass) AND a.attnum > 0 "
                    "AND NOT a.attisdropped "
                    "AND pg_get_serial_sequence(:t, a.attname) IS NOT NULL "
                    "LIMIT 1"), {'t': nombre}).first()
                if fila:
                    col, seq = fila
                    d.execute(text(
                        f"SELECT setval('{seq}', "
                        f"(SELECT COALESCE(MAX({col}),1) FROM {nombre}))"))
                else:
                    print(f'  ({nombre} no tiene secuencia — nada que reiniciar)')
        if not args.ejecutar:
            d.rollback() if hasattr(d, 'rollback') else None
            modo = ' --flota' if args.flota else ''
            print(f'Simulacro: no se escribió nada. Usa{modo} --ejecutar --host <destino>.')
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
