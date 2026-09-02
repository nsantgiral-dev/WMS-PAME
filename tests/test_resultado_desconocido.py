"""
El tercer estado de un POST: **no sabemos**.

Un POST a Siesa tiene tres desenlaces, no dos. «Entró» y «Siesa lo rechazó»
son distinguibles; el que faltaba es «salió y no volvió la respuesta», que con
Siesa tardando 30-60 s es **el caso más probable de un timeout** — no el menos.

Tratarlo como fallo dispara las dos reacciones que duplican:

    · el pre-flag se revierte y el DLQ reintenta con la guardia abajo
    · el mensaje manda al operario a reintentar a mano

Las dos ocurrían. La Regla 3 dice que un POST no reintenta ante timeout, y sin
un tipo de excepción propio esa regla **no se podía cumplir**: el timeout
llegaba a los handlers como `Exception`, indistinguible de un rechazo de Siesa,
que sí se puede reintentar sin riesgo.

## Por qué la nota crédito y no otra

`RECIBO_CAJA` ya verificaba con `cxc_cruce.esta_saldada` antes de revertir —es
la corrección del 2026-08-13—. Su hermana `NOTA_CREDITO_FACTURA` revertía a
ciegas bajo un comentario que afirmaba «fallo explícito: no se creó nada»,
dentro de un `except Exception` que atrapa el timeout. Una política, dos
implementaciones, y nada explicaba la diferencia.
"""
import json

import pytest

from app.services.connekta_gateway import (
    ConnektaResultadoDesconocido,
    _POST_READ_TIMEOUT,
)


class TestElTimeoutDelPost:
    def test_espera_lo_que_siesa_tarda(self):
        """Medido entre 30 y 60 s. Estaba en 30: la mitad de las escrituras
        exitosas se reportaban como fallidas."""
        assert _POST_READ_TIMEOUT >= 60, (
            f'el timeout de lectura del POST es {_POST_READ_TIMEOUT}s y Siesa '
            f'tarda entre 30 y 60 — cortar antes no evita el documento, solo '
            f'esconde que se creó')

    def test_el_gateway_lo_usa_de_verdad(self):
        """Un valor configurado que nadie lee es peor que no tenerlo: dice que
        el problema está resuelto."""
        import ast
        import pathlib
        src = pathlib.Path('app/services/connekta_gateway.py').read_text()
        arbol = ast.parse(src)
        usos = [n for n in ast.walk(arbol)
                if isinstance(n, ast.Name) and n.id == '_POST_READ_TIMEOUT'
                and isinstance(n.ctx, ast.Load)]
        assert len(usos) >= 2, (
            '_POST_READ_TIMEOUT casi no se lee — el `timeout=` del POST '
            'probablemente tiene el número escrito a mano al lado')
        assert 'timeout=(10, 30)' not in src, 'volvió el timeout de 30s'

    def test_el_mensaje_no_manda_a_reintentar(self):
        """El texto anterior decía «reintenta confirmar»: la Regla 3 frena al
        DLQ, pero ese mensaje le entregaba el duplicado a una persona.

        **Por AST y no por texto.** La primera versión buscaba la cadena
        prohibida en el bloque fuente y se atrapó en el comentario que explica
        que se quitó — la octava vez que un detector de texto se caza solo en
        este repo. Lo que importa no es qué dice el archivo: es qué cadena
        llega al `raise`.
        """
        import ast
        import pathlib
        arbol = ast.parse(
            pathlib.Path('app/services/connekta_gateway.py').read_text())

        mensajes = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.ExceptHandler):
                continue
            tipo = ast.dump(nodo.type) if nodo.type else ''
            if 'Timeout' not in tipo:
                continue
            for hijo in ast.walk(nodo):
                if not isinstance(hijo, ast.Raise) or not hijo.exc:
                    continue
                exc = hijo.exc
                nombre = getattr(getattr(exc, 'func', None), 'id', None)
                partes = []
                for a in getattr(exc, 'args', []):
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        partes.append(a.value)
                    elif isinstance(a, ast.JoinedStr):
                        partes += [v.value for v in a.values
                                   if isinstance(v, ast.Constant)
                                   and isinstance(v.value, str)]
                mensajes.append((nombre, ' '.join(partes)))

        del_post = [m for m in mensajes
                    if m[0] == 'ConnektaResultadoDesconocido']
        assert del_post, (
            f'ningún `except Timeout` levanta ConnektaResultadoDesconocido — '
            f'los handlers no lo pueden distinguir de un rechazo de Siesa. '
            f'Vistos: {mensajes}')
        for _, texto in del_post:
            assert 'reintenta confirmar' not in texto, (
                'el mensaje del timeout sigue mandando a reintentar')
            assert 'erific' in texto, (
                f'el mensaje no le dice a la persona qué hacer: {texto!r}')


