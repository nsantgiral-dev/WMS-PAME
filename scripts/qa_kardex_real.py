"""
¿Funciona el kardex contra Siesa? — verificación de SOLO LECTURA.

Hace únicamente GETs (nunca un POST) con las credenciales de `.env.qa`, fuerza
`MODO_ENSAYO=true` en el proceso y no toca ninguna base: no llama a
`descargar_kardex` (que escribe `kardex_movimientos`), reimplementa el recorrido
en memoria con las MISMAS funciones de lectura (`_parsear_fila`,
`totales_declarados`).

Qué contesta, en orden:

  1. ¿Responde la consulta del kardex (`KARDEX_CONSULTA_NOMBRE`)? Se prueba por
     el endpoint dinámico y por el estándar, y contra una consulta dinámica de
     control que se sabe registrada: un 401 en las dos del kardex con la de
     control en 200 es «esta consulta no está habilitada para este token», no
     «Siesa caído».
  2. Si responde: forma real (claves, tipos, `Datos` vs `Table`, totales
     declarados), y si la página N contiene las mismas filas dos veces
     (conjuntos, no posiciones).
  3. `--cuadre`: recorre TODAS las páginas (miles; fuera de horario, Regla 14)
     y compara, por SKU de NB1, la suma del kardex (entradas − salidas) contra
     la existencia de `API_v2_Inventarios_InvFecha`. Si el kardex trae los
     saldos iniciales (699), las dos cifras deben coincidir.

Resultado del 2026-09-24 (Siesa QA, 11:45–12:20 Bogotá):
  · `papeleriamedellin_papeleriamedellin_API_custom_KardexWMS` → 401 por los dos
    endpoints («verifique si tiene permisos asignados a la consulta dinamica»),
    también con el nombre de un solo prefijo del commit d9d704e. La de control
    (`papeleriamedellin_WMS_Stock_Bodega_v2`) → 200 con el mismo token.
  · El sobre del endpoint dinámico declara `total_páginas`/`total_registros`.
  · Orden: una pasada completa de la de control declaró 35.604 registros,
    trajo 35.604 filas y solo 22.345 (bodega, referencia) distintas. La página
    50 pedida dos veces con 5 s de diferencia no compartió ninguna fila.

Uso:
    venv/bin/python scripts/qa_kardex_real.py [--cuadre] [--skus A,B,C]
"""
import argparse
import os
import sys
import time
from collections import defaultdict

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

CONTROL = 'papeleriamedellin_WMS_Stock_Bodega_v2'
SKUS_DEFECTO = 'PAPELSP9218,PAPELSP9830,PAPELSP6948'


def _cargar_entorno():
    from dotenv import load_dotenv
    ruta = os.path.join(RAIZ, '.env.qa')
    if not os.path.exists(ruta):
        # Worktree: el .env.qa vive en el repositorio principal.
        ruta = os.path.join(RAIZ.split('/.claude/worktrees/')[0], '.env.qa')
    load_dotenv(ruta, override=True)
    os.environ['MODO_ENSAYO'] = 'true'           # ningún POST sale
    os.environ.pop('DATABASE_URL', None)         # y ninguna base se toca


