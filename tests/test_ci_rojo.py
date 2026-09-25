# Prueba TEMPORAL: comprueba que «Wait for CI» de Railway frena un despliegue
# cuando GitHub Actions sale rojo. Se revierte apenas se confirme.


def test_ci_rojo_a_proposito():
    assert False, "rojo a propósito: Railway NO debe desplegar este commit"
