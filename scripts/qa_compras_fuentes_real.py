#!/usr/bin/env python
"""
Verificación EN VIVO de las fuentes de compras (m046compras) contra Siesa QA.
**Solo GET.**

Qué verifica, en orden (cada paso imprime su veredicto):

1. **OCs abiertas** (`API_v2_Compras_Ordenes`, `f420_ind_estado = 1` y `= 2`):
   que la paginación termine (y si repite rowids), cuántas líneas, cuántas
   vienen SIN los campos `_base` (irían a «sin unidad base»), a qué bodegas
   (dentro/fuera de `_BODEGAS_PV`), y qué campos del contrato que usa el sync
   **no vinieron** en la respuesta real (Regla 1: contrato vs. respuesta).
2. **Unidades**: que `f421_cant_pedida_base ≈ f421_cant_pedida × f421_factor`
   en las líneas que traen las dos cosas (si no, la regla de unidad base está
   mal y se dice).
3. **Historial** (`f420_ind_estado = 3 AND f420_fecha >= ''AAAAMMDD''`): que el
   filtro de fecha entre comillas traiga filas, y cuántas traen
   `f420_fecha_ts_parcial` / `_cumplido` — de ahí sale el lead time medido.
   Compara contra el mismo filtro SIN comillas (Siesa suele contestar «sin
   registros» en vez de rechazarlo).
4. **Proveedores** (`API_v2_Terceros`, nunca consultada desde el WMS): si está
   registrada en Connekta (o da 401) y cuántos proveedores activos trae.
5. **Clasificación (238920)**: qué campos trae la respuesta GET (el `.docx` es
   el del plano de importación) y, si están los del plano, los planes que
   existen con cuántos ítems — para que el dueño elija `SIESA_CRITERIO_MARCA`.
6. **Kardex**: una sola página de la consulta dinámica del kardex (¿sigue en
   401?). NO descarga el kardex.
7. Con lo sincronizado en la base local: `en_camino`, `lead_time` por
   proveedor y cuántos SKU tienen precio de OC.

Cerrojos (igual que `qa_fotos_siesa_real.py`): `MODO_ENSAYO=true` en el proceso
**y** `ConnektaGateway._post` reemplazado por uno que revienta; `DATABASE_URL`
forzado a un SQLite desechable (el `.env.qa` trae la base de Railway y NO se
usa); credenciales cargadas en el proceso, nunca impresas. Fuera de 7:00–19:30
Bogotá sale sin tocar Siesa (Regla 14).

Uso:
    venv/bin/python scripts/qa_compras_fuentes_real.py
    venv/bin/python scripts/qa_compras_fuentes_real.py --desde 2026-01-01 --sin-terceros
    SIESA_CRITERIO_MARCA=005 venv/bin/python scripts/qa_compras_fuentes_real.py --solo-marca
"""
import argparse
import os
import sys
import tempfile
from collections import Counter
from datetime import date

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

#: Campos de la respuesta que el sync lee. Si alguno no viene en la respuesta
#: real, `.get()` devuelve None y el dato se pierde en silencio: se lista.
CAMPOS_QUE_USA_EL_SYNC = (
    'f420_rowid', 'f420_id_co', 'f420_id_tipo_docto', 'f420_consec_docto', 'f420_fecha',
    'f420_ind_estado', 'f420_fecha_ts_aprobacion', 'f420_fecha_ts_parcial',
    'f420_fecha_ts_cumplido', 'f420_id_moneda_docto', 'f420_tasa_conv', 'f200_id_prov',
    'f200_nit_prov', 'f200_razon_social_prov', 'f202_id_sucursal_prov', 'f120_referencia',
    'f150_id', 'f421_rowid', 'f421_id_co_movto', 'f421_ind_estado', 'f421_ind_obsequio',
    'f421_id_unidad_medida', 'f421_factor', 'f421_cant_pedida', 'f421_cant_entrada',
    'f421_cant_pedida_base', 'f421_cant_entrada_base', 'f421_cant_importacion_base',
    'f421_precio_unitario', 'f421_fecha_entrega',
)


def _cargar_entorno(db_path: str):
    from dotenv import dotenv_values
    ruta = os.path.join(RAIZ, '.env.qa')
    if not os.path.exists(ruta):
        ruta = os.path.join(RAIZ.split('/.claude/worktrees/')[0], '.env.qa')
    for k, v in dotenv_values(ruta).items():
        if k == 'DATABASE_URL' or v is None:
            continue
        if k == 'SIESA_CRITERIO_MARCA' and os.environ.get(k):
            continue                    # la de la línea de comandos manda
        os.environ[k] = v
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    os.environ['MODO_ENSAYO'] = 'true'
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-compras-fuentes-32-bytes-o-mas-xxxxx')
    if not os.environ['DATABASE_URL'].startswith('sqlite:///'):
        raise SystemExit('DATABASE_URL no es SQLite: este script no corre contra otra base')