class TestLaNotaCreditoNoRevierteAntePocaCerteza:
    """El detector ciego del arreglo: se rompe a propósito y se exige verlo."""

    def _job_nc(self, db, monkeypatch, excepcion):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs

        r = RecaudoEntrega(ruta_id=1, tarea_id=1, monto_cobrado=1000,
                           estado_entrega='RECHAZADO', forma_pago='EFECTIVO')
        db.session.add(r)
        db.session.flush()
        job = SiesaJob(tipo='NOTA_CREDITO_FACTURA', estado='PENDIENTE',
                       referencia_tipo='RecaudoEntrega', referencia_id=r.id,
                       payload=json.dumps({'recaudo_id': r.id,
                                           'tipo_docto_fe': 'FEW',
                                           'consec_fe': 1466,
                                           'es_total': True}))
        db.session.add(job)
        db.session.commit()

        from app.services.connekta_gateway import connekta

        # La NC necesita los rowids de la factura antes del POST.
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [
            {'f470_rowid': 1, 'f470_vlr_neto': 1000,
             'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1'}])

        def _boom(*a, **k):
            raise excepcion
        # `_ejecutar_job` importa el singleton dentro de la función, así que
        # el parche va sobre el objeto, no sobre el módulo.
        monkeypatch.setattr(connekta, 'trigger_nota_factura_crear_cruzar',
                            _boom)
        return r, job

    def test_un_timeout_deja_el_pre_flag_arriba(self, db, monkeypatch):
        """Con la bandera abajo el DLQ reintenta, y si la NC ya existía eso es
        una segunda nota crédito: un documento fiscal que alguien reversa a
        mano."""
        from app.models.recaudo_entrega import RecaudoEntrega
        r, job = self._job_nc(db, monkeypatch,
                              ConnektaResultadoDesconocido('sin respuesta'))
        rid = r.id   # el pre-flag lo enciende el handler (Regla 6), no el test

        from app.services import siesa_job_service as sjs
        with pytest.raises(ConnektaResultadoDesconocido):
            sjs._ejecutar_job(job)

        assert RecaudoEntrega.query.get(rid).siesa_nc_triggered is True, (
            'el pre-flag se revirtió ante un resultado DESCONOCIDO — el '
            'siguiente ciclo del DLQ crea la segunda nota crédito')

    def test_un_rechazo_explicito_si_revierte(self, db, monkeypatch):
        """La otra mitad, que el arreglo no puede romper: si Siesa contestó
        que no, no se creó nada y el reintento es correcto. Congelar acá
        dejaría la nota crédito sin emitir para siempre."""
        from app.models.recaudo_entrega import RecaudoEntrega
        r, job = self._job_nc(db, monkeypatch,
                              ValueError('Siesa rechazó el documento'))
        rid = r.id   # el pre-flag lo enciende el handler (Regla 6), no el test

        from app.services import siesa_job_service as sjs
        with pytest.raises(ValueError):
            sjs._ejecutar_job(job)

        assert RecaudoEntrega.query.get(rid).siesa_nc_triggered is False, (
            'un rechazo explícito de Siesa dejó el pre-flag arriba — la NC '
            'no se va a reintentar nunca')


class TestElDlqNoReintentaLoDesconocido:
    def test_va_a_fallido_sin_reprogramarse(self, db, monkeypatch):
        """Lo que desbloquea esto no es esperar: es que alguien mire Siesa.
        Un job que se reprograma solo termina duplicando mientras nadie
        mira."""
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        from app.services import siesa_job_service as sjs

        job = SiesaJob(tipo='ENTRADA_OC', estado=EstadoSiesaJob.PENDIENTE,
                       referencia_tipo='Recepcion', referencia_id=1,
                       payload='{}')
        db.session.add(job)
        db.session.commit()
        jid = job.id

        monkeypatch.setattr(
            sjs, '_ejecutar_job',
            lambda j: (_ for _ in ()).throw(
                ConnektaResultadoDesconocido('sin respuesta de Siesa')))
        sjs._run_dlq_jobs()

        j = SiesaJob.query.get(jid)
        assert j.estado == EstadoSiesaJob.FALLIDO
        assert j.proximo_intento is None, (
            'quedó reprogramado: el DLQ lo va a reintentar y puede duplicar')
        assert 'desconocido' in (j.error_ultimo or '').lower() or \
               'sin respuesta' in (j.error_ultimo or '').lower()
