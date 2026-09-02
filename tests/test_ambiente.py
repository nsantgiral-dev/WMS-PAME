"""
El ambiente se declara y se contrasta. No se detecta.

El 19 de agosto de 2026 el Gestor de Cartera pasó ocho horas mostrando cartera
de QA creyendo que era producción. **Las cuatro comprobaciones técnicas dieron
en verde**, porque una copia hereda las cuatro: mismo host de producción, misma
compañía, documentos con fecha de hoy, y montos coherentes entre sí.

El WMS comparte esa plataforma y **escribe**. Lo que allá fue una pantalla
equivocada, acá serían remisiones descargando inventario y facturas
electrónicas en una base que no opera.

Este archivo protege tres propiedades, y las tres son sobre **no engañarse**:

1. el default es ALARMA, no neutro;
2. una declaración caduca sola cuando cambia la configuración;
3. el tamiz de credenciales no degrada a verde cuando no pudo correr.

## La tercera es la más fácil de romper sin notarlo

Un `except: return {'veredicto': 'BASES_DISTINTAS'}` haría pasar todos los
tests del caso feliz y convertiría el tamiz en un adorno que siempre dice que
sí. Es exactamente el adaptador que degrada hacia la respuesta buena.
"""
import pytest


@pytest.fixture
def usuario(db):
    from app.models.usuario import Usuario
    u = Usuario.query.filter_by(email='amb@test.com').first()
    if not u:
        u = Usuario(email='amb@test.com', nombre='Santiago', rol='admin',
                    activo=True)
        u.set_password('test123')
        db.session.add(u)
        db.session.commit()
    return u


def _declarar(usuario_id, **kw):
    from app.services import ambiente
    datos = {
        'concepto': 'existencia de PAPELSP9218 en NB1',
        'cifra_wms': '412',
        'cifra_externa': '412',
        'fuente_externa': 'conteo físico del 2026-08-19, jefe de bodega',
    }
    datos.update(kw)
    return ambiente.declarar_contraste(usuario_id=usuario_id, **datos)


class TestElDefaultEsAlarma:
    def test_sin_declaraciones_esta_en_alarma(self, db):
        """**El corazón de todo.** Allá no sonó nada en ocho horas justamente
        porque nadie había declarado nada, y el silencio se leyó como
        conformidad."""
        from app.services import ambiente
        est = ambiente.estado()
        assert est['estado'] == 'ALARMA'
        assert est['motivos'], 'está en alarma y no dice por qué'
        assert 'pregunta sin hacer' in ' '.join(est['motivos'])

    def test_el_estado_no_se_apoya_en_el_host(self, db):
        """El host y la compañía se publican como configuración, **no como
        evidencia**: son la primera y la segunda de las cuatro que no
        distinguen nada."""
        from app.services import ambiente
        est = ambiente.estado()
        assert 'NO distinguen' in est['advertencia']
        assert est['config']['host']
        assert est['estado'] == 'ALARMA'


class TestSiNoSePuedeLeerTampocoEsVerde:
    def test_la_tabla_ausente_da_alarma_y_no_un_500(self, db, monkeypatch):
        """Encontrado en vivo: entre que el deploy arranca y `flask db
        upgrade` corre —o si la migración falla— la tabla no existe y la
        consulta revienta.

        Un 500 con traza es lo que un operador aprende a ignorar, y además
        deja la franja del dashboard sin motivo que mostrar. **No poder leer
        el registro de declaraciones no es un problema técnico menor: es que
        no hay contraste declarado.**
        """
        from app.models.declaracion_ambiente import DeclaracionAmbiente
        from app.services import ambiente

        class _QueRevienta:
            def order_by(self, *a):
                raise Exception('relation "declaraciones_ambiente" does not exist')
        monkeypatch.setattr(DeclaracionAmbiente, 'query', _QueRevienta())

        est = ambiente.estado()
        assert est['estado'] == 'ALARMA'
        assert 'm010declaracionambiente' in ' '.join(est['motivos'])
        assert est['advertencia'], 'la salida degradada perdió la advertencia'


