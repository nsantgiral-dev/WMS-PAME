"""
Prueba en vivo de 251546 (PapeleriaMedellin_NotaCredito_CrearCruzarDian_WMS)
contra Siesa QA. Script suelto, no toca connekta_gateway.py.

Paso 1: busca una factura FEW abierta (saldo > 0) via CxC para usar de prueba.
Paso 2: arma el payload con las 5 secciones (Inicial/Docto ventas comercial/
        Cuotas CxC/Movimientos/Entidades dinamicas/Final) y lo postea a 251546.
Paso 3: imprime la respuesta cruda de Siesa.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.services.connekta_gateway import connekta

CONECTOR_ID = '251546'
NOMBRE_CONECTOR = 'PapeleriaMedellin_NotaCredito_CrearCruzarDian_WMS'

app = create_app()

with app.app_context():
    print(f"MODO_SIMULACION={connekta.modo_simulacion} MODO_ENSAYO={connekta.modo_ensayo}")
    print(f"CO={connekta.centro_op!r} BODEGA={connekta.bodega!r} TIPO_DOCTO_NC={connekta.tipo_docto_nota_credito!r}")

    tipo_docto_fe = os.environ.get('TEST_TIPO_DOCTO_FE', 'FEW')

    print("\nBuscando factura abierta (saldo > 0) via API_v2_CxC_General...")
    res = connekta._get('API_v2_CxC_General', {
        'paginacion': 'numPag=1|tamPag=100',
        'parametros': (
            f"f353_id_co_cruce = ''{connekta.centro_op}'' "
            f"AND f353_id_tipo_docto_cruce = ''{tipo_docto_fe}''"
        ),
    })
    rows = res.get('detalle', {}).get('Table', [])
    print(f"Filas encontradas: {len(rows)}")
    if rows and 'alerta' in rows[0]:
        print("ALERTA:", rows[0])
        rows = []
    # Ordenar por consecutivo descendente — preferir facturas mas recientes
    rows.sort(key=lambda r: int(r.get('f353_consec_docto_cruce') or 0), reverse=True)

    candidato = None
    for r in rows:
        if 'alerta' in r:
            continue
        consec = r.get('f353_consec_docto_cruce')
        saldo = float(r.get('f353_total_db') or 0) - float(r.get('f353_total_cr') or 0)
        if consec and saldo > 0:
            candidato = r
            break

    if not candidato:
        print("No se encontro ninguna factura abierta. Filas crudas (primeras 5):")
        for r in rows[:5]:
            print(" ", r)
        sys.exit(1)

    consec_fe = candidato['f353_consec_docto_cruce']
    nit = candidato.get('f353_id_tercero') or candidato.get('f200_id') or ''
    saldo = float(candidato.get('f353_total_db') or 0) - float(candidato.get('f353_total_cr') or 0)
    print(f"\nCandidato: {tipo_docto_fe}-{consec_fe} NIT={nit} saldo={saldo}")

    print("\nObteniendo lineas via get_rowids_factura...")
    rowids_data = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
    if not rowids_data:
        print("Sin lineas — abortando.")
        sys.exit(1)
    for r in rowids_data:
        print(" ", {k: r.get(k) for k in ('f120_referencia', 'f470_rowid', 'f470_cant_base', 'f470_vlr_neto', 'f150_id', 'f470_id_unidad_medida')})

    valor_cruce = sum(float(r.get('f470_vlr_neto') or 0) for r in rowids_data)
    print(f"\nvalor_cruce (suma f470_vlr_neto, NUNCA bruto) = {valor_cruce}")

    fecha_hoy = os.environ.get('TEST_FECHA', connekta._fecha_hoy_bogota())
    fecha_vcto = connekta.get_vencimiento_factura(tipo_docto_fe, consec_fe)
    cia = int(connekta.id_cia_siesa)
    co = connekta.centro_op

    cab = rowids_data[0]
    tercero = (cab.get('f200_nit_fact') or cab.get('f200_id_fact') or nit or '').strip()
    tercero_vendedor = (cab.get('f200_nit_vendedor') or cab.get('f200_id_vendedor') or '').strip() or 'Generico'
    tercero_rem = (cab.get('f200_nit_rem') or cab.get('f200_id_rem') or tercero).strip()

    docto_ventas = {
        'F_CIA': cia,
        'F_CONSEC_AUTO_REG': 1,
        'F350_ID_CO': co,
        'F350_ID_TIPO_DOCTO': connekta.tipo_docto_nota_credito,
        'F350_CONSEC_DOCTO': 0,
        'F350_FECHA': fecha_hoy,
        'F350_ID_TERCERO': tercero,
        'F350_ID_CLASE_DOCTO': 525,
        'F350_IND_ESTADO': 0,
        'F350_IND_IMPRESION': 0,
        'f461_id_sucursal_fact': (cab.get('f461_id_sucursal_fact') or '').strip(),
        'f461_id_tipo_cli_fact': (cab.get('f461_id_tipo_cli_fact') or '').strip(),
        'f461_id_co_fact': (cab.get('f461_id_co_fact') or co).strip(),
        'f461_id_tercero_rem': tercero_rem,
        'f461_id_sucursal_rem': (cab.get('f461_id_sucursal_rem') or cab.get('f461_id_sucursal_fact') or '').strip(),
        'f461_id_tercero_vendedor': tercero_vendedor,
        'f461_id_cond_pago': (cab.get('f461_id_cond_pago') or '').strip(),
        'f461_notas': 'Prueba WMS 251546 - crear+cruzar+motivo DIAN',
        'F461_IND_GENERA_KIT': '0',
        'F430_ID_TIPO_DOCTO': tipo_docto_fe,
        'F430_CONSEC_DOCTO': int(consec_fe) if str(consec_fe).isdigit() else consec_fe,
        **connekta._build_transportador_vacio(),
    }
    print("\nCabecera real usada:", {k: docto_ventas[k] for k in (
        'F350_ID_TERCERO', 'f461_id_sucursal_fact', 'f461_id_tipo_cli_fact',
        'f461_id_co_fact', 'f461_id_tercero_rem', 'f461_id_sucursal_rem',
        'f461_id_tercero_vendedor', 'f461_id_cond_pago')})

    cuotas_cxc = {
        'F_CIA': cia,
        'F350_ID_CO': co,
        'F350_ID_TIPO_DOCTO': connekta.tipo_docto_nota_credito,
        'F350_CONSEC_DOCTO': 0,
        'F353_ID_TIPO_DOCTO_CRUCE': tipo_docto_fe,
        'F353_CONSEC_DOCTO_CRUCE': docto_ventas['F430_CONSEC_DOCTO'],
        'F353_NRO_CUOTA_CRUCE': 0,
        'F353_VLR_CRUCE': connekta._fmt_decimal_sin_signo(valor_cruce, 15, 4),
        'F_PORCENTAJE_CUOTA': '000.00',
        'F353_FECHA_VCTO': fecha_vcto,
        'F353_VLR__DSCTO_PP': connekta._fmt_decimal_sin_signo(0, 15),
        'F_PORCENTAJE_PP': '000.00',
        'F353_FECHA_DSCTO_PP': '',
    }

    movimientos = []
    for i, lin in enumerate(rowids_data, 1):
        movimientos.append({
            'F_CIA': cia,
            'f470_id_co': co,
            'f470_id_tipo_docto': connekta.tipo_docto_nota_credito,
            'f470_consec_docto': 0,
            'f470_nro_registro': i,
            'f470_id_bodega': lin.get('f150_id') or connekta.bodega,
            'f470_id_concepto': 502,
            'f470_id_motivo': connekta.motivo_ventas,
            'f470_ind_obsequio': 0,
            'f470_ind_solo_valor': 0,
            'f470_id_co_movto': co,
            'f470_id_un_movto': os.environ.get('TEST_UN', '99'),
            'f470_id_unidad_medida': lin.get('f470_id_unidad_medida') or connekta.uom_default,
            'f470_id_lista_precio': (lin.get('f470_id_lista_precio') or connekta.lista_precio or '001'),
            'f470_cant_base': connekta._fmt_decimal_sin_signo(lin['f470_cant_base'], 15),
            'f470_cant_2': connekta._fmt_decimal_sin_signo(0, 15),
            'f470_ind_impto_asumido': 0,
            'f470_referencia_item': lin.get('f120_referencia') or '',
            'f470_rowid_movto': int(lin['f470_rowid']),
            'f470_id_item': '',
            'f470_codigo_barras': '',
            'f470_id_ubicacion_aux': '',
            'f470_id_lote': '',
            'f470_id_ccosto_movto': '',
            'f470_id_causal_devol': '',
            'f470_vlr_bruto': connekta._fmt_decimal_sin_signo(lin.get('f470_vlr_bruto') or lin.get('f470_vlr_neto') or 0, 15),
            'f470_notas': '',
        })

    entidad = {
        'F_CIA': cia,
        'F_ACTUALIZA_REG': 1,
        'f350_id_co': co,
        'f350_id_tipo_docto': connekta.tipo_docto_nota_credito,
        'f350_consec_docto': 0,
        'f753_id_grupo_entidad': 'FE_CONCEPTOS NC 2.1',
        'f753_id_entidad': 'EUNOECO015',
        'f753_id_atributo': 'co015_concepto_nc',
        'f753_dato_numerico': 0,
        'f753_id_tipo_entidad': os.environ.get('TEST_TIPO_ENTIDAD', 'G504_1'),
        'f753_dato_texto': '',
        'f753_id_maestro': os.environ.get('TEST_ID_MAESTRO', 'MUNOECO017'),
        'f753_id_maestro_detalle': os.environ.get('TEST_ID_MAESTRO_DETALLE', '1'),
    }

    payload = {
        'Inicial': [{'F_CIA': cia}],
        'Docto. ventas comercial': [docto_ventas],
        'Cuotas CxC': [cuotas_cxc],
        'Movimientos': movimientos,
        'Entidades dinámicas': [entidad],
        'Final': [{'F_CIA': cia}],
    }

    print("\n=== PAYLOAD ===")
    import json
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    print("\n=== POST a 251546 ===")
    resultado = connekta._post(
        CONECTOR_ID, NOMBRE_CONECTOR, payload,
        url=connekta.url_post_dinamico,
        extra_params={'idSistema': connekta.id_sistema},
    )
    print("\n=== RESULTADO ===")
    print(json.dumps(resultado, indent=2, ensure_ascii=False, default=str))

    with open(os.path.join(os.path.dirname(__file__), '_test_251546_out.txt'), 'w', encoding='utf-8') as f:
        f.write(f"Factura de prueba: {tipo_docto_fe}-{consec_fe}\n")
        f.write(json.dumps(resultado, indent=2, ensure_ascii=False, default=str))
