"""Un entero ≥ 0 que llega por JSON — una sola definición.

La usaban dos copias idénticas (`MobileService._entero_no_negativo` y
`empaque_producto._entero`, 2026-10-02). Un arreglo en una (aceptar `Decimal`,
por ejemplo) habría divergido de la otra.
"""


def entero_no_negativo(valor, campo: str) -> int:
    """`valor` como entero ≥ 0, o `ValueError`. Un booleano no es un número
    (en Python `True == 1`); `2.0` y `'2'` sí lo son."""
    if isinstance(valor, bool) or valor is None:
        raise ValueError(f'{campo} debe ser un número entero')
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    if isinstance(valor, str) and valor.strip().isdigit():
        valor = int(valor.strip())
    if not isinstance(valor, int) or valor < 0:
        raise ValueError(f'{campo} debe ser un número entero mayor o igual a 0')
    return valor
