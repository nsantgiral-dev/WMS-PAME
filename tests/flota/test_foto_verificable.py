"""La metadata de una foto no dice «ok» sin un archivo que se pueda verificar.

**El caso** (QA real con flota@, 2026-09-25): toda foto de flota respondía 410
aunque su fila decía `estado='ok'`. `FLOTA_FOTOS_DIR=/data/flota-fotos` y el
volumen montado en `/data` del servicio web. Lo que el código permitía y ahora
no:

- `AlmacenLocal.guardar` marcaba `ok` apenas `write_bytes` volvía: nada
  releía el archivo. Ahora se hace `fsync`, se relee y se compara el hash; si
  no es la misma foto, la fila nace `pendiente_evidencia`.
- Una `FLOTA_FOTOS_DIR` relativa se resolvía contra el directorio de cada
  proceso: el que escribe y el que sirve podían mirar carpetas distintas.
  Ahora es un error de configuración declarado.
- `GET /flota/foto/<id>` contestaba 410 a tres cosas distintas. Ahora:
  `nunca_se_guardo` (410), `archivo_ausente` (410) y `almacen_sin_configurar`
  (503, no es la foto: es el servicio).
- Las listas (fotos del turno, adjuntos, kilometrajes en duda) publicaban el
  estado de la fila. Ahora `estado_verificable`: `ok` solo con el archivo.
- `almacen_fotos` en el health dice, desde el proceso que sirve, qué carpeta
  mira, si es un volumen o el disco del contenedor, y cuántas fotos `ok` no
  tienen archivo.

Y dos de la misma familia: una lectura cuyo tablero no se guardó no «tiene
foto» (nace en duda), y «ficha completa» tiene una sola definición.
"""
import ast
from pathlib import Path

import pytest

from tests.flota.test_foto_atada_a_la_lectura import _AUTH, _jpeg_url, _traspaso, mundo  # noqa: F401

RAIZ = Path(__file__).resolve().parents[2]