class TestUnaDeclaracionValeSoloParaSuAmbiente:
    def test_declarar_saca_de_alarma(self, db, usuario):
        from app.services import ambiente
        _declarar(usuario.id)
        est = ambiente.estado()
        assert est['estado'] == 'DECLARADO'
        assert est['ultima_declaracion']['declarado_por_nombre'] == 'Santiago'
        assert est['ultima_declaracion']['fuente_externa'].startswith('conteo')

    def test_cambiar_el_host_la_invalida(self, db, usuario, monkeypatch):
        """**El detector ciego del mecanismo.** Es el escenario del 19 de
        agosto al revés: alguien declara estando en QA, y el corte a producción
        heredaría ese verde si la declaración no caducara con la config."""
        from app.services import ambiente
        from app.services.connekta_gateway import connekta
        _declarar(usuario.id)
        assert ambiente.estado()['estado'] == 'DECLARADO'

        monkeypatch.setattr(connekta, 'base_url',
                            'https://servicios.siesacloud.com')
        est = ambiente.estado()
        assert est['estado'] == 'ALARMA', (
            'la declaración sobrevivió a un cambio de host — es exactamente '
            'cómo un verde de QA se hereda al corte de producción')
        assert 'configuración cambió' in ' '.join(est['motivos'])

    def test_cambiar_la_compania_tambien(self, db, usuario, monkeypatch):
        from app.services import ambiente
        from app.services.connekta_gateway import connekta
        _declarar(usuario.id)
        monkeypatch.setattr(connekta, 'id_compania', '9999')
        assert ambiente.estado()['estado'] == 'ALARMA'

    def test_volver_a_declarar_en_el_ambiente_nuevo_lo_reactiva(
            self, db, usuario, monkeypatch):
        """No es una trampa sin salida: se sale declarando de nuevo, que es
        justo el trabajo que se quiere forzar."""
        from app.services import ambiente
        from app.services.connekta_gateway import connekta
        _declarar(usuario.id)
        monkeypatch.setattr(connekta, 'base_url', 'https://servicios.siesacloud.com')
        assert ambiente.estado()['estado'] == 'ALARMA'
        _declarar(usuario.id, concepto='existencia de PAPELSP9218 en NB1 (prod)')
        assert ambiente.estado()['estado'] == 'DECLARADO'


class TestLaDeclaracionExigeContenido:
    @pytest.mark.parametrize('vacio', [
        'concepto', 'cifra_wms', 'cifra_externa', 'fuente_externa'])
    def test_ningun_campo_puede_ir_en_blanco(self, db, usuario, vacio):
        """Una declaración sin qué se cuadró, contra qué y con qué números no
        se puede auditar después: sería una firma sobre nada."""
        with pytest.raises(ValueError, match='obligatorio'):
            _declarar(usuario.id, **{vacio: '   '})

    def test_guarda_la_config_del_momento(self, db, usuario):
        from app.services import ambiente
        d = _declarar(usuario.id)
        assert d.huella_config == ambiente.huella_config()
        assert d.host and d.id_compania


