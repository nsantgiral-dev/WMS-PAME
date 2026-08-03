#!/usr/bin/env python
"""
Reset transaccional del acta de corte — borra el ensayo, conserva la memoria.

Existe porque el riesgo real no es que este script se equivoque: es que alguien
escriba un DELETE a mano el día del corte y se lleve por delante la memoria
analítica. Mejor que exista uno correcto a que se improvise uno.

LA FRONTERA
  Tablas OPERATIVAS  → nacen limpias en el acta de corte.
    Picks, packing, bultos, rutas, recaudos, recepciones, traslados, conteos,
    movimientos y jobs generados validando la app.

  Tablas ANALÍTICAS  → NUNCA se tocan.
    serie_vigia, alarma_vigia, kardex_movimientos, stock_diario,
    juicios_temporada. Sin las 26 semanas de referencia el CUSUM queda ciego
    ~6 meses y se pierde la alarma de Florencia — la primera certificada.
    TSB, ROP y newsvendor consumen esa misma historia.

  Tablas MAESTRAS    → NUNCA se tocan.
    Productos, ubicaciones, usuarios, proveedores, acuerdos, vehículos, y el
    expediente de flota: ficha técnica, documentos y plantillas de inspección.

ARCHIVOS DE FOTOS — lo que este script NO puede limpiar
  Vaciar `flota_foto` borra las filas, no los archivos del volumen. Quedan
  huérfanos ocupando disco. Se limpian aparte con `--fotos`, y solo entonces:
  borrar archivos antes que filas dejaría referencias apuntando a nada, que es
  peor que un archivo de más.

Deny-by-default: solo se vacía lo que está en OPERATIVAS. Si alguien agrega una
tabla protegida a esa lista, el script se niega a correr.

Uso:
    venv/bin/python scripts/reset_transaccional.py              # simulacro
    venv/bin/python scripts/reset_transaccional.py --ejecutar   # de verdad

Después de rellenar, correr scripts/verificar_carga_vigia.py: con los mismos
hashes debe dar CERTIFICADO idéntico. Ese es el mejor test del reset — si el
canon sigue reproduciendo S⁻=6.30, la limpieza fue quirúrgica.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERDE, ROJO, AMAR, GRIS, FIN = '\033[92m', '\033[91m', '\033[93m', '\033[90m', '\033[0m'

# Se vacían. Orden importa: hijos antes que padres por las FKs.
OPERATIVAS = [
    'items_packing',
    'bultos',
    'recaudos_entrega',
    'tareas_packing',
    'tareas_picking',
    'tareas_devolucion',
    'tareas_reposicion',
    'items_recepcion',
    'recepciones',
    'items_solicitud_traslado',
    'solicitudes_traslado',
    'rutas_despacho',
    'sesiones_conteo',
    'movimientos_inventario',
    'siesa_jobs',
    'pedidos_siesa',
    'ubicaciones_huerfanas',
    'fugas_recompra',
    # ── Flota (agregado 2026-08-03) ────────────────────────────────────────
    # Registros del ensayo: turnos, lecturas y fotos. Se vacían.
    #
    # NO están acá `flota_ficha_tecnica` ni `flota_documento_vehiculo`: son el
    # levantamiento de campo. Media mañana recorriendo cinco vehículos, con la
    # foto del tablero y la medida de llanta en la mano. Borrarlas en el corte
    # obliga a hacerlo dos veces, y la segunda nadie la hace.
    'flota_foto',
    'flota_lectura_odometro',
    'flota_custodia',
]

# NUNCA. Si aparecen en OPERATIVAS, el script aborta.
PROTEGIDAS_ANALITICAS = {
    'serie_vigia': 'línea base del CUSUM — sin ella, ciego ~6 meses',
    'alarma_vigia': 'la alarma de Florencia, primera certificada',
    'kardex_movimientos': 'historia de demanda que alimenta los 4 modelos',
    'stock_diario': 'denominador de la descensura',
    'juicios_temporada': 'juicio humano registrado, no recalculable',
}

PROTEGIDAS_MAESTRAS = {
    'productos', 'ubicaciones', 'ubicaciones_productos', 'usuarios', 'almacenes',
    'proveedores', 'acuerdos_marco', 'precios_proveedor', 'vehiculos',
    'conductores', 'rutas_maestras', 'rutas_maestras_paradas', 'lpn',
    'producto_empaques', 'siesa_mapeo_unidades', 'productos_bloqueados',
    'producto_clasificacion_abc', 'contenedores', 'ficha_importacion',
    'items_en_transito', 'stock_siesa',
    # Flota: el expediente del vehículo y el catálogo de inspección.
    # `flota_ficha_tecnica` es levantamiento de campo, no registro de ensayo.
    # `flota_documento_vehiculo` es la vigencia real de SOAT y tecnomecánica.
    # Las plantillas son el catálogo versionado — borrarlas dejaría las
    # inspecciones viejas apuntando a ítems que ya no existen.
    'flota_ficha_tecnica', 'flota_documento_vehiculo',
    'flota_plantilla_inspeccion', 'flota_item_inspeccion',
}

CANONES = ['docs/canon_florencia.json', 'docs/canon_PLANTILLA.json',
           'docs/canones/facturas_co.json', 'docs/canones/rop_dual.json',
           'docs/canones/clasificacion_sb.json']


def _guard():
    """El script se niega a correr si la lista fue contaminada."""
    protegidas = set(PROTEGIDAS_ANALITICAS) | PROTEGIDAS_MAESTRAS
    invasoras = [t for t in OPERATIVAS if t in protegidas]
    if invasoras:
        print(f'\n  {ROJO}ABORTADO — hay tablas protegidas en la lista de borrado:{FIN}')
        for t in invasoras:
            razon = PROTEGIDAS_ANALITICAS.get(t, 'tabla maestra')
            print(f'    · {t} — {razon}')
        print()
        return False
    return True


def _limpiar_fotos_huerfanas(db):
    """Borra del volumen los archivos que ya no tiene ninguna fila.

    Vaciar `flota_foto` borra las filas, no los archivos: quedan ocupando disco
    sin que nadie los reclame. El volumen de Railway tiene tamaño fijo, y
    **llenarse en silencio es el próximo modo de fallo de este diseño** — a
    partir de ahí toda foto nueva cae en `pendiente_evidencia`.

    El orden importa y es este: **primero las filas, después los archivos.**
    Al revés dejaría referencias apuntando a nada, que es peor que un archivo
    de más — un hueco silencioso contra disco ocupado.
    """
    import os
    from pathlib import Path as _P

    from sqlalchemy import text

    raiz = os.getenv('FLOTA_FOTOS_DIR')
    if raiz is None or not raiz.strip():
        return 'FLOTA_FOTOS_DIR no está configurada — no hay volumen que limpiar.'

    vivas = {r[0] for r in db.session.execute(
        text('SELECT storage_ref FROM flota_foto'))}
    base = _P(raiz.strip())
    borrados = liberado = 0
    for archivo in base.rglob('*'):
        if not archivo.is_file():
            continue
        rel = str(archivo.relative_to(base))
        if rel in vivas:
            continue
        liberado += archivo.stat().st_size
        archivo.unlink()
        borrados += 1
    return f'{borrados} archivos huérfanos borrados · {liberado / 1_048_576:.1f} MB liberados'


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ejecutar', action='store_true',
                   help='Borra de verdad. Sin esto solo simula.')
    p.add_argument('--fotos', action='store_true',
                   help='Borra tambien los archivos huerfanos del volumen de '
                        'flota. Solo DESPUES de vaciar las filas.')
    args = p.parse_args()

    if not _guard():
        return 2

    from app import create_app
    from app.extensions import db
    from sqlalchemy import text, func

    app = create_app()
    with app.app_context():
        print()
        print('  RESET TRANSACCIONAL — ACTA DE CORTE')
        print('  ' + '─' * 54)
        if not args.ejecutar:
            print(f'  {AMAR}SIMULACRO — nada se borra. Usa --ejecutar para hacerlo real.{FIN}')
        print()

        # Lo que se conserva, contado ANTES
        print(f'  {VERDE}Se conservan (memoria analítica):{FIN}')
        antes = {}
        for t, razon in PROTEGIDAS_ANALITICAS.items():
            try:
                n = db.session.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
            except Exception:
                n = None
            antes[t] = n
            print(f'    {n if n is not None else "?":>9}  {t}  {GRIS}{razon}{FIN}')

        print(f'\n  {ROJO}Se vacían (transaccional de ensayo):{FIN}')
        total = 0
        for t in OPERATIVAS:
            try:
                n = db.session.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
            except Exception:
                print(f'    {GRIS}{"—":>9}  {t} (no existe){FIN}')
                continue
            total += n or 0
            print(f'    {n:>9}  {t}')

        print(f'\n  {GRIS}Total de filas a borrar: {total:,}{FIN}')

        if not args.ejecutar:
            print(f'\n  {AMAR}Simulacro terminado. Nada se tocó.{FIN}\n')
            return 0

        for t in OPERATIVAS:
            try:
                db.session.execute(text(f'DELETE FROM {t}'))
            except Exception as e:
                print(f'  {AMAR}aviso: {t} — {e}{FIN}')
        db.session.commit()

        if args.fotos:
            print(f'\n  {VERDE}Limpiando archivos huérfanos de flota…{FIN}')
            print(f'  {_limpiar_fotos_huerfanas(db)}')

        # Verificación posterior: la memoria sigue ahí
        print(f'\n  {VERDE}Verificando que la memoria sobrevivió…{FIN}')
        ok = True
        for t, n_antes in antes.items():
            if n_antes is None:
                continue
            n = db.session.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
            marca = f'{VERDE}✓{FIN}' if n == n_antes else f'{ROJO}✗{FIN}'
            if n != n_antes:
                ok = False
            print(f'    {marca} {t}: {n_antes} → {n}')

        faltan = [c for c in CANONES
                  if not os.path.exists(os.path.join(
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))), c))]
        if faltan:
            ok = False
            print(f'\n  {ROJO}Canones faltantes:{FIN}')
            for c in faltan:
                print(f'    · {c}')

        print('\n  ' + '─' * 54)
        if ok:
            print(f'  {VERDE}RESET COMPLETO{FIN} — memoria analítica intacta.')
            print(f'  {GRIS}Siguiente: rellenar y correr scripts/verificar_carga_vigia.py.')
            print(f'  Con los mismos hashes debe dar CERTIFICADO idéntico — si el canon')
            print(f'  sigue reproduciendo S-=6.30, la limpieza fue quirúrgica.{FIN}\n')
            return 0
        print(f'  {ROJO}RESET CON PÉRDIDAS{FIN} — revisar antes de continuar.\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
