"""
Tests del Vigía CUSUM — detección de corrimientos operativos.

Cisnes negros que previene:
- CUSUM con matemática incorrecta no detecta declive real → pérdida de ventas silenciosa
- Alarma cerrada sin causa → corrimiento se ignora, responsable invisible
- Doble alarma para la misma semana × serie × tipo → ruido que enmascara alarmas reales
- Series con < 8 puntos intentan calcular → crash en estadísticas
- σ_ref = 0 (serie constante) → división por cero en z-score
- Semana incompleta evaluada → falso positivo permanente cada semana
- Aviso y alarma confundidos → fatiga de alarmas o escalación perdida
"""
import pytest
from datetime import date, timedelta


def _crear_serie_semanal(db, nombre_serie, valores, fecha_inicio=None):
    """Helper: crea una serie semanal en la BD, terminando ANTES de la semana actual."""
    from app.services.vigia_service import SerieVigia, _lunes_semana_actual
    lunes_actual = _lunes_semana_actual()
    # Empezar lo suficientemente atrás para que toda la serie quede en semanas cerradas
    if fecha_inicio is None:
        fecha_inicio = lunes_actual - timedelta(weeks=len(valores) + 1)
    for i, valor in enumerate(valores):
        semana = fecha_inicio + timedelta(weeks=i)
        if semana >= lunes_actual:
            break  # No crear datos en la semana actual
        db.session.add(SerieVigia(
            serie=nombre_serie, semana=semana,
            valor=valor, registros=10,
        ))
    db.session.commit()


