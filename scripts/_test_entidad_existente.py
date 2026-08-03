"""
Prueba: adjuntar SOLO Entidades dinamicas (motivo DIAN) a una NC que YA EXISTE
en Siesa (creada en una prueba anterior de 251546, consec_docto=57, en
Elaboracion). No crea nada nuevo -- si esto funciona, confirma que el
"documento no existe" de antes era por referenciar consec=0 (auto_reg) en
vez del consecutivo real ya asignado.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.services.connekta_gateway import connekta

CONECTOR_ID = '251546'
NOMBRE_CONECTOR = 'PapeleriaMedellin_NotaCredito_CrearCruzarDian_WMS'

app = create_app()

with app.app_context():
    cia = int(connekta.id_cia_siesa)
    co = connekta.centro_op
    consec_real = int(os.environ.get('TEST_CONSEC_REAL', '57'))

    entidad = {
        'F_CIA': cia,
        'F_ACTUALIZA_REG': 1,
        'f350_id_co': co,
        'f350_id_tipo_docto': connekta.tipo_docto_nota_credito,
        'f350_consec_docto': consec_real,
        'f753_id_grupo_entidad': 'FE_CONCEPTOS NC 2.1',
        'f753_id_entidad': 'EUNOECO015',
        'f753_id_atributo': 'co015_concepto_nc',
        'f753_dato_numerico': 0,
        'f753_id_tipo_entidad': 'G504_1',
        'f753_dato_texto': '',
        'f753_id_maestro': 'MUNOECO017',
        'f753_id_maestro_detalle': '1',
    }

    payload = {
        'Inicial': [{'F_CIA': cia}],
        'Entidades dinámicas': [entidad],
        'Final': [{'F_CIA': cia}],
    }

    print("=== PAYLOAD (solo Entidades, consec real =", consec_real, ") ===")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    print("\n=== POST a 251546 (solo seccion Entidades) ===")
    try:
        resultado = connekta._post(
            CONECTOR_ID, NOMBRE_CONECTOR, payload,
            url=connekta.url_post_dinamico,
            extra_params={'idSistema': connekta.id_sistema},
        )
        print("\n=== RESULTADO ===")
        print(json.dumps(resultado, indent=2, ensure_ascii=False, default=str))
    except Exception as e:
        print("\n=== ERROR ===")
        print(str(e))
