"""El mínimo de 1600 px valida un número medido, no uno prometido.

## Qué costaba

La regla 7 del módulo exige que una **foto-dato** —el tablero con el odómetro,
una factura, un documento— tenga mínimo 1600 px de lado largo, *«porque el
odómetro es un número de seis dígitos fotografiado a las 5 a.m. en patio, y a
800×600 recomprimido a calidad 40 no es legible»*.

Ese mínimo lo impone un CHECK en la base. Y hasta el 2026-09-01 el CHECK
evaluaba `datos['ancho']` y `datos['alto']` **tal como venían en el JSON del
cliente**:

    campos = {'ancho': datos['ancho'] if 'ancho' in datos else None, ...}
    validar_formato(clase, mime, campos['ancho'], campos['alto'])

O sea: cualquiera que arme el POST a mano declara `{"ancho": 4000, "alto":
3000}` sobre una imagen de 100×75 y pasa. **La única evidencia del kilometraje
se apoyaba en un dato autorreportado por quien la sube.**

Y la pieza para medirlo estaba construida: `AlmacenLocal.dimensiones()` usa PIL
desde la tanda 1 y **no tenía un solo caller**. Es la séptima aparición de
`función-sin-caller` dentro del módulo — y ésta habría reventado el día que
alguien la usara, porque `from PIL import Image` está a nivel de método y en un
entorno sin Pillow levanta `ImportError`.

## Por qué se mide sobre los bytes y no sobre el archivo guardado

`dimensiones()` toma un `storage_ref`, y cuando hay que validar todavía no se
guardó nada. Medir sobre `contenido` —los bytes ya decodificados— es además más
honesto: valida exactamente lo que se va a escribir, no lo que quedó en disco.

## Por qué NO rechaza cuando no puede medir

Un PDF adjunto no tiene píxeles. Un archivo truncado tampoco. Y un traspaso de
custodia no se puede trabar a las 5 a.m. porque PIL no supo abrir algo: ante
«no sé» se conserva lo declarado y sigue el camino de validación de siempre.
Es la regla 4 —«un estado que puede ser *no sé* se modela con palabras»—
aplicada a una medición: `None` no es cero.

## Para el PWA de hoy esto es no-op

`flotaComprimir` (`flota.js:418-440`) dibuja en un canvas de `w`×`h` y declara
esos mismos `w`×`h`. Verificado generando JPEG reales de 1600×1200, 800×600,
1600×900 y 1200×1600: **medido == declarado en los cuatro**. El cambio no
rechaza nada que hoy funcione; solo deja de creerle a quien no sea ese cliente.
"""
import base64
import io

import pytest

from flota.adaptadores.almacen_fotos import _medir_dimensiones, guardar_foto


def _jpeg(ancho, alto):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (ancho, alto), (17, 17, 17)).save(buf, 'JPEG', quality=85)
    return buf.getvalue()


def _data_url(contenido, mime='image/jpeg'):
    return f'data:{mime};base64,' + base64.b64encode(contenido).decode()


def _payload(contenido, *, clase='foto_dato', ancho=None, alto=None, mime='image/jpeg'):
    d = {'clase': clase, 'data_url': _data_url(contenido, mime)}
    if ancho is not None:
        d['ancho'] = ancho
    if alto is not None:
        d['alto'] = alto
    return d


