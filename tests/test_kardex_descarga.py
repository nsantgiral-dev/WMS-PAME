"""
Tests de la descarga de kardex.

Cisne negro que previenen: UNA DESCARGA PARCIAL REPORTADA COMO ÉXITO.

Un kardex truncado no produce un error — produce descensura equivocada, ROP
equivocado y temporada equivocada, todo plausible y sin una sola alarma. Y no
era hipotético: ~17.000 peticiones a 0.1-0.2s son 28-57 minutos, y el corte
duro estaba DENTRO de ese rango, no cerca.

Antes había tres formas de terminar —fin natural, timeout, excepción— y las
tres caían en el mismo retorno de éxito.
"""
import os
import re

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(_RAIZ, rel), encoding='utf-8') as f:
        return f.read()


class TestParcialEsFallo:
    """La propiedad central: completa y truncado no pueden verse igual."""

    def test_sin_datos_no_reporta_exito(self, app, db):
        """En simulación la primera página viene vacía: no es una descarga buena."""
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        assert r['ok'] is False
        assert r['estado'] == 'SIN_DATOS'

    def test_el_resultado_declara_como_termino(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        for clave in ('ok', 'estado', 'rango_pedido', 'rango_traido',
                      'pagina_final', 'reanudar_desde'):
            assert clave in r, f'falta {clave} en el resultado'

    def test_ok_solo_es_true_si_esta_completa(self):
        """Guard estructural: ok se deriva del estado, no se pone a mano."""
        src = _src('app/services/kardex_service.py')
        assert "completa = estado == 'COMPLETA'" in src
        assert "'ok': completa" in src

    def test_los_tres_finales_estan_diferenciados(self):
        """Timeout, excepción y fin natural: tres estados, no uno."""
        src = _src('app/services/kardex_service.py')
        for estado in ('TIMEOUT_PARCIAL', 'ERROR_PARCIAL', 'SIN_DATOS', 'COMPLETA'):
            assert estado in src, f'falta el estado {estado}'

    def test_una_parcial_advierte_de_no_seguir(self):
        """Correr /reconstruir sobre un kardex truncado propaga el hueco."""
        src = _src('app/services/kardex_service.py')
        assert 'advertencia' in src and 'reconstruir' in src


class TestRangoPedidoVsTraido:
    """La comparación que delata el truncamiento sin mirar los logs."""

    def test_el_rango_pedido_sale_de_los_argumentos(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20250115', '20250220', max_minutos=1)
        assert r['rango_pedido']['desde'] == '2025-01-15'
        assert r['rango_pedido']['hasta'] == '2025-02-20'

    def test_el_rango_traido_es_none_si_no_llego_nada(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        assert r['rango_traido']['desde'] is None


class TestReanudacion:
    """Trocear por página, no por fecha.

    La consulta dinámica NO acepta filtros de fecha —se filtra en Python
    después de recibir— así que acotar el rango no ahorra ni una petición.
    Lo que sí funciona es reanudar desde donde se cortó.
    """

    def test_acepta_pagina_inicial(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', pagina_inicial=42, max_minutos=1)
        assert r['pagina_inicial'] == 42

    def test_una_completa_no_deja_punto_de_reanudacion(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        if r['ok']:
            assert r['reanudar_desde'] is None

    def test_el_tope_de_tiempo_bajo_del_corte_anterior(self):
        """25 min por corrida: mejor varias honestas que una que se rinde."""
        src = _src('app/services/kardex_service.py')
        assert "KARDEX_MAX_MINUTOS', '25'" in src

    def test_el_panel_ofrece_reanudar(self):
        js = _src('app/static/pwa/kardex.js')
        assert 'reanudar_desde' in js
        assert 'kardexDescargar(r.reanudar_desde)' in js.replace('${', '').replace('}', '')


class TestAvisoOperativo:
    """17.000 llamadas contra el ERP que factura en siete puntos de venta."""

    def test_el_endpoint_avisa_del_impacto(self):
        src = _src('app/routes/kardex.py')
        assert 'FUERA DE HORARIO' in src.upper()

    def test_el_panel_lo_muestra_antes_de_pulsar(self):
        """El aviso sirve antes del clic, no en un log después."""
        js = _src('app/static/pwa/kardex.js')
        assert '17.000' in js and 'fuera de horario' in js.lower()


class TestPerfilMensual:
    """El rango no ve los agujeros del medio; el perfil sí.

    Un fallo de reanudación produce un kardex que abarca todo el rango con un
    hueco en marzo, y rango-pedido-vs-traído diría COMPLETA. Es el mismo error
    de siempre: el rango es la REPRESENTACIÓN de la completitud; la COSA es que
    estén todas las filas.
    """

    def test_kardex_vacio_no_crashea(self, app, db):
        from app.services.kardex_service import perfil_mensual_kardex
        assert perfil_mensual_kardex()['meses'] == []

    def test_detecta_un_mes_ausente_en_medio(self, app, db):
        """EL CASO QUE MOTIVA TODO: rango completo, agujero adentro."""
        from datetime import date
        from app.services.kardex_service import KardexMovimiento, perfil_mensual_kardex
        # Enero y marzo con datos; febrero VACÍO. El rango ene→mar se ve intacto.
        for mes, dia in ((1, 15), (3, 15)):
            for i in range(40):
                db.session.add(KardexMovimiento(
                    referencia=f'R{i}', bodega='NB1', fecha=date(2026, mes, dia),
                    concepto=501, naturaleza=2, cantidad=1))
        db.session.commit()

        p = perfil_mensual_kardex()
        assert '2026-02' in p['meses_ausentes'], 'no detectó el mes ausente'
        assert p['sin_huecos'] is False

    def test_detecta_un_mes_anomalamente_bajo(self, app, db):
        from datetime import date
        from app.services.kardex_service import KardexMovimiento, perfil_mensual_kardex
        for mes, n in ((1, 100), (2, 3), (3, 100)):  # febrero casi vacío
            for i in range(n):
                db.session.add(KardexMovimiento(
                    referencia=f'R{i}', bodega='NB1', fecha=date(2026, mes, 10),
                    concepto=501, naturaleza=2, cantidad=1))
        db.session.commit()

        p = perfil_mensual_kardex()
        assert '2026-02' in p['huecos']
        assert p['sin_huecos'] is False

    def test_meses_parejos_no_dan_falsa_alarma(self, app, db):
        """Un detector que grita siempre es tan inútil como uno que calla."""
        from datetime import date
        from app.services.kardex_service import KardexMovimiento, perfil_mensual_kardex
        for mes in (1, 2, 3, 4):
            for i in range(50):
                db.session.add(KardexMovimiento(
                    referencia=f'R{i}', bodega='NB1', fecha=date(2026, mes, 10),
                    concepto=501, naturaleza=2, cantidad=1))
        db.session.commit()
        assert perfil_mensual_kardex()['sin_huecos'] is True


class TestReconstruirDenyByDefault:
    """Una advertencia se ignora bajo presión de agenda; un rechazo no.

    Reconstruir sobre un kardex truncado fabrica días sin movimiento que sí lo
    tuvieron — demanda censurada inventada por el descargador, contaminando
    justo el modelo que existe para corregir la censura.
    """

    def test_rechaza_sin_descarga_completa(self, app, db, client, jwt_token_admin):
        resp = client.post('/api/kardex/reconstruir', json={},
                           headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert resp.status_code == 409, 'debe RECHAZAR, no advertir'
        d = resp.get_json()
        assert 'RECHAZADO' in d['error']
        assert 'que_hacer' in d and 'override' in d

    def test_el_override_existe_y_queda_auditado(self):
        src = _src('app/routes/kardex.py')
        assert 'forzar' in src
        assert 'OVERRIDE' in src, 'el override debe dejar rastro en el log'

    def test_rechaza_si_hay_descarga_en_curso(self, app, db, client, jwt_token_admin):
        from app.routes.kardex import _kardex_descarga_estado
        _kardex_descarga_estado['en_curso'] = True
        try:
            resp = client.post('/api/kardex/reconstruir', json={},
                               headers={'Authorization': f'Bearer {jwt_token_admin}'})
            assert resp.status_code == 409
        finally:
            _kardex_descarga_estado['en_curso'] = False


class TestSupuestoDeReanudacion:
    """El orden de la consulta no está declarado — y de eso depende reanudar."""

    def test_una_parcial_declara_el_supuesto(self, app, db):
        """Si el orden no es estable, la página N no contiene lo mismo dos veces."""
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        if not r['ok'] and r['estado'] != 'SIN_DATOS':
            assert 'ESTABLE' in r['_supuesto_de_reanudacion'].upper()

    def test_el_codigo_documenta_que_el_orden_lo_define_siesa(self):
        src = _src('app/services/kardex_service.py')
        assert 'orden' in src.lower() and 'HUECOS' in src


class TestIdempotenciaDeLaIngesta:
    """La duplicación es PEOR que la omisión: el perfil mensual no la delata.

    Un mes con 8% de filas de más se ve plausible. Y movimientos duplicados
    inflan la demanda, que infla el ROP, que infla el contenedor — el bug de
    25× por otra puerta.

    Antes: INSERT plano sobre índice NO único. Pulsar "Descargar" dos veces
    duplicaba el kardex entero.
    """

    def test_el_mismo_movimiento_da_el_mismo_hash(self):
        from datetime import date
        from app.services.kardex_service import hash_movimiento
        a = hash_movimiento(date(2026, 3, 1), 'FEW', 'NB1', 'REF1', 501, 2, 10)
        b = hash_movimiento(date(2026, 3, 1), 'FEW', 'NB1', 'REF1', 501, 2, 10)
        assert a == b

    def test_cambiar_cualquier_campo_cambia_el_hash(self):
        from datetime import date
        from app.services.kardex_service import hash_movimiento
        base = dict(fecha=date(2026, 3, 1), tipo_docto='FEW', bodega='NB1',
                    referencia='REF1', concepto=501, naturaleza=2, cantidad=10)
        h0 = hash_movimiento(**base)
        for campo, valor in (('bodega', 'NB2'), ('referencia', 'REF2'),
                             ('concepto', 502), ('naturaleza', 1), ('cantidad', 11)):
            assert hash_movimiento(**{**base, campo: valor}) != h0, \
                f'cambiar {campo} debe cambiar la identidad'

    def test_la_clave_natural_distingue_movimientos_identicos(self):
        """Sin consecutivo+línea, dos movimientos legítimamente iguales
        colapsarían en uno. Con ella, la identidad es exacta."""
        from datetime import date
        from app.services.kardex_service import hash_movimiento
        base = dict(fecha=date(2026, 3, 1), tipo_docto='FEW', bodega='NB1',
                    referencia='REF1', concepto=501, naturaleza=2, cantidad=10)
        assert (hash_movimiento(**base, consec_docto='100', nro_registro=1)
                != hash_movimiento(**base, consec_docto='100', nro_registro=2))

    def test_el_modelo_tiene_indice_unico_sobre_la_identidad(self):
        from app.services.kardex_service import KardexMovimiento
        indices = [i for i in KardexMovimiento.__table__.indexes
                   if 'hash_origen' in [c.name for c in i.columns]]
        assert indices, 'falta el índice sobre hash_origen'
        assert indices[0].unique, 'el índice DEBE ser único o la duplicación vuelve'

    def test_la_descarga_reporta_duplicados_omitidos(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        assert 'duplicados_omitidos' in r

    def test_la_descarga_reporta_el_total_declarado_por_siesa(self, app, db):
        """Si Siesa declara cuántas filas hay, es prueba por CONTEO —
        superior al perfil mensual, que la infiere por distribución."""
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        assert 'total_declarado_por_siesa' in r


class TestPruebaDeEstabilidad:
    """Dos peticiones en vez de diecisiete mil."""

    def test_existe_la_prueba(self):
        from app.services.kardex_service import KardexService
        assert hasattr(KardexService, 'probar_estabilidad_paginacion')

    def test_el_endpoint_esta_registrado(self, app):
        rutas = [str(r) for r in app.url_map.iter_rules()]
        assert any('probar-paginacion' in r for r in rutas)

    def test_el_veredicto_dice_que_hacer_si_es_inestable(self):
        src = _src('app/services/kardex_service.py')
        assert 'UNA sola sesión' in src or 'UNA sola sesion' in src


class TestRitmoRealMedido:
    """El ritmo se midió en producción, no se supuso.

    Log de Railway (27-jul-2026): 18 páginas en 58 segundos = 3.41 s/página.
    La estimación previa era 0.1-0.2 s/página — 23 veces optimista. A ese
    ritmo, 17.000 páginas son 16 horas, no 28-57 minutos.
    """

    def test_el_codigo_documenta_el_ritmo_medido(self):
        src = _src('app/services/kardex_service.py')
        assert '3.41' in src, 'el ritmo medido debe quedar escrito, no supuesto'
        assert 'HORAS' in src, 'la consecuencia (horas, no minutos) debe estar declarada'

    def test_el_tope_por_corrida_hace_inevitable_reanudar(self):
        """Con 25 min por corrida y 16 horas de trabajo, son ~40 corridas.
        Reanudar deja de ser red de seguridad: es el único camino."""
        import os as _os
        tope = int(_os.environ.get('KARDEX_MAX_MINUTOS', '25'))
        paginas_por_corrida = tope * 60 / 3.41
        corridas = 17000 / paginas_por_corrida
        assert corridas > 10, (
            f'Con {tope} min por corrida hacen falta ~{corridas:.0f} corridas. '
            f'Si este número bajara de 10, revisar el supuesto de ritmo.'
        )
