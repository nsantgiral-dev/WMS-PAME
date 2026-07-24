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


class TestFuenteSeries:
    """El campo `fuente` separa la línea base histórica de la operación viva.

    Existe porque la limpieza transaccional del acta de corte debe poder borrar
    picks y pedidos de prueba SIN tocar serie_vigia: sin las 26 semanas de
    referencia el CUSUM no puede calcular mu_ref/sigma_ref y queda ciego.
    """

    def test_default_es_produccion(self, app, db):
        """Una serie creada sin especificar fuente nace como PRODUCCION."""
        from app.services.vigia_service import SerieVigia, _lunes_semana_actual
        s = SerieVigia(serie='test_fuente', semana=_lunes_semana_actual() - timedelta(weeks=2),
                       valor=100, registros=5)
        db.session.add(s)
        db.session.commit()
        assert s.fuente == 'PRODUCCION'

    def test_historico_y_produccion_conviven_en_la_misma_serie(self, app, db):
        """El panel debe poder mostrar línea continua: ambas fuentes en una serie."""
        from app.services.vigia_service import SerieVigia, _lunes_semana_actual
        lunes = _lunes_semana_actual()
        db.session.add(SerieVigia(serie='mixta_006', semana=lunes - timedelta(weeks=3),
                                  valor=100, registros=5, fuente='HISTORICO'))
        db.session.add(SerieVigia(serie='mixta_006', semana=lunes - timedelta(weeks=2),
                                  valor=110, registros=5, fuente='PRODUCCION'))
        db.session.commit()

        todas = SerieVigia.query.filter_by(serie='mixta_006').all()
        assert len(todas) == 2, 'ambas fuentes deben coexistir en la misma serie'
        historicas = [s for s in todas if s.fuente == 'HISTORICO']
        assert len(historicas) == 1, 'se debe poder filtrar por fuente sin borrar nada'

    def test_alimentar_picking_marca_produccion(self, app, db):
        """Las series que genera el scheduler son PRODUCCION, no HISTORICO."""
        from app.services.vigia_service import VigiaService, SerieVigia
        VigiaService.alimentar_adopcion_picking()
        vivas = SerieVigia.query.filter(
            SerieVigia.serie.in_(['adopcion_picking', 'brecha_picking'])).all()
        assert all(s.fuente == 'PRODUCCION' for s in vivas), \
            'series del scheduler deben marcarse PRODUCCION'


class TestSchedulerVigia:
    """El scheduler es la diferencia entre tener el termómetro puesto y tenerlo en el cajón.

    adopcion_picking mide si los operarios adoptaron el WMS. Si nadie llama a la
    función, la serie nunca se puebla y la métrica del go-live no existe.
    """

    def test_init_scheduler_existe(self):
        """vigia_service debe exponer init_scheduler para que app/__init__ lo arranque."""
        from app.services import vigia_service
        assert hasattr(vigia_service, 'init_scheduler'), 'falta init_scheduler'
        assert callable(vigia_service.init_scheduler)

    def test_alimentar_series_vivas_es_el_entrypoint_del_cron(self):
        """El job del cron debe existir y aceptar app= para el contexto."""
        import inspect
        from app.services import vigia_service
        assert callable(vigia_service.alimentar_series_vivas)
        params = inspect.signature(vigia_service.alimentar_series_vivas).parameters
        assert 'app' in params, 'el job necesita app= para abrir app_context'

    def test_esta_registrado_en_schedulers_esenciales(self):
        """Guard anti-regresión: si alguien saca vigia de la lista, esto falla.

        Va en esenciales y no en pesados a propósito — los pesados dependen de
        HEAVY_SCHEDULERS=true y si esa variable falta el cron nunca corre.
        """
        import re
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / 'app' / '__init__.py'
        contenido = src.read_text(encoding='utf-8')

        # El corchete de cierre va en su propia línea: los tags internos
        # ('[DLQ_SCHEDULER]') tambien traen ']' y cortarian un match no-greedy.
        esenciales = re.search(r'_scheduler_esenciales\s*=\s*\[(.*?)\n\s*\]', contenido, re.S)
        assert esenciales, 'no se encontró la lista _scheduler_esenciales'
        assert 'vigia_service' in esenciales.group(1), \
            'vigia_service debe estar en _scheduler_esenciales, no en _scheduler_pesados'


