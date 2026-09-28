#!/usr/bin/env python
"""
¿De dónde puede salir la demanda de compras? — medición EN VIVO, **solo GET**.

Compras necesita una sola cosa de Siesa para proponer: **cuánto se vendió, por
SKU y día, en toda la red** (NB1 por pedido + las tiendas por caja). Este
script mide, contra el Siesa que se le indique, cuál de las fuentes posibles
lo entrega hoy, con qué cobertura y a qué costo en páginas. Ver CLAUDE.md
«Compras: de dónde sale la demanda (2026-09-27)».

Qué prueba, en orden:

  1. **El kardex** (`…_KardexWMS`): los dos nombres que tuvo × los dos
     endpoints (dinámico `ejecutarconsulta`, estándar `ejecutarconsultaestandar`).
     El 401 del RIT resultó ser una consulta ESTÁNDAR llamada por el endpoint
     dinámico: por eso se prueban las cuatro combinaciones. Con la consulta de
     control (`papeleriamedellin_WMS_Stock_Bodega_v2`) al lado: un 401 en el
     kardex con la de control en 200 es «esta consulta no está habilitada para
     este token», no «Siesa caído».
  2. **¿Las dinámicas aceptan `parametros`?** Se pide la consulta de control
     con y sin filtro de bodega y se comparan los `total_registros`
     declarados. Si el total no cambia, la dinámica ignora el filtro y la
     consulta de ventas nueva tiene que llevar su ventana de fechas dentro del
     SQL (como ya hace `papeleriamedellin_WMS_Remision_DesdePedido`).
  3. **`papeleriamedellin_pame_descubrir_tablas`**: lo que devuelve HOY (el SQL
     vive en Siesa; se edita en Generic Transfer, no se manda desde acá).
     Reconoce por sus columnas cuál de los SQL de validación tiene puesto
     (`demanda_fuentes.SQL_VALIDACION`: esquema, conceptos de un día, volumen
     por mes, un SKU en un mes) y lo interpreta. **Solo para descubrir y
     validar: nunca como fuente de negocio.** Medido en producción el
     2026-09-27: hoy tiene un SQL con ORDER BY sin OFFSET → 500.
  4. **Consultas estándar candidatas** a movimientos/POS (nombres plausibles;
     un 401 aquí puede ser «no existe» o «sin permiso»: no se afirma cuál).
  5. **`API_v2_Ventas_Facturas_DesdePedido`** (la de las fotos): por CO para un
     día, y un mes entero del CO del CDI, para medir volumen y qué bodegas,
     conceptos y tipos de documento trae (¿aparece la venta de caja?).
  6. **`API_v2_Inventarios_InvFecha`** del SKU de control en NB1 (existencia,
     POS no acumulado) y de los más vendidos del día.

Cerrojos (un script de verificación que puede escribir no es un script de
verificación):

  - `requests.post`/`put`/`patch`/`delete`, `Session.request` con método que no
    sea GET y `ConnektaGateway._post` se reemplazan por funciones que revientan,
    **antes** de importar nada de la app; y `MODO_ENSAYO=true`.
  - `DATABASE_URL` tiene que ser SQLite (el script no usa base, pero se niega a
    correr con otra en el entorno).
  - Las credenciales no se imprimen ni se escriben: se leen del entorno del
    proceso (`railway run` las inyecta en producción; `--ambiente qa` las lee
    de `.env.qa`).
  - `tamPag` ≤ 100 (Regla 10), topes de páginas y pausas.

Uso:
    # QA (Siesa QA responde ~7:00–19:30 Bogotá)
    venv/bin/python scripts/qa_demanda_fuentes_real.py --ambiente qa

    # Producción (solo GET), credenciales inyectadas por Railway:
    railway run --environment production --service WMS-PAME -- env \\
        DATABASE_URL=sqlite:////tmp/x.db SYNC_SCHEDULER=false MODO_ENSAYO=true \\
        HEAVY_SCHEDULERS=false WORKER_SKIP_ESSENTIAL=true \\
        venv/bin/python scripts/qa_demanda_fuentes_real.py --ambiente produccion

    Resultado del 2026-09-27 (producción, 20:32 Bogotá, domingo): ver CLAUDE.md
    «Compras: de dónde sale la demanda (2026-09-27)».

    Opciones: --dia AAAA-MM-DD (default: el último día hábil), --mes AAAA-MM
    (default: la semana anterior al día, día por día), --sku PAPELSP9218, --bodega NB1,
    --sin-mes (no recorre el mes), --solo kardex,dinamicas,descubrir,estandar,ventas,invfecha
    --sql (imprime el SQL propuesto y sale, sin tocar Siesa)
    --leer reciente|historico [--max-paginas N] (sección 7: la lectura de verdad,
      el mismo código del botón, sobre SQLite desechable)
"""
import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

