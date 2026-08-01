"""
Errores del dominio de flota.

Son excepciones, no códigos de retorno, por la regla 5: en este módulo nada
degrada hacia algo que se parezca al éxito. Una lectura que no se puede
aceptar levanta; no devuelve el último valor conocido.
"""


class ErrorFlota(Exception):
    """Raíz de los errores del dominio. Nunca se levanta directamente."""


class LecturaRechazada(ErrorFlota):
    """Una lectura de odómetro viola la monotonía y no se declara corrección."""


class CustodiaInvalida(ErrorFlota):
    """Una custodia rompe cardinalidad, cobertura temporal o arco exclusivo."""


class FotoInvalida(ErrorFlota):
    """Una foto no tiene padre resoluble o degrada su clase."""