class TestArnesVerificacion:
    """Las tres pruebas de certificación de la carga histórica.

    Se corren una vez, tras subir los TXT y antes de devolver VIGIA_CARGAR_TXT
    a false. Certifican que la línea base entró bien y que la ingesta Connekta
    puede heredar sus convenciones.
    """

    def test_sin_datos_no_certifica(self, app, db):
        """Base vacía NUNCA certifica — es el estado actual de producción."""
        from app.services.vigia_service import verificar_carga_historica
        r = verificar_carga_historica()
        assert r['certificado'] is False
        conteo = next(p for p in r['pruebas'] if p['prueba'].startswith('1.'))
        assert any('no se cargó' in f for f in conteo['fallos'])

    def test_sin_canon_queda_pendiente_no_inventa(self, app, db, tmp_path):
        """Sin canon NI flags, la prueba 2 se marca pendiente en vez de fabricar un número.

        Con el canon del repo presente esto no ocurre — pero el arnés nunca debe
        inventar una referencia si se queda sin ella.
        """
        import json
        from app.services.vigia_service import verificar_carga_historica
        vacio = tmp_path / 'canon_vacio.json'
        vacio.write_text(json.dumps({'esperado': {}}), encoding='utf-8')

        _crear_serie_semanal(db, 'facturas_006', [100] * 20)
        r = verificar_carga_historica(canon_path=str(vacio))
        backtest = next(p for p in r['pruebas'] if p['prueba'].startswith('2.'))
        assert backtest.get('pendiente_valor_referencia') is True

    def test_semana_de_alarma_distinta_falla(self, app, db):
        """Si la tubería cambió, la semana de alarma no coincide y la prueba revienta."""
        from app.services.vigia_service import SerieVigia, verificar_carga_historica
        # Serie estable que colapsa: garantiza una alarma BAJA
        valores = [100] * 26 + [20] * 6
        _crear_serie_semanal(db, 'facturas_006', valores)
        for s in SerieVigia.query.filter_by(serie='facturas_006').all():
            s.fuente = 'HISTORICO'
        db.session.commit()

        r = verificar_carga_historica(semana_alarma='1999-01-04')
        backtest = next(p for p in r['pruebas'] if p['prueba'].startswith('2.'))
        assert backtest['ok'] is False
        assert any('semana de alarma' in f for f in backtest['fallos'])

    def test_conteo_detecta_semanas_faltantes(self, app, db):
        """Cargar menos semanas de las esperadas no puede pasar silenciosamente."""
        from app.services.vigia_service import SerieVigia, verificar_carga_historica
        _crear_serie_semanal(db, 'facturas_006', [100] * 10)
        for s in SerieVigia.query.filter_by(serie='facturas_006').all():
            s.fuente = 'HISTORICO'
        db.session.commit()

        r = verificar_carga_historica(semanas=53)
        conteo = next(p for p in r['pruebas'] if p['prueba'].startswith('1.'))
        assert conteo['ok'] is False
        assert any('semanas' in f for f in conteo['fallos'])

    def test_series_de_produccion_no_cuentan_como_historico(self, app, db):
        """La prueba 1 solo mira HISTORICO: lo que genere el scheduler no infla el conteo."""
        from app.services.vigia_service import SerieVigia, verificar_carga_historica
        _crear_serie_semanal(db, 'adopcion_picking', [5] * 10)  # nace PRODUCCION
        r = verificar_carga_historica()
        conteo = next(p for p in r['pruebas'] if p['prueba'].startswith('1.'))
        assert conteo['observado']['semanas_distintas'] == 0, \
            'series PRODUCCION no deben contarse como línea base histórica'

    def test_endpoint_verificar_carga(self, app, db, client, jwt_token_admin):
        """GET /api/vigia/verificar-carga responde con las tres pruebas."""
        resp = client.get('/api/vigia/verificar-carga?semanas=53&cos=6',
                          headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert resp.status_code == 200
        d = resp.get_json()
        assert 'certificado' in d and 'contexto_comparable' in d
        # 0. parámetros, 0b. insumos, 1. conteo, 2. canon, 3. alarma
        assert len(d['pruebas']) == 5
        assert d['canon']['cargado'] is True


class TestCanonFlorencia:
    """El canon vive en el repo con procedencia, no en la memoria de nadie.

    Distingue dos preguntas que se confunden en una:
      - Prueba de diseño ('¿detecta dentro del plazo de la spec?') — cerrada.
      - Prueba de reproducción ('¿produccion reproduce lo certificado, con los
        mismos insumos y parámetros?') — vigente, exacta, no se afloja.
    """

    def test_canon_existe_y_tiene_procedencia(self):
        """Un canon sin procedencia es tradición oral con formato JSON."""
        from app.services.vigia_service import cargar_canon
        canon = cargar_canon()
        assert canon is not None, 'falta docs/canon_florencia.json'
        assert canon['esperado']['semana_alarma'] == '2025-12-29'
        assert abs(canon['esperado']['s_minus'] - 6.30) < 1e-9
        assert canon['procedencia']['origen'], 'el canon debe declarar de dónde salió'
        assert canon['serie']['nombre_en_bd'] == 'facturas_006'

    def test_tolerancia_es_de_flotantes_no_de_criterio(self):
        """±0.05 cubre punto flotante. Más que eso sería aflojar el criterio."""
        from app.services.vigia_service import cargar_canon
        assert cargar_canon()['esperado']['tolerancia_s_minus'] <= 0.05

    def test_usa_el_canon_sin_pasar_argumentos(self, app, db):
        """El arnés lee el canon solo: nadie tiene que recordar el número."""
        from app.services.vigia_service import SerieVigia, verificar_carga_historica
        valores = [100] * 26 + [20] * 6
        _crear_serie_semanal(db, 'facturas_006', valores)
        for s in SerieVigia.query.filter_by(serie='facturas_006').all():
            s.fuente = 'HISTORICO'
        db.session.commit()

        r = verificar_carga_historica()
        backtest = next(p for p in r['pruebas'] if p['prueba'].startswith('2.'))
        # Serie sintética: no reproduce el canon, pero DEBE haberlo aplicado
        assert backtest.get('pendiente_valor_referencia') is not True
        assert backtest['esperado']['semana_alarma'] == '2025-12-29'

    def test_parametros_distintos_marcan_contexto_no_comparable(self, app, db, monkeypatch):
        """Con k distinto, una divergencia en S- no acusa a la tubería."""
        import app.services.vigia_service as vs
        monkeypatch.setattr(vs, 'CUSUM_K', 0.9)
        r = vs.verificar_carga_historica()
        params = next(p for p in r['pruebas'] if p['prueba'].startswith('0.'))
        assert params['ok'] is False
        assert any('k' in f for f in params['fallos'])
        assert r['contexto_comparable'] is False

    def test_parametros_de_produccion_coinciden_con_el_canon(self, app, db):
        """Guard: si alguien cambia CUSUM_K por env, la certificación lo detecta."""
        from app.services.vigia_service import verificar_carga_historica
        r = verificar_carga_historica()
        params = next(p for p in r['pruebas'] if p['prueba'].startswith('0.'))
        assert params['ok'], f"parámetros divergen del canon: {params['fallos']}"

    def test_insumos_distintos_no_es_fallo_de_tuberia(self, app, db, tmp_path):
        """Hash que no coincide → 'insumos distintos', no acusación al código."""
        import json
        from app.services.vigia_service import verificar_carga_historica, CANON_PATH

        with open(CANON_PATH, encoding='utf-8') as f:
            canon = json.load(f)
        canon['insumos'] = {'registrado': True, 'archivos': [
            {'archivo': 'original.txt', 'sha256': 'a' * 64, 'bytes': 10}]}
        canon_falso = tmp_path / 'canon.json'
        canon_falso.write_text(json.dumps(canon), encoding='utf-8')

        otro = tmp_path / 'otro.txt'
        otro.write_text('contenido distinto', encoding='utf-8')

        r = verificar_carga_historica(insumos=[str(otro)], canon_path=str(canon_falso))
        ins = next(p for p in r['pruebas'] if p['prueba'].startswith('0b'))
        assert ins['insumos_distintos'] is True
        assert r['contexto_comparable'] is False, \
            'insumos distintos deben invalidar el contexto, no reprobar la tubería'

    def test_insumos_iguales_dan_contexto_comparable(self, app, db, tmp_path):
        """Mismo hash → el contexto es válido y la prueba 2 sí juzga la tubería."""
        import json
        from app.services.vigia_service import (verificar_carga_historica,
                                                sha256_archivo, CANON_PATH)
        txt = tmp_path / 'ventas.txt'
        txt.write_text('linea de ventas', encoding='utf-8')

        with open(CANON_PATH, encoding='utf-8') as f:
            canon = json.load(f)
        canon['insumos'] = {'registrado': True, 'archivos': [
            {'archivo': 'ventas.txt', 'sha256': sha256_archivo(str(txt)), 'bytes': 15}]}
        canon_falso = tmp_path / 'canon.json'
        canon_falso.write_text(json.dumps(canon), encoding='utf-8')

        r = verificar_carga_historica(insumos=[str(txt)], canon_path=str(canon_falso))
        ins = next(p for p in r['pruebas'] if p['prueba'].startswith('0b'))
        assert ins['ok'] is True
        assert not ins.get('insumos_distintos')
        assert r['contexto_comparable'] is True

    def test_sin_hashes_registrados_avisa_pero_no_reprueba(self, app, db):
        """Canon sin insumos registrados: no bloquea, pero deja el aviso."""
        from app.services.vigia_service import verificar_carga_historica
        r = verificar_carga_historica()
        ins = next(p for p in r['pruebas'] if p['prueba'].startswith('0b'))
        assert ins['ok'] is True
        assert ins.get('no_verificable') is True
        assert 'SHA-256' in ins['aviso']
