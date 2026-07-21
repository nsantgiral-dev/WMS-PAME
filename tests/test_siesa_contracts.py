"""
Tier 2 — Contract Tests: Payload vs Spec DOCX.
Valida que cada trigger_* genera un payload con TODOS los campos
que el conector Siesa requiere según la documentación oficial.

Si alguien quita un campo, renombra una key, o cambia una sección,
estos tests fallan ANTES de que el error llegue a producción.
"""
import os
import pytest
from unittest.mock import patch, MagicMock

os.environ.setdefault('CONNEKTA_MODO_SIMULACION', 'true')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('SYNC_SCHEDULER', 'false')

from app.services.connekta_gateway import ConnektaGateway


def _make_gateway():
    """Gateway con config mínima para tests."""
    gw = ConnektaGateway()
    gw.modo_simulacion = False
    gw.modo_ensayo = False
    gw.tipo_docto_recibo_caja = 'RC'
    gw.tipo_docto_docto_contable = 'NI'
    gw.tipo_docto_nota_factura = 'NCE'
    gw.cobrador_rc = '9876'
    gw.flujo_efectivo_rc = '1103'
    gw.cxc_auxiliar = '13050501'
    gw.medio_pago_efectivo = 'EFE'
    gw.medio_pago_transferencia = 'TBA'
    gw.medio_pago_tarjeta = 'TDC'
    gw.id_cia_siesa = '1'
    gw.centro_op = '003'
    gw.unidad_negocio = '99'
    gw._co_caja_map = {'001': '001', '003': '999'}
    gw._forma_pago_map = {
        'EFECTIVO': 'EFE', 'TRANSFERENCIA': 'TBA',
        'TARJETA': 'TDC', 'CONSIGNACION': 'TBA',
    }
    return gw


