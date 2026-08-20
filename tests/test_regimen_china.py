"""
El régimen China corría apagado, y la pantalla no lo decía.

Dos defectos apilados, encontrados el 2026-08-19:

1. **`Producto.origen` no tiene ningún escritor.** No lo pone el sync de
   Siesa, no está en la lista blanca de `actualizar_producto`, no hay ruta ni
   script ni backfill. Con la columna vacía `es_china` es siempre `False`:
   todo SKU recibe lead time nacional y `R=0`, `resultados_chi` sale vacío, y
   la propuesta de contenedor con él. En cascada, `total_china` da 0 →
   `cobertura_fichas` da 0 → el modo es **siempre SHADOW** y la compuerta G5
   no puede abrirse ni llenando todas las fichas.

2. **El cruce por marca miraba la columna equivocada.** Preguntaba si `'M003'`
   era subcadena de `origen`, cuyos valores son `'NACIONAL'` y `'CHINA'`.
   `'M003' in 'CHINA'` es `False`: **la rama no podía darse ni con `origen`
   poblado al 100%**. El comentario de `MARCAS_CHINA` decía «se cruzan con
   `producto.marca_siesa`» y describía un cruce que el código no hacía.

El segundo se arregla acá. El primero es dato de negocio —qué SKU es de
importación— y no se puede inventar; lo que sí se puede es **dejar de fingir
que el régimen está activo**, que es lo que hace `insumo_origen`.
"""
import pytest


@pytest.fixture
def productos(db):
    from app.models.producto import Producto
    hechos = {}
    for codigo, origen, marca in (
        ('SKU-NAC', 'NACIONAL', 'M999'),
        ('SKU-CHI', 'CHINA', None),
        ('SKU-MARCA', None, 'M003'),
        ('SKU-NADA', None, None),
    ):
        p = Producto.query.filter_by(codigo=codigo).first()
        if not p:
            p = Producto(codigo=codigo, nombre=codigo, codigo_siesa=codigo)
            db.session.add(p)
        p.origen = origen
        p.marca_siesa = marca
        hechos[codigo] = p
    db.session.commit()
    return hechos


class TestElCruceDeMarca:
    def test_la_marca_se_busca_en_marca_no_en_origen(self, db, productos):
        """**El detector ciego.** `'M003' in 'CHINA'` es False: la rama de
        marca era inalcanzable, y quien arreglara `origen` sin arreglar esto
        habría dejado los SKU chinos identificados solo por marca con lead
        time nacional y la pantalla en verde."""
        import ast
        import pathlib
        src = pathlib.Path('app/services/armador_service.py').read_text()
        arbol = ast.parse(src)
        # El `any(... for m in MARCAS_CHINA)` tiene que iterar contra la
        # variable de marca, no contra la de origen.
        encontrado = False
        for n in ast.walk(arbol):
            if not isinstance(n, ast.GeneratorExp):
                continue
            texto = ast.dump(n)
            if 'MARCAS_CHINA' not in texto:
                continue
            encontrado = True
            assert "id='marca'" in texto, (
                'el cruce por marca sigue comparando contra otra cosa: '
                f'{texto[:200]}')
            assert 'productos_origen' not in texto, (
                'volvió a buscar la marca dentro del campo de origen')
        assert encontrado, 'desapareció el cruce por MARCAS_CHINA'


class TestElInsumoSeDeclara:
    def _rop(self, monkeypatch, demanda):
        from app.services import armador_service
        monkeypatch.setattr(armador_service, '_demanda_por_sku',
                            lambda *a, **k: demanda, raising=False)
        return armador_service

    def test_sin_origen_ni_marca_lo_dice(self, db, productos):
        """Un modelo que corre sobre un insumo ausente y no lo declara es la
        forma más cara de este repo: el número sale con cara de bueno."""
        from app.models.producto import Producto
        for p in Producto.query.all():
            p.origen = None
            p.marca_siesa = None
        db.session.commit()

        from app.services.armador_service import ArmadorService
        r = ArmadorService.rop_dual()
        assert 'insumo_origen' in r, (
            'el ROP dual no declara si tenía con qué distinguir el régimen')
        assert r['insumo_origen']['regimen_china_operativo'] is False
        assert 'lead time nacional' in (r['insumo_origen']['nota'] or '')

    def test_con_origen_poblado_se_declara_operativo(self, db, productos):
        from app.services.armador_service import ArmadorService
        r = ArmadorService.rop_dual()
        assert r['insumo_origen']['regimen_china_operativo'] is True
        assert r['insumo_origen']['skus_con_origen'] >= 2
        assert r['insumo_origen']['nota'] is None

    def test_cuenta_la_marca_aunque_falte_el_origen(self, db, productos):
        """`SKU-MARCA` no tiene `origen` y sí `marca_siesa`. Con el cruce
        arreglado, la marca sola alcanza para el régimen — que es para lo que
        la columna se creó."""
        from app.services.armador_service import ArmadorService
        r = ArmadorService.rop_dual()
        assert r['insumo_origen']['skus_con_marca'] >= 2
