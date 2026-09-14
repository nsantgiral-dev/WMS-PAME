"""
Tier 1 — Tests puros de formato Siesa.
Sin DB, sin Flask. Validan _fmt_valor, _fecha_hoy_bogota, mappings.
Si estos fallan, NADA funciona en Siesa.
"""
import os
import pytest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

# Forzar simulación antes de importar
os.environ.setdefault('CONNEKTA_MODO_SIMULACION', 'true')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('SYNC_SCHEDULER', 'false')

from app.services.connekta_gateway import ConnektaGateway

_TZ_BOGOTA = ZoneInfo('America/Bogota')


# ═══════════════════════════════════════════════════════════════════
# _fmt_valor — DecimalConSigno (21 chars exactos)
# ═══════════════════════════════════════════════════════════════════

class TestFmtValor:
    @staticmethod
    def fmt(v):
        return ConnektaGateway._fmt_valor(v)

    def test_entero_positivo(self):
        assert self.fmt(1500000) == '+000000001500000.0000'

    def test_entero_negativo(self):
        assert self.fmt(-50000) == '-000000000050000.0000'

    def test_cero(self):
        assert self.fmt(0) == '+000000000000000.0000'

    def test_decimal(self):
        assert self.fmt(123.45) == '+000000000000123.4500'

    def test_float_pequeno_ica(self):
        # ICA 6.9x1000 sobre $1,000,000 = $6,900
        assert self.fmt(6900) == '+000000000006900.0000'

    def test_float_con_4_decimales(self):
        assert self.fmt(0.0069) == '+000000000000000.0069'

    def test_siempre_21_chars(self):
        for v in [0, 1, -1, 999999999999999, -999999999999999, 0.0001, 123.4567]:
            resultado = self.fmt(v)
            assert len(resultado) == 21, f'_fmt_valor({v}) = "{resultado}" tiene {len(resultado)} chars, esperados 21'

    def test_signo_en_posicion_0(self):
        assert self.fmt(100)[0] == '+'
        assert self.fmt(-100)[0] == '-'
        assert self.fmt(0)[0] == '+'

    def test_punto_en_posicion_correcta(self):
        # signo(1) + enteros(15) + punto(1) + decimales(4) = posición 16
        for v in [0, 1500000, -50000, 0.0069]:
            resultado = self.fmt(v)
            assert resultado[16] == '.', f'_fmt_valor({v}) = "{resultado}" — punto en pos {resultado.index(".")}, esperado 16'

    def test_monto_grande(self):
        # Factura de $50 millones
        resultado = self.fmt(50000000)
        assert resultado == '+000000050000000.0000'

    def test_negativo_cero(self):
        # -0.0 debe ser positivo
        assert self.fmt(-0.0)[0] == '+'


# ═══════════════════════════════════════════════════════════════════
# _fecha_hoy_bogota — YYYYMMDD en timezone Colombia
# ═══════════════════════════════════════════════════════════════════

class TestFechaHoyBogota:

    def test_formato_8_chars_sin_separadores(self):
        resultado = ConnektaGateway._fecha_hoy_bogota()
        assert len(resultado) == 8
        assert resultado.isdigit()

    def test_formato_yyyymmdd(self):
        resultado = ConnektaGateway._fecha_hoy_bogota()
        year = int(resultado[:4])
        month = int(resultado[4:6])
        day = int(resultado[6:8])
        assert 2020 <= year <= 2030
        assert 1 <= month <= 12
        assert 1 <= day <= 31

    @patch('app.utils.fecha.datetime')
    def test_23_utc_es_dia_anterior_en_bogota(self, mock_dt):
        # 23:00 UTC del 15 de julio = 18:00 COT del 15 de julio (mismo día)
        # 05:30 UTC del 16 de julio = 00:30 COT del 16 de julio
        # Pero 05:00 UTC del 16 = 00:00 COT del 16
        # El punto clave: después de 7PM Colombia, UTC ya es el día siguiente
        mock_dt.now.return_value = datetime(2026, 7, 16, 4, 59, tzinfo=ZoneInfo('UTC')).astimezone(_TZ_BOGOTA)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        resultado = ConnektaGateway._fecha_hoy_bogota()
        # 04:59 UTC = 23:59 COT del 15 → debería ser '20260715'
        assert resultado == '20260715'

    @patch('app.utils.fecha.datetime')
    def test_06_utc_es_mismo_dia_en_bogota(self, mock_dt):
        # 06:00 UTC = 01:00 COT → mismo día
        mock_dt.now.return_value = datetime(2026, 7, 16, 6, 0, tzinfo=ZoneInfo('UTC')).astimezone(_TZ_BOGOTA)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        resultado = ConnektaGateway._fecha_hoy_bogota()
        assert resultado == '20260716'


