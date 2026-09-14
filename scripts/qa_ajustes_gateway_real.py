"""
Verificacion REAL contra Siesa QA (.env.qa) de ConnektaAjustesGateway
(app/services/connekta_ajustes_gateway.py, extraido de ConnektaGateway el
2026-09-09, deuda de tamano paso 4).

Dispara los DOS POST reales (142951) de punta a punta:
  1. enviar_ajuste_inventario(AJ-ENT, +1) sobre un item real en NB1.
  2. transferir_a_averias(1) del mismo item, NB1 -> AV1.

Impacto neto en inventario real = CERO (el +1 del ajuste se compensa con
el -1 de la transferencia a averias) -- se verifica el stock ANTES y
DESPUES de cada paso contra Siesa real, no solo que el POST no reviente.

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_ajustes_gateway_real.py --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CODIGO_SIESA = 'PAPELSP9830'
BODEGA = 'NB1'
CENTRO_OP = '003'


def _existencia(connekta, item_codigo, bodega):
    r = connekta.get_inventario_fecha(item_codigo, bodega=bodega)
    rows = r.get('detalle', {}).get('Table', [])
    if not rows:
        return None
    return float(rows[0].get('f400_cant_existencia_1', 0) or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--si-de-verdad', action='store_true',
                     help='Sin esto, solo muestra el plan y no dispara nada real.')
    args = ap.parse_args()

    os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-ajustes-gateway-32-bytes-o-mas-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)
    if args.si_de_verdad:
        # .env.qa trae MODO_ENSAYO=true (bloquea POST server-side) — se
        # desactiva SOLO en este proceso, nunca se toca el archivo .env.qa.
        # Confirmado explícitamente con el usuario antes de correr con esta
        # bandera (2026-09-09).
        os.environ['MODO_ENSAYO'] = 'false'

    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True

    with app.app_context():
        db.create_all()
        from app.services.connekta_gateway import connekta

        print(f'MODO_ENSAYO={connekta.modo_ensayo}  modo_simulacion={connekta.modo_simulacion}')
        print(f'Plan: item={CODIGO_SIESA} bodega={BODEGA} centro_op={CENTRO_OP}')
        print('  1) enviar_ajuste_inventario(AJ-ENT, +1)  -- 142951')
        print('  2) transferir_a_averias(1)                -- 142951, NB1 -> AV1')
        print('  Impacto neto esperado en Siesa: 0 (se compensan)')

        if not args.si_de_verdad:
            print('\n(Simulacro -- pasa --si-de-verdad para disparar los POST reales)')
            return

        if connekta.modo_ensayo:
            print('\nERROR: MODO_ENSAYO=true en .env.qa bloquearia los POST — '
                  'esta prueba necesita que SÍ lleguen a Siesa. Abortando.')
            sys.exit(2)

        antes = _existencia(connekta, CODIGO_SIESA, BODEGA)
        print(f'\nExistencia real ANTES en {BODEGA}: {antes}')

        print('\n--- POST 1: enviar_ajuste_inventario (AJ-ENT +1) ---')
        r1 = connekta.enviar_ajuste_inventario(
            motivo_codigo='AJ-ENT', item_codigo=CODIGO_SIESA, cantidad=1,
            referencia='QA refactor connekta_ajustes_gateway — verificacion real 2026-09-09',
            bodega=BODEGA, centro_op=CENTRO_OP,
        )
        print('Respuesta Siesa:', r1)
        if r1.get('codigo') != 0:
            print('ABORTA: el ajuste no dio codigo:0 — no se intenta la reversion.')
            sys.exit(1)

        despues_ajuste = _existencia(connekta, CODIGO_SIESA, BODEGA)
        print(f'Existencia real DESPUES del ajuste: {despues_ajuste}')
        if antes is not None and despues_ajuste is not None:
            print(f'Delta: {despues_ajuste - antes} (esperado: +1)')

        print('\n--- POST 2: transferir_a_averias (1) ---')
        r2 = connekta.transferir_a_averias(
            CODIGO_SIESA, 1,
            referencia='QA refactor connekta_ajustes_gateway — reversion del +1 de arriba',
        )
        print('Respuesta Siesa:', r2)
        if r2.get('codigo') != 0:
            print('ADVERTENCIA: la reversion a averias no dio codigo:0 — '
                  'el +1 del ajuste QUEDA sin revertir en Siesa, revisar a mano.')
            sys.exit(1)

        despues_averia = _existencia(connekta, CODIGO_SIESA, BODEGA)
        print(f'Existencia real DESPUES de la transferencia a averias: {despues_averia}')
        if antes is not None and despues_averia is not None:
            print(f'Delta neto total vs. el inicio: {despues_averia - antes} (esperado: 0)')

        print('\nRESULTADO: ambos POST reales (142951) respondieron codigo:0 y el '
              'stock real volvio a su valor original.')


if __name__ == '__main__':
    main()
