"""
Verificacion REAL contra Siesa QA (.env.qa) de ConnektaComprasGateway
(app/services/connekta_compras_gateway.py, extraido de ConnektaGateway el
2026-09-09, deuda de tamano paso 3).

Dispara el POST real (142948, confirmar_entrada_compras) contra OC67
(003-OC-67), creada por el usuario el 2026-09-09 especificamente para esta
prueba -- DISPAPELES SAS, item PAPELSP9218, 20 unidades pedidas, comprador
FIGUEROA ANACONA NELLY CARMENSA (NIT 52430291). Se confirma la entrada
COMPLETA (20/20) para cerrar la linea.

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_compras_gateway_real.py --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

ID_CO_OC = '003'
TIPO_DOCTO_OC = 'OC'
CONSEC_DOCTO_OC = '67'
CODIGO_SIESA = 'PAPELSP9218'
PROVEEDOR_NIT = '860028580'
SUCURSAL_PROV = '001'
COMPRADOR_NIT = '52430291'
COND_PAGO = 'P01'
FECHA_ENTREGA = '2026-09-09'
CANTIDAD_A_RECIBIR = 20.0


def _linea_oc(connekta):
    r = connekta.get_ordenes_compra_aprobadas(sin_filtros=True, consec=CONSEC_DOCTO_OC)
    rows = r.get('detalle', {}).get('Table', [])
    for row in rows:
        if (row.get('f420_id_co') == ID_CO_OC
                and row.get('f120_referencia') == CODIGO_SIESA):
            return row
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--si-de-verdad', action='store_true',
                     help='Sin esto, solo muestra el plan y no dispara nada real.')
    args = ap.parse_args()

    os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-compras-gateway-32-bytes-o-mas-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)
    if args.si_de_verdad:
        # .env.qa trae MODO_ENSAYO=true (bloquea POST server-side) — se
        # desactiva SOLO en este proceso, nunca se toca el archivo .env.qa.
        # Confirmado explícitamente con el usuario (2026-09-09).
        os.environ['MODO_ENSAYO'] = 'false'

    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True

    with app.app_context():
        db.create_all()
        from app.services.connekta_gateway import connekta

        print(f'MODO_ENSAYO={connekta.modo_ensayo}  modo_simulacion={connekta.modo_simulacion}')
        print(f'Plan: OC {ID_CO_OC}-{TIPO_DOCTO_OC}-{CONSEC_DOCTO_OC} (DISPAPELES SAS), '
              f'item {CODIGO_SIESA}, recibir {CANTIDAD_A_RECIBIR} unidades — conector 142948')

        antes = _linea_oc(connekta)
        if not antes:
            print('ERROR: no se encontró la línea real de OC67 — abortando sin tocar nada.')
            sys.exit(2)
        print(f'\nANTES — f421_cant_pedida={antes.get("f421_cant_pedida")} '
              f'f421_cant_entrada={antes.get("f421_cant_entrada")} '
              f'f421_ind_estado={antes.get("f421_ind_estado")} '
              f'comprador={antes.get("f200_id_comprador")}')

        if not args.si_de_verdad:
            print('\n(Simulacro -- pasa --si-de-verdad para disparar el POST real)')
            return

        if connekta.modo_ensayo:
            print('\nERROR: MODO_ENSAYO=true seguiría bloqueando el POST. Abortando.')
            sys.exit(2)

        print('\n--- POST: confirmar_entrada_compras (142948) ---')
        resultado = connekta.confirmar_entrada_compras(
            id_co_oc=ID_CO_OC,
            tipo_docto_oc=TIPO_DOCTO_OC,
            consec_docto_oc=CONSEC_DOCTO_OC,
            items=[{
                'producto_codigo': CODIGO_SIESA,
                'cantidad_recibida': CANTIDAD_A_RECIBIR,
                'bodega': antes.get('f150_id'),
                'uom': antes.get('f421_id_unidad_medida'),
                'fecha_entrega': FECHA_ENTREGA,
            }],
            proveedor_id=PROVEEDOR_NIT,
            sucursal_prov=SUCURSAL_PROV,
            tercero_comprador=COMPRADOR_NIT,
            moneda_docto='COP', moneda_conv='COP', moneda_local='COP',
            tasa_conv=1.0, tasa_local=1.0,
            cond_pago=COND_PAGO,
            num_docto_referencia=f'{TIPO_DOCTO_OC}-{CONSEC_DOCTO_OC}',
        )
        print('Respuesta Siesa:', resultado)
        if resultado.get('codigo') != 0:
            print('ABORTA: la entrada no dio codigo:0.')
            sys.exit(1)

        despues = _linea_oc(connekta)
        if despues:
            print(f'\nDESPUES — f421_cant_pedida={despues.get("f421_cant_pedida")} '
                  f'f421_cant_entrada={despues.get("f421_cant_entrada")} '
                  f'f421_ind_estado={despues.get("f421_ind_estado")}')
        else:
            print('\nDESPUES — la línea ya no aparece en OCs aprobadas '
                  '(consistente con quedar 100% recibida/cerrada).')

        print('\nRESULTADO: el POST real (142948) respondió codigo:0 contra OC67 real.')


if __name__ == '__main__':
    main()
