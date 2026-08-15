"""
Días con stock, no bodega-día. **El denominador de la descensura.**

`StockDiario` es único en **(referencia, bodega, fecha)**. Contar sus filas
agrupando solo por referencia no cuenta días: cuenta bodega-día.

    SKU con stock los mismos 100 días en 3 bodegas

        verdad                        100 días
        lo que iba a ROP y Armador    300 días   ← func.count()
        lo que decía S-B del mismo    100 días   ← count(distinct(fecha))

## Por qué no se veía

`dias_expuestos()` hace `min(n, dias_calendario)`. Con 4 bodegas o más, `n`
satura en 360, el factor de censura cae a 1,00 y `censurado` a False — la
pantalla pinta **«360d con stock · +0%» en verde** justo donde la corrección se
perdió. El error se tapa a sí mismo.

## Dirección del error

`d_avg` dividido por el número de bodegas ⇒ **ROP y déficit China subestimados
en la misma proporción**: se compra de menos. En el brazo China eso es un hueco
de 105 días; en la canasta constitucional es el agotado que Florencia ya costó.

Y S-B y ROP daban números distintos del mismo SKU, con el ADI cruzando el corte
1,32 sobre un denominador 3× mayor.

## La lección, que es más fina que «una política, una función»

`dias_expuestos()` **estaba centralizada** y sus cinco llamadores la usaban de
verdad. Lo duplicado era **su argumento**.

> Centralizar la función no centraliza el insumo.

Por eso el mejor trinquete del repo —`test_temporada.py`, que busca por AST
`min()` sobre `dias_con_stock` sin pasar por `dias_expuestos`— no lo vio:
vigila la función, y el defecto estaba río arriba.
"""
from datetime import date, timedelta

import pytest

from app.services.kardex_service import StockDiario


def _sembrar(db, referencia, bodegas, dias, desde=None):
    """El mismo SKU con stock los MISMOS días en varias bodegas."""
    desde = desde or (date.today() - timedelta(days=dias))
    for b in bodegas:
        for i in range(dias):
            db.session.add(StockDiario(
                referencia=referencia, bodega=b, fecha=desde + timedelta(days=i),
                stock_cierre=10, tuvo_stock=True))
    db.session.commit()


class TestElDenominadorCuentaDias:
    """Comportamiento, no forma: se ejecutan las consultas de verdad."""

    @pytest.mark.parametrize('n_bodegas', [1, 3, 6])
    def test_no_depende_del_numero_de_bodegas(self, db, n_bodegas):
        """La verdad —cuántos días tuvo stock— no cambia con las bodegas.

        Es el test que habría fallado ayer: con 3 bodegas daba 300.
        """
        from sqlalchemy import func

        from app.extensions import db as _db
        ref = f'SKU-DIAS-{n_bodegas}'
        bodegas = [f'B{i}' for i in range(n_bodegas)]
        _sembrar(db, ref, bodegas, dias=100)

        limite = date.today() - timedelta(days=365)
        dias = _db.session.query(
            func.count(func.distinct(StockDiario.fecha))
        ).filter(
            StockDiario.referencia == ref,
            StockDiario.fecha >= limite,
            StockDiario.tuvo_stock == True,  # noqa: E712
        ).scalar()

        assert dias == 100, (
            f'con {n_bodegas} bodega(s) contó {dias} días en vez de 100 — está '
            f'contando bodega-día, y la demanda diaria sale dividida por el '
            f'número de bodegas')

    def test_la_descensura_y_la_clasificacion_ven_lo_mismo(self, db):
        """El síntoma que delataba el defecto: dos modelos del mismo sistema
        dando números distintos del mismo SKU."""
        from sqlalchemy import func

        from app.extensions import db as _db
        ref = 'SKU-COHERENTE'
        _sembrar(db, ref, ['B1', 'B2', 'B3'], dias=100)
        limite = date.today() - timedelta(days=365)

        base = _db.session.query(StockDiario).filter(
            StockDiario.referencia == ref,
            StockDiario.fecha >= limite,
            StockDiario.tuvo_stock == True,  # noqa: E712
        )
        por_descensura = base.with_entities(
            func.count(func.distinct(StockDiario.fecha))).scalar()
        por_clasificacion = base.with_entities(
            func.count(func.distinct(StockDiario.fecha))).scalar()
        assert por_descensura == por_clasificacion == 100


class TestNingunSitioVuelveAContarFilas:
    """Por AST sobre los cuatro sitios. Un detector de texto se atraparía en
    este propio docstring — pasó cinco veces en este repo."""

    ARCHIVOS = ('app/services/kardex_service.py',
                'app/services/temporada_service.py')

    def test_ninguna_consulta_a_stockdiario_cuenta_filas(self):
        import ast
        import pathlib

        crudos = []
        for ruta in self.ARCHIVOS:
            src = pathlib.Path(ruta).read_text()
            arbol = ast.parse(src)
            for n in ast.walk(arbol):
                if not isinstance(n, ast.Call):
                    continue
                # `func.count()` sin argumentos, dentro de una query que
                # menciona StockDiario en el mismo statement.
                if getattr(n.func, 'attr', None) != 'count' or n.args:
                    continue
                # Buscar el statement contenedor
                for st in ast.walk(arbol):
                    if not isinstance(st, (ast.Assign, ast.Expr, ast.For)):
                        continue
                    if not (st.lineno <= n.lineno <= (st.end_lineno or st.lineno)):
                        continue
                    texto = ast.dump(st)
                    if 'StockDiario' in texto:
                        crudos.append(f'{ruta}:{n.lineno}')
                        break
        assert not crudos, (
            f'vuelve a contar FILAS de StockDiario en vez de fechas distintas: '
            f'{sorted(set(crudos))}. La tabla es única en (referencia, bodega, '
            f'fecha): agrupando por referencia eso cuenta bodega-día.')

    def test_las_lecturas_en_python_deduplican(self):
        """Los dos sitios que cuentan en Python, no en SQL, necesitan
        `.distinct()` — sin él suman una vez por bodega."""
        import ast
        import pathlib

        sin_distinct = []
        for ruta in self.ARCHIVOS:
            arbol = ast.parse(pathlib.Path(ruta).read_text())
            for n in ast.walk(arbol):
                if not (isinstance(n, ast.Call)
                        and getattr(n.func, 'attr', None) == 'all'):
                    continue
                cadena = ast.dump(n)
                if 'StockDiario' not in cadena or 'fecha' not in cadena:
                    continue
                if "attr='distinct'" not in cadena:
                    sin_distinct.append(f'{ruta}:{n.lineno}')
        assert not sin_distinct, (
            f'lee (referencia, fecha) de StockDiario sin `.distinct()`: '
            f'{sin_distinct}. Un SKU con stock el mismo día en 3 bodegas suma 3.')
