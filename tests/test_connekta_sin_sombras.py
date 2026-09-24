"""El parche de un test sobre la instancia `connekta` no sobrevive a su test
(`tests/conftest.py::_connekta_sin_metodos_pegados_a_la_instancia`).

Los dos tests van en este orden a propósito: el primero deja el método pegado
a la instancia, como lo hace `monkeypatch.setattr(connekta, ...)` al
restaurar; el segundo exige que ya no esté y que un parche por clase se oiga.
"""


def test_1_un_test_deja_un_metodo_pegado_a_la_instancia():
    from app.services.connekta_gateway import connekta
    connekta._get = lambda *a, **k: {'detalle': {'Table': []}}
    assert '_get' in vars(connekta)


def test_2_el_siguiente_test_no_lo_hereda(monkeypatch):
    from app.services.connekta_gateway import ConnektaGateway, connekta
    assert '_get' not in vars(connekta)
    llamadas = []
    monkeypatch.setattr(ConnektaGateway, '_get', lambda self, *a, **k: llamadas.append(1) or {})
    connekta._get('X')
    assert llamadas == [1], 'el parche por clase quedó sordo'