CONTROL_DINAMICA = 'papeleriamedellin_WMS_Stock_Bodega_v2'
DESCUBRIR = 'papeleriamedellin_pame_descubrir_tablas'
KARDEX_NOMBRES = (
    'papeleriamedellin_papeleriamedellin_API_custom_KardexWMS',
    'papeleriamedellin_API_custom_KardexWMS',
)
#: Nombres PLAUSIBLES de consultas estándar de movimientos o POS. Ninguno está
#: confirmado: se preguntan para saber, no se usan en código.
ESTANDAR_CANDIDATAS = (
    'API_v2_Inventarios_Movimientos',
    'API_v2_Inventarios_Kardex',
    'API_v2_Inventarios_Documentos',
    'API_v2_Ventas_Facturas',
    'API_v2_Ventas_POS',
    'API_v2_POS_Facturas',
    'API_v2_Ventas_Facturas_POS',
)
API_VENTAS = 'API_v2_Ventas_Facturas_DesdePedido'
API_INVFECHA = 'API_v2_Inventarios_InvFecha'
PAUSA_S = 0.6


# ══════════════════════════════════════════════════════════════════════════════
# Cerrojos — antes de importar la app
# ══════════════════════════════════════════════════════════════════════════════

class EscrituraBloqueada(RuntimeError):
    pass


def _cerrar_escrituras():
    import requests

    def _no(*_a, **_k):
        raise EscrituraBloqueada('Este script es de SOLO LECTURA: se intentó una escritura HTTP')

    for nombre in ('post', 'put', 'patch', 'delete'):
        setattr(requests, nombre, _no)
        setattr(requests.api, nombre, _no)
    _original = requests.Session.request

    def _solo_get(self, method, url, *a, **k):
        if str(method).upper() != 'GET':
            raise EscrituraBloqueada(f'Método {method} bloqueado: este script es de solo lectura')
        return _original(self, method, url, *a, **k)

    requests.Session.request = _solo_get


def _preparar_entorno(ambiente):
    if ambiente == 'qa':
        from dotenv import dotenv_values
        ruta = os.path.join(RAIZ, '.env.qa')
        if not os.path.exists(ruta):
            ruta = os.path.join(RAIZ.split('/.claude/worktrees/')[0], '.env.qa')
        for k, v in dotenv_values(ruta).items():
            if k == 'DATABASE_URL' or v is None:
                continue
            os.environ[k] = v
    os.environ['MODO_ENSAYO'] = 'true'
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ['HEAVY_SCHEDULERS'] = 'false'
    os.environ['WORKER_SKIP_ESSENTIAL'] = 'true'
    url = os.environ.get('DATABASE_URL', '')
    if url and not url.startswith('sqlite:'):
        raise SystemExit('DATABASE_URL no es SQLite: este script se niega a correr '
                         'con otra base en el entorno.')
    if not url:
        os.environ['DATABASE_URL'] = 'sqlite://'


def _bloquear_post_del_gateway():
    from app.services.connekta_gateway import ConnektaGateway

    def _no_post(*_a, **_k):
        raise EscrituraBloqueada('ConnektaGateway._post bloqueado en este script')

    ConnektaGateway._post = _no_post


# ══════════════════════════════════════════════════════════════════════════════
# Lectura cruda (para ver el código HTTP, que `_get` convierte en excepción)
# ══════════════════════════════════════════════════════════════════════════════

def _pedir(gw, nombre, url, pagina=1, tam=100, parametros=None, timeout=90):
    import requests
    params = {'idCompania': gw.id_compania, 'descripcion': nombre,
              'paginacion': f'numPag={pagina}|tamPag={min(int(tam), 100)}'}
    if parametros:
        params['parametros'] = parametros
    t0 = time.time()
    try:
        r = requests.get(url, headers=gw.headers, params=params, timeout=timeout)
    except Exception as e:                                    # noqa: BLE001
        return None, {'_error': str(e)[:200]}, time.time() - t0
    try:
        cuerpo = r.json()
    except ValueError:
        cuerpo = {'_texto': r.text[:300]}
    time.sleep(PAUSA_S)
    return r.status_code, cuerpo, time.time() - t0


def _filas(cuerpo):
    det = (cuerpo or {}).get('detalle') if isinstance(cuerpo, dict) else None
    if not isinstance(det, dict):
        return []
    return det.get('Datos') or det.get('Table') or []


def _totales(cuerpo):
    from app.services.kardex_service import totales_declarados
    return totales_declarados(cuerpo)


def _resumen(status, cuerpo):
    det = (cuerpo or {}).get('detalle') if isinstance(cuerpo, dict) else None
    if isinstance(det, str) or det is None:
        msg = det or (cuerpo or {}).get('mensaje') or (cuerpo or {}).get('_error') \
            or (cuerpo or {}).get('_texto')
        return str(msg)[:160]
    reg, pag = _totales(cuerpo)
    return f'{len(_filas(cuerpo))} filas · total_registros={reg} · total_páginas={pag}'


