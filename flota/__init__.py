"""
Módulo FLOTA — control de flota dentro de WMS-PAME.

Frontera del módulo (la razón de que viva fuera de `app/`):

    flota/dominio/     puro. No importa Flask, ni SQLAlchemy, ni `app.*`.
    flota/puertos.py   Protocols que el dominio consume. Sin I/O.
    flota/adaptadores/ I/O. Aquí y solo aquí se toca la base y el storage.
    flota/api/         Flask. Traduce HTTP ↔ dominio.

La dirección de dependencia es una sola: api → adaptadores → puertos → dominio.
El dominio no mira hacia arriba. Está verificado por trinquete
(`tests/flota/test_trinquetes_flota.py::TestTrinqueteFronteraDominio`), no por
disciplina — `app/` demostró que la disciplina no sobrevive al volumen.

Reglas del módulo: `flota/CLAUDE.md`.
Especificación de la tanda 1: `docs/flota/ESPECIFICACION_T1.md`.
"""