class TestElTamizDeCredenciales:
    """Lo que probó el punto allá: dos hosts, dos ConniKey, dos tokens, y
    4.601 filas idénticas."""

    def _con_alternas(self, monkeypatch):
        monkeypatch.setenv('CONNEKTA_URL_ALTERNA', 'https://integradorqa.x.com')
        monkeypatch.setenv('CONNEKTA_IKEY_ALTERNA', 'k2')
        monkeypatch.setenv('CONNEKTA_ITOKEN_ALTERNA', 't2')
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'modo_simulacion', False)

    def test_filas_identicas_es_una_sola_base(self, db, monkeypatch):
        from app.services import ambiente
        self._con_alternas(monkeypatch)
        iguales = [{'f430_consec_docto': 1352}, {'f430_consec_docto': 1353}]
        monkeypatch.setattr(ambiente, '_consultar_para_test', None,
                            raising=False)

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {'detalle': {'Table': iguales}}

        import requests
        monkeypatch.setattr(requests, 'get', lambda *a, **k: _Resp())

        r = ambiente.comparar_credenciales()
        assert r['veredicto'] == 'MISMA_BASE'
        assert 'una sola base' in r['motivo']

    def test_contenido_distinto_descarta_esa_falla_y_solo_esa(self, db,
                                                              monkeypatch):
        from app.services import ambiente
        self._con_alternas(monkeypatch)
        respuestas = iter([
            {'detalle': {'Table': [{'f430_consec_docto': 1352}]}},
            {'detalle': {'Table': [{'f430_consec_docto': 999}]}},
        ])

        class _Resp:
            def __init__(self, d): self._d = d
            def raise_for_status(self): pass
            def json(self): return self._d

        import requests
        monkeypatch.setattr(requests, 'get',
                            lambda *a, **k: _Resp(next(respuestas)))

        r = ambiente.comparar_credenciales()
        assert r['veredicto'] == 'BASES_DISTINTAS'
        assert 'ninguna otra' in r['motivo'], (
            'el mensaje deja creer que «bases distintas» significa «estamos '
            'en producción», que es la conclusión que no se puede sacar')

    def test_sin_credenciales_alternas_no_dice_que_todo_bien(self, db,
                                                             monkeypatch):
        """**El detector ciego.** «No se pudo comparar» no descarta nada."""
        from app.services import ambiente
        for v in ('CONNEKTA_URL_ALTERNA', 'CONNEKTA_IKEY_ALTERNA',
                  'CONNEKTA_ITOKEN_ALTERNA'):
            monkeypatch.delenv(v, raising=False)
        r = ambiente.comparar_credenciales()
        assert r['veredicto'] == 'NO_SE_PUDO'
        assert r['veredicto'] != 'BASES_DISTINTAS'

    def test_si_la_consulta_falla_tampoco(self, db, monkeypatch):
        """Un `except` que devolviera «bases distintas» haría pasar el caso
        feliz y convertiría el tamiz en un adorno que siempre dice que sí."""
        from app.services import ambiente
        self._con_alternas(monkeypatch)
        import requests

        def _boom(*a, **k):
            raise requests.exceptions.ConnectionError('sin red')
        monkeypatch.setattr(requests, 'get', _boom)

        r = ambiente.comparar_credenciales()
        assert r['veredicto'] == 'NO_SE_PUDO'

    def test_dos_respuestas_vacias_no_son_la_misma_base(self, db, monkeypatch):
        """Dos consultas que no devuelven nada tienen la misma huella y **no
        prueban nada**: es el cero que se lee como coincidencia."""
        from app.services import ambiente
        self._con_alternas(monkeypatch)

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {'detalle': {'Table': []}}

        import requests
        monkeypatch.setattr(requests, 'get', lambda *a, **k: _Resp())

        r = ambiente.comparar_credenciales()
        assert r['veredicto'] != 'MISMA_BASE', (
            'dos tablas vacías se declararon «misma base» — la huella '
            'coincide porque no hay nada que comparar')


class TestLosEndpoints:
    def test_ambiente_en_alarma_responde_409(self, client, db, jwt_token):
        """Un monitor externo que solo mira el código tiene que verlo. El
        silencio es lo que costó ocho horas allá."""
        r = client.get('/api/health/ambiente',
                       headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code in (409, 403)
        if r.status_code == 409:
            assert r.get_json()['estado'] == 'ALARMA'

    def test_declarar_exige_sesion(self, client, db):
        r = client.post('/api/health/ambiente/declarar', json={})
        assert r.status_code == 401

    def test_el_tamiz_exige_sesion(self, client, db):
        r = client.get('/api/health/ambiente/tamiz')
        assert r.status_code == 401