def _t(titulo):
    print('\n' + '═' * 78 + f'\n{titulo}\n' + '═' * 78)


# ══════════════════════════════════════════════════════════════════════════════
# 1 · Kardex
# ══════════════════════════════════════════════════════════════════════════════

def probar_kardex(gw, conclusiones):
    _t('1 · Kardex (…_KardexWMS): dos nombres × dos endpoints, y la de control')
    from app.services.kardex_service import _parsear_fila
    urls = {'dinámico': gw.url_get_dinamico, 'estándar': gw.url_get}
    responde = None
    for nombre in KARDEX_NOMBRES + (CONTROL_DINAMICA,):
        for etiqueta, url in urls.items():
            status, cuerpo, seg = _pedir(gw, nombre, url, tam=5)
            print(f'  [{status}] {nombre} ({etiqueta}, {seg:.1f}s): {_resumen(status, cuerpo)}')
            if nombre in KARDEX_NOMBRES and status == 200 and _filas(cuerpo) and responde is None:
                responde = (nombre, url, etiqueta, cuerpo)
    if responde is None:
        conclusiones.append('KARDEX: NO responde con este token por ningún nombre ni endpoint. '
                            'Es permiso/registro en Siesa, no código.')
        return None
    nombre, url, etiqueta, cuerpo = responde
    reg, pag = _totales(cuerpo)
    print(f'\n  RESPONDE: {nombre} por el endpoint {etiqueta}. Declara {reg} registros.')
    filas = _filas(cuerpo)
    print('  Columnas:', ', '.join(sorted(filas[0].keys())))
    legibles = [f for f in map(_parsear_fila, filas) if f]
    print(f'  Legibles {len(legibles)}/{len(filas)} · con clave natural '
          f"{sum(1 for f in legibles if f['consec_docto'] or f['nro_registro'])}")
    # ¿Orden estable? La misma página dos veces (conjuntos, no posiciones).
    _s, c1, _ = _pedir(gw, nombre, url, pagina=2, tam=100)
    time.sleep(3)
    _s, c2, _ = _pedir(gw, nombre, url, pagina=2, tam=100)
    a = {f['hash'] for f in map(_parsear_fila, _filas(c1)) if f}
    b = {f['hash'] for f in map(_parsear_fila, _filas(c2)) if f}
    print(f'  Página 2 pedida dos veces: {len(a & b)}/{len(a)} filas en común')
    fechas = sorted(f['fecha'] for f in map(_parsear_fila, _filas(c1) + filas) if f)
    if fechas:
        print(f'  Fechas en la muestra: {fechas[0]} … {fechas[-1]}')
    # ¿Acepta un filtro de fecha? Si el total declarado baja, sí.
    hace30 = (date.today() - timedelta(days=30)).strftime('%Y%m%d')
    st, cf, _ = _pedir(gw, nombre, url, tam=5, parametros=f"f350_fecha >= ''{hace30}''")
    regf, _p = _totales(cf)
    print(f'  Con filtro f350_fecha >= {hace30}: [{st}] total_registros={regf} (sin filtro {reg})')
    estable = a and len(a & b) == len(a)
    conclusiones.append(
        f'KARDEX: responde ({nombre}, endpoint {etiqueta}), {reg} registros declarados '
        f'(~{(reg or 0) // 100} páginas de 100); orden '
        f"{'ESTABLE' if estable else 'NO estable'} en la muestra; filtro de fecha "
        f"{'SÍ reduce el total' if regf and reg and regf < reg else 'no reduce el total (se ignora o falla)'}.")
    return responde


# ══════════════════════════════════════════════════════════════════════════════
# 2 · ¿Las dinámicas aceptan parámetros?
# ══════════════════════════════════════════════════════════════════════════════

def probar_parametros_dinamica(gw, bodega, conclusiones):
    _t('2 · ¿Una consulta dinámica acepta `parametros`? (la de control, con y sin filtro)')
    s0, c0, _ = _pedir(gw, CONTROL_DINAMICA, gw.url_get_dinamico, tam=5)
    s1, c1, _ = _pedir(gw, CONTROL_DINAMICA, gw.url_get_dinamico, tam=5,
                       parametros=f"f150_id = ''{bodega}''")
    r0, _ = _totales(c0)
    r1, _ = _totales(c1)
    print(f'  sin filtro: [{s0}] {_resumen(s0, c0)}')
    print(f'  con f150_id = {bodega}: [{s1}] {_resumen(s1, c1)}')
    bods = Counter(str(f.get('f150_id') or f.get('bodega') or '').strip() for f in _filas(c1))
    print(f'  bodegas en la página filtrada: {dict(bods)}')
    if r0 and r1 and r1 < r0:
        conclusiones.append('DINÁMICAS: aceptan `parametros` (el total bajó con el filtro). '
                            'La consulta de ventas nueva puede filtrarse por fecha en cada llamada.')
    elif s1 == 200:
        conclusiones.append('DINÁMICAS: ignoran `parametros` (mismo total con y sin filtro). '
                            'La consulta de ventas nueva lleva su ventana de fechas DENTRO del SQL.')
    else:
        conclusiones.append(f'DINÁMICAS: con `parametros` respondió {s1}: no se usa filtro en tiempo de llamada.')


