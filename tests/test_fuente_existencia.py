"""
El número del conteo dice contra qué se comparó.

`existencia_siesa` es un nombre que promete procedencia y no la tenía: ante
Siesa caído el servicio caía al stock del WMS **con solo un `logger.warning`** y
lo guardaba en la misma columna. El propio docstring de `comparar_conteo` lo
admitía — *«compara contra existencia_siesa (que ahora almacena stock WMS)»*.

## Por qué importa, y no es cosmético

El ajuste que sale a Siesa es un **delta**: `fisica − existencia_siesa`.

    base correcta (Siesa)   →  Siesa queda en  fisica                    ✓
    base del WMS            →  Siesa queda en  siesa_real + (fisica − wms)

O sea que **precisamente cuando WMS y Siesa discrepan —la única razón para
contar— el ajuste empeora el descuadre.**

## La decisión de negocio (2026-08-15)

> No. Y además ningún ajuste se aprueba el mismo día, con sistema o sin sistema.
> El ajuste de inventario es la única transacción de esta compañía que
> **siempre puede esperar**: no detiene una venta, ni un despacho, ni un
> recaudo, ni una nómina.

Con eso, la aprobación con Siesa caído deja de ser un warning y pasa a negarse.
Si alguien la necesita, no está corrigiendo la verdad: está desbloqueando una
operación — y entonces el kardex deja de ser una medición para volverse un
residuo que se corrige cada vez que estorba.
"""
import pytest

from app.models.conteo import SesionConteo


@pytest.fixture
def sitio(almacen, producto, ub_picking):
    """`ubicacion_id`, `almacen_id` y `producto_id` son NOT NULL — una sesión de
    conteo siempre cuenta ALGO en ALGÚN lado. Se usan las fixtures del proyecto
    en vez de fabricar filas nuevas: inventar el mundo del test es cómo se
    verifica un flujo que nadie tiene."""
    return {'ubicacion_id': ub_picking.id, 'almacen_id': almacen.id,
            'producto_id': producto.id}


class TestLaProcedenciaSeDeclaraSiempre:
    def test_la_columna_existe_y_admite_las_dos(self, db, sitio):
        s = SesionConteo(codigo='CC-FE-1', tipo='DIARIO_ABC', estado='PENDIENTE',
                         fuente_existencia='SIESA', **sitio)
        db.session.add(s)
        db.session.commit()
        assert s.fuente_existencia == 'SIESA'

    def test_viaja_en_el_to_dict(self, db, sitio):
        s = SesionConteo(codigo='CC-FE-2', tipo='DIARIO_ABC', estado='PENDIENTE',
                         fuente_existencia='WMS', **sitio)
        db.session.add(s)
        db.session.commit()
        assert s.to_dict()['fuente_existencia'] == 'WMS'

    def test_se_escribe_en_el_caso_BUENO_tambien(self):
        """Un campo que solo se escribe cuando falla deja el caso sano
        indistinguible del histórico sin dato."""
        import ast
        import pathlib

        arbol = ast.parse(pathlib.Path('app/services/conteo_service.py').read_text())
        valores = {
            a.value.value for a in ast.walk(arbol)
            if isinstance(a, ast.Assign) and isinstance(a.value, ast.Constant)
            for t in a.targets
            if (isinstance(t, ast.Name) and t.id == '_fuente_existencia')
            or (isinstance(t, ast.Attribute) and t.attr == 'fuente_existencia')
        }
        assert {'SIESA', 'WMS'} <= valores, (
            f'solo se declara la procedencia en algunos casos: {valores}')


class TestNoSeAjustaContraElWMS:
    """La decisión de Operaciones, en código."""

    def test_la_aprobacion_se_niega_si_siesa_no_responde(self, db, sitio, almacen):
        """Ejecutando la aprobación, no leyendo su fuente.

        Antes era `logger.warning(...)` y el ajuste salía igual. Hasta el
        2026-09-23 esto se comprobaba por AST —«la rama `else` de
        `if existencia_siesa ...` levanta»—, atado a una forma del código que
        dejó de existir: la aprobación ya no lee Siesa, usa la foto que el
        conteo guardó. La propiedad es la misma y se mide por lo que hace: una
        sesión cuya base fue el WMS (Siesa no respondió al contar) no produce
        ningún job, levanta, y no cambia de estado.
        """
        import pytest as _pytest
        from app.models.siesa_job import SiesaJob
        from app.services.conteo_service import ConteoService
        almacen.centro_op_siesa = '003'
        s = SesionConteo(codigo='CC-FE-WMS', tipo='DIARIO_ABC', estado='DESCUADRE',
                         producto_codigo_siesa='PROD-001',
                         cantidad_fisica=10, existencia_siesa=7, diferencia=3,
                         fuente_existencia='WMS', **sitio)
        db.session.add(s)
        db.session.commit()

        with _pytest.raises(ValueError, match='siempre puede esperar'):
            ConteoService._encolar_ajuste_fisico(s, aprobador_id=None)
        db.session.rollback()
        assert SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO').count() == 0, (
            'la aprobación volvió a seguir de largo sin foto de Siesa. El '
            'ajuste sale como delta sobre la base del WMS y deja a Siesa peor de '
            'lo que estaba.')
        assert db.session.get(SesionConteo, s.id).estado == 'DESCUADRE'

    def test_el_mensaje_dice_que_puede_esperar(self):
        import pathlib
        src = pathlib.Path('app/services/conteo_service.py').read_text()
        assert 'siempre puede esperar' in src


class TestCNT07:
    """Lo que ya salió antes de la negación, y el día que alguien la deshaga."""

    def _sesion(self, db, sitio, **kw):
        base = dict(**sitio,
                    codigo=f'CC-07-{kw.get("n", 0)}', tipo='DIARIO_ABC',
                    estado='AJUSTADO', cantidad_fisica=10, existencia_siesa=7,
                    diferencia=3, siesa_triggered=True)
        base.update({k: v for k, v in kw.items() if k != 'n'})
        s = SesionConteo(**base)
        db.session.add(s)
        db.session.commit()
        return s

    def _res(self):
        from app.services import auditoria
        r = auditoria.auditar('conteo')
        return next(x for x in r['resultados'] if x['codigo'] == 'CNT-07')

    def test_un_ajuste_sobre_base_del_wms_bloquea(self, db, sitio):
        self._sesion(db, sitio, n=1, fuente_existencia='WMS')
        assert self._res()['total'] == 1

    def test_sobre_base_de_siesa_no(self, db, sitio):
        self._sesion(db, sitio, n=2, fuente_existencia='SIESA')
        assert self._res()['total'] == 0

    def test_el_historico_en_NULL_no_se_da_ni_por_bueno_ni_por_malo(self, db, sitio):
        """`NULL` es «no se sabe con qué se comparó», que es la verdad de todas
        las filas anteriores a la columna. Rellenarlas sería inventar la
        procedencia justo en el campo que existe para no inventarla."""
        self._sesion(db, sitio, n=3, fuente_existencia=None)
        assert self._res()['total'] == 0

    def test_sin_haber_disparado_a_siesa_no_aplica(self, db, sitio):
        self._sesion(db, sitio, n=4, fuente_existencia='WMS', siesa_triggered=False)
        assert self._res()['total'] == 0