@pytest.fixture
def carpeta(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    return tmp_path


def _foto_ok(db, carpeta, autor):
    from flota.adaptadores.almacen_fotos import guardar_foto
    from flota.adaptadores.modelos import Foto
    from datetime import datetime
    campos = guardar_foto({'clase': 'evidencia_estado', 'data_url': _jpeg_url(800, 600),
                           'ancho': 800, 'alto': 600, 'angulo': 'frontal'})
    f = Foto(entidad_tipo='custodia_inicio', entidad_id=1, ts_captura=datetime.utcnow(),
             autor_usuario_id=autor.id, **campos)
    db.session.add(f)
    db.session.commit()
    return f


class TestOkSoloConElArchivoReleido:

    def test_si_al_releer_no_es_la_misma_foto_nace_pendiente(self, carpeta, monkeypatch):
        from flota.adaptadores import almacen_fotos as af
        monkeypatch.setattr(af, '_sha_de_archivo', lambda ruta: None)
        campos = af.guardar_foto({'clase': 'evidencia_estado', 'data_url': _jpeg_url(800, 600),
                                  'ancho': 800, 'alto': 600})
        assert campos['estado'] == 'pendiente_evidencia'
        assert campos['hash_sha256'] == ''

    def test_una_carpeta_relativa_no_es_un_almacen(self, monkeypatch):
        from flota.adaptadores import almacen_fotos as af
        monkeypatch.setenv('FLOTA_FOTOS_DIR', 'flota-fotos')
        with pytest.raises(af.AlmacenNoConfigurado, match='absoluta'):
            af.AlmacenLocal().guardar(b'x' * 10, 'image/jpeg')

    def test_un_archivo_corrupto_con_el_mismo_nombre_se_reemplaza(self, carpeta):
        import base64
        import hashlib
        from flota.adaptadores.almacen_fotos import AlmacenLocal
        contenido = base64.b64decode(_jpeg_url(10, 10).split(',', 1)[1])
        ref = AlmacenLocal().guardar(contenido, 'image/jpeg')
        (carpeta / ref).write_bytes(b'truncado')
        assert AlmacenLocal().guardar(contenido, 'image/jpeg') == ref
        assert hashlib.sha256((carpeta / ref).read_bytes()).hexdigest() in ref


class TestTresRespuestasDistintas:

    def test_archivo_ausente_es_410_con_su_motivo(self, client, jwt_token_admin, usuario_admin, db, carpeta):
        f = _foto_ok(db, carpeta, usuario_admin)
        (carpeta / f.storage_ref).unlink()
        r = client.get(f'/flota/foto/{f.id}', headers=_AUTH(jwt_token_admin))
        assert r.status_code == 410 and r.get_json()['motivo'] == 'archivo_ausente'

    def test_sin_almacen_configurado_es_503_no_410(self, client, jwt_token_admin, usuario_admin, db,
                                                    carpeta, monkeypatch):
        f = _foto_ok(db, carpeta, usuario_admin)
        monkeypatch.delenv('FLOTA_FOTOS_DIR')
        r = client.get(f'/flota/foto/{f.id}', headers=_AUTH(jwt_token_admin))
        assert r.status_code == 503 and r.get_json()['motivo'] == 'almacen_sin_configurar'

    def test_la_foto_que_esta_se_sirve(self, client, jwt_token_admin, usuario_admin, db, carpeta):
        f = _foto_ok(db, carpeta, usuario_admin)
        r = client.get(f'/flota/foto/{f.id}', headers=_AUTH(jwt_token_admin))
        assert r.status_code == 200 and r.data[:2] == b'\xff\xd8'


class TestLaMetadataNoDiceOkSinArchivo:

    def test_estado_verificable(self, db, usuario_admin, carpeta, monkeypatch):
        from flota.adaptadores.almacen_fotos import estado_verificable
        f = _foto_ok(db, carpeta, usuario_admin)
        assert estado_verificable(f) == 'ok'
        (carpeta / f.storage_ref).unlink()
        assert estado_verificable(f) == 'sin_archivo'
        monkeypatch.delenv('FLOTA_FOTOS_DIR')
        assert estado_verificable(f) == 'almacen_sin_configurar'

    def test_las_fotos_del_turno_dicen_sin_archivo(self, client, jwt_token_admin, db, mundo, carpeta):
        from flota.adaptadores.modelos import Custodia, Foto
        r = _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'evidencia_estado', 'angulo': 'frontal', 'data_url': _jpeg_url(800, 600),
             'ancho': 800, 'alto': 600}])
        assert r.status_code in (200, 201), r.get_json()
        c = Custodia.query.filter_by(vehiculo_id=mundo['veh']).order_by(Custodia.id.desc()).first()
        f = Foto.query.filter_by(entidad_id=c.id).first()
        (carpeta / f.storage_ref).unlink()
        d = client.get(f'/flota/custodia/{c.id}/fotos', headers=_AUTH(jwt_token_admin)).get_json()
        assert [x['estado'] for x in d['fotos']] == ['sin_archivo']

    def test_el_health_cuenta_las_fotos_ok_sin_archivo(self, db, usuario_admin, carpeta):
        from flota.adaptadores.almacen_fotos import diagnostico_almacen
        a, b = _foto_ok(db, carpeta, usuario_admin), None
        d = diagnostico_almacen()
        assert (d['configurada'], d['existe'], d['escribible']) == (True, True, True)
        assert d['mismo_disco_que_el_contenedor'] in (True, False)
        assert (d['fotos_ok_revisadas'], d['fotos_ok_sin_archivo']) == (1, 0)
        (carpeta / a.storage_ref).unlink()
        d = diagnostico_almacen()
        assert d['fotos_ok_sin_archivo'] == 1 and d['ejemplos_sin_archivo'] == [a.id]

    def test_el_health_lo_publica(self, client, jwt_token_admin, db, carpeta):
        d = client.get('/flota/health', headers=_AUTH(jwt_token_admin)).get_json()
        assert d['almacen_fotos']['raiz'] == str(carpeta)


