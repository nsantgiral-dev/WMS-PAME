"""
Veintiocho requisiciones sueltas en Siesa, por consultar demasiado pronto.

La primera auditoría contra producción agrupó los 53 errores de traslado por
causa, y la más numerosa —28— no era un rechazo:

    «174646 aceptada por Siesa pero el WMS no pudo leer el consecutivo.
     El despacho usará 173076 (fallback). La RIT huérfana en Siesa debe
     cerrarse manualmente.»

La RIT **sí entra**. Lo que falla es leerla de vuelta.

## La causa está escrita en el propio reglamento del proyecto

CLAUDE.md, Regla 20: *«Después de POST exitoso, Siesa tarda ~10-12 s en
procesar — no consultar inmediatamente.»*

`aprobar_solicitud` hace el POST del 174646 y consulta el consecutivo **en la
línea siguiente**. Llega temprano, no encuentra nada, y el traslado sigue por
el fallback dejando una requisición suelta que alguien tiene que cerrar a mano.

Se descartó el truncamiento antes de llegar acá: `f440_referencia` mide 20 y el
código del traslado 16, así que cabe entero.

## Por qué el reintento va en el despacho y no en un `sleep`

Dormir 10 segundos dentro del request de aprobación castiga a quien aprueba por
un problema de tiempos del ERP. El despacho es el siguiente momento natural del
flujo y ocurre minutos u horas después — para entonces la espera que la Regla 20
pedía ya pasó, sin que nadie la haya esperado.
"""
import pathlib

import pytest

_SERVICIO = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
             / 'traslado_service.py')


_MARCA = ('AVISO: 174646 aceptada por Siesa pero el WMS no pudo leer el '
          'consecutivo. El despacho usará 173076 (fallback).')


@pytest.fixture
def solicitud_huerfana(db, almacen):
    """Una solicitud aprobada con la marca de RIT huérfana, lista para despachar."""
    from app.models.usuario import Usuario
    from tests.flujo import conductor_de_flujo as cf

    ids = {}
    for rol, email in (('tienda', 'tienda_rit@test.com'),
                       ('admin', 'admin_rit@test.com'),
                       ('operario', 'op_rit@test.com')):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
            u.set_password('t')
            db.session.add(u); db.session.flush()
        ids[rol] = u.id
    db.session.commit()

    s = cf.flujo_traslado(db, almacen, ids['tienda'], ids['admin'], ids['operario'])
    s.siesa_error = _MARCA
    s.siesa_requisicion_consec = None
    for it in s.items:
        it.cantidad_enviada = it.cantidad_aprobada
    db.session.commit()
    return s


class TestElDespachoVuelveAPreguntar:
    """Se ejerce la rama, no se busca el texto.

    La primera versión de este test comprobaba que `recuperar_consec_rit`
    apareciera en el fuente del método. Una mutación que apagaba la condición
    dejaba la llamada intacta dentro de la rama muerta y el test seguía en
    verde — el mismo modo de fallo que CLAUDE.md ya documenta para los
    detectores de texto, y el cuarto de esta clase en el repo.
    """

    def test_recupera_el_consecutivo_y_deja_de_ser_huerfana(
            self, db, solicitud_huerfana, monkeypatch):
        from app.services import traslado_service as ts

        monkeypatch.setattr(ts.siesa_traslado, 'recuperar_consec_rit',
                            lambda codigo: 4242)
        # El despacho real hablaría con Siesa; acá solo interesa el reintento.
        monkeypatch.setattr(ts.siesa_traslado, 'despachar_desde_rit',
                            lambda **kw: {'simulado': True})

        ts.TrasladoService.despachar(solicitud_huerfana.id)

        db.session.refresh(solicitud_huerfana)
        assert solicitud_huerfana.siesa_requisicion_consec == 4242
        assert solicitud_huerfana.siesa_error is None, (
            'la marca de huérfana quedó puesta sobre una RIT que sí se pudo leer')

    def test_si_sigue_sin_poder_leerse_no_rompe_el_despacho(
            self, db, solicitud_huerfana, monkeypatch):
        """El reintento es una mejora, no un requisito: si Siesa tampoco
        responde ahora, el traslado sigue por el fallback como antes."""
        from app.services import traslado_service as ts

        monkeypatch.setattr(ts.siesa_traslado, 'recuperar_consec_rit',
                            lambda codigo: None)
        monkeypatch.setattr(ts.siesa_traslado, 'registrar_salida_transito',
                            lambda **kw: {'simulado': True})

        ts.TrasladoService.despachar(solicitud_huerfana.id)
        db.session.refresh(solicitud_huerfana)
        assert solicitud_huerfana.siesa_requisicion_consec is None

    def test_solo_reintenta_sobre_el_caso_de_la_huerfana(self):
        """No sobre cualquier `siesa_error`: un rechazo estructural del 174646
        no se arregla volviendo a leer, y consultar por gusto gasta una llamada
        a Siesa en cada despacho."""
        fuente = _SERVICIO.read_text(encoding='utf-8')
        i = fuente.find('recuperar_consec_rit', fuente.find('def despachar'))
        contexto = fuente[max(0, i - 400):i]
        assert 'no pudo leer el consecutivo' in contexto

    def test_no_duerme_en_el_request(self):
        """La Regla 20 pide esperar, no bloquear. Un `sleep` acá castiga a quien
        aprueba por un problema de tiempos del ERP."""
        fuente = _SERVICIO.read_text(encoding='utf-8')
        i = fuente.find('def despachar')
        j = fuente.find('\n    @staticmethod', i + 10)
        cuerpo = fuente[i:j if j > 0 else i + 8000]
        assert 'time.sleep' not in cuerpo and 'sleep(' not in cuerpo


class TestLaReferenciaCabeEnSiesa:
    """Se verificó antes de buscar la causa en otro lado: si el código no
    cupiera, el filtro exacto del recovery nunca encontraría la fila y el
    reintento tampoco serviría."""

    def test_el_codigo_del_traslado_cabe_en_f440_referencia(self):
        import re
        import zipfile
        from html import unescape

        spec = (pathlib.Path(__file__).resolve().parents[1] / 'docs' / 'siesa-specs'
                / '174646 - API_v1_Inventarios_Comercial_RequisicionesParaTransferir.docx')
        xml = zipfile.ZipFile(spec).read('word/document.xml').decode('utf-8')
        xml = re.sub(r'</w:tc>', '\t', xml)
        xml = re.sub(r'</w:p>|</w:tr>', '\n', xml)
        lineas = [re.sub(r'[ ]+', ' ', l).strip()
                  for l in unescape(re.sub(r'<[^>]+>', '', xml)).split('\n')]
        i = next(k for k, l in enumerate(lineas) if l.startswith('f440_referencia'))
        nums = [x for x in lineas[i:i + 8] if x.isdigit()]
        ancho = int(nums[-1])
        # `ST-AAAAMMDD-XXXX` — el formato que genera el WMS.
        assert ancho >= len('ST-20260612-6F7C'), (
            f'f440_referencia mide {ancho}: el código del traslado se trunca en '
            f'Siesa y el recovery no lo va a encontrar nunca')
