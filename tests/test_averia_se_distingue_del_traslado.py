"""El documento de avería **no puede parecerse a un traslado de reposición**.

## El defecto que este archivo cierra

`transferir_a_averias` emite tipo `TRA`, clase 67 y concepto 607 — los mismos
que una transferencia inter-bodega ordinaria. Lo único que los separa es
`f470_id_motivo`, que sale de `SIESA_MOTIVO_AVERIA`.

Y esa variable tenía un fallback: `os.getenv('SIESA_MOTIVO_AVERIA', '') or
self.motivo_traslado`. Sin configurar, la avería viajaba con el motivo del
traslado normal y en Siesa **las dos cosas eran el mismo documento**.

No era un error de código: en el maestro de Siesa el concepto 0607 tenía **un
solo motivo** (`01 Transferencia`). Literalmente no había otro que poner. El
2026-09-15 se creó `02 Transferencia por avería` en producción y en QA.

## Qué vigila

Que el motivo de la avería y el del traslado ordinario **no sean el mismo** una
vez configurados. El fallback se conserva a propósito —sin él, una variable
ausente sería un rechazo duro de Siesa en vez de un documento genérico— pero
con la variable puesta, los dos documentos tienen que separarse.
"""
import pytest


def _gateway(monkeypatch, motivo_averia, motivo_traslado='01'):
    monkeypatch.setenv('CONNEKTA_BODEGA', 'NB1')
    monkeypatch.setenv('CONNEKTA_CENTRO_OP', '003')
    monkeypatch.setenv('SIESA_BODEGA_AVERIAS', 'AV1')
    monkeypatch.setenv('SIESA_TIPO_DOCTO_TRASLADO', 'TRA')
    monkeypatch.setenv('SIESA_MOTIVO_TRASLADO', motivo_traslado)
    if motivo_averia is None:
        monkeypatch.delenv('SIESA_MOTIVO_AVERIA', raising=False)
    else:
        monkeypatch.setenv('SIESA_MOTIVO_AVERIA', motivo_averia)

    from app.services.connekta_gateway import ConnektaGateway
    from app.services.connekta_ajustes_gateway import ConnektaAjustesGateway
    core = ConnektaGateway()
    gw = ConnektaAjustesGateway(core)
    capturado = {}

    def _post_falso(*a, **kw):
        for x in a:
            if isinstance(x, dict) and 'Documentos' in x:
                capturado['payload'] = x
        return {'codigo': 0, 'mensaje': 'simulado'}

    core._post = _post_falso
    return gw, capturado


def _motivo_de_averia(monkeypatch, motivo_averia, motivo_traslado='01'):
    gw, cap = _gateway(monkeypatch, motivo_averia, motivo_traslado)
    gw.transferir_a_averias('PAPELSP9830', 1, 'prueba')
    return cap['payload']['Movimientos'][0]['f470_id_motivo']


def test_la_averia_no_viaja_con_el_motivo_del_traslado(monkeypatch):
    assert _motivo_de_averia(monkeypatch, '02', motivo_traslado='01') == '02'


def test_configurada_los_dos_documentos_se_distinguen(monkeypatch):
    """La propiedad de fondo: en Siesa no pueden ser el mismo documento."""
    motivo_traslado = '01'
    assert _motivo_de_averia(monkeypatch, '02', motivo_traslado) != motivo_traslado


def test_sin_configurar_cae_al_de_traslado_y_eso_es_deliberado(monkeypatch):
    """El fallback se conserva: una variable ausente da un documento genérico,
    no un rechazo duro de Siesa. Este test existe para que quitarlo sea una
    decisión y no un descuido."""
    assert _motivo_de_averia(monkeypatch, None, motivo_traslado='01') == '01'


def test_el_resto_del_documento_no_cambia(monkeypatch):
    """Dirección contraria: el motivo nuevo no puede haber movido nada más."""
    gw, cap = _gateway(monkeypatch, '02')
    gw.transferir_a_averias('PAPELSP9830', 1, 'prueba')
    d = cap['payload']['Documentos'][0]
    m = cap['payload']['Movimientos'][0]
    assert d['f350_id_clase_docto'] == 67
    assert d['f350_id_tipo_docto'] == 'TRA'
    assert d['f450_id_concepto'] == 607
    assert d['f450_id_bodega_salida'] == 'NB1'
    assert d['f450_id_bodega_entrada'] == 'AV1'
    # La AV1 de PRODUCCIÓN no maneja lotes: `None` es el valor correcto.
    assert m['f470_id_lote'] is None
    assert m['f470_id_lote_ent'] is None
