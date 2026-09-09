"""
Verificacion REAL contra Siesa QA (.env.qa) de ConnektaTrasladosGateway
(app/services/connekta_traslados_gateway.py, extraido de ConnektaGateway el
2026-09-09, deuda de tamano paso 6).

En este ambiente el flujo real de traslados NO pasa por RIT (174646/174720/
174930 no se usan) -- solo STS (173076) y ETS (173079) directo, confirmado
por el usuario el 2026-09-09. Esta prueba mueve las MISMAS 10 unidades de
PAPELSP9218 en cadena por las 10 bodegas operadas (BODEGAS_OPERADAS en
tests/test_bodegas_coherentes.py), origen -> destino -> origen del
siguiente salto, para ejercitar STS+ETS contra cada bodega real al menos
una vez (como origen y como destino).

Item elegido: PAPELSP9218 (confirmado en catalogo Siesa QA con stock real
en 8/10 bodegas al 2026-09-09; el codigo interno WMS '0017368' que se
penso usar primero NO existe en el catalogo Siesa bajo ninguna variante
-- se descarto tras verificar en vivo con buscar_item_por_referencia).

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_traslados_gateway_real.py --si-de-verdad
    venv/Scripts/python.exe scripts/qa_traslados_gateway_real.py --si-de-verdad --desde-salto 3
    venv/Scripts/python.exe scripts/qa_traslados_gateway_real.py --si-de-verdad --cadena NB1,PT1,FF1,FN1,FP1

Corrida real del 2026-09-09 (dos tramos, documentado en CLAUDE.md):
  Tramo 1 (cadena por defecto): NB1->NS1->NS2->NC1->FC1->PC1 -- 5 saltos OK,
    se detuvo en PC1->PT1 porque PC1 tenía "cantidad disponible" NEGATIVA en
    Siesa QA (152 unidades preexistentes en "salida sin confirmar" + 8
    comprometidas, ya excedían la existencia antes de este traslado) -- un
    estado real de datos en QA, no un bug del código. Las 10 unidades
    quedaron físicamente en PC1.
  Tramo 2 (--cadena NB1,PT1,FF1,FN1,FP1): cubre las bodegas restantes
    evitando PC1 como origen, con una segunda tanda de 10 unidades desde NB1
    (que sí tiene disponible de sobra).
"""
import argparse
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

ITEM = 'PAPELSP9218'
CANTIDAD = 10.0

# Orden de la cadena -- las 10 bodegas operadas, visitadas una vez cada una.
CADENA = ['NB1', 'NS1', 'NS2', 'NC1', 'FC1', 'PC1', 'PT1', 'FF1', 'FN1', 'FP1']


def _stock(connekta, bodega):
    try:
        res = connekta.get_inventario_fecha(ITEM, bodega=bodega)
        rows = res.get('detalle', {}).get('Table', []) if isinstance(res, dict) else []
        return rows[0].get('f400_cant_existencia_1') if rows else 0.0
    except Exception as e:
        print(f'  (no se pudo leer stock de {bodega}: {e})')
        return None