# ══════════════════════════════════════════════════════════════════════════════
# 3 · descubrir_tablas
# ══════════════════════════════════════════════════════════════════════════════

def probar_descubrir(gw, sku, bodega, conclusiones):
    """Lee lo que tenga HOY `…_descubrir_tablas` y lo reconoce por sus columnas:
    los cuatro SQL de `demanda_fuentes.SQL_VALIDACION` (esquema, conceptos de un
    día, volumen por mes, un SKU en un mes) o el de la consulta de ventas."""
    _t(f'3 · {DESCUBRIR} — lo que tiene HOY (el SQL se edita en Siesa)')
    from app.services.demanda_fuentes import COLUMNAS_VENTAS_DIA, leer_fila_venta_dia
    filas, pag, total = [], 1, None
    while pag <= 30:
        status, cuerpo, seg = _pedir(gw, DESCUBRIR, gw.url_get_dinamico, pagina=pag, tam=100)
        if pag == 1:
            print(f'  [{status}] ({seg:.1f}s) {_resumen(status, cuerpo)}')
        if status != 200:
            break
        lote = _filas(cuerpo)
        filas.extend(lote)
        total = _totales(cuerpo)[0]
        if len(lote) < 100 or (total and len(filas) >= total):
            break
        pag += 1
    if not filas:
        conclusiones.append('DESCUBRIR: sin filas — no se pudo validar nada. Pegar uno de '
                            'los SQL de `demanda_fuentes.SQL_VALIDACION` (python '
                            'scripts/qa_demanda_fuentes_real.py --sql).')
        return
    cols = set(filas[0].keys())
    print(f'  {len(filas)} filas en {pag} página(s) · columnas: {", ".join(sorted(cols))}')
    if {'tabla', 'columna'} <= cols:
        por_tabla = {}
        for f in filas:
            por_tabla.setdefault(str(f.get('tabla')).strip(), []).append(str(f.get('columna')).strip())
        for t, cs in sorted(por_tabla.items()):
            print(f'    {t}: {", ".join(sorted(cs))}')
        necesarias = {'f470_rowid_docto', 'f470_rowid_item_ext', 'f470_rowid_bodega',
                      'f470_id_concepto', 'f470_ind_naturaleza', 'f470_cant_base',
                      'f350_rowid', 'f350_fecha', 'f350_ind_estado', 'f350_id_cia',
                      'f121_rowid', 'f121_rowid_item', 'f120_rowid', 'f120_referencia',
                      'f150_rowid', 'f150_id'}
        vistas = {c for cs in por_tabla.values() for c in cs}
        faltan = sorted(necesarias - vistas)
        conclusiones.append('ESQUEMA: ' + ('todas las columnas del SQL existen: se puede registrar.'
                                           if not faltan else f'FALTAN {faltan}: corregir el SQL '
                                           'antes de registrarlo.'))
    elif {'tipo_docto', 'concepto', 'naturaleza'} <= cols:
        for f in sorted(filas, key=lambda x: (str(x.get('bodega')), str(x.get('tipo_docto')))):
            print(f"    {str(f.get('bodega')).strip():5} {str(f.get('tipo_docto')).strip():5} "
                  f"concepto {f.get('concepto')} nat {f.get('naturaleza')} estado {f.get('estado')}: "
                  f"{f.get('lineas')} líneas, {f.get('unidades')} und")
        tiendas = {str(f.get('bodega')).strip() for f in filas
                   if str(f.get('concepto')) == '501' and str(f.get('bodega')).strip() != 'NB1'}
        conclusiones.append('CONCEPTOS DEL DÍA: la venta 501 aparece en las bodegas '
                            f'{sorted(tiendas) or "ninguna fuera de NB1"} — si están las tiendas, '
                            'la caja (POS acumulado) SÍ entra a la T470 como 501.')
    elif {'anio', 'mes', 'filas'} <= cols:
        tot = 0
        for f in sorted(filas, key=lambda x: (int(x.get('anio') or 0), int(x.get('mes') or 0))):
            n = int(f.get('filas') or 0)
            tot += n
            print(f"    {f.get('anio')}-{int(f.get('mes') or 0):02d}: {n} filas "
                  f"({(n + 99) // 100} páginas) · {f.get('referencias')} refs · "
                  f"{f.get('bodegas')} bodegas · vendido {f.get('vendido')}")
        conclusiones.append(f'VOLUMEN: {tot} filas ≈ {(tot + 99) // 100} páginas para el '
                            'histórico; el reciente (14 días) ≈ medio mes de filas.')
    elif set(COLUMNAS_VENTAS_DIA) <= cols:
        leidas = [x for x in map(leer_fila_venta_dia, filas) if x]
        del_sku = [x for x in leidas if x['referencia'] == sku and x['bodega'] == bodega]
        ordenes = sorted(x['orden'] for x in leidas)
        print(f'  Reconocido: la consulta de ventas. {len(leidas)}/{len(filas)} legibles; '
              f'orden {ordenes[:1]}…{ordenes[-1:]} de total_filas '
              f'{leidas[0]["total_filas"] if leidas else None}')
        for x in sorted(del_sku, key=lambda y: y['fecha']):
            print(f"    {x['fecha']} {x['bodega']}: vendido {x['vendido']} devuelto {x['devuelto']}")
        completa = bool(leidas) and ordenes == list(range(1, leidas[0]['total_filas'] + 1))
        conclusiones.append(f'VENTAS (SQL de prueba): {len(leidas)} filas legibles, numeración '
                            f"{'completa 1…N' if completa else 'con huecos o repetidas'}; "
                            f'{sku} en {bodega}: {sum(x["vendido"] for x in del_sku)} vendido. '
                            'Cuadrar contra la sección 5 (facturas desde pedido) del mismo mes.')
    else:
        for f in filas[:3]:
            print('   ', {k: f[k] for k in list(f)[:12]})
        conclusiones.append('DESCUBRIR: responde con otro SQL (no uno de validación).')


