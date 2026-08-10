"""
Que la evidencia del arranque sobreviva al reinicio, y que diga la verdad.

Medido en producción el 2026-08-10: `/api/siesa/setup-inicial-estado` respondía
`resultado_catalogo: null`, `resultado_stock: null` — después de tres deploys el
mismo día. Los tres estados vivían en diccionarios de módulo, así que `null`
significaba a la vez **«nunca se corrió»** y **«se corrió antes del último
reinicio»**, y el endpoint respondía lo mismo para ambos.

No es una estadística perdida. La secuencia del corte es catálogo → códigos de
barras → **stock inicial una sola vez**, y cargar el stock dos veces duplica el
inventario de arranque. La única defensa era la memoria de quien lo ejecutó.

Lo segundo que se arregla acá: «códigos de barras cargados» no se podía marcar
con honestidad. El endpoint reportaba el resultado de la última corrida, no la
cobertura — un sync que actualizó 3 de 12.000 productos se veía igual que uno
que los cubrió todos.
"""
from datetime import datetime, timedelta

import pytest

from app.models.producto import Producto
from app.models.registro_sync import TIPOS, RegistroSync
from app.services import registro_sync_service as reg


class TestUnaCorridaSobreviveAlReinicio:

    def test_abrir_y_cerrar_ok_deja_fila(self, db):
        rid = reg.abrir('catalogo')
        assert rid is not None
        reg.cerrar_ok(rid, {'creados': 12})

        u = reg.ultimo('catalogo')
        assert u['ok'] is True
        assert u['resultado'] == {'creados': 12}
        assert u['fin'] is not None

    def test_cerrar_error_guarda_el_motivo(self, db):
        rid = reg.abrir('stock')
        reg.cerrar_error(rid, ValueError('Siesa no respondió'))
        u = reg.ultimo('stock')
        assert u['ok'] is False
        assert 'Siesa no respondió' in u['error']

    def test_un_tipo_desconocido_no_crea_serie_paralela(self, db):
        """Tipo libre = un typo crea una serie que nadie consulta."""
        assert reg.abrir('catalgo') is None          # sin la 'o'
        assert RegistroSync.query.filter_by(tipo='catalgo').count() == 0

    def test_cerrar_con_id_None_no_revienta(self, db):
        """`abrir` devuelve None si no pudo anotar. El sync sigue igual: anotar
        no puede romper lo anotado."""
        reg.cerrar_ok(None, {'x': 1})
        reg.cerrar_error(None, 'x')


class TestAbiertaNoEsLoMismoQueFallida:
    """Una corrida abierta es un proceso que murió a mitad —reinicio, OOM,
    deploy—. Marcarla como fallo diría que se sabe algo que no se sabe."""

    def test_una_corrida_abierta_se_declara_como_tal(self, db):
        reg.abrir('catalogo')
        u = reg.ultimo('catalogo')
        assert u['ok'] is None
        assert u['estado'] == 'en_curso_o_interrumpida'

    def test_no_cuenta_como_exitosa(self, db):
        reg.abrir('catalogo')
        assert reg.ultimo_ok('catalogo') is None

    def test_ultimo_y_ultimo_ok_responden_preguntas_distintas(self, db):
        """Sincronizó bien, después falló: `ultimo` dice fallo, `ultimo_ok` dice
        cuándo fue la última buena. Las dos son verdad."""
        r1 = reg.abrir('catalogo')
        reg.cerrar_ok(r1, {'creados': 5})
        RegistroSync.query.get(r1).inicio = datetime.utcnow() - timedelta(days=1)
        r2 = reg.abrir('catalogo')
        reg.cerrar_error(r2, 'timeout')

        assert reg.ultimo('catalogo')['ok'] is False
        assert reg.ultimo_ok('catalogo')['id'] == r1


