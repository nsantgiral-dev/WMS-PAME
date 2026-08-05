"""
El SOAT llega por correo en PDF, y la pantalla solo aceptaba foto con cámara.

Quien cargaba el documento tenía que abrir el PDF y fotografiar la pantalla —
un rodeo que además degrada el original justo en lo que importa: el número y la
fecha de vencimiento.

**Y no era solo incómodo: no funcionaba.** Los adjuntos se guardaban con clase
`foto_dato`, cuyo CHECK exige 1600 px de lado largo salvo que la fila esté en
`pendiente_evidencia`. Como el guardado exitoso escribe `estado='ok'`, una foto
de SOAT de 1200 px violaba el CHECK y el endpoint respondía 409 «viola una regla
de la base» — mientras la pantalla prometía, en letra amarilla, que quedaría
como `pendiente_evidencia`. La promesa era falsa y el guardado imposible.

La clase `documento_adjunto` existe para eso, y **no** para aflojar el umbral de
`foto_dato`: ese mínimo protege al odómetro, seis dígitos fotografiados a las
5 a.m. en patio. El vencimiento del SOAT, en cambio, se digita en su propio
campo y se puede contrastar con el archivo.
"""
import base64

import pytest

from flota.dominio.errores import FotoInvalida
from flota.dominio.fotos import exige_dimensiones, mimes_permitidos, validar_formato
from flota.dominio.valores import ClaseFoto


@pytest.fixture
def flota_mundo(db):
    from app.models.almacen import Almacen
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='ADJ100', tipo='NHR', activo=True)
    alm = Almacen(codigo='ADJ-SEDE', nombre='Sede adjuntos')
    db.session.add_all([veh, alm])
    db.session.commit()
    return {'placa': veh.placa, 'veh': veh.id, 'alm': alm.id}

# PDF mínimo real. Bytes de verdad: un placeholder probaría el placeholder.
_PDF = (b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
        b'2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n'
        b'trailer<</Root 1 0 R>>\n%%EOF\n')
_PDF_URL = 'data:application/pdf;base64,' + base64.b64encode(_PDF).decode()


class TestLaPoliticaDeFormatos:
    """El dominio decide qué se acepta; el almacén solo sabe poner extensión."""

    def test_un_pdf_solo_vale_como_adjunto_de_documento(self):
        assert 'application/pdf' in mimes_permitidos(ClaseFoto.DOCUMENTO_ADJUNTO)
        assert 'application/pdf' not in mimes_permitidos(ClaseFoto.FOTO_DATO)
        assert 'application/pdf' not in mimes_permitidos(ClaseFoto.EVIDENCIA_ESTADO)

    def test_un_pdf_como_foto_de_odometro_se_rechaza(self):
        """El tablero se fotografía. Un PDF ahí es un bug del cliente, no una
        preferencia del usuario."""
        with pytest.raises(FotoInvalida, match='foto_dato'):
            validar_formato(ClaseFoto.FOTO_DATO, 'application/pdf', 1600, 1200)

    def test_un_formato_desconocido_no_se_guarda_por_si_acaso(self):
        with pytest.raises(FotoInvalida):
            validar_formato(ClaseFoto.DOCUMENTO_ADJUNTO, 'application/zip', None, None)

    def test_solo_el_adjunto_puede_no_traer_dimensiones(self):
        assert exige_dimensiones(ClaseFoto.FOTO_DATO) is True
        assert exige_dimensiones(ClaseFoto.EVIDENCIA_ESTADO) is True
        assert exige_dimensiones(ClaseFoto.DOCUMENTO_ADJUNTO) is False

    def test_una_foto_sin_dimensiones_se_rechaza(self):
        """Aceptar 0×0 aquí devolvería el bug original por la puerta de atrás:
        una imagen que nadie sabe si sirve, declarada como si sirviera."""
        with pytest.raises(FotoInvalida, match='ancho y alto'):
            validar_formato(ClaseFoto.EVIDENCIA_ESTADO, 'image/jpeg', None, None)

    def test_dimensiones_a_medias_se_rechazan_hasta_en_el_adjunto(self):
        with pytest.raises(FotoInvalida, match='a medias'):
            validar_formato(ClaseFoto.DOCUMENTO_ADJUNTO, 'image/jpeg', 800, None)

    def test_un_adjunto_que_SI_es_imagen_conserva_sus_dimensiones(self):
        validar_formato(ClaseFoto.DOCUMENTO_ADJUNTO, 'image/jpeg', 1200, 900)