def _pedir(connekta, nombre, url, pagina=1, tam=100):
    import requests
    params = {'idCompania': connekta.id_compania, 'descripcion': nombre,
              'paginacion': f'numPag={pagina}|tamPag={min(tam, 100)}'}  # Regla 10
    r = requests.get(url, headers=connekta.headers, params=params, timeout=120)
    try:
        cuerpo = r.json()
    except ValueError:
        cuerpo = {'_texto': r.text[:300]}
    return r.status_code, cuerpo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cuadre', action='store_true',
                    help='recorre todas las páginas y cuadra contra InvFecha')
    ap.add_argument('--skus', default=SKUS_DEFECTO)
    ap.add_argument('--bodega', default='NB1')
    args = ap.parse_args()

    _cargar_entorno()
    from app.services.connekta_gateway import connekta
    from app.services.kardex_service import (
        NOMBRE_CONSULTA, _parsear_fila, totales_declarados)

    print(f'Siesa: {connekta.host_siesa} · ensayo={connekta.modo_ensayo} · '
          f'simulación={connekta.modo_simulacion}')
    if connekta.modo_simulacion:
        print('Sin credenciales: no hay nada que probar.')
        return 2

    # ── 1. ¿Responde? ───────────────────────────────────────────────────
    urls = {'dinamico': connekta.url_get_dinamico, 'estandar': connekta.url_get}
    responde = None
    for nombre in (NOMBRE_CONSULTA, CONTROL):
        for etiqueta, url in urls.items():
            status, cuerpo = _pedir(connekta, nombre, url, tam=5)
            det = cuerpo.get('detalle')
            resumen = det if isinstance(det, str) else (
                f"{len((det or {}).get('Datos') or (det or {}).get('Table') or [])} filas, "
                f"totales={totales_declarados(cuerpo)}")
            print(f'[{status}] {nombre} ({etiqueta}): {str(resumen)[:140]}')
            if nombre == NOMBRE_CONSULTA and status == 200 and responde is None:
                responde = url
    if responde is None:
        print('\nVEREDICTO: la consulta del kardex NO responde con este token. '
              'Revisar en Siesa → Administración → Permisos servicios si está '
              'registrada y con permiso (dinámicas y estándar), y con qué nombre.')
        return 1

    # ── 2. Forma y orden ────────────────────────────────────────────────
    _, p1 = _pedir(connekta, NOMBRE_CONSULTA, responde)
    det = p1.get('detalle') or {}
    filas = det.get('Datos') or det.get('Table') or []
    print(f"\nClave de filas: {'Datos' if det.get('Datos') else 'Table'} · "
          f"totales declarados {totales_declarados(p1)}")
    if filas:
        for k, v in filas[0].items():
            print(f'   {k!r}: {type(v).__name__} = {str(v)[:60]}')
        legibles = [f for f in map(_parsear_fila, filas) if f]
        print(f'Filas legibles: {len(legibles)}/{len(filas)} · sin clave natural: '
              f"{sum(1 for f in legibles if not f['consec_docto'] and f['nro_registro'] is None)}")
    a = {f['hash'] for f in map(_parsear_fila, filas) if f}
    time.sleep(5)
    _, p1b = _pedir(connekta, NOMBRE_CONSULTA, responde)
    detb = p1b.get('detalle') or {}
    b = {f['hash'] for f in map(_parsear_fila, detb.get('Datos') or detb.get('Table') or []) if f}
    print(f'Página 1 pedida dos veces: {len(a & b)}/{len(a)} filas en común')

    if not args.cuadre:
        return 0

    # ── 3. Cuadre contra InvFecha ───────────────────────────────────────
    skus = {s.strip() for s in args.skus.split(',') if s.strip()}
    total_reg, total_pag = totales_declarados(p1)
    neto = defaultdict(float)
    identidades = set()
    pagina = 1
    while True:
        _, cuerpo = _pedir(connekta, NOMBRE_CONSULTA, responde, pagina=pagina)
        d = cuerpo.get('detalle') or {}
        filas = d.get('Datos') or d.get('Table') or []
        if not filas:
            break
        for f in filter(None, map(_parsear_fila, filas)):
            identidades.add(f['hash'])
            if f['bodega'] == args.bodega and f['referencia'] in skus:
                neto[f['referencia']] += f['cantidad'] if f['naturaleza'] == 1 else -f['cantidad']
        if pagina % 100 == 0:
            print(f'  página {pagina}/{total_pag}')
        if (total_pag and pagina >= total_pag) or (not total_pag and len(filas) < 100):
            break
        pagina += 1
    print(f'\nRecorridas {pagina} páginas · declarados {total_reg} · '
          f'movimientos distintos {len(identidades)}')
    for s in sorted(skus):
        inv = connekta.get_inventario_fecha(s, args.bodega)
        filas_inv = (inv or {}).get('detalle', {}).get('Table', [])
        existencia = sum(float(r.get('f400_cant_existencia_1') or 0) for r in filas_inv)
        print(f'  {s} {args.bodega}: kardex neto {neto[s]:.2f} · InvFecha {existencia:.2f} '
              f'· diferencia {neto[s] - existencia:+.2f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
