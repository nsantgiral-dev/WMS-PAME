"""
Las tres banderas de idempotencia financiera, después de la auditoría del
2026-08-13.

Tres auditorías independientes convergieron sobre el mismo par de líneas:

    rutas.py:1244             recaudo.siesa_dc_triggered = True   # «evitar doble encolado»
    siesa_job_service.py:1249 if recaudo.siesa_dc_triggered: return {'idempotente': True}

El endpoint de liquidar-completo encolaba los documentos de retención y acto
seguido encendía **la bandera que el ejecutor usa como guarda**. Cuando el job
corría, la leía, se declaraba idempotente y se marcaba completado **sin enviar
nada**.

**Ningún documento de retención llegó nunca a Siesa por esa vía**, y el log, la
pantalla y el tablero informaban éxito.

Y lo que lo volvía indetectable: la verificación que ya estaba pedida —«una
liquidación con retención, que nunca ha corrido»— **no lo habría descubierto**.
La corrida se ve exitosa desde el WMS. Solo abrir Siesa y no encontrar el
documento lo revela.

## La causa de fondo

Una bandera con dos significados. «Ya encolé» y «ya envié» no son lo mismo, y
usar el mismo booleano para los dos hace que uno de los dos esté siempre mal.
"""
import pytest

from app.models.recaudo_entrega import RecaudoEntrega


class TestUnaBanderaNoPuedeSignificarDosCosas:

    def test_el_endpoint_no_toca_la_bandera_del_ejecutor(self):
        """El anti-doble-encolado se resuelve mirando la cola, no la bandera de
        envío. Si vuelve a tocarla, ninguna retención llega a Siesa."""
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1]
                  / 'app' / 'routes' / 'rutas.py').read_text(encoding='utf-8')
        assert 'recaudo.siesa_dc_triggered = True' not in fuente, (
            '\nEl endpoint volvió a encender `siesa_dc_triggered`, que es la '
            'guarda de idempotencia del ejecutor. Con eso, cada job encolado '
            'se declara idempotente y se completa SIN ENVIAR NADA.')

    def test_el_endpoint_deduplica_mirando_la_cola(self):
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1]
                  / 'app' / 'routes' / 'rutas.py').read_text(encoding='utf-8')
        assert '_pucs_en_cola' in fuente


class TestLasRetencionesSonNYLaBanderaEsUna:
    """Un cliente con retefuente + reteIVA + ICA son **tres** documentos."""

    def test_cada_cuenta_se_marca_por_separado(self, app):
        with app.app_context():
            r = RecaudoEntrega()
            r.marcar_puc_enviada('13551501')
            assert r.pucs_enviadas() == {'13551501'}
            r.marcar_puc_enviada('13551701')
            assert r.pucs_enviadas() == {'13551501', '13551701'}

    def test_marcar_una_no_bloquea_las_otras(self, app):
        """El defecto exacto: con la guarda sobre el booleano, la segunda y la
        tercera retención se declaraban idempotentes sin enviarse."""
        with app.app_context():
            r = RecaudoEntrega()
            r.marcar_puc_enviada('13551501')
            assert '13551701' not in r.pucs_enviadas()
            assert '13551801' not in r.pucs_enviadas()

    def test_revertir_una_no_borra_las_demas(self, app):
        with app.app_context():
            r = RecaudoEntrega()
            r.marcar_puc_enviada('13551501')
            r.marcar_puc_enviada('13551701')
            r.desmarcar_puc('13551501')
            assert r.pucs_enviadas() == {'13551701'}
            assert r.siesa_dc_triggered is True, (
                'queda una enviada: la bandera resumen sigue encendida')

    def test_revertir_la_ultima_apaga_la_bandera_resumen(self, app):
        with app.app_context():
            r = RecaudoEntrega()
            r.marcar_puc_enviada('13551501')
            r.desmarcar_puc('13551501')
            assert r.siesa_dc_triggered is False

    def test_un_json_ilegible_no_reenvia(self, app):
        """Ante no saber qué se envió, NO se reenvía: un documento contable
        duplicado es un ajuste manual en el ERP. Regla 0."""
        with app.app_context():
            r = RecaudoEntrega()
            r.siesa_dc_pucs = '{esto no es json'
            assert r.pucs_enviadas() == {'__ILEGIBLE__'}

    def test_la_guarda_del_ejecutor_es_por_cuenta(self):
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1]
                  / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        i = fuente.find("if job.tipo == 'DOCUMENTO_CONTABLE_RET'")
        j = fuente.find("raise ValueError(f'Tipo de job no reconocido", i)
        bloque = fuente[i:j]
        assert 'pucs_enviadas()' in bloque
        assert 'recaudo.siesa_dc_triggered' not in bloque, (
            'la guarda volvió al booleano: solo la primera retención se envía')


class TestLaNotaCreditoMarcaAntesDelPost:
    """Era la única de las tres que marcaba DESPUÉS.

    Un crash entre el POST y el commit dejaba la bandera en False, el DLQ
    reintentaba y Siesa recibía una segunda nota crédito. RC y DC ya marcaban
    antes; ésta no, y nada explicaba por qué.
    """

    def test_marca_antes_de_llamar_a_connekta(self):
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1]
                  / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        i = fuente.find("if job.tipo == 'NOTA_CREDITO_FACTURA'")
        j = fuente.find("if job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE'", i)
        bloque = fuente[i:j]

        pos_flag = bloque.find('recaudo.siesa_nc_triggered = True')
        pos_post = bloque.find('connekta.trigger_nota_factura_crear_cruzar')
        assert pos_flag != -1 and pos_post != -1
        assert pos_flag < pos_post, (
            '\nLa nota crédito volvió a marcarse DESPUÉS del POST. Un crash en '
            'esa ventana produce una segunda NC en Siesa (Regla 6).')

    def test_revierte_ante_fallo_explicito(self):
        """La otra mitad del pre-flag: sin la reversión, un fallo real dejaría
        la NC bloqueada para siempre."""
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1]
                  / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        i = fuente.find("if job.tipo == 'NOTA_CREDITO_FACTURA'")
        j = fuente.find("if job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE'", i)
        assert 'recaudo.siesa_nc_triggered = False' in fuente[i:j]