class TestElAlmacenGuardaYDevuelveElPDF:

    @pytest.fixture(autouse=True)
    def _dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

    def test_lo_que_se_guarda_es_lo_que_vuelve(self):
        from flota.adaptadores.almacen_fotos import AlmacenLocal

        a = AlmacenLocal()
        ref = a.guardar(_PDF, 'application/pdf')
        assert ref.endswith('.pdf')
        assert a.leer(ref) == _PDF

    def test_guardar_foto_deja_la_fila_sin_dimensiones(self):
        from flota.adaptadores.almacen_fotos import guardar_foto

        campos = guardar_foto({'clase': 'documento_adjunto', 'data_url': _PDF_URL})
        assert campos['estado'] == 'ok'
        assert campos['mime'] == 'application/pdf'
        assert campos['ancho'] is None and campos['alto'] is None
        assert campos['bytes'] == len(_PDF)

    def test_el_mime_sale_del_CONTENIDO_no_de_lo_que_diga_el_cliente(self):
        """Si el JSON declara 'image/jpeg' y sube un PDF, la fila no puede
        afirmar un formato que el archivo no tiene."""
        from flota.adaptadores.almacen_fotos import guardar_foto

        campos = guardar_foto({'clase': 'documento_adjunto', 'mime': 'image/jpeg',
                               'data_url': _PDF_URL})
        assert campos['mime'] == 'application/pdf'

    def test_un_pdf_declarado_como_foto_de_custodia_no_pasa(self):
        from flota.adaptadores.almacen_fotos import guardar_foto

        with pytest.raises(FotoInvalida):
            guardar_foto({'clase': 'evidencia_estado', 'data_url': _PDF_URL,
                          'ancho': 800, 'alto': 600})