# ══════════════════════════════════════════════════════════════════════════════
# 4 · Estándar candidatas
# ══════════════════════════════════════════════════════════════════════════════

def probar_estandar(gw, conclusiones):
    _t('4 · Consultas ESTÁNDAR candidatas a movimientos / POS (nombres no confirmados)')
    responden = []
    for nombre in ESTANDAR_CANDIDATAS:
        status, cuerpo, _ = _pedir(gw, nombre, gw.url_get, tam=1)
        print(f'  [{status}] {nombre}: {_resumen(status, cuerpo)}')
        if status == 200:
            responden.append(nombre)
            f = _filas(cuerpo)
            if f:
                print('     columnas:', ', '.join(sorted(f[0].keys()))[:400])
    conclusiones.append('ESTÁNDAR candidatas: ' + (', '.join(responden) if responden else
                        'ninguna responde (401 = no registrada o sin permiso; no se afirma cuál)'))


# ══════════════════════════════════════════════════════════════════════════════
# 5 · Facturas desde pedido
# ══════════════════════════════════════════════════════════════════════════════

def _leer_ventas(gw, filtro, max_paginas):
    from app.services.fotos_siesa_service import leer_paginado
    return leer_paginado(gw, API_VENTAS, filtro, clave=lambda f: f.get('f470_rowid'),
                         max_paginas=max_paginas)


def _perfil_ventas(filas):
    por_bod = Counter(str(f.get('f150_id') or '').strip() for f in filas)
    conceptos = Counter(str(f.get('f470_id_concepto')) for f in filas)
    tipos = Counter(str(f.get('f350_id_tipo_docto') or '').strip() for f in filas)
    docs = {(f.get('f350_id_co'), f.get('f350_id_tipo_docto'), f.get('f350_consec_docto')) for f in filas}
    refs = {str(f.get('f120_referencia') or '').strip() for f in filas}
    und = sum(float(f.get('f470_cant_base') or 0) for f in filas)
    return {'lineas': len(filas), 'documentos': len(docs), 'referencias': len(refs),
            'unidades': round(und, 2), 'bodegas': dict(por_bod), 'conceptos': dict(conceptos),
            'tipos_docto': dict(tipos)}