class TestNuncaCorrioNoSeConfundeConReinicio:
    """El defecto original, en una línea."""

    def test_sin_corridas_lo_dice(self, db):
        e = reg.estado_persistido('catalogo')
        assert e['ultima_corrida'] is None
        assert e['alguna_vez_ok'] is False
        assert 'inconsistencia' not in e

    def test_memoria_dice_que_si_y_tabla_no_tiene_nada_se_declara(self, db):
        """No se elige una de las dos versiones — elegir sería inventar."""
        e = reg.estado_persistido('catalogo', en_memoria_corrio=True)
        assert 'inconsistencia' in e
        assert 'registros_sync' in e['inconsistencia']

    def test_con_corrida_ok_no_hay_inconsistencia(self, db):
        reg.cerrar_ok(reg.abrir('catalogo'), {'creados': 1})
        e = reg.estado_persistido('catalogo', en_memoria_corrio=True)
        assert e['alguna_vez_ok'] is True
        assert 'inconsistencia' not in e


class TestElInvarianteDeCierreSeEjerce:
    """Un cierre a medias —`ok` puesto y `fin` en NULL— se leería como corrida
    terminada. El CHECK está en el modelo Y en la migración: si viviera solo en
    la migración, `create_all()` no lo tendría y ningún test lo tocaría."""

    def test_la_tabla_declara_el_check(self):
        nombres = {c.name for c in RegistroSync.__table__.constraints}
        assert 'ck_registro_sync_cierre_completo' in nombres

    def test_rechaza_ok_sin_fin(self, db):
        from sqlalchemy.exc import IntegrityError
        db.session.add(RegistroSync(tipo='catalogo', inicio=datetime.utcnow(),
                                    ok=True, fin=None))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_rechaza_fin_sin_ok(self, db):
        from sqlalchemy.exc import IntegrityError
        db.session.add(RegistroSync(tipo='catalogo', inicio=datetime.utcnow(),
                                    ok=None, fin=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_acepta_los_dos_estados_legitimos(self, db):
        db.session.add(RegistroSync(tipo='catalogo', inicio=datetime.utcnow()))
        db.session.add(RegistroSync(tipo='stock', inicio=datetime.utcnow(),
                                    ok=True, fin=datetime.utcnow()))
        db.session.commit()      # no levanta


class TestCoberturaDeCodigosDeBarras:

    def _prod(self, db, codigo, barras=None, activo=True):
        p = Producto(codigo=codigo, nombre=f'P {codigo}', codigo_barras=barras,
                     activo=activo)
        db.session.add(p); db.session.commit()
        return p

    def test_cuenta_solo_activos_y_con_barras_no_vacio(self, db):
        from app.services.inventario_siesa_service import cobertura_catalogo
        self._prod(db, 'A1', '7701234567890')
        self._prod(db, 'A2', '')            # vacío NO cuenta como cubierto
        self._prod(db, 'A3', None)
        self._prod(db, 'A4', '7709999999999', activo=False)   # inactivo no entra

        c = cobertura_catalogo()
        assert c['productos_activos'] == 3
        assert c['con_codigo_barras'] == 1
        assert c['sin_codigo_barras'] == 2
        assert c['porcentaje'] == 33.3

    def test_sin_catalogo_el_porcentaje_es_None_no_cero(self, db):
        """`0%` diría «no hay cobertura». La verdad es «no hay catálogo», que es
        un problema distinto y anterior."""
        from app.services.inventario_siesa_service import cobertura_catalogo
        c = cobertura_catalogo()
        assert c['productos_activos'] == 0
        assert c['porcentaje'] is None
        assert c['hay_catalogo'] is False


class TestLaListaDeLosQueNoSeEscanean:
    """2.118 SKU que el operario tiene que teclear. La lista existe para que
    alguien de bodega la mire y diga si reconoce productos comunes — la pregunta
    «¿alguno es de alta rotación?» necesita demanda, que necesita el kardex, que
    necesita el corte."""

    _URL = '/api/productos/sin-codigo-barras'

    def _prod(self, db, codigo, barras=None):
        from app.models.producto import Producto
        p = Producto(codigo=codigo, nombre=f'Producto {codigo}',
                     codigo_barras=barras, activo=True)
        db.session.add(p); db.session.commit()
        return p

    @pytest.fixture
    def h(self, jwt_token_admin):
        return {'Authorization': f'Bearer {jwt_token_admin}'}

    def test_lista_solo_los_que_no_tienen(self, client, db, h):
        self._prod(db, 'CON', '7701234567890')
        self._prod(db, 'SIN1')
        self._prod(db, 'SIN2', '')

        d = client.get(self._URL, headers=h).get_json()
        assert d['total'] == 2
        assert {p['codigo'] for p in d['productos']} == {'SIN1', 'SIN2'}

    def test_viene_con_el_denominador(self, client, db, h):
        """Una lista de 2.118 sin el total se lee como catástrofe o como nada,
        según el ánimo del que la mire."""
        self._prod(db, 'CON', '770')
        self._prod(db, 'SIN1')
        d = client.get(self._URL, headers=h).get_json()
        assert d['cobertura']['productos_activos'] == 2
        assert d['cobertura']['porcentaje'] == 50.0

    def test_csv_se_puede_bajar(self, client, db, h):
        self._prod(db, 'SIN1')
        r = client.get(self._URL + '?formato=csv', headers=h)
        assert r.status_code == 200
        assert 'text/csv' in r.headers['Content-Type']
        assert 'attachment' in r.headers['Content-Disposition']
        assert 'SIN1' in r.get_data(as_text=True)

    def test_sin_catalogo_no_dice_0_por_ciento(self, client, db, h):
        d = client.get(self._URL, headers=h).get_json()
        assert d['total'] == 0
        assert d['cobertura']['hay_catalogo'] is False
        assert d['cobertura']['porcentaje'] is None

    def test_sin_token_no_pasa(self, client):
        assert client.get(self._URL).status_code == 401


class TestElEndpointExponeLoPersistido:

    def test_setup_inicial_trae_persistido_y_cobertura(self, db):
        from app.services.inventario_siesa_service import estado_setup_inicial
        reg.cerrar_ok(reg.abrir('stock'), {'cargados': 900})

        e = estado_setup_inicial()
        assert set(e['persistido']) == set(TIPOS)
        assert e['persistido']['stock']['alguna_vez_ok'] is True
        assert e['persistido']['catalogo']['alguna_vez_ok'] is False
        assert 'cobertura' in e

    def test_los_campos_en_memoria_siguen_existiendo(self, db):
        """No se rompe a quien ya los lee — solo dejan de ser la respuesta."""
        from app.services.inventario_siesa_service import estado_setup_inicial
        e = estado_setup_inicial()
        for campo in ('en_curso', 'fase', 'resultado_catalogo', 'resultado_stock'):
            assert campo in e


class TestLosSyncsRegistranDeVerdad:
    """Anti-función-sin-caller: el servicio puede estar perfecto y no llamarse
    desde ningún sync. Se verifica sobre el código, no sobre el diseño."""

    import pathlib
    _SERVICIOS = {
        'catalogo': 'app/services/siesa_sync_service.py',
        'barcodes': 'app/services/siesa_barcode_sync_service.py',
        'stock': 'app/services/inventario_siesa_service.py',
    }

    @pytest.mark.parametrize('tipo', ['catalogo', 'barcodes', 'stock'])
    def test_el_sync_abre_y_cierra_su_registro(self, tipo):
        from pathlib import Path
        raiz = Path(__file__).resolve().parents[1]
        fuente = (raiz / self._SERVICIOS[tipo]).read_text(encoding='utf-8')
        assert f"abrir('{tipo}')" in fuente, (
            f'{self._SERVICIOS[tipo]} no abre registro para {tipo} — el estado '
            f'seguiría viviendo solo en memoria')
        assert 'cerrar_ok(' in fuente, f'{tipo}: nunca cierra con éxito'
        assert 'cerrar_error(' in fuente, f'{tipo}: no registra el fallo'