class TestLaTablaImponeLoMismoQueElDominio:
    """Una regla que solo vive en el código se salta con un INSERT.

    Y una que solo vive en la base no se puede explicar. Van las dos, y este
    test es el que impide que se separen.
    """

    def _foto(self, **kw):
        from datetime import datetime

        from flota.adaptadores.modelos import Foto

        base = dict(
            clase='documento_adjunto', entidad_tipo='documento', entidad_id=1,
            storage_ref='2026/08/abc.pdf', hash_sha256='a' * 64, bytes=100,
            ancho=None, alto=None, mime='application/pdf',
            ts_captura=datetime(2026, 8, 5), autor_usuario_id=1,
        )
        base.update(kw)
        return Foto(**base)

    def test_un_adjunto_sin_dimensiones_entra(self, app, db):
        db.session.add(self._foto())
        db.session.commit()

    def test_una_foto_de_custodia_sin_dimensiones_NO_entra(self, app, db):
        from sqlalchemy.exc import IntegrityError

        db.session.add(self._foto(clase='evidencia_estado', mime='image/jpeg'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_una_dimension_a_medias_NO_entra(self, app, db):
        from sqlalchemy.exc import IntegrityError

        db.session.add(self._foto(ancho=800))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_cero_bytes_sigue_sin_entrar(self, app, db):
        from sqlalchemy.exc import IntegrityError

        db.session.add(self._foto(bytes=0))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_el_vocabulario_de_la_tabla_sale_del_dominio(self):
        """Escrito así para que agregar una clase no exija acordarse de una
        tupla suelta en el adaptador — el desfase entre las dos es invisible
        hasta que un INSERT legítimo falla."""
        from flota.adaptadores.modelos import CLASE_FOTO

        assert CLASE_FOTO == tuple(c.value for c in ClaseFoto)
        assert 'documento_adjunto' in CLASE_FOTO


class TestElEndpointAceptaElArchivo:

    def _url(self, m):
        return f"/flota/vehiculo/{m['placa']}/documentos"

    def _auth(self, t):
        return {'Authorization': f'Bearer {t}'}

    def _guardar(self, client, token, mundo, archivo=None, clave='archivo'):
        from datetime import timedelta

        from app.utils.fecha import dia_operativo

        cuerpo = {
            'tipo': 'soat', 'numero': 'S-9', 'entidad': 'Aseguradora',
            'fecha_expedicion': (dia_operativo() - timedelta(days=300)).isoformat(),
            'fecha_vencimiento': (dia_operativo() + timedelta(days=30)).isoformat(),
        }
        if archivo is not None:
            cuerpo[clave] = archivo
        return client.post(self._url(mundo), json=cuerpo, headers=self._auth(token))

    @pytest.fixture(autouse=True)
    def _dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

    def test_un_pdf_se_acepta_y_se_declara_como_pdf(
            self, client, jwt_token_admin, flota_mundo):
        r = self._guardar(client, jwt_token_admin, flota_mundo, {
            'clase': 'documento_adjunto', 'data_url': _PDF_URL,
            'ancho': None, 'alto': None, 'mime': 'application/pdf',
        })
        assert r.status_code == 201, r.get_json()
        adj = r.get_json()['adjunto']
        assert adj['es_pdf'] is True
        assert adj['estado'] == 'ok'
        assert adj['ancho'] is None

    def test_el_archivo_se_puede_VOLVER_A_LEER(
            self, client, jwt_token_admin, flota_mundo):
        """Un almacén de solo escritura no es un almacén: la fila diría que hay
        un SOAT y el día que alguien lo pida no estaría."""
        r = self._guardar(client, jwt_token_admin, flota_mundo, {
            'clase': 'documento_adjunto', 'data_url': _PDF_URL,
        })
        foto_id = r.get_json()['adjunto']['id']

        v = client.get(f'/flota/foto/{foto_id}', headers=self._auth(jwt_token_admin))
        assert v.status_code == 200
        assert v.data == _PDF
        assert v.mimetype == 'application/pdf'

    def test_el_nombre_viejo_foto_sigue_funcionando(
            self, client, jwt_token_admin, flota_mundo):
        """Hay clientes desplegados que mandan `foto`. Romperlos para renombrar
        un campo sería cambiar un problema por otro peor."""
        r = self._guardar(client, jwt_token_admin, flota_mundo, {
            'clase': 'documento_adjunto', 'data_url': _PDF_URL,
        }, clave='foto')
        assert r.status_code == 201
        assert r.get_json()['adjunto'] is not None

    def test_un_archivo_invalido_da_400_y_no_500(
            self, client, jwt_token_admin, flota_mundo):
        """Es información accionable para quien está subiendo, no un fallo del
        servidor."""
        r = self._guardar(client, jwt_token_admin, flota_mundo, {
            'clase': 'documento_adjunto',
            'data_url': 'data:application/zip;base64,UEsDBA==',
        })
        assert r.status_code == 400
        assert 'zip' in r.get_json()['detalle']

    def test_sin_archivo_el_documento_igual_se_guarda(
            self, client, jwt_token_admin, flota_mundo):
        """El archivo es respaldo del dato, no requisito para registrarlo: sin
        esto, "no lo pude escanear" se convierte en "no lo registré"."""
        r = self._guardar(client, jwt_token_admin, flota_mundo)
        assert r.status_code == 201
        assert r.get_json()['adjunto'] is None


class TestLaPantallaNoPideCamaraObligatoria:
    """TRINQUETE — el `capture="environment"` era el problema reportado.

    Con ese atributo el teléfono abre la cámara directo y no ofrece el
    explorador de archivos: el PDF que mandó la aseguradora no tenía por dónde
    entrar.
    """

    def _js(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / 'app' / 'static' / 'pwa'
                / 'flota.js').read_text(encoding='utf-8')

    def test_hay_una_entrada_que_acepta_pdf(self):
        js = self._js()
        assert 'accept="image/*,application/pdf"' in js

    def test_esa_entrada_no_fuerza_la_camara(self):
        """La entrada de archivo no puede llevar `capture` — el bug entero."""
        js = self._js()
        i = js.index('id="doc-archivo"')
        assert 'capture' not in js[i:i + 200]

    def test_el_adjunto_se_manda_como_documento_adjunto(self):
        js = self._js()
        assert "flotaFotoPayload(FLOTA_FOTO_DOC, 'documento_adjunto')" in js
        assert "flotaFotoPayload(FLOTA_FOTO_DOC, 'foto_dato')" not in js, (
            'volver a foto_dato hace que una foto de SOAT de 1200 px devuelva '
            '409: el CHECK de resolución la rechaza')

    def test_el_visor_distingue_pdf_de_imagen(self):
        """Un PDF dentro de un <img> no falla ruidosamente: pinta un icono roto
        y parece que el archivo no está."""
        js = self._js()
        assert "mime === 'application/pdf'" in js
        assert '<object data=' in js
