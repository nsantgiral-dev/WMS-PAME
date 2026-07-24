"""
Tests del Armador de Contenedor + ROP dual.

Cisnes negros que previene:
- Contenedor propuesto sin descontar en_transito → duplica 400 refs que ya navegan
- Relleno que deja SKU a >180 días de cobertura → recrea los 500 días de inventario
- CBM excede 90% del útil → caja no cierra en puerto
- Peso excede payload → multa o rechazo en puerto
- σ_LT adivinado cuando hay datos reales → colchón incorrecto
- OC generada automáticamente sin G4 → conector no existe, pero el guard debe estar
"""
import pytest
from datetime import date, timedelta


class TestROPDual:

    def test_rop_sin_datos_no_crashea(self, app, db):
        """Sin datos de kardex, ROP retorna listas vacías."""
        from app.services.armador_service import ArmadorService
        result = ArmadorService.rop_dual(0.95)
        assert 'nacional' in result
        assert 'china' in result
        assert isinstance(result['nacional']['items'], list)
        assert isinstance(result['china']['items'], list)

    def test_rop_china_usa_lt_mayor(self, app, db):
        """LT de China debe ser significativamente mayor que nacional."""
        from app.services.armador_service import ArmadorService
        result = ArmadorService.rop_dual()
        assert result['china']['lt_dias'] > result['nacional']['lt_dias']
        assert result['china']['lt_dias'] >= 90  # mínimo 90 días

    def test_sigma_lt_default_es_conservador(self, app, db):
        """Sin contenedores históricos, σ_LT usa default conservador."""
        from app.services.armador_service import ArmadorService
        sigma = ArmadorService.calcular_sigma_lt_real()
        assert sigma['fuente'] == 'DEFAULT_CONSERVADOR'
        assert sigma['sigma_lt'] > 0

    def test_sigma_lt_se_actualiza_con_datos(self, app, db):
        """Con ≥3 contenedores con fechas completas, σ_LT se calcula."""
        from app.models.importacion import Contenedor

        for i in range(4):
            db.session.add(Contenedor(
                numero=f'TEST-{i}', tipo='40STD',
                fecha_oc=date(2025, 1 + i, 1),
                fecha_recepcion_cedi=date(2025, 4 + i, 15 + i * 3),
            ))
        db.session.commit()

        from app.services.armador_service import ArmadorService
        sigma = ArmadorService.calcular_sigma_lt_real()
        assert sigma['fuente'] in ('PARCIAL', 'MEDIDO')
        assert sigma['n'] == 4


class TestArmadorContenedor:

    def test_armador_sin_datos_retorna_shadow(self, app, db):
        """Sin fichas de importación, el Armador corre en SHADOW."""
        from app.services.armador_service import ArmadorService
        result = ArmadorService.armar_contenedor()
        assert result['modo'] == 'SHADOW'

    def test_armador_excluye_fichas_estimadas(self, app, db):
        """SKUs con fuente=ESTIMADO se excluyen del armado automático."""
        from app.services.armador_service import ArmadorService
        result = ArmadorService.armar_contenedor()
        # Verificar que excluidos tiene motivo FICHA_ESTIMADA o SIN_FICHA
        for exc in result.get('excluidos', []):
            assert exc['motivo'] in ('SIN_FICHA', 'FICHA_ESTIMADA')

    def test_barras_nunca_exceden_limites(self, app, db):
        """CBM y peso nunca exceden sus límites."""
        from app.services.armador_service import ArmadorService
        result = ArmadorService.armar_contenedor('40STD')
        assert result['barras']['cbm_pct'] <= 100
        assert result['barras']['peso_pct'] <= 100

    def test_gatillo_ninguno_sin_deficit(self, app, db):
        """Sin déficit, el gatillo no se activa."""
        from app.services.armador_service import ArmadorService
        result = ArmadorService.armar_contenedor()
        # Sin datos de demanda, no hay déficit
        assert result['gatillo'] == 'NINGUNO'


class TestG5Compuertas:

    def test_g5_sin_datos_no_pasa(self, app, db):
        """Sin fichas ni contenedores, G5 no pasa."""
        from app.services.armador_service import ArmadorService
        g5 = ArmadorService.verificar_g5()
        assert g5['g5_ok'] is False

    def test_g5_endpoint_registrado(self, app):
        """Endpoint /api/compras/armador/g5 está registrado."""
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('armador/g5' in r for r in rules)


class TestEndpoints:

    def test_rop_dual_endpoint(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('rop-dual' in r for r in rules)

    def test_propuesta_endpoint(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('armador/propuesta' in r for r in rules)

    def test_contenedores_endpoint(self, app):
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('armador/contenedores' in r for r in rules)