class TestSeMideYNoSeCree:
    def test_una_foto_dato_que_MIENTE_sobre_su_tamano_queda_con_su_medida_real(
            self, tmp_path, monkeypatch):
        """**El hueco que se cierra.** 100×75 declarados como 4000×3000.

        El mínimo de 1600 px no lo impone `validar_formato`: lo impone el CHECK
        `ck_flota_foto_dato_resolucion` en el INSERT. Lo que este cambio hace es
        que ese CHECK reciba el número **medido** en vez del prometido — o sea,
        que la fila que llega a la base ya no pueda mentir."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        fila = guardar_foto(_payload(_jpeg(100, 75), ancho=4000, alto=3000))
        assert (fila['ancho'], fila['alto']) == (100, 75), (
            'la fila conservó el tamaño declarado: el CHECK de 1600 px seguiría '
            'evaluando un número autorreportado')

    def test_lo_que_queda_guardado_es_lo_MEDIDO(self, tmp_path, monkeypatch):
        """Una foto legítima cuyo cliente declara mal por un bug: se guarda la
        medida real, no la declarada. La fila no puede afirmar un tamaño que el
        archivo no tiene."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        fila = guardar_foto(_payload(_jpeg(1600, 1200), ancho=999, alto=888))
        assert (fila['ancho'], fila['alto']) == (1600, 1200)

    def test_avisa_cuando_lo_declarado_no_coincide(self, tmp_path, monkeypatch, caplog):
        """El desacuerdo no se traga: queda en el log con los dos números.
        Un cliente que declara mal es un cliente con un bug, y hay que poder
        verlo sin abrir la base."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        with caplog.at_level('WARNING'):
            guardar_foto(_payload(_jpeg(1600, 1200), ancho=999, alto=888))
        assert any('999' in r.message and '1600' in r.message
                   for r in caplog.records), (
            'el desacuerdo entre lo declarado y lo medido no dejó rastro')


class TestNoRompeLoQueFunciona:
    """Detector en las dos direcciones. Sin esto, «ahora se mide» no distingue
    el arreglo de haber roto la subida de fotos."""

    def test_el_caso_del_PWA_de_hoy_pasa_igual(self, tmp_path, monkeypatch):
        """`flotaComprimir` declara exactamente lo que dibuja. Es el 100% del
        tráfico real y tiene que seguir entrando sin cambios."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        fila = guardar_foto(_payload(_jpeg(1600, 1200), ancho=1600, alto=1200))
        assert (fila['ancho'], fila['alto']) == (1600, 1200)
        assert fila['storage_ref']
        assert fila['hash_sha256']

    @pytest.mark.parametrize('ancho,alto', [(1600, 1200), (1200, 1600),
                                            (1600, 900), (2000, 1500)])
    def test_varias_formas_legitimas_entran(self, tmp_path, monkeypatch, ancho, alto):
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        fila = guardar_foto(_payload(_jpeg(ancho, alto), ancho=ancho, alto=alto))
        assert (fila['ancho'], fila['alto']) == (ancho, alto)

    def test_una_foto_dato_chica_y_honesta_sigue_dando_su_tamano(
            self, tmp_path, monkeypatch):
        """El cliente honesto no cambia de comportamiento: 800×600 declarado y
        medido dan lo mismo. Quien decide si entra es el CHECK, igual que
        antes."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        fila = guardar_foto(_payload(_jpeg(800, 600), ancho=800, alto=600))
        assert (fila['ancho'], fila['alto']) == (800, 600)


class TestCuandoNoSePuedeMedir:
    """`None` es «no sé», no «cero». Y no traba a nadie."""

    @pytest.mark.parametrize('contenido,etiqueta', [
        (b'%PDF-1.4 documento', 'PDF'),
        (b'\x00\x01\x02\x03', 'basura'),
    ])
    def test_medir_devuelve_None_en_vez_de_reventar(self, contenido, etiqueta):
        assert _medir_dimensiones(contenido) is None, (
            f'{etiqueta}: debería devolver None, no levantar ni inventar un cero')

    def test_un_PDF_adjunto_sigue_entrando_sin_dimensiones(self, tmp_path, monkeypatch):
        """La clase `adjunto` no exige dimensiones — un PDF no tiene píxeles.
        Medir no puede cambiar eso."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        fila = guardar_foto(_payload(b'%PDF-1.4 x', clase='documento_adjunto',
                                     mime='application/pdf'))
        assert fila['ancho'] is None and fila['alto'] is None

    def test_lo_declarado_sobrevive_cuando_no_se_puede_medir(self, tmp_path,
                                                            monkeypatch):
        """Si PIL no abre el archivo, se conserva lo que dijo el cliente y la
        validación de siempre decide. No se pierde información ni se inventa."""
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        fila = guardar_foto(_payload(b'%PDF-1.4 x', clase='documento_adjunto',
                                     mime='application/pdf',
                                     ancho=1700, alto=2200))
        assert (fila['ancho'], fila['alto']) == (1700, 2200)


