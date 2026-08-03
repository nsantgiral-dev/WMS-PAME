"""
El almacén de fotos: guardar y RECUPERAR.

La prueba que importa no es que el guardado no reviente — es que la foto se
pueda volver a leer. Un almacén de solo escritura no es un almacén: la fila dice
que hay evidencia y no la hay, que es exactamente lo que pasaba hasta el
2026-08-03 con `storage_ref: 'inline://pendiente-subida'` y un hash de ceros.
"""
import base64
import hashlib

import pytest

from flota.adaptadores.almacen_fotos import (
    AlmacenLocal,
    ErrorAlmacen,
    desde_data_url,
    guardar_foto,
)

# JPEG mínimo real — bytes de verdad, no un placeholder.
_JPEG_B64 = (
    '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof'
    'Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh'
    'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR'
    'CAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA'
    'AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK'
    'FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG'
    'h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl'
    '5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+iiigD//Z'
)
_DATA_URL = f'data:image/jpeg;base64,{_JPEG_B64}'
_BYTES = base64.b64decode(_JPEG_B64)


@pytest.fixture
def almacen(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    return AlmacenLocal()


class TestGuardarYRecuperar:

    def test_lo_que_se_guarda_es_lo_que_vuelve(self, almacen):
        """La prueba entera del almacén, en una línea.

        Si esto falla, todo lo demás —el hash, el estado, el contador— describe
        una evidencia que no se puede mirar.
        """
        ref = almacen.guardar(_BYTES, 'image/jpeg')
        assert almacen.leer(ref) == _BYTES

    def test_el_nombre_del_archivo_es_su_propio_hash(self, almacen):
        """Direccionado por contenido: el hash no se puede falsear.

        Si el archivo cambia, deja de coincidir con su nombre. Un
        `hash_sha256` calculado aparte del contenido dice la verdad hasta que
        alguien lo edita.
        """
        ref = almacen.guardar(_BYTES, 'image/jpeg')
        assert hashlib.sha256(_BYTES).hexdigest() in ref

    def test_la_misma_foto_dos_veces_ocupa_un_archivo(self, almacen, tmp_path):
        """Ocho ángulos por turno, cinco vehículos, todos los días."""
        a = almacen.guardar(_BYTES, 'image/jpeg')
        b = almacen.guardar(_BYTES, 'image/jpeg')
        assert a == b
        assert len(list(tmp_path.rglob('*.jpg'))) == 1

    def test_leer_lo_que_no_existe_levanta_en_vez_de_devolver_vacio(self, almacen):
        with pytest.raises(ErrorAlmacen, match='no está en el almacén'):
            almacen.leer('2026/08/inexistente.jpg')

    def test_sin_carpeta_configurada_levanta_en_vez_de_ir_a_tmp(self, monkeypatch):
        """Un default a /tmp guardaría las fotos y las perdería en el deploy.

        Eso es peor que no guardarlas: en vez de un hueco visible da una
        evidencia que se evapora.
        """
        monkeypatch.delenv('FLOTA_FOTOS_DIR', raising=False)
        with pytest.raises(ErrorAlmacen, match='FLOTA_FOTOS_DIR'):
            AlmacenLocal().guardar(_BYTES, 'image/jpeg')

    def test_un_mime_no_soportado_no_pasa(self, almacen):
        with pytest.raises(ErrorAlmacen, match='mime'):
            almacen.guardar(b'PK\\x03\\x04', 'application/zip')


class TestElCampoQueVaALaFila:

    def test_el_hash_sale_del_contenido_no_de_ceros(self, almacen):
        campos = guardar_foto({'clase': 'foto_dato', 'data_url': _DATA_URL,
                               'ancho': 1600, 'alto': 1200})
        assert campos['hash_sha256'] == hashlib.sha256(_BYTES).hexdigest()
        assert campos['hash_sha256'] != '0' * 64
        assert campos['estado'] == 'ok'
        assert campos['bytes'] == len(_BYTES)

    def test_la_referencia_apunta_a_algo_real(self, almacen):
        campos = guardar_foto({'clase': 'foto_dato', 'data_url': _DATA_URL,
                               'ancho': 1600, 'alto': 1200})
        assert almacen.leer(campos['storage_ref']) == _BYTES

    def test_sin_almacen_queda_declarada_y_dice_por_que(self, monkeypatch):
        """No inventa: la fila dice que la foto no se guardó, y el motivo."""
        monkeypatch.delenv('FLOTA_FOTOS_DIR', raising=False)
        campos = guardar_foto({'clase': 'foto_dato', 'data_url': _DATA_URL,
                               'ancho': 1600, 'alto': 1200})
        assert campos['estado'] == 'pendiente_evidencia'
        assert campos['hash_sha256'] == ''
        assert 'FLOTA_FOTOS_DIR' in campos['storage_ref']
        # El tamaño SÍ se conoce: la foto existió, lo que falta es dónde quedó.
        assert campos['bytes'] == len(_BYTES)

    def test_un_data_url_ilegible_es_error_del_cliente_y_levanta(self, almacen):
        """No se guarda media fila con datos inventados."""
        with pytest.raises(ErrorAlmacen):
            guardar_foto({'clase': 'foto_dato', 'data_url': 'esto-no-es-una-foto',
                          'ancho': 1, 'alto': 1})

    def test_el_cliente_no_decide_la_referencia_ni_el_hash(self, almacen):
        """Aunque los mande, se ignoran: los pone el servidor."""
        campos = guardar_foto({
            'clase': 'foto_dato', 'data_url': _DATA_URL, 'ancho': 1600, 'alto': 1200,
            'storage_ref': 's3://mentira.jpg', 'hash_sha256': 'f' * 64,
        })
        assert campos['storage_ref'] != 's3://mentira.jpg'
        assert campos['hash_sha256'] != 'f' * 64


class TestDataUrl:

    def test_extrae_bytes_y_mime(self):
        contenido, mime = desde_data_url(_DATA_URL)
        assert contenido == _BYTES
        assert mime == 'image/jpeg'

    def test_una_cadena_cualquiera_no_pasa(self):
        with pytest.raises(ErrorAlmacen):
            desde_data_url('https://ejemplo.com/foto.jpg')