def _tabla(resp):
    det = resp.get('detalle') if isinstance(resp, dict) else None
    if isinstance(det, dict):
        return det.get('Table', det.get('Datos'))
    return None


def _paso(n, titulo):
    print(f'\n  {n} · {titulo}')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--desde', default=None, help='YYYY-MM-DD del historial (default: hace 365 días)')
    p.add_argument('--sin-historial', action='store_true')
    p.add_argument('--sin-terceros', action='store_true')
    p.add_argument('--sin-marca', action='store_true')
    p.add_argument('--solo-marca', action='store_true')
    p.add_argument('--db', default=None, help='ruta del SQLite (por defecto, temporal)')
    args = p.parse_args()

    db_path = args.db or os.path.join(tempfile.mkdtemp(prefix='qa_compras_'), 'compras.db')
    _cargar_entorno(db_path)

    from app import create_app
    from app.extensions import db
    from app.services import compras_fuentes, compras_oc_sync
    from app.services.connekta_gateway import ConnektaGateway, connekta
    from app.services.fotos_siesa_service import ventana_abierta
    from app.services.inventario_siesa_service import _BODEGAS_PV
    from app.services.maestro_compras_carga import filas_marca_desde_siesa, _CLAVES_PLAN

    def _sin_post(self, *a, **k):
        raise RuntimeError('qa_compras_fuentes_real: un POST no debería ocurrir nunca acá')
    ConnektaGateway._post = _sin_post

    app = create_app()
    with app.app_context():
        db.create_all()
        assert connekta.modo_ensayo, 'MODO_ENSAYO no quedó activo'
        if connekta.modo_simulacion:
            raise SystemExit('Sin credenciales de Connekta (.env.qa): nada que verificar')
        if not ventana_abierta():
            raise SystemExit('Fuera de la ventana de Siesa (7:00–19:30 Bogotá). Regla 14.')
        print('\n  FUENTES DE COMPRAS — verificación en vivo (solo GET)')
        print(f'  Siesa: {connekta.host_siesa}  ·  base local: {db_path}')

        if not args.solo_marca:
            # 1 · OCs abiertas, crudo: contrato vs respuesta, bodegas, unidades
            _paso(1, 'OCs abiertas (API_v2_Compras_Ordenes)')
            crudas = []
            for estado in compras_oc_sync.ESTADOS_ABIERTOS:
                f, ok, motivo, pags = compras_oc_sync._descargar(
                    connekta, connekta.api_ordenes, f'f420_ind_estado = {estado}', 0.5)
                print(f'    estado {estado}: {len(f)} filas en {pags} página(s) · '
                      f'completa={ok} {motivo or ""}')
                crudas.extend(f)
            if crudas:
                faltan = [c for c in CAMPOS_QUE_USA_EL_SYNC if c not in crudas[0]]
                print(f'    campos del contrato que NO vinieron: {faltan or "ninguno"}')
                sin_base = sum(1 for r in crudas if r.get('f421_cant_pedida_base') is None
                               or r.get('f421_cant_entrada_base') is None)
                print(f'    líneas sin cantidades _base: {sin_base} de {len(crudas)}')
                bodegas = Counter((r.get('f150_id') or '').strip() or '(vacía)' for r in crudas)
                fuera = {b: n for b, n in bodegas.items() if b not in _BODEGAS_PV}
                print(f'    bodegas: {dict(bodegas.most_common())}')
                print(f'    fuera de _BODEGAS_PV (no cuentan como en camino): {fuera or "ninguna"}')
                monedas = Counter((r.get('f420_id_moneda_docto') or '').strip() or '(vacía)'
                                  for r in crudas)
                print(f'    monedas: {dict(monedas)}')

                _paso(2, 'Unidades: pedida_base ≈ pedida × factor')
                malas = []
                for r in crudas:
                    try:
                        pb, pe, fac = (float(r['f421_cant_pedida_base']),
                                       float(r['f421_cant_pedida']), float(r['f421_factor']))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if abs(pb - pe * fac) > 0.01:
                        malas.append((r.get('f421_rowid'), pe, fac, pb))
                print(f'    líneas donde NO cuadra: {len(malas)} {malas[:5]}')

            r = compras_oc_sync.sincronizar_ocs(gateway=connekta)
            print(f'    sync a la base local: {r}')

            if not args.sin_historial:
                _paso(3, 'Historial de OCs cumplidas (lead time y precio)')
                from app.services.siesa_filtro import lit_fecha
                desde = date.fromisoformat(args.desde) if args.desde else None
                h = compras_oc_sync.sincronizar_historial(gateway=connekta, desde=desde)
                print(f'    con comillas: {h}')
                d = desde or date.fromisoformat(h.get('desde'))
                sin = connekta._get(connekta.api_ordenes, {
                    'parametros': f"f420_ind_estado = 3 AND f420_fecha >= {d.strftime('%Y%m%d')}",
                    'paginacion': 'numPag=1|tamPag=100'})
                print(f'    SIN comillas (página 1): {len(_tabla(sin) or [])} filas '
                      f'(con comillas se esperan más; {lit_fecha(d)} es la forma verificada)')
                from app.models.compras_fuentes import OcLineaSiesa
                cumpl = OcLineaSiesa.query.filter(OcLineaSiesa.estado_oc == 3).all()
                print(f'    cumplidas guardadas: {len(cumpl)} · con ts_parcial: '
                      f'{sum(1 for l in cumpl if l.fecha_parcial)} · con ts_cumplido: '
                      f'{sum(1 for l in cumpl if l.fecha_cumplido)}')

            if not args.sin_terceros:
                _paso(4, 'Proveedores del maestro (API_v2_Terceros)')
                t = compras_oc_sync.sincronizar_proveedores_terceros(gateway=connekta)
                print(f'    {t}')

        if not args.sin_marca:
            _paso(5, 'Clasificación de ítems (238920) — ¿cuál plan es «marca»?')
            try:
                resp = connekta.get_clasificacion_items(pagina=1)
                filas = _tabla(resp) or []
                print(f'    página 1: {len(filas)} filas · campos: '
                      f'{sorted(filas[0]) if filas else "(sin filas)"}')
                if filas:
                    minus = {k.lower(): k for k in filas[0]}
                    kp = next((minus[c] for c in _CLAVES_PLAN if c in minus), None)
                    if kp:
                        planes = Counter(str(f.get(kp)).strip() for f in filas)
                        print(f'    planes en la página 1: {dict(planes.most_common())}')
            except Exception as e:
                print(f'    ERROR: {e}')
            if os.getenv('SIESA_CRITERIO_MARCA'):
                m = filas_marca_desde_siesa(connekta)
                print(f'    plan {os.getenv("SIESA_CRITERIO_MARCA")}: '
                      f'{len(m.get("filas", []))} ítems · completa={m.get("completa")} '
                      f'{m.get("omitido") or m.get("campos_no_reconocidos") or ""}')
            else:
                print('    SIESA_CRITERIO_MARCA sin configurar: no se aplica nada (lo decide el dueño)')

        if args.solo_marca:
            return

        _paso(6, 'Kardex: una página de la consulta dinámica (sin descargar)')
        from app.services.kardex_service import NOMBRE_CONSULTA
        try:
            k = connekta._get(NOMBRE_CONSULTA, {'paginacion': 'numPag=1|tamPag=100'},
                              url=connekta.url_get_dinamico)
            print(f'    {NOMBRE_CONSULTA}: {len(_tabla(k) or [])} filas en la página 1')
        except Exception as e:
            print(f'    {NOMBRE_CONSULTA}: ERROR {str(e)[:200]} '
                  '(401 = permiso de la consulta en Siesa; KARDEX_AUTO no servirá hasta resolverlo)')

        _paso(7, 'Lo que queda en la base local')
        ec = compras_fuentes.en_camino()
        top = sorted(ec['por_sku'].items(), key=lambda kv: -kv[1])[:10]
        print(f'    en camino: {len(ec["por_sku"])} SKU · top {top}')
        print(f'    declaración: { {k: v for k, v in ec["declaracion"].items() if k != "sync_oc"} }')
        obs = compras_fuentes.observaciones_lead_time()
        print(f'    observaciones de lead time: {len(obs["por_oc"])} · descartadas {obs["descartadas"]}')
        for fila in compras_fuentes.lead_times_por_proveedor(obs)[:15]:
            print(f'      {fila["proveedor"]}: {fila["lt_dias"]}±{fila["sigma_lt"]} d '
                  f'n={fila["n_proveedor"]} nivel={fila["nivel"]}')
        from app.models.compras_fuentes import OcLineaSiesa
        refs = [r for (r,) in db.session.query(OcLineaSiesa.referencia).distinct() if r]
        precios = compras_fuentes.precios_oc(refs)
        con = sum(1 for v in precios.values() if v.get('costo'))
        print(f'    SKU con precio de OC (en pesos vía a_cop, unidad base): {con} de {len(refs)} '
              f'· solo con moneda sin conversión: {len(precios) - con}')


if __name__ == '__main__':
    main()