# ═══════════════════════════════════════════════════════════════════
# CO → Caja mapping
# ═══════════════════════════════════════════════════════════════════

class TestCoCajaMapping:

    def setup_method(self):
        self.gw = ConnektaGateway()

    def test_001_neiva_sur(self):
        assert self.gw._co_caja_map.get('001') == '001'

    def test_002_neiva_centro(self):
        assert self.gw._co_caja_map.get('002') == '004'

    def test_003_bodega_cd(self):
        assert self.gw._co_caja_map.get('003') == '999'

    def test_006_florencia(self):
        assert self.gw._co_caja_map.get('006') == '013'

    def test_co_desconocido_fallback_999(self):
        assert self.gw._co_caja_map.get('XYZ', '999') == '999'

    def test_ferias_007_008_009(self):
        for co in ['007', '008', '009']:
            assert self.gw._co_caja_map.get(co) == '999', f'CO {co} debería mapear a 999'


# ═══════════════════════════════════════════════════════════════════
# Forma de pago → Medio de pago Siesa
# ═══════════════════════════════════════════════════════════════════

class TestFormaPagoMapping:

    def setup_method(self):
        self.gw = ConnektaGateway()

    def test_efectivo(self):
        assert self.gw._forma_pago_map.get('EFECTIVO') == self.gw.medio_pago_efectivo

    def test_transferencia(self):
        assert self.gw._forma_pago_map.get('TRANSFERENCIA') == self.gw.medio_pago_transferencia

    def test_tarjeta(self):
        assert self.gw._forma_pago_map.get('TARJETA') == self.gw.medio_pago_tarjeta

    def test_consignacion_mapea_a_transferencia(self):
        assert self.gw._forma_pago_map.get('CONSIGNACION') == self.gw.medio_pago_transferencia

    def test_forma_desconocida_revienta_en_vez_de_reportarse_como_efectivo(self):
        """Hasta el 2026-08-31 un `forma_pago` sin medio Siesa configurado
        (CHEQUE, EXENTO, cualquier valor no mapeado) caía silencioso al
        default de `.get()` y se reportaba como EFECTIVO — un cheque
        cuadraba la caja de Siesa con plata que nunca entró en billetes.
        Ahora revienta antes del POST (Regla 6) en vez de mentir el medio."""
        with patch.object(self.gw, '_post', return_value={'detalle': 'ok'}):
            with pytest.raises(ValueError, match='CHEQUE'):
                self.gw.trigger_recibo_caja(
                    tercero_nit='900123456', sucursal='001', monto=1000,
                    forma_pago='CHEQUE', tipo_docto_fe='FE', consec_fe='1',
                )
            with pytest.raises(ValueError, match='EXENTO'):
                self.gw.trigger_recibo_caja(
                    tercero_nit='900123456', sucursal='001', monto=1000,
                    forma_pago='EXENTO', tipo_docto_fe='FE', consec_fe='1',
                )
            with pytest.raises(ValueError, match='BITCOIN'):
                self.gw.trigger_recibo_caja(
                    tercero_nit='900123456', sucursal='001', monto=1000,
                    forma_pago='BITCOIN', tipo_docto_fe='FE', consec_fe='1',
                )

    def test_medios_default_correctos(self):
        assert self.gw.medio_pago_efectivo == 'EFE'
        assert self.gw.medio_pago_transferencia == 'TBA'
        assert self.gw.medio_pago_tarjeta == 'TDC'
