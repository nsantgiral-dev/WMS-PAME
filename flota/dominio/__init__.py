"""
Núcleo del dominio de flota. Puro: sin Flask, sin SQLAlchemy, sin `app.*`.

La frontera está verificada por trinquete, no por costumbre:
`tests/flota/test_trinquetes_flota.py::TestTrinqueteFronteraDominio`.

Aquí viven las políticas. Cada una existe UNA vez y todos la consumen — el
fallback que se escribió dos veces en `kardex_service` divergió en tres horas
y costó 25× de sobreestimación.
"""