def _assert_21_chars(payload, path=''):
    """Verifica recursivamente que todos los DecimalConSigno tengan 21 chars."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            _assert_21_chars(v, f'{path}.{k}')
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            _assert_21_chars(v, f'{path}[{i}]')
    elif isinstance(payload, str) and len(payload) > 0 and payload[0] in '+-' and '.' in payload:
        # Parece DecimalConSigno
        assert len(payload) == 21, (
            f'{path} = "{payload}" tiene {len(payload)} chars, esperados 21'
        )


# ═══════════════════════════════════════════════════════════════════
# 142888 — ReciboCaja (trigger_recibo_caja)
# ═══════════════════════════════════════════════════════════════════

class TestContract142888:
    """Valida payload de trigger_recibo_caja contra spec DOCX 142888."""

    def _capture_payload(self, gw, **kwargs):
        """Ejecuta trigger_recibo_caja y captura el payload enviado a _post."""
        defaults = dict(
            tercero_nit='900123456',
            sucursal='001',
            monto=1500000.0,
            forma_pago='EFECTIVO',
            tipo_docto_fe='FE',
            consec_fe='5020',
            co_factura='001',
            cuenta_cxc='13050501',
            notas='test RC',
        )
        defaults.update(kwargs)
        with patch.object(gw, '_post', return_value={'codigo': 0}) as mock_post:
            gw.trigger_recibo_caja(**defaults)
            return mock_post.call_args[1].get('payload') or mock_post.call_args[0][2]

    def test_5_secciones_presentes(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        for seccion in ['Inicial', 'RCyotrosingresos', 'Caja', 'CxC', 'Final']:
            assert seccion in payload, f'Sección "{seccion}" falta en payload 142888'

    def test_header_campos_obligatorios(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        header = payload['RCyotrosingresos'][0]
        campos_obligatorios = [
            'F_CIA', 'F_CONSEC_AUTO_REG', 'F350_ID_CO', 'F350_ID_TIPO_DOCTO',
            'F350_CONSEC_DOCTO', 'F350_FECHA', 'F357_ID_CAJA', 'F357_FECHA_RECAUDO',
            'F350_ID_TERCERO', 'F357_ID_MONEDA_INGRESO', 'F357_VALOR_INGRESO',
            'F357_ID_MONEDA_APLICAR', 'F357_VALOR_APLICAR_REAL', 'F357_ID_COBRADOR',
            'F357_ID_UN', 'F357_ID_FE', 'F350_ID_CLASE_DOCTO',
            'F350_IND_ESTADO', 'F350_IND_IMPRESION', 'F350_NOTAS',
            'F357_IND_VALIDA_MEDPAGO',
        ]
        for campo in campos_obligatorios:
            assert campo in header, f'Campo "{campo}" falta en RCyotrosingresos'

    def test_header_valores_fijos(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        header = payload['RCyotrosingresos'][0]
        assert header['F_CIA'] == 1, 'F_CIA debe ser 1 (no 8215)'
        assert header['F350_ID_CLASE_DOCTO'] == 13, 'Clase docto RC = 13'
        assert header['F357_IND_VALIDA_MEDPAGO'] == 0, 'No validar medio vs caja'
        assert header['F_CONSEC_AUTO_REG'] == 1, 'Consecutivo automático'

    def test_caja_campos_obligatorios(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        caja = payload['Caja'][0]
        campos = [
            'F_CIA', 'F350_ID_CO', 'F350_ID_TIPO_DOCTO', 'F350_CONSEC_DOCTO',
            'F358_ID_MEDIOS_PAGO', 'F358_VALOR', 'F358_ID_BANCO',
            'F358_NRO_CHEQUE', 'F358_NRO_CUENTA', 'F358_COD_SEGURIDAD',
            'F358_NRO_AUTORIZACION', 'F358_FECHA_VCTO', 'F358_REFERENCIA_OTROS',
            'F358_FECHA_CONSIGNACION', 'f358_docto_banco_cg',
        ]
        for campo in campos:
            assert campo in caja, f'Campo "{campo}" falta en Caja'

    def test_caja_efectivo_banco_vacio(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw, forma_pago='EFECTIVO')
        caja = payload['Caja'][0]
        assert caja['F358_ID_BANCO'] == '', 'Banco debe estar vacío para efectivo'
        assert caja['F358_NRO_CUENTA'] == '', 'NroCuenta debe estar vacío para efectivo'

    def test_caja_transferencia_campos_consignacion(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw, forma_pago='TRANSFERENCIA')
        caja = payload['Caja'][0]
        assert caja['F358_REFERENCIA_OTROS'] != '', 'Transferencia requiere referencia'
        assert caja['F358_FECHA_CONSIGNACION'] != '', 'Transferencia requiere fecha consignación'
        assert caja['f358_docto_banco_cg'] == 'CG', 'Transferencia requiere docto_banco_cg=CG'

    def test_cxc_campos_obligatorios(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        cxc = payload['CxC'][0]
        campos = [
            'F_CIA', 'F350_ID_CO', 'F350_ID_TIPO_DOCTO', 'F350_CONSEC_DOCTO',
            'F353_ID_AUXILIAR_DOCTO_CRUCE', 'F353_ID_CO_DOCTO_CRUCE',
            'F353_ID_UN_DOCTO_CRUCE', 'F353_ID_SUCURSAL_DOCTO_CRUCE',
            'F353_ID_TIPO_DOCTO_CRUCE', 'F353_CONSEC_DOCTO_CRUCE',
            'F353_NRO_CUOTA_CRUCE', 'F354_VALOR_CR',
            'F354_VALOR_APLICADO_PP', 'F354_VALOR_APROVECHA', 'F354_VALOR_RETENCION',
        ]
        for campo in campos:
            assert campo in cxc, f'Campo "{campo}" falta en CxC'

    def test_cxc_auxiliar_dinamico(self):
        """F353_ID_AUXILIAR_DOCTO_CRUCE usa cuenta_cxc, no hardcoded."""
        gw = _make_gateway()
        payload = self._capture_payload(gw, cuenta_cxc='13050502')
        cxc = payload['CxC'][0]
        assert cxc['F353_ID_AUXILIAR_DOCTO_CRUCE'] == '13050502'

    def test_cxc_auxiliar_fallback(self):
        """Sin cuenta_cxc, usa self.cxc_auxiliar como fallback."""
        gw = _make_gateway()
        payload = self._capture_payload(gw, cuenta_cxc='')
        cxc = payload['CxC'][0]
        assert cxc['F353_ID_AUXILIAR_DOCTO_CRUCE'] == '13050501'

    def test_todos_decimal_21_chars(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        _assert_21_chars(payload)

    def test_fecha_formato_yyyymmdd(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        fecha = payload['RCyotrosingresos'][0]['F350_FECHA']
        assert len(fecha) == 8 and fecha.isdigit(), f'Fecha "{fecha}" no es YYYYMMDD'


# ═══════════════════════════════════════════════════════════════════
# 142882 — DocumentoContable (trigger_documento_contable)
# ═══════════════════════════════════════════════════════════════════

class TestContract142882:
    """Valida payload de trigger_documento_contable contra spec DOCX 142882."""

    def _capture_payload(self, gw, **kwargs):
        defaults = dict(
            tercero_nit='900123456',
            sucursal='001',
            cuenta_puc='13551501',
            monto=25000.0,
            base_gravable=1000000.0,
            tipo_docto_fe='FE',
            consec_fe='5020',
            co_factura='001',
            cuenta_cxc='13050501',
            notas='test DC retención',
        )
        defaults.update(kwargs)
        with patch.object(gw, '_post', return_value={'codigo': 0}) as mock_post:
            gw.trigger_documento_contable(**defaults)
            return mock_post.call_args[1].get('payload') or mock_post.call_args[0][2]

    def test_5_secciones_presentes(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        for seccion in ['Inicial', 'Documentocontable', 'Movimientocontable', 'MovimientoCxC', 'Final']:
            assert seccion in payload, f'Sección "{seccion}" falta en payload 142882'

    def test_documentocontable_campos(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        doc = payload['Documentocontable'][0]
        campos = [
            'F_CIA', 'F_CONSEC_AUTO_REG', 'F350_ID_CO', 'F350_ID_TIPO_DOCTO',
            'F350_CONSEC_DOCTO', 'F350_FECHA', 'F350_ID_TERCERO',
            'F350_ID_CLASE_DOCTO', 'F350_IND_ESTADO', 'F350_IND_IMPRESION', 'F350_NOTAS',
        ]
        for campo in campos:
            assert campo in doc, f'Campo "{campo}" falta en Documentocontable'

    def test_documentocontable_valores_fijos(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        doc = payload['Documentocontable'][0]
        assert doc['F350_ID_CLASE_DOCTO'] == 30, 'Clase docto NI = 30 (asiento contable)'
        assert doc['F_CIA'] == 1
        assert doc['F350_ID_TERCERO'] == '900123456'

    def test_movimientocontable_campos(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        mc = payload['Movimientocontable'][0]
        campos = [
            'F_CIA', 'F350_ID_CO', 'F350_ID_TIPO_DOCTO', 'F350_CONSEC_DOCTO',
            'F351_ID_AUXILIAR', 'F351_ID_TERCERO', 'F351_ID_CO_MOV', 'F351_ID_UN',
            'F351_ID_CCOSTO', 'F351_VALOR_DB', 'F351_VALOR_CR',
            'F351_VALOR_DB_ALT', 'F351_VALOR_CR_ALT', 'F351_BASE_GRAVABLE', 'F351_NOTAS',
        ]
        for campo in campos:
            assert campo in mc, f'Campo "{campo}" falta en Movimientocontable'

    def test_movimientocontable_nombre_campo_co_mov(self):
        """Spec dice F351_ID_CO_MOV, no F351_ID_CO_MOVTO."""
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        mc = payload['Movimientocontable'][0]
        assert 'F351_ID_CO_MOV' in mc, 'Debe ser F351_ID_CO_MOV (no MOVTO)'
        assert 'F351_ID_CO_MOVTO' not in mc, 'F351_ID_CO_MOVTO es nombre incorrecto'

    def test_movimiento_cxc_29_campos(self):
        """MovimientoCxC debe tener los 29 campos del spec DOCX."""
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        mcxc = payload['MovimientoCxC'][0]
        campos = [
            'F_CIA', 'F350_ID_CO', 'F350_ID_TIPO_DOCTO', 'F350_CONSEC_DOCTO',
            'F351_ID_AUXILIAR', 'F351_ID_TERCERO', 'F351_ID_CO_MOV', 'F351_ID_UN',
            'F351_ID_CCOSTO', 'F351_VALOR_DB', 'F351_VALOR_CR',
            'F351_VALOR_DB_ALT', 'F351_VALOR_CR_ALT', 'F351_NOTAS',
            'F353_ID_SUCURSAL', 'F353_ID_TIPO_DOCTO_CRUCE', 'F353_CONSEC_DOCTO_CRUCE',
            'F353_NRO_CUOTA_CRUCE', 'F353_FECHA_VCTO', 'F353_FECHA_DSCTO_PP',
            'F353_VLR_DSCTO_PP', 'F354_VALOR_APLICADO_PP', 'F354_VALOR_APLICADO_PP_ALT',
            'F354_VALOR_APROVECHA', 'F354_VALOR_APROVECHA_ALT',
            'F354_VALOR_RETENCION', 'F354_VALOR_RETENCION_ALT',
            'F354_TERCERO_VEND', 'F354_NOTAS',
        ]
        for campo in campos:
            assert campo in mcxc, f'Campo "{campo}" falta en MovimientoCxC 142882'
        assert len(campos) == 29

    def test_movimiento_cxc_tercero_presente(self):
        """F351_ID_TERCERO obligatorio en MovimientoCxC (diferencia vs 142888)."""
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        mcxc = payload['MovimientoCxC'][0]
        assert mcxc['F351_ID_TERCERO'] == '900123456'

    def test_movimiento_cxc_fechas_presentes(self):
        """F353_FECHA_VCTO y F353_FECHA_DSCTO_PP obligatorios."""
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        mcxc = payload['MovimientoCxC'][0]
        assert len(mcxc['F353_FECHA_VCTO']) == 8
        assert len(mcxc['F353_FECHA_DSCTO_PP']) == 8

    def test_movimiento_cxc_tercero_vend(self):
        """F354_TERCERO_VEND obligatorio en MovimientoCxC."""
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        mcxc = payload['MovimientoCxC'][0]
        assert mcxc['F354_TERCERO_VEND'] == '900123456'

    def test_todos_decimal_21_chars(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        _assert_21_chars(payload)


# ═══════════════════════════════════════════════════════════════════
# 142946 — NotaFactura (trigger_nota_factura)
# ═══════════════════════════════════════════════════════════════════

class TestContract142946:
    """Valida payload de trigger_nota_factura contra spec."""

    def _capture_payload(self, gw, **kwargs):
        defaults = dict(
            tipo_docto_fe='FE',
            consec_fe='5020',
            lineas=[{
                'f470_rowid_movto': 12345,
                'f470_cant_base': 5,
                'f470_id_bodega': 'NB1',
                'f470_id_unidad_medida': 'UND',
            }],
            notas='Devolución test',
        )
        defaults.update(kwargs)
        with patch.object(gw, '_post', return_value={'codigo': 0}) as mock_post:
            gw.trigger_nota_factura(**defaults)
            return mock_post.call_args[1].get('payload') or mock_post.call_args[0][2]

    def test_secciones_sin_underscore(self):
        """142946 usa 'Doctoventascomercial' (sin underscore) — diferente de 238925."""
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        assert 'Doctoventascomercial' in payload, 'Debe ser Doctoventascomercial (sin _)'
        assert 'Docto_ventas_comercial' not in payload, '238925 usa underscore, 142946 NO'
        assert 'Movtoventascomercial' in payload

    def test_secciones_presentes(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        for sec in ['Inicial', 'Doctoventascomercial', 'Movtoventascomercial', 'Final']:
            assert sec in payload, f'Sección "{sec}" falta en payload 142946'

    def test_fecha_yyyymmdd(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        doc = payload['Doctoventascomercial'][0]
        fecha = doc.get('F350_FECHA', '')
        assert len(fecha) == 8 and fecha.isdigit(), f'Fecha "{fecha}" no es YYYYMMDD'

    def test_f_cia_es_1(self):
        gw = _make_gateway()
        payload = self._capture_payload(gw)
        assert payload['Inicial'][0]['F_CIA'] == 1
        assert payload['Final'][0]['F_CIA'] == 1
