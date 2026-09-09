"""
Verificacion REAL contra Siesa QA (.env.qa) de que ConnektaConsultasGateway
(app/services/connekta_consultas_gateway.py, extraido de ConnektaGateway el
2026-09-09, deuda de tamano paso 5) sigue hablando con Siesa exactamente
igual que antes del refactor -- son puros GET, sin efectos secundarios en
Siesa (no crea ni modifica ningun documento).

Corre cada metodo migrado via el singleton `connekta` (la via real que usa
el resto del WMS) y confirma que la llamada real llega, responde, y que el
gateway sigue delegando a self._consultas correctamente.

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_consultas_gateway_real.py
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CODIGO_SIESA = 'PAPELSP9830'
NIT_CLIENTE_CONOCIDO = '1000124053'


def main():
    os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-consultas-gateway-32-bytes-o-mas-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True

    fallas = []

    def check(nombre, cond, detalle=''):
        estado = 'PASS' if cond else 'FAIL'
        print(f'[{estado}] {nombre}' + (f' -- {detalle}' if detalle and not cond else ''))
        if not cond:
            fallas.append(nombre)

    with app.app_context():
        db.create_all()

        from app.services.connekta_gateway import connekta
        from app.services.connekta_consultas_gateway import ConnektaConsultasGateway

        check('modo_simulacion=False (credenciales .env.qa cargadas)',
              connekta.modo_simulacion is False,
              'faltan CONNEKTA_IKEY/CONNEKTA_ITOKEN en .env.qa')
        check('connekta._consultas es ConnektaConsultasGateway',
              isinstance(connekta._consultas, ConnektaConsultasGateway))

        print('\n=== Sub-lote 1: bodegas/ubicaciones/stock ===')
        bodegas = connekta.get_bodegas_siesa()
        rows = bodegas.get('detalle', {}).get('Table', [])
        check('get_bodegas_siesa trae filas reales', len(rows) > 0, f'{len(rows)} filas')

        ubic = connekta.get_ubicaciones_siesa(bodega_id='NB1', pagina=1)
        rows = ubic.get('detalle', {}).get('Table', [])
        check('get_ubicaciones_siesa(NB1) trae filas reales', len(rows) > 0, f'{len(rows)} filas')

        stock = connekta.get_stock_bodega('NB1')
        rows = stock.get('detalle', {}).get('Table', [])
        check('get_stock_bodega(NB1) responde con estructura esperada',
              'detalle' in stock and isinstance(rows, list), f'{len(rows)} filas')

        print('\n=== Sub-lote 2: pedidos/FE/catalogo/OC/compromisos ===')
        pedidos = connekta.get_pedidos_aprobados()
        check('get_pedidos_aprobados responde codigo 0',
              pedidos.get('codigo') == 0, str(pedidos)[:200])

        ocs = connekta.get_ordenes_compra_aprobadas(sin_filtros=True)
        check('get_ordenes_compra_aprobadas responde con Table',
              'Table' in ocs.get('detalle', {}), str(ocs)[:200])

        catalogo = connekta.get_items_catalogo(pagina=1)
        rows = catalogo.get('detalle', {}).get('Table', [])
        check('get_items_catalogo trae filas reales', len(rows) > 0, f'{len(rows)} filas')

        item = connekta.buscar_item_por_referencia(CODIGO_SIESA)
        check(f'buscar_item_por_referencia({CODIGO_SIESA}) encuentra el item real',
              item is not None and item.get('codigo_siesa') == CODIGO_SIESA, str(item))

        barras = connekta.buscar_barras_por_referencia(CODIGO_SIESA)
        check(f'buscar_barras_por_referencia({CODIGO_SIESA}) responde (lista, puede ser vacia)',
              isinstance(barras, list), str(barras))

        unidades = connekta.get_items_unidades_medida(pagina=1)
        check('get_items_unidades_medida responde con detalle',
              'detalle' in unidades)

        print('\n=== Sub-lote 3: terceros/facturas/CxC ===')
        vendedores = connekta.get_vendedor_contacto()
        check('get_vendedor_contacto trae filas reales (maestro de 100 vendedores)',
              len(vendedores) > 0, f'{len(vendedores)} filas')

        cxc = connekta.get_cxc_general(NIT_CLIENTE_CONOCIDO)
        check(f'get_cxc_general({NIT_CLIENTE_CONOCIDO}) responde (lista, puede ser vacia)',
              isinstance(cxc, list), f'{len(cxc)} filas')

        terceros = connekta.get_terceros_contacto(pagina=1)
        check('get_terceros_contacto trae filas reales', len(terceros) > 0,
              f'{len(terceros)} filas')

        print('\n=== Circuit breaker (usado por todos los GET de arriba) ===')
        estado_cb = connekta.circuit_state()
        check('circuit breaker sigue CLOSED tras N llamadas reales exitosas',
              estado_cb['state'] == 'CLOSED', str(estado_cb))

    print('\n' + '=' * 60)
    if fallas:
        print(f'RESULTADO: {len(fallas)} verificacion(es) fallaron: {fallas}')
        sys.exit(1)
    else:
        print('RESULTADO: todas las verificaciones reales contra Siesa QA pasaron.')
        sys.exit(0)


if __name__ == '__main__':
    main()