class TestElTableroQueNoSeGuardoNoRespaldaLaLectura:

    def test_la_lectura_nace_en_duda_si_el_tablero_no_se_guardo(
            self, client, jwt_token_admin, db, mundo, carpeta, monkeypatch):
        from flota.adaptadores import almacen_fotos as af
        from flota.adaptadores.modelos import Foto, LecturaOdometro
        monkeypatch.setattr(af, '_sha_de_archivo', lambda ruta: None)
        r = _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'foto_dato', 'angulo': 'tablero', 'data_url': _jpeg_url(1600, 1200),
             'ancho': 1600, 'alto': 1200}])
        assert r.status_code in (200, 201), r.get_json()
        lec = LecturaOdometro.query.filter_by(vehiculo_id=mundo['veh']).one()
        assert Foto.query.get(lec.foto_id).estado == 'pendiente_evidencia'
        assert lec.confianza == 'dudosa', 'una foto que no se guardó no respalda el kilometraje'
        d = client.get('/flota/odometro/dudosas', headers=_AUTH(jwt_token_admin)).get_json()
        fila = next(x for x in d['pendientes'] if x['lectura_id'] == lec.id)
        assert (fila['tiene_foto'], fila['estado_foto']) == (False, 'pendiente_evidencia')

    def test_con_el_tablero_guardado_no_se_marca_por_falta_de_foto(
            self, client, jwt_token_admin, db, mundo, carpeta):
        from flota.adaptadores.modelos import LecturaOdometro
        r = _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'foto_dato', 'angulo': 'tablero', 'data_url': _jpeg_url(1600, 1200),
             'ancho': 1600, 'alto': 1200}])
        assert r.status_code in (200, 201), r.get_json()
        lec = LecturaOdometro.query.filter_by(vehiculo_id=mundo['veh']).one()
        assert 'foto' not in (lec.motivo_dudosa or '').lower()


# ═════════════════════════════════════════════════════════════════════════════
# «Ficha completa»: una definición
# ═════════════════════════════════════════════════════════════════════════════

class TestFichaCompletaUnaDefinicion:

    def test_sin_capacidad_del_tanque_no_esta_completa_en_ningun_lado(self, db, client,
                                                                       jwt_token_admin):
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import FichaTecnica
        from flota.adaptadores.salida import ficha_de
        v = Vehiculo(placa='FICHA01', tipo='NHR', activo=True)
        db.session.add(v)
        db.session.flush()
        from datetime import datetime
        f = FichaTecnica(vehiculo_id=v.id, combustible='diesel',
                         sistema_frenos='hidraulico', frenos_fuente='manual_fabricante',
                         tiene_freno_escape='no', distribucion='correa',
                         distribucion_fuente='manual_fabricante', transmision_final='cardan',
                         posiciones_llanta=4, km_inicial=1000, km_inicial_ts=datetime.utcnow(),
                         capacidad_tanque_fuente='sin_dato')
        db.session.add(f)
        db.session.commit()
        assert f.completa() is False and 'capacidad_tanque' in f.faltantes()
        assert ficha_de(f)[0] == 'incompleta'
        d = client.get('/flota/vehiculo/FICHA01/ficha', headers=_AUTH(jwt_token_admin)).get_json()
        assert d['completa'] is False and 'capacidad_tanque' in d['falta']


#: Llamadas a `.atributos_sin_dato()` fuera del modelo, con su porqué. **Solo
#: encoge.** Decidir completitud con ella es la definición vieja.
LISTAS_SIN_DATO_DECLARADAS = {
    ('flota/api/ficha.py', '_serializar'): 'publica la lista para pintar qué atributos no se saben',
    ('flota/adaptadores/medicion.py', 'atributos_sin_dato'):
        'el health cuenta atributos sin dato: otra pregunta que «¿está completa?»',
}


def _llamadas():
    out = set()
    for base in ('flota', 'app'):
        for f in sorted((RAIZ / base).rglob('*.py')):
            if f.name == 'modelos.py' or '/tests/' in str(f):
                continue
            for fn in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'atributos_sin_dato' for n in ast.walk(fn)):
                    out.add((str(f.relative_to(RAIZ)), fn.name))
    return out


class TestTrinqueteFichaCompleta:

    def test_nadie_decide_completitud_con_la_lista_vieja(self):
        nuevas = _llamadas() - set(LISTAS_SIN_DATO_DECLARADAS)
        assert not nuevas, (f'completitud de ficha fuera de FichaTecnica.faltantes: {sorted(nuevas)}')

    def test_solo_encoge(self):
        assert not set(LISTAS_SIN_DATO_DECLARADAS) - _llamadas()
