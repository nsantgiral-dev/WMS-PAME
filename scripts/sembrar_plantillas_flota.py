"""
Siembra las plantillas de inspección de flota. Idempotente.

    venv/bin/python scripts/sembrar_plantillas_flota.py

No actualiza una plantilla que ya existe: editarla en sitio cambiaría el
significado de las inspecciones ya hechas bajo ella. Para cambiar el catálogo se
crea `<tipo>_v2` en `flota/adaptadores/catalogo.py`.

Se corre a mano y no desde una migración a propósito. Es dato, no esquema —
quiero ver el diff cuando alguien cambie un gesto, y quiero poder re-ejecutarlo
sin miedo.
"""
import sys

from app import create_app
from app.extensions import db
from flota.adaptadores.catalogo import sembrar


def main():
    app = create_app()
    with app.app_context():
        from flota.adaptadores.modelos import ItemInspeccion, PlantillaInspeccion
        resultado = sembrar(db)
        for codigo, estado in sorted(resultado.items()):
            p = PlantillaInspeccion.query.filter_by(codigo=codigo).one()
            bloq = len(p.bloqueantes())
            total = ItemInspeccion.query.filter_by(plantilla_id=p.id).count()
            print(f'  {codigo:<22} {estado:<12} {bloq} bloqueantes · {total} ítems')
    return 0


if __name__ == '__main__':
    sys.exit(main())
