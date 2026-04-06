"""
seed_abc_csv.py — Carga inicial de clasificación ABC desde CSV exportado de Siesa.

Uso:
    python seed_abc_csv.py ruta/al/archivo.csv [--dry-run] [--sep ;]

Argumentos:
    archivo     CSV exportado desde Siesa (Consultas Flex o reporte de Conteo Cíclico)
    --dry-run   Muestra lo que haría sin modificar la BD
    --sep       Separador del CSV (default: auto-detecta , o ;)
    --col-ref   Nombre de la columna con el código del ítem (auto-detectado si no se da)
    --col-abc   Nombre de la columna con la clasificación ABC (auto-detectado)

El script detecta automáticamente:
  - Las filas basura del encabezado de Siesa (nombre empresa, fecha, filtros)
  - La fila real de títulos de columna
  - Los posibles nombres de columna (referencia, codigo, item, clasificacion, abc, etc.)

Siesa exporta con nombres de columna como:
    "Referencia", "Ítem", "Código", "Clasificación", "ABC Rotación", etc.
"""
import sys
import csv
import os
import re
import argparse
import io


# ── Nombres de columna que Siesa usa para el código del ítem ──────────────────
ALIASES_REF = [
    'referencia', 'ref', 'codigo', 'código', 'item', 'ítem',
    'id_item', 'id item', 'cod_item', 'cod. item', 'codigo item',
    'código item', 'referencia item', 'f120_referencia',
]

# ── Nombres de columna que Siesa usa para la clasificación ABC ────────────────
ALIASES_ABC = [
    'clasificacion', 'clasificación', 'abc', 'clase', 'rotacion abc',
    'rotación abc', 'abc rotacion', 'abc rotación', 'clasificacion abc',
    'clasificación abc', 'clase abc', 'tipo abc', 'grupo abc',
    'clasificacion_abc',
]


def _normalizar(s):
    """Minúsculas + quitar acentos + quitar caracteres raros para comparar."""
    s = s.lower().strip()
    for a, b in [('á','a'),('é','e'),('í','i'),('ó','o'),('ú','u'),('ñ','n')]:
        s = s.replace(a, b)
    return re.sub(r'[^a-z0-9 _]', '', s)


def _detectar_separador(ruta):
    with open(ruta, 'rb') as f:
        muestra = f.read(4096).decode('utf-8-sig', errors='replace')
    puntos = muestra.count(';')
    comas = muestra.count(',')
    tabs = muestra.count('\t')
    if tabs > puntos and tabs > comas:
        return '\t'
    return ';' if puntos > comas else ','


def _encontrar_col(headers_norm, aliases):
    """Retorna el índice de la primera columna que matchea algún alias."""
    for alias in aliases:
        alias_n = _normalizar(alias)
        for i, h in enumerate(headers_norm):
            if alias_n in h or h in alias_n:
                return i
    return None


def _encontrar_fila_headers(rows, sep):
    """
    Siesa pone filas basura al inicio (empresa, fecha, filtros).
    La fila de headers es la primera que contiene palabras clave de columna.
    Retorna (indice_fila, [headers]).
    """
    palabras_clave = set(
        _normalizar(a) for aliases in [ALIASES_REF, ALIASES_ABC] for a in aliases
    )
    for i, row in enumerate(rows):
        if not row or not any(c.strip() for c in row):
            continue
        row_norm = [_normalizar(c) for c in row]
        coincidencias = sum(
            1 for cell in row_norm
            if any(kw in cell or cell in kw for kw in palabras_clave if len(kw) > 2)
        )
        if coincidencias >= 1:
            return i, row
    return None, None


