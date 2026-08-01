"""
Adaptadores del módulo de flota — la única capa que toca I/O.

Aquí se permite importar `app.*`, SQLAlchemy y el object storage. El dominio
no. Si un import de `app.*` aparece en `flota/dominio/`, el trinquete de
frontera rompe el build.
"""