class TestLaMedicionEsRealYNoUnaProxy:
    """El canario del instrumento: si `_medir_dimensiones` devolviera siempre
    `None`, todos los tests de arriba que exigen rechazo pasarían igual —porque
    caerían al camino de «conservar lo declarado»— y el hueco seguiría abierto
    sin que nada avisara."""

    def test_mide_de_verdad_un_jpeg(self):
        assert _medir_dimensiones(_jpeg(1600, 1200)) == {'ancho': 1600, 'alto': 1200}

    def test_distingue_dos_tamanos_distintos(self):
        a = _medir_dimensiones(_jpeg(1600, 1200))
        b = _medir_dimensiones(_jpeg(800, 600))
        assert a != b, 'la medición devuelve lo mismo para tamaños distintos'

    def test_pillow_esta_declarado_como_dependencia(self):
        """El 2026-09-01 el venv local no tenía Pillow aunque
        `requirements.txt` lo declara, y `_medir_dimensiones` degradaba a
        `None` en silencio: la medición no ocurría y nada lo decía.

        Este test no comprueba que esté instalado —eso lo dice el import de
        arriba, que fallaría— sino que siga **declarado**, para que producción
        lo tenga."""
        import pathlib
        req = (pathlib.Path(__file__).resolve().parents[2] / 'requirements.txt'
               ).read_text().lower()
        assert 'pillow' in req, (
            'Pillow salió de requirements.txt: `_medir_dimensiones` va a '
            'devolver None siempre y el mínimo de 1600 px vuelve a validar el '
            'número que declara el cliente')


class TestElCHECKDeLaBaseRecibeLoMedido:
    """De punta a punta: lo que decide si la foto entra es
    `ck_flota_foto_dato_resolucion` en el INSERT, y este cambio existe para que
    ese CHECK evalúe un número medido.

    Sin esta clase, todo lo de arriba prueba que `guardar_foto` **devuelve** lo
    medido — y nada prueba que eso llegue a la base y sirva de algo.
    """

    @pytest.fixture
    def autor(self, db):
        from app.models.usuario import Usuario
        u = Usuario(email='foto_ct@test.com', nombre='CT', rol='admin', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        return u.id

    @staticmethod
    def _guardar(tmp_path, monkeypatch, contenido, ancho, alto):
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        return guardar_foto(_payload(contenido, ancho=ancho, alto=alto))

    @staticmethod
    def _fila(campos, autor):
        from datetime import datetime
        from flota.adaptadores.modelos import Foto
        return Foto(entidad_tipo='custodia_inicio', entidad_id=1,
                    ts_captura=datetime(2026, 9, 1, 5, 0, 0),
                    autor_usuario_id=autor, **campos)

    def test_un_cliente_que_miente_ya_no_entra_como_evidencia(
            self, app, db, tmp_path, monkeypatch, autor):
        """**El caso completo.** 100×75 declarados 4000×3000: antes la fila
        llegaba con 4000, el CHECK la daba por buena y quedaba como evidencia
        legible del odómetro. Ahora llega con 100 y la base la rechaza.

        El mensaje se comprueba: un rechazo por otra restricción —un NOT NULL
        olvidado en el test, por ejemplo— haría pasar este test sin que el
        CHECK de resolución hubiera intervenido."""
        from sqlalchemy.exc import IntegrityError, OperationalError

        campos = self._guardar(tmp_path, monkeypatch, _jpeg(100, 75), 4000, 3000)
        assert (campos['ancho'], campos['alto']) == (100, 75)
        db.session.add(self._fila(campos, autor))
        with pytest.raises((IntegrityError, OperationalError)) as e:
            db.session.commit()
        db.session.rollback()
        assert 'resolucion' in str(e.value).lower(), (
            f'la fila se rechazó por otra cosa, no por el CHECK de resolución: '
            f'{e.value}')

    def test_la_foto_legitima_del_PWA_entra(self, app, db, tmp_path, monkeypatch,
                                            autor):
        """La otra dirección, y es la que importa que no se rompa: el tráfico
        real de todos los días sigue pasando el CHECK."""
        from flota.adaptadores.modelos import Foto

        campos = self._guardar(tmp_path, monkeypatch, _jpeg(1600, 1200), 1600, 1200)
        db.session.add(self._fila(campos, autor))
        db.session.commit()
        assert Foto.query.count() == 1