def _hop(connekta, co_de_bodega, extraer_consec, idx, origen, destino):
    codigo = f'CAD{idx:02d}{origen}{destino}'
    items = [{'codigo_siesa': ITEM, 'cantidad': CANTIDAD}]

    print(f'\n=== Salto {idx}: {origen} -> {destino}  (codigo={codigo}) ===')
    antes_origen = _stock(connekta, origen)
    antes_destino = _stock(connekta, destino)
    print(f'  ANTES  {origen}={antes_origen}  {destino}={antes_destino}')

    # _post ya lanza excepción si Siesa devuelve codigo != 0 -- si llegamos
    # acá sin excepción, el documento fue aceptado (mismo criterio que
    # TrasladoService, no un chequeo propio de 'codigo').
    print('  --- POST STS (173076) ---')
    res_sts = connekta.transferencia_transito_salida(
        bodega_origen=origen, bodega_transito=connekta.bodega_transito,
        items=items, codigo_solicitud=codigo, bodega_destino=destino,
    )
    print('  Respuesta STS:', res_sts)

    consec_salida = extraer_consec(res_sts)
    if not consec_salida:
        consec_salida = connekta.get_consec_salida_transito_by_alterno(codigo)
        print(f'  consec_salida recuperado via recovery: {consec_salida}')
    if not consec_salida:
        raise RuntimeError(f'STS salto {idx}: no se pudo obtener consec_salida ni por recovery')

    co_destino = co_de_bodega(destino)
    print(f'  --- POST ETS (173079)  consec_salida={consec_salida}  co_destino={co_destino} ---')
    res_ets = connekta.transferencia_transito_entrada(
        bodega_transito=connekta.bodega_transito, bodega_destino=destino,
        items=items, codigo_solicitud=codigo, consec_salida=consec_salida,
        co_destino=co_destino, bodega_origen=origen,
    )
    print('  Respuesta ETS:', res_ets)

    consec_entrada = extraer_consec(res_ets)
    if not consec_entrada:
        consec_entrada = connekta.get_consec_entrada_transito_by_alterno(codigo)
        print(f'  consec_entrada recuperado via recovery: {consec_entrada}')

    time.sleep(1)
    despues_origen = _stock(connekta, origen)
    despues_destino = _stock(connekta, destino)
    print(f'  DESPUES  {origen}={despues_origen}  {destino}={despues_destino}')
    return consec_salida, consec_entrada


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--si-de-verdad', action='store_true',
                     help='Sin esto, solo muestra el plan y no dispara nada real.')
    ap.add_argument('--desde-salto', type=int, default=1,
                     help='Reanudar la cadena desde este salto (1-indexed) si un salto anterior ya se completo.')
    ap.add_argument('--cadena', type=str, default=None,
                     help='Override de la cadena de bodegas, separadas por coma (ej NB1,PT1,FF1). '
                          'Util para saltar una bodega bloqueada sin tocar el default.')
    args = ap.parse_args()

    cadena = args.cadena.split(',') if args.cadena else CADENA

    os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-traslados-gateway-32-bytes-o-mas-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)
    if args.si_de_verdad:
        os.environ['MODO_ENSAYO'] = 'false'

    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True

    with app.app_context():
        db.create_all()
        from app.services.bodegas import co_de_bodega
        from app.services.connekta_gateway import connekta
        from app.services.traslado_service import TrasladoService
        extraer_consec = TrasladoService._extraer_consec

        print(f'MODO_ENSAYO={connekta.modo_ensayo}  modo_simulacion={connekta.modo_simulacion}')
        print(f'Cadena planeada ({len(cadena)} bodegas, {len(cadena) - 1} saltos): '
              f'{" -> ".join(cadena)}')
        print(f'Item={ITEM}  cantidad por salto={CANTIDAD}')

        if not args.si_de_verdad:
            print('\n(Simulacro -- pasa --si-de-verdad para disparar los POST reales)')
            return

        if connekta.modo_ensayo:
            print('\nERROR: MODO_ENSAYO=true seguiría bloqueando los POST. Abortando.')
            sys.exit(2)

        resultados = []
        for idx in range(1, len(cadena)):
            if idx < args.desde_salto:
                print(f'\n(saltando hop {idx}, ya completado en corrida anterior)')
                continue
            origen, destino = cadena[idx - 1], cadena[idx]
            consec_salida, consec_entrada = _hop(
                connekta, co_de_bodega, extraer_consec, idx, origen, destino)
            resultados.append((idx, origen, destino, consec_salida, consec_entrada))

        print('\n=== RESUMEN ===')
        for idx, origen, destino, cs, ce in resultados:
            print(f'  Salto {idx}: {origen}->{destino}  STS consec={cs}  ETS consec={ce}')
        print(f'\nRESULTADO: {len(resultados)}/{len(cadena) - 1} saltos completados con codigo:0.')


if __name__ == '__main__':
    main()