def cargar_csv(ruta, sep=None, col_ref=None, col_abc=None, dry_run=False):
    if not os.path.exists(ruta):
        print(f"✗ Archivo no encontrado: {ruta}")
        sys.exit(1)

    sep = sep or _detectar_separador(ruta)
    print(f"Separador detectado: {repr(sep)}")

    # Leer todas las filas crudas
    with open(ruta, 'r', encoding='utf-8-sig', errors='replace') as f:
        reader = csv.reader(f, delimiter=sep)
        todas_las_filas = list(reader)

    if not todas_las_filas:
        print("✗ El archivo está vacío")
        sys.exit(1)

    # Encontrar la fila de headers (saltando basura estética de Siesa)
    idx_header, headers_raw = _encontrar_fila_headers(todas_las_filas, sep)

    if idx_header is None:
        # Fallback: asumir que la primera fila no vacía es el header
        for i, row in enumerate(todas_las_filas):
            if any(c.strip() for c in row):
                idx_header = i
                headers_raw = row
                break

    print(f"Filas de encabezado Siesa omitidas: {idx_header}")
    print(f"Columnas detectadas: {headers_raw}")

    headers_norm = [_normalizar(h) for h in headers_raw]

    # Detectar columnas
    idx_ref = _encontrar_col(headers_norm, [col_ref] if col_ref else ALIASES_REF)
    idx_abc = _encontrar_col(headers_norm, [col_abc] if col_abc else ALIASES_ABC)

    if idx_ref is None:
        print(f"\n✗ No encontré columna de código/referencia.")
        print(f"  Columnas disponibles: {headers_raw}")
        print(f"  Pasa --col-ref 'nombre exacto de la columna'")
        sys.exit(1)

    if idx_abc is None:
        print(f"\n✗ No encontré columna de clasificación ABC.")
        print(f"  Columnas disponibles: {headers_raw}")
        print(f"  Pasa --col-abc 'nombre exacto de la columna'")
        sys.exit(1)

    print(f"Columna código/ref: [{idx_ref}] '{headers_raw[idx_ref]}'")
    print(f"Columna ABC:        [{idx_abc}] '{headers_raw[idx_abc]}'")

    # Parsear datos
    datos = []
    filas_invalidas = 0
    for row in todas_las_filas[idx_header + 1:]:
        if not row or not any(c.strip() for c in row):
            continue
        try:
            codigo = row[idx_ref].strip()
            abc = row[idx_abc].strip().upper()
        except IndexError:
            filas_invalidas += 1
            continue

        if not codigo or abc not in ('A', 'B', 'C'):
            filas_invalidas += 1
            continue

        datos.append((codigo, abc))

    print(f"\nRegistros válidos:  {len(datos)}")
    print(f"Registros omitidos: {filas_invalidas}")

    if not datos:
        print("✗ No hay datos válidos para cargar")
        sys.exit(1)

    # Distribución
    from collections import Counter
    dist = Counter(abc for _, abc in datos)
    print(f"Distribución ABC: A={dist.get('A',0)}  B={dist.get('B',0)}  C={dist.get('C',0)}")

    if dry_run:
        print("\n[DRY RUN] Los primeros 10 registros que se actualizarían:")
        for codigo, abc in datos[:10]:
            print(f"  {codigo} → {abc}")
        print("\n[DRY RUN] Sin cambios en la BD. Quita --dry-run para ejecutar.")
        return

    # Conectar a la BD y actualizar
    print("\nConectando a la base de datos...")
    try:
        # Cargar configuración de Flask para obtener DATABASE_URL
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dotenv import load_dotenv
        load_dotenv()
        import psycopg2

        db_url = os.getenv('DATABASE_URL', '')
        if not db_url:
            # Intentar armar desde variables individuales
            host = os.getenv('PGHOST') or os.getenv('DB_HOST', 'localhost')
            port = os.getenv('PGPORT') or os.getenv('DB_PORT', '5432')
            dbname = os.getenv('PGDATABASE') or os.getenv('DB_NAME', 'wms')
            user = os.getenv('PGUSER') or os.getenv('DB_USER', 'postgres')
            pwd = os.getenv('PGPASSWORD') or os.getenv('DB_PASSWORD', '')
            db_url = f"postgresql://{user}:{pwd}@{host}:{port}/{dbname}"

        # psycopg2 no acepta el prefijo postgres:// de Railway
        db_url = db_url.replace('postgres://', 'postgresql://', 1)

        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
    except ImportError:
        print("✗ psycopg2 no instalado. Instala con: pip install psycopg2-binary")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error conectando a BD: {e}")
        sys.exit(1)

    # Bulk update por lotes
    actualizados = 0
    no_encontrados = []
    LOTE = 500

    for i in range(0, len(datos), LOTE):
        lote = datos[i:i + LOTE]
        for codigo, abc in lote:
            cur.execute(
                """
                UPDATE productos
                SET clasificacion_abc = %s
                WHERE (codigo_siesa = %s OR codigo = %s)
                  AND activo = true
                """,
                (abc, codigo, codigo)
            )
            if cur.rowcount == 0:
                no_encontrados.append(codigo)
            else:
                actualizados += cur.rowcount
        conn.commit()
        print(f"  Procesados {min(i + LOTE, len(datos))}/{len(datos)}...", end='\r')

    cur.close()
    conn.close()

    print(f"\n\n{'─'*50}")
    print(f"✓ Actualizados:    {actualizados} productos")
    print(f"✗ No encontrados:  {len(no_encontrados)} códigos")
    if no_encontrados:
        preview = no_encontrados[:20]
        print(f"  Primeros 20: {preview}")
        if len(no_encontrados) > 20:
            # Guardar log completo
            log_path = ruta.replace('.csv', '_no_encontrados.txt').replace('.xlsx', '_no_encontrados.txt')
            with open(log_path, 'w') as lf:
                lf.write('\n'.join(no_encontrados))
            print(f"  Log completo: {log_path}")
    print(f"{'─'*50}")
    print(f"Distribución final en BD: A={dist.get('A',0)}  B={dist.get('B',0)}  C={dist.get('C',0)}")
    print("\n✓ Listo. El scheduler de las 6am ya puede generar tareas de conteo cíclico.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Carga inicial de clasificación ABC desde CSV de Siesa'
    )
    parser.add_argument('archivo', help='Ruta al CSV exportado de Siesa')
    parser.add_argument('--dry-run', action='store_true',
                        help='Mostrar lo que haría sin modificar la BD')
    parser.add_argument('--sep', default=None,
                        help='Separador del CSV (, o ; o tab). Default: auto-detecta')
    parser.add_argument('--col-ref', default=None,
                        help='Nombre exacto de la columna con el código del ítem')
    parser.add_argument('--col-abc', default=None,
                        help='Nombre exacto de la columna con la clasificación ABC')
    args = parser.parse_args()

    cargar_csv(
        ruta=args.archivo,
        sep=args.sep,
        col_ref=args.col_ref,
        col_abc=args.col_abc,
        dry_run=args.dry_run,
    )