class TestCusumMatematica:
    """Invariantes matemáticas del CUSUM tabular bilateral."""

    def test_serie_estable_no_genera_alarma_roja(self, app, db):
        """Serie estable NO dispara ALARMA (h=4.5). Puede generar AVISO — eso es por diseño."""
        import random
        random.seed(42)
        valores = [1000 + random.gauss(0, 50) for _ in range(30)]
        _crear_serie_semanal(db, 'test_estable', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_estable')

        assert 'error' not in resultado
        alarmas_rojas = [p for p in resultado['cusum'] if p['severidad'] == 'ALARMA']
        assert len(alarmas_rojas) == 0, (
            f'Serie estable generó {len(alarmas_rojas)} ALARMAS — '
            f'AVISOs son aceptables, ALARMAs no')

    def test_declive_2sigma_detecta_baja(self, app, db):
        """Declive de 2σ+ se detecta como BAJA."""
        import random
        random.seed(7)
        ref = [1000 + random.gauss(0, 100) for _ in range(26)]
        valores = ref + [500, 500, 500, 500]
        _crear_serie_semanal(db, 'test_declive_2sigma', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_declive_2sigma')

        assert 'error' not in resultado
        bajas = [p for p in resultado['cusum'] if p['alarma'] == 'BAJA']
        assert len(bajas) > 0, 'Declive de 2σ no generó ninguna alarma BAJA'

    def test_pico_2sigma_detecta_sube(self, app, db):
        """Pico de 2σ+ se detecta como SUBE."""
        import random
        random.seed(8)
        ref = [1000 + random.gauss(0, 100) for _ in range(26)]
        valores = ref + [1800, 1800, 1800, 1800]
        _crear_serie_semanal(db, 'test_pico', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_pico')

        subes = [p for p in resultado['cusum'] if p['alarma'] == 'SUBE']
        assert len(subes) > 0, 'Pico de 2σ no generó alarma SUBE'

    def test_s_plus_y_s_minus_nunca_negativos(self, app, db):
        """Invariante CUSUM: S⁺ y S⁻ nunca pueden ser negativos."""
        import random
        random.seed(99)
        valores = [random.gauss(500, 200) for _ in range(40)]
        _crear_serie_semanal(db, 'test_no_negativo', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_no_negativo')

        for p in resultado['cusum']:
            assert p['s_plus'] >= 0, f'S⁺ negativo: {p["s_plus"]}'
            assert p['s_minus'] >= 0, f'S⁻ negativo: {p["s_minus"]}'

    def test_sigma_cero_no_crash(self, app, db):
        """Serie constante (σ=0) no divide por cero — usa fallback σ=1."""
        valores = [1000] * 26 + [500, 500]
        _crear_serie_semanal(db, 'test_constante', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_constante')

        assert 'error' not in resultado
        assert resultado['sigma_ref'] > 0, 'σ_ref debería ser > 0 (fallback)'


class TestDosBandas:
    """Dos bandas: AVISO (h=3.0) y ALARMA (h=4.5)."""

    def test_aviso_dispara_antes_que_alarma(self, app, db):
        """AVISO (h=3.0) se detecta antes que ALARMA (h=4.5) — da margen de reacción."""
        import random
        random.seed(13)
        # Declive gradual: ~1.5σ por semana — acumula lento
        ref = [1000 + random.gauss(0, 80) for _ in range(26)]
        declive = [600, 600, 600, 600, 600, 600]  # declive sostenido
        valores = ref + declive
        _crear_serie_semanal(db, 'test_dos_bandas', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_dos_bandas')

        avisos = [p for p in resultado['cusum'] if p['severidad'] == 'AVISO']
        alarmas = [p for p in resultado['cusum'] if p['severidad'] == 'ALARMA']

        # Debe haber al menos avisos O alarmas (el declive es fuerte)
        total = avisos + alarmas
        assert len(total) > 0, 'Declive sostenido no generó ni aviso ni alarma'

        # Si hay ambos, el primer aviso debe ser antes que la primera alarma
        if avisos and alarmas:
            primer_aviso_idx = next(i for i, p in enumerate(resultado['cusum'])
                                    if p['severidad'] == 'AVISO')
            primer_alarma_idx = next(i for i, p in enumerate(resultado['cusum'])
                                     if p['severidad'] == 'ALARMA')
            assert primer_aviso_idx <= primer_alarma_idx, (
                'ALARMA disparó antes que AVISO — las bandas están invertidas')

    def test_resultado_incluye_ambos_umbrales(self, app, db):
        """El resultado del CUSUM incluye h_aviso y h_alarma."""
        import random
        random.seed(50)
        valores = [1000 + random.gauss(0, 50) for _ in range(10)]
        _crear_serie_semanal(db, 'test_umbrales', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_umbrales')

        assert 'h_aviso' in resultado, 'Resultado no incluye h_aviso'
        assert 'h_alarma' in resultado, 'Resultado no incluye h_alarma'
        assert resultado['h_aviso'] < resultado['h_alarma'], 'h_aviso debe ser < h_alarma'


class TestGuardSemanaIncompleta:
    """La semana en curso NUNCA se evalúa — datos parciales = falsos positivos."""

    def test_semana_actual_se_excluye(self, app, db):
        """Datos de la semana en curso no aparecen en el resultado del CUSUM."""
        from app.services.vigia_service import SerieVigia, VigiaService, _lunes_semana_actual

        lunes_actual = _lunes_semana_actual()

        # Crear 10 semanas cerradas + 1 semana actual (parcial)
        for i in range(11):
            semana = lunes_actual - timedelta(weeks=10 - i)
            db.session.add(SerieVigia(
                serie='test_guard', semana=semana,
                valor=1000 if semana < lunes_actual else 100,  # Dato parcial = valor bajo
                registros=10,
            ))
        db.session.commit()

        resultado = VigiaService.ejecutar_cusum('test_guard')

        assert 'error' not in resultado
        # La semana actual NO debe estar en los resultados
        semanas_resultado = [p['semana'] for p in resultado['cusum']]
        assert lunes_actual.isoformat() not in semanas_resultado, (
            'La semana en curso apareció en el CUSUM — guard fallido')

    def test_dato_parcial_no_genera_falsa_alarma(self, app, db):
        """Un valor bajo en la semana actual no genera alarma falsa."""
        from app.services.vigia_service import SerieVigia, VigiaService, _lunes_semana_actual

        lunes_actual = _lunes_semana_actual()

        # 26 semanas estables + semana actual con valor absurdamente bajo
        for i in range(27):
            semana = lunes_actual - timedelta(weeks=26 - i)
            valor = 1000 if semana < lunes_actual else 1  # 1 = parcial
            db.session.add(SerieVigia(
                serie='test_parcial', semana=semana,
                valor=valor, registros=10,
            ))
        db.session.commit()

        resultado = VigiaService.ejecutar_cusum('test_parcial')
        assert 'error' not in resultado
        # No debe haber alarmas — la serie estable no tiene corrimiento
        alarmas = [p for p in resultado['cusum'] if p['alarma'] is not None]
        assert len(alarmas) == 0, (
            f'Dato parcial generó {len(alarmas)} alarmas falsas — guard no funciona')


class TestSerieInsuficiente:
    """Guard: series con menos de 8 puntos cerrados no deben procesarse."""

    def test_menos_de_8_semanas_rechaza(self, app, db):
        """CUSUM requiere mínimo 8 semanas cerradas — menos retorna error."""
        _crear_serie_semanal(db, 'test_corta', [100, 200, 300])

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_corta')
        assert 'error' in resultado

    def test_serie_inexistente_rechaza(self, app, db):
        """Serie que no existe retorna error, no crash."""
        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('serie_fantasma')
        assert 'error' in resultado


class TestAlarmaAntiSilencio:
    """Regla constitucional: ninguna ALARMA se cierra sin causa documentada."""

    def test_cerrar_alarma_sin_causa_falla(self, app, db):
        """ALARMA: causa vacía o corta (<20 chars) rechaza el cierre."""
        from app.services.vigia_service import AlarmaVigia, VigiaService
        from app.extensions import db as _db

        alarma = AlarmaVigia(
            serie='test', semana=date.today(), tipo='BAJA',
            severidad='ALARMA',
            s_valor=5.0, mu_ref=1000, sigma_ref=100,
        )
        _db.session.add(alarma)
        _db.session.commit()

        with pytest.raises(ValueError, match='20 caracteres'):
            VigiaService.cerrar_alarma(alarma.id, 'muy corta', 1)

    def test_cerrar_aviso_con_causa_corta(self, app, db):
        """AVISO: causa >= 5 chars es suficiente (no exige 20)."""
        from app.services.vigia_service import AlarmaVigia, VigiaService
        from app.extensions import db as _db

        alarma = AlarmaVigia(
            serie='test', semana=date.today(), tipo='BAJA',
            severidad='AVISO',
            s_valor=3.5, mu_ref=1000, sigma_ref=100,
        )
        _db.session.add(alarma)
        _db.session.commit()

        resultado = VigiaService.cerrar_alarma(alarma.id, 'Revisado', 1)
        assert resultado['ok'] is True

    def test_cerrar_alarma_con_causa_valida(self, app, db):
        """ALARMA: causa >= 20 chars con responsable cierra correctamente."""
        from app.services.vigia_service import AlarmaVigia, VigiaService
        from app.extensions import db as _db

        alarma = AlarmaVigia(
            serie='test', semana=date.today(), tipo='BAJA',
            severidad='ALARMA',
            s_valor=5.0, mu_ref=1000, sigma_ref=100,
        )
        _db.session.add(alarma)
        _db.session.commit()

        resultado = VigiaService.cerrar_alarma(
            alarma.id,
            'Temporada baja por vacaciones de mitad de año — revisado con gerencia regional',
            1,
        )
        assert resultado['ok'] is True

        _db.session.refresh(alarma)
        assert alarma.cerrada is True
        assert alarma.responsable_id == 1

    def test_cerrar_alarma_inexistente_falla(self, app, db):
        """Cerrar alarma que no existe lanza LookupError."""
        from app.services.vigia_service import VigiaService
        with pytest.raises(LookupError):
            VigiaService.cerrar_alarma(99999, 'causa de al menos veinte caracteres', 1)


class TestDeduplicacionAlarmas:
    """Idempotencia: ejecutar CUSUM 2 veces no duplica alarmas."""

    def test_doble_ejecucion_no_duplica(self, app, db):
        """Correr CUSUM dos veces sobre la misma serie no crea alarmas duplicadas."""
        import random
        random.seed(11)
        ref = [1000 + random.gauss(0, 100) for _ in range(26)]
        valores = ref + [400, 400, 400, 400]
        _crear_serie_semanal(db, 'test_dedup', valores)

        from app.services.vigia_service import VigiaService
        r1 = VigiaService.ejecutar_cusum('test_dedup')
        r2 = VigiaService.ejecutar_cusum('test_dedup')

        assert r1['alarmas_nuevas'] > 0, 'Primera ejecución debería crear alarmas'
        assert r2['alarmas_nuevas'] == 0, 'Segunda ejecución NO debería crear alarmas nuevas'


class TestReferenciasRobustas:
    """μ_ref y σ_ref usan mediana y MAD (resistentes a outliers)."""

    def test_mediana_no_media(self, app, db):
        """Con un outlier extremo, μ_ref (mediana) debe resistir."""
        valores = [1000] * 25 + [10000] + [1000] * 4
        _crear_serie_semanal(db, 'test_robusto', valores)

        from app.services.vigia_service import VigiaService
        resultado = VigiaService.ejecutar_cusum('test_robusto')

        assert resultado['mu_ref'] == 1000, (
            f'μ_ref={resultado["mu_ref"]} — debería ser 1000 (mediana)')


class TestEndpoints:
    """Los endpoints de Vigía están registrados y responden."""

    def test_vigia_registrado(self, app):
        """Blueprint vigia registrado con prefijo /api/vigia."""
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('vigia' in r for r in rules), 'Blueprint vigia no registrado'

    def test_alarmas_endpoint(self, app, db, client, jwt_token_admin):
        """GET /api/vigia/alarmas responde sin crash."""
        resp = client.get('/api/vigia/alarmas',
                          headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'alarmas' in data

    def test_series_endpoint(self, app, db, client, jwt_token_admin):
        """GET /api/vigia/series responde sin crash."""
        resp = client.get('/api/vigia/series',
                          headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'series' in data

    def test_cerrar_alarma_sin_responsable_rechaza(self, app, db, client, jwt_token_admin):
        """ALARMA sin responsable_id explícito retorna 400 — sin fallback."""
        from app.services.vigia_service import AlarmaVigia
        alarma = AlarmaVigia(
            serie='test_ep', semana=date.today(), tipo='BAJA',
            severidad='ALARMA', s_valor=5.0, mu_ref=1000, sigma_ref=100,
        )
        db.session.add(alarma)
        db.session.commit()

        resp = client.post(f'/api/vigia/alarmas/{alarma.id}/cerrar',
                           headers={'Authorization': f'Bearer {jwt_token_admin}',
                                    'Content-Type': 'application/json'},
                           json={'causa': 'Causa suficientemente larga para una alarma roja del vigia'})
        assert resp.status_code == 400, 'ALARMA sin responsable_id deberia ser rechazada'
        assert 'responsable_id' in resp.get_json()['error']

    def test_cargar_txt_bloqueado_sin_env(self, app, db, client, jwt_token_admin):
        """POST /api/vigia/cargar-txt retorna 403 cuando VIGIA_CARGAR_TXT no está seteado."""
        import os
        old = os.environ.pop('VIGIA_CARGAR_TXT', None)
        try:
            import io
            data = {'archivo': (io.BytesIO(b'test'), 'test.txt')}
            resp = client.post('/api/vigia/cargar-txt',
                               headers={'Authorization': f'Bearer {jwt_token_admin}'},
                               data=data, content_type='multipart/form-data')
            assert resp.status_code == 403, 'cargar-txt deberia estar bloqueado sin VIGIA_CARGAR_TXT=true'
            assert 'deshabilitada' in resp.get_json()['error']
        finally:
            if old is not None:
                os.environ['VIGIA_CARGAR_TXT'] = old
