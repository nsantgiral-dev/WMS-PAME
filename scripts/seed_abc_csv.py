"""
seed_abc_csv.py — Carga inicial de clasificación ABC desde CSV/Excel de Siesa.

Formato esperado (reporte "Recalculo de rotación ABC" de Siesa Enterprise):
    Item | Referencia | Extensiones | Descripción | Clasificación | Nro. transacciones

La columna clave es "Referencia" — se cruza contra productos.codigo_siesa.
Las filas de encabezado de Siesa (nombre empresa, fecha, filtros) se saltan
automáticamente. El archivo puede venir en cualquier orden.

Uso:
    python seed_abc_csv.py ruta/al/archivo.csv
    python seed_abc_csv.py ruta/al/archivo.csv --dry-run
    python seed_abc_csv.py ruta/al/archivo.xlsx          # también acepta Excel
    python seed_abc_csv.py archivo.csv --sep ";"         # forzar separador
"""
import sys
import os
import re
import csv
import argparse
from collections import Counter


# ── Aliases para columna de código de producto ────────────────────────────────
ALIASES_REF = [
    'referencia', 'ref', 'codigo', 'código', 'item ref', 'cod item',
    'codigo item', 'código item', 'referencia item', 'f120_referencia',
    'codigo siesa', 'código siesa',
]

# ── Aliases para columna de clasificación ABC ─────────────────────────────────
ALIASES_ABC = [
    'clasificacion', 'clasificación', 'abc', 'clase', 'clasif',
    'clasificacion abc', 'clasificación abc', 'rotacion abc', 'rotación abc',
]

# ── Aliases para columna de transacciones (opcional, para Watchdog) ───────────
ALIASES_TXN = [
    'nro. transacciones', 'nro transacciones', 'transacciones', 'txn',
    'num transacciones', 'número transacciones', 'numero transacciones',
    'cantidades', 'movimientos',
]


def _norm(s):
    """Normaliza texto para comparación: minúsculas, sin acentos, sin puntuación."""
    s = str(s).lower().strip()
    for a, b in [('á','a'),('é','e'),('í','i'),('ó','o'),('ú','u'),('ñ','n')]:
        s = s.replace(a, b)
    return re.sub(r'[^\w\s]', '', s).strip()


def _detectar_sep(ruta):
    with open(ruta, 'rb') as f:
        muestra = f.read(8192).decode('utf-8-sig', errors='replace')
    tabs = muestra.count('\t')
    puntos = muestra.count(';')
    comas = muestra.count(',')
    if tabs >= max(puntos, comas):
        return '\t'
    return ';' if puntos >= comas else ','


def _col(headers_norm, aliases):
    """Retorna índice de la columna que coincide con algún alias."""
    for alias in aliases:
        an = _norm(alias)
        for i, h in enumerate(headers_norm):
            if an == h or an in h or h in an:
                return i
    return None


def _leer_filas(ruta, sep):
    """Lee el archivo y retorna todas las filas como listas de strings."""
    ext = os.path.splitext(ruta)[1].lower()

    if ext in ('.xlsx', '.xls'):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
            ws = wb.active
            filas = []
            for row in ws.iter_rows(values_only=True):
                filas.append([str(c) if c is not None else '' for c in row])
            return filas
        except ImportError:
            print("✗ Para leer Excel instala: pip install openpyxl")
            sys.exit(1)

    with open(ruta, 'r', encoding='utf-8-sig', errors='replace') as f:
        reader = csv.reader(f, delimiter=sep)
        return list(reader)