def probar_ventas(gw, dia, mes, co_cdi, recorrer_mes, conclusiones):
    from app.services.fotos_siesa_service import cos_operados
    from app.services.siesa_filtro import lit, lit_fecha
    _t(f'5 · {API_VENTAS} por CO el {dia} (y la semana {mes[0]}…{mes[1]}, día por día)')
    cos = cos_operados()
    top = Counter()
    total_dia = 0
    for co in cos:
        filtro = (f'f350_id_co = {lit(co, "f350_id_co")} '
                  f'AND f350_fecha >= {lit_fecha(dia)} AND f350_fecha <= {lit_fecha(dia)}')
        t0 = time.time()
        lec = _leer_ventas(gw, filtro, 20)
        p = _perfil_ventas(lec.filas)
        total_dia += p['lineas']
        print(f"  CO {co}: {'completa' if lec.completa else 'INCOMPLETA: ' + str(lec.motivo)} · "
              f"{lec.paginas} pág · {time.time() - t0:.1f}s · {p['lineas']} líneas · {p['documentos']} docs · "
              f"{p['unidades']:g} und · bodegas {p['bodegas']} · conceptos {p['conceptos']} · tipos {p['tipos_docto']}")
        for f in lec.filas:
            if str(f.get('f470_id_concepto')) == '501':
                top[(str(f.get('f120_referencia') or '').strip(), str(f.get('f150_id') or '').strip())] += \
                    float(f.get('f470_cant_base') or 0)
    conclusiones.append(f'VENTAS DESDE PEDIDO {dia}: {total_dia} líneas en {len(cos)} COs '
                        '(ver por CO arriba: una tienda con 0 líneas no vende por pedido; su venta es de caja).')
    if recorrer_mes:
        # Una semana DÍA POR DÍA: el rango de un mes entero en una sola consulta
        # no alcanza a responder en 30 s (medido en producción el 2026-09-27).
        a, b = mes
        for co in (co_cdi, '004'):
            tot = Counter()
            refs = set()
            t0 = time.time()
            incompletos = []
            d = a
            while d <= b:
                filtro = (f'f350_id_co = {lit(co, "f350_id_co")} '
                          f'AND f350_fecha >= {lit_fecha(d)} AND f350_fecha <= {lit_fecha(d)}')
                lec = _leer_ventas(gw, filtro, 30)
                p = _perfil_ventas(lec.filas)
                if not lec.completa:
                    incompletos.append(f'{d}: {lec.motivo}')
                tot['lineas'] += p['lineas']
                tot['paginas'] += lec.paginas
                tot['documentos'] += p['documentos']
                tot['unidades'] += p['unidades']
                refs |= {str(f.get('f120_referencia') or '').strip() for f in lec.filas}
                print(f"    CO {co} {d}: {p['lineas']} líneas · {lec.paginas} pág · "
                      f"{'completa' if lec.completa else 'INCOMPLETA'}")
                d += timedelta(days=1)
            print(f"  Semana {a}…{b} CO {co}: {tot['lineas']} líneas · {tot['paginas']} pág · "
                  f"{tot['documentos']} docs · {len(refs)} refs · {tot['unidades']:g} und · "
                  f"{time.time() - t0:.0f}s · incompletos {incompletos or 'ninguno'}")
            conclusiones.append(f"VENTAS DESDE PEDIDO semana {a}…{b} CO {co}: {tot['lineas']} líneas, "
                                f"{tot['paginas']} páginas, {len(refs)} referencias.")
    return [k for k, _v in top.most_common(3)]


# ══════════════════════════════════════════════════════════════════════════════
# 6 · InvFecha
# ══════════════════════════════════════════════════════════════════════════════

def probar_invfecha(gw, pares, conclusiones):
    _t('6 · InvFecha: existencia, comprometido y POS no acumulado')
    for ref, bod in pares:
        st, cuerpo, _ = _pedir(gw, API_INVFECHA, gw.url_get, tam=10,
                               parametros=f"f120_referencia = ''{ref}'' AND f150_id = ''{bod}''")
        filas = _filas(cuerpo)
        ex = sum(float(f.get('f400_cant_existencia_1') or 0) for f in filas)
        com = sum(float(f.get('f400_cant_comprometida_1') or 0) for f in filas)
        pos = sum(float(f.get('f400_cant_pos_1') or 0) for f in filas)
        ssc = sum(float(f.get('f400_cant_salida_sin_conf_1') or 0) for f in filas)
        print(f'  [{st}] {ref} {bod}: {len(filas)} fila(s) · existencia {ex:g} · comprometido {com:g} · '
              f'POS no acumulado {pos:g} · salida sin confirmar {ssc:g}')
    print('\n  ¿Hay venta de caja sin acumular (POS) por bodega? (primera página, 5 filas)')
    for bod in ('NS1', 'NC1', 'PC1', 'PT1', 'FC1', 'FN1', 'NB1'):
        st, cuerpo, _ = _pedir(gw, API_INVFECHA, gw.url_get, tam=5,
                               parametros=f"f150_id = ''{bod}'' AND f400_cant_pos_1 <> 0")
        filas = _filas(cuerpo)
        print(f"    [{st}] {bod}: {len(filas)} fila(s) con POS no acumulado"
              + (f" · ej. {filas[0].get('f120_referencia')} pos={filas[0].get('f400_cant_pos_1')}" if filas else ''))
    conclusiones.append('INVFECHA: ver sección 6 (un POS no acumulado > 0 es venta de caja que todavía '
                        'no está en t470).')


# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 7 · La lectura de verdad (cuando la consulta nueva esté registrada)
# ══════════════════════════════════════════════════════════════════════════════

def probar_lectura_ventas(reciente, max_paginas, conclusiones):
    """Corre `demanda_fuentes.descargar_ventas_dia` —el mismo código del botón—
    contra una base SQLite DESECHABLE y dice qué fuente elegiría la cascada.
    Solo GET; `_post` bloqueado."""
    _t(f"7 · La lectura de la venta diaria ({'reciente' if reciente else 'histórico'}), "
       f'tope {max_paginas} páginas, base SQLite desechable')
    from app import create_app
    from app.extensions import db
    app = create_app()
    if not str(app.config.get('SQLALCHEMY_DATABASE_URI', '')).startswith('sqlite'):
        raise SystemExit('La app no quedó sobre SQLite: no se sigue.')
    with app.app_context():
        db.create_all()
        from app.services import demanda_fuentes as dfu
        r = dfu.descargar_ventas_dia(reciente=reciente, max_paginas=max_paginas)
        for k, v in r.items():
            print(f'  {k}: {v}')
        f = dfu.fuente_de_demanda()
        print(f"  Cascada: {f['fuente']} · {f.get('texto')}")
        print(f"  Apta para: {f['apta_para']}")
        conclusiones.append(f"LECTURA: {'completa' if r['completa'] else 'INCOMPLETA'} · "
                            f"{r['filas_leidas']}/{r['filas_declaradas']} filas en {r['paginas']} "
                            f"páginas, {r['minutos']} min · días guardados {r['dias_guardados']} · "
                            f"motivo {r['motivo']}")


# ══════════════════════════════════════════════════════════════════════════════
# 8 · Ensayo de la bandeja con datos reales (facturas desde pedido + existencias)
# ══════════════════════════════════════════════════════════════════════════════

def ensayo_bandeja(dias, conclusiones):
    """La máquina entera, sin la consulta nueva: fotografía las facturas desde
    pedido de `dias` días (GET, por CO y día), descarga las existencias de las
    bodegas (GET, la consulta de siempre) y corre la bandeja real sobre una
    base SQLite DESECHABLE. Es la demanda PARCIAL (sin la caja de las tiendas):
    lo que proponga es una cota inferior, y la bandeja lo dice."""
    _t(f'8 · Ensayo de la bandeja: {dias} días de facturas desde pedido + existencias')
    from app import create_app
    from app.extensions import db
    app = create_app()
    if not str(app.config.get('SQLALCHEMY_DATABASE_URI', '')).startswith('sqlite'):
        raise SystemExit('La app no quedó sobre SQLite: no se sigue.')
    with app.app_context():
        db.create_all()
        from app.services import compras_bandeja, demanda_fuentes as dfu
        from app.services.inventario_siesa_service import _descargar_inventario_siesa_raw
        t0 = time.time()
        rr = dfu.rellenar_ventas_pedido(dias)
        print(f"  Facturas desde pedido: {rr['corridas_completas']} corridas completas, "
              f"{rr['n_incompletas']} incompletas ({time.time() - t0:.0f}s)")
        for x in rr['incompletas'][:5]:
            print('    ·', x)
        t1 = time.time()
        inv = _descargar_inventario_siesa_raw(forzar=True) or {}
        print(f"  Existencias: {sum(len(v) for v in inv.values())} filas de "
              f"{len(inv)} bodegas ({time.time() - t1:.0f}s)")
        f = dfu.fuente_de_demanda()
        print(f"  Cascada: {f['fuente']} · {f.get('texto')}")
        b = compras_bandeja.bandeja()
        res = b.get('resumen') or {}
        print(f"  Bandeja: {b.get('estado')} · {res.get('lineas')} líneas · urgentes "
              f"{res.get('urgentes')} · esta semana {res.get('esta_semana')} · próximas "
              f"{res.get('proximas')} · demanda {b.get('demanda', {}).get('nivel')}")
        lineas = [l for p in b.get('proveedores') or [] for l in p['lineas']]
        lineas.sort(key=lambda l: (compras_bandeja.ORDEN_URGENCIA[l['urgencia']],
                                   -(l['porque'].get('vende_dia') or 0)))
        for l in lineas[:15]:
            p = l['porque']
            print(f"    {l['urgencia']:11} {l['referencia']:14} pedir {l['pedir_unidades']:>6} · "
                  f"vende {p.get('vende_dia'):.2f}/día · posición {p.get('posicion')} · "
                  f"punto de pedido {p.get('punto_de_pedido')}")
        conclusiones.append(f"ENSAYO BANDEJA: fuente {f['fuente']} ({f['cobertura'].get('dias')} días) → "
                            f"{b.get('estado')}, {res.get('lineas')} líneas "
                            f"({res.get('urgentes')} urgentes). Parcial: cota inferior.")


def _ultimo_habil(hoy):
    d = hoy - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _mes_anterior(hoy):
    primero = hoy.replace(day=1)
    fin = primero - timedelta(days=1)
    return fin.replace(day=1), fin


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ambiente', choices=('qa', 'produccion'), required=False)
    ap.add_argument('--dia')
    ap.add_argument('--mes', help='AAAA-MM (mide ese mes día por día; default: la semana anterior)')
    ap.add_argument('--sku', default='PAPELSP9218')
    ap.add_argument('--bodega', default='NB1')
    ap.add_argument('--co-cdi', default='003')
    ap.add_argument('--sin-mes', action='store_true')
    ap.add_argument('--solo', default='')
    ap.add_argument('--sql', action='store_true', help='imprime el SQL propuesto y sale')
    ap.add_argument('--leer', choices=('reciente', 'historico'),
                    help='corre la lectura real (sección 7) sobre SQLite desechable')
    ap.add_argument('--max-paginas', type=int, default=300)
    ap.add_argument('--ensayo-bandeja', type=int, metavar='DIAS',
                    help='sección 8: facturas desde pedido de DIAS días + existencias + bandeja')
    args = ap.parse_args()

    if args.sql:
        os.environ.setdefault('DATABASE_URL', 'sqlite://')
        from app.services import demanda_fuentes as dfu
        # Una consulta dinámica por ventana (2026-09-27): la reciente (relativa)
        # y una por período del histórico, con fechas FIJAS. El nombre es el
        # que el WMS va a pedir: registrarla con ese nombre exacto.
        n = 1
        print(f'-- {n}) Consulta dinámica NUEVA: {dfu.consulta_ventas_dia(True)} '
              f'(la de todos los días, {dfu.DIAS_RECIENTE} días)')
        print(dfu.sql_ventas_dia(dfu.DIAS_RECIENTE))
        for v in dfu.ventanas_historicas():
            n += 1
            print(f'\n-- {n}) Consulta dinámica NUEVA: {v["consulta"]} '
                  f'(histórico, {v["desde"]} a {v["hasta"]})')
            print(dfu.sql_de_ventana(v))
        for k, sql in dfu.SQL_VALIDACION.items():
            print(f'\n-- Validación en papeleriamedellin_pame_descubrir_tablas: {k}')
            print(sql)
        return 0
    if not args.ambiente:
        ap.error('--ambiente qa|produccion es obligatorio')

    _cerrar_escrituras()
    _preparar_entorno(args.ambiente)
    _bloquear_post_del_gateway()
    from app.services.connekta_gateway import connekta as gw
    from app.utils.fecha import dia_operativo

    hoy = dia_operativo()
    dia = date.fromisoformat(args.dia) if args.dia else _ultimo_habil(hoy)
    if args.mes:
        a = date.fromisoformat(args.mes + '-01')
        b = (a.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        mes = (a, b)
    else:
        lunes = dia - timedelta(days=dia.weekday() + 7)
        mes = (lunes, lunes + timedelta(days=6))
    solo = {s.strip() for s in args.solo.split(',') if s.strip()}

    def corre(seccion):
        return not solo or seccion in solo

    print(f'Ambiente pedido: {args.ambiente} · Siesa: {gw.host_siesa} · ensayo={gw.modo_ensayo} · '
          f'simulación={gw.modo_simulacion} · hora Bogotá {datetime.utcnow() - timedelta(hours=5):%Y-%m-%d %H:%M}')
    if gw.modo_simulacion:
        print('Sin credenciales en el entorno: no hay nada que medir.')
        return 2
    if not gw.modo_ensayo:
        print('MODO_ENSAYO no quedó activo: se aborta.')
        return 3

    conclusiones = []
    if corre('kardex'):
        probar_kardex(gw, conclusiones)
    if corre('dinamicas'):
        probar_parametros_dinamica(gw, args.bodega, conclusiones)
    if corre('descubrir'):
        probar_descubrir(gw, args.sku, args.bodega, conclusiones)
    if corre('estandar'):
        probar_estandar(gw, conclusiones)
    top = []
    if corre('ventas'):
        top = probar_ventas(gw, dia, mes, args.co_cdi, not args.sin_mes, conclusiones)
    if corre('invfecha'):
        pares = [(args.sku, args.bodega)] + [p for p in top if p != (args.sku, args.bodega)]
        probar_invfecha(gw, pares, conclusiones)

    if args.leer:
        probar_lectura_ventas(args.leer == 'reciente', args.max_paginas, conclusiones)
    if args.ensayo_bandeja:
        ensayo_bandeja(max(28, min(args.ensayo_bandeja, 120)), conclusiones)

    _t('CONCLUSIONES')
    for c in conclusiones:
        print(' ·', c)
    return 0


if __name__ == '__main__':
    sys.exit(main())