def _encontrar_header(filas):
    """
    Salta las filas basura del encabezado de Siesa (empresa, fecha, filtros).
    La fila de headers es la que contiene 'Referencia' y 'Clasificacion'.
    Retorna (idx_header, fila_headers).
    """
    palabras_clave = {'referencia', 'ref', 'clasificacion', 'clasificacion', 'abc', 'clasif'}

    for i, fila in enumerate(filas):
        celdas_norm = [_norm(c) for c in fila]
        hits = sum(1 for c in celdas_norm if c in palabras_clave or any(p in c for p in palabras_clave))
        if hits >= 1:
            return i, fila

    # Fallback: primera fila no vacía
    for i, fila in enumerate(filas):
        if any(c.strip() for c in fila):
            return i, fila

    return 0, filas[0] if filas else []


def cargar(ruta, sep=None, dry_run=False):
    if not os.path.exists(ruta):
        print(f"✗ Archivo no encontrado: {ruta}")
        sys.exit(1)

    ext = os.path.splitext(ruta)[1].lower()
    es_excel = ext in ('.xlsx', '.xls')

    if not es_excel:
        sep = sep or _detectar_sep(ruta)
        print(f"Separador: {repr(sep)}")

    filas = _leer_filas(ruta, sep or ',')
    print(f"Total filas leídas: {len(filas)}")

    idx_h, headers = _encontrar_header(filas)
    print(f"Filas basura Siesa omitidas: {idx_h}")
    print(f"Headers detectados: {headers}")

    hn = [_norm(h) for h in headers]

    # Detectar columnas
    i_ref = _col(hn, ALIASES_REF)
    i_abc = _col(hn, ALIASES_ABC)
    i_txn = _col(hn, ALIASES_TXN)  # opcional

    if i_ref is None:
        print(f"\n✗ No encontré columna 'Referencia'.")
        print(f"  Columnas disponibles: {headers}")
        print(f"  Pasa --col-ref 'nombre exacto'")
        sys.exit(1)

    if i_abc is None:
        print(f"\n✗ No encontré columna 'Clasificación'.")
        print(f"  Columnas disponibles: {headers}")
        sys.exit(1)

    print(f"\nColumna Referencia:    [{i_ref}] '{headers[i_ref]}'")
    print(f"Columna Clasificación: [{i_abc}] '{headers[i_abc]}'")
    if i_txn is not None:
        print(f"Columna Transacciones: [{i_txn}] '{headers[i_txn]}' (Watchdog calibration)")
    else:
        print(f"Columna Transacciones: no encontrada (se omite, no es crítica)")

    # Parsear datos
    datos = []
    omitidos = 0

    for fila in filas[idx_h + 1:]:
        # Saltar filas vacías o que son resúmenes al pie de Siesa
        if not fila or not any(c.strip() for c in fila):
            continue
        try:
            referencia = fila[i_ref].strip()
            clasificacion = fila[i_abc].strip().upper()
        except IndexError:
            omitidos += 1
            continue

        # Validación
        if not referencia or referencia in ('-', 'None', ''):
            omitidos += 1
            continue
        if clasificacion not in ('A', 'B', 'C'):
            omitidos += 1
            continue

        txn = 0
        if i_txn is not None:
            try:
                txn = int(str(fila[i_txn]).replace('.', '').replace(',', '').strip() or 0)
            except (ValueError, IndexError):
                txn = 0

        datos.append((referencia, clasificacion, txn))

    if not datos:
        print("\n✗ No hay datos válidos. Verifica el archivo.")
        sys.exit(1)

    dist = Counter(c for _, c, _ in datos)
    print(f"\nRegistros válidos:  {len(datos)}")
    print(f"Omitidos:           {omitidos}")
    print(f"Distribución ABC:   A={dist['A']}  B={dist['B']}  C={dist['C']}")

    if dry_run:
        print(f"\n[DRY-RUN] Primeros 15 registros que se actualizarían:")
        print(f"  {'Referencia':<20} {'Clase':<6} {'Transacciones'}")
        print(f"  {'─'*45}")
        for ref, abc, txn in datos[:15]:
            print(f"  {ref:<20} {abc:<6} {txn:>10}")
        print(f"\n[DRY-RUN] Sin cambios en BD. Quita --dry-run para ejecutar.")
        return

    # ── Conectar a BD ────────────────────────────────────────────────────────
    print("\nConectando a la base de datos...")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("✗ Instala: pip install psycopg2-binary")
        sys.exit(1)

    db_url = os.getenv('DATABASE_URL', '')
    if not db_url:
        host = os.getenv('PGHOST', 'localhost')
        port = os.getenv('PGPORT', '5432')
        dbname = os.getenv('PGDATABASE', 'wms')
        user = os.getenv('PGUSER', 'postgres')
        pwd = os.getenv('PGPASSWORD', '')
        db_url = f"postgresql://{user}:{pwd}@{host}:{port}/{dbname}"

    db_url = db_url.replace('postgres://', 'postgresql://', 1)

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
    except Exception as e:
        print(f"✗ Error conectando: {e}")
        sys.exit(1)

    # ── Bulk update en lotes de 500 ──────────────────────────────────────────
    actualizados = 0
    no_encontrados = []
    LOTE = 500

    print(f"Actualizando {len(datos)} productos...")

    for i in range(0, len(datos), LOTE):
        lote = datos[i:i + LOTE]

        # Preparar lista para executemany
        params = [(abc, ref, ref) for ref, abc, _ in lote]

        cur.executemany(
            """
            UPDATE productos
            SET clasificacion_abc = %s
            WHERE (codigo_siesa = %s OR codigo = %s)
              AND activo = true
            """,
            params
        )
        # Detectar los que no matchearon (rowcount no sirve en executemany)
        # Verificar individualmente los que quedaron sin actualizar
        refs_lote = [ref for ref, _, _ in lote]
        cur.execute(
            """
            SELECT codigo_siesa, codigo
            FROM productos
            WHERE (codigo_siesa = ANY(%s) OR codigo = ANY(%s)) AND activo = true
            """,
            (refs_lote, refs_lote)
        )
        encontrados_set = set()
        for row in cur.fetchall():
            if row[0]: encontrados_set.add(row[0])
            if row[1]: encontrados_set.add(row[1])

        for ref, _, _ in lote:
            if ref not in encontrados_set:
                no_encontrados.append(ref)
            else:
                actualizados += 1

        conn.commit()
        pct = min(i + LOTE, len(datos))
        print(f"  {pct}/{len(datos)} procesados...", end='\r')

    cur.close()
    conn.close()

    # ── Resultado ────────────────────────────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"✓ Productos actualizados:  {actualizados}")
    print(f"✗ Códigos no encontrados:  {len(no_encontrados)}")

    if no_encontrados:
        log_path = os.path.splitext(ruta)[0] + '_no_encontrados.txt'
        with open(log_path, 'w') as lf:
            lf.write('\n'.join(no_encontrados))
        print(f"  → Log guardado en: {log_path}")
        print(f"  Estos códigos existen en Siesa pero no en el WMS todavía.")
        if len(no_encontrados) <= 10:
            for ref in no_encontrados:
                print(f"    - {ref}")

    print(f"{'─'*55}")
    print(f"Distribución cargada: A={dist['A']}  B={dist['B']}  C={dist['C']}")
    print(f"\n✓ ABC cargado. El scheduler de las 6am ya puede generar tareas.")
    print(f"  O genera manualmente desde el panel Inventario → ABC → Generar A+B+C")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Carga clasificación ABC desde CSV/Excel de Siesa (Recalculo de rotación ABC)'
    )
    parser.add_argument('archivo', help='Ruta al CSV o Excel exportado de Siesa')
    parser.add_argument('--dry-run', action='store_true',
                        help='Ver qué se actualizaría sin tocar la BD')
    parser.add_argument('--sep', default=None,
                        help='Separador CSV (, ; o tab). Se auto-detecta si no se da.')
    args = parser.parse_args()

    cargar(ruta=args.archivo, sep=args.sep, dry_run=args.dry_run)
