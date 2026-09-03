"""El bin de averías, probado sobre el escritor que CORRE.

`tests/test_bin_averias_del_escritor.py` dice en su cabecera que «no construye
la ubicación: llama al escritor real» — y llama a
`devolucion_service.confirmar_ubicacion`. Ese archivo declara
**`DEPRECATED (2026-07-28)`** en su primera línea y **no tiene un solo caller de
producción**: sus cuatro funciones dan cero en el grafo de llamadas.

El escritor vivo es `DevolucionClienteService._resolver_ubicacion`
(`routes/devoluciones.py` → `confirmar_entrada_fisica` → acá). Una auditoría del
2026-08-21 lo midió revirtiendo cada uno en memoria:

    escritor VIVO revertido a HEAD   -> 57 passed   (nadie lo mira)
    gemelo MUERTO revertido          ->  6 failed   (seis lo miran)

y `grep -rn "_resolver_ubicacion" tests/` devolvía **cero**.

Once tests protegiendo un módulo sin callers no protegen producción. Este
archivo cubre la vía viva, en las dos direcciones.

> El arreglo cae en la muerta y los tests no avisan, porque los de la muerta
> pasan.
"""
import pytest

from app.extensions import db
from app.models.almacen import Almacen
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.services.devolucion_cliente_service import (
    DevolucionClienteService,
    _UBICACION_AVERIADOS,
)
from app.services.picking_service import es_ubicacion_vendible


@pytest.fixture
def escenario(app, db):
    alm = Almacen(nombre='CD', codigo='CD', bodega_siesa_id='NB1')
    prod = Producto(codigo='SKU-AV', nombre='SKU averiable', codigo_siesa='SKU-AV')
    db.session.add_all([alm, prod])
    db.session.flush()
    return alm, prod


def _resolver(alm, prod, averiado=True):
    ub = DevolucionClienteService._resolver_ubicacion(prod.id, alm.id, averiado)
    db.session.flush()
    return ub


class TestElEscritorVivoMarcaElBin:
    def test_el_bin_nace_marcado_como_averias(self, app, escenario):
        """Lo que rompía: el bin nacía con `tipo_zona` en el default del modelo
        ('GENERAL', o sea VENDIBLE) y la mercancía que el recepcionista acababa
        de declarar averiada entraba al FEFO de pedidos de cliente."""
        alm, prod = escenario
        ub = _resolver(alm, prod)

        assert ub.codigo == _UBICACION_AVERIADOS
        assert es_ubicacion_vendible(ub) is False, (
            'el escritor VIVO creó el bin de averías marcado como vendible')

    def test_el_bin_ya_existente_sin_marcar_se_repara(self, app, escenario):
        """La red que el docstring de `m016` promete para justificar su `WHERE`
        estrecho: «el propio servicio la vuelve a corregir la próxima vez que
        alguien confirme una avería ahí».

        Esa rama se escribió el 2026-08-20 **en el gemelo muerto**, así que la
        promesa no existía en el camino vivo. Una base que no corrió m016 —o una
        copia— seguía despachando esta mercancía a clientes."""
        alm, prod = escenario
        viejo = Ubicacion(codigo=_UBICACION_AVERIADOS, almacen_id=alm.id,
                          zona='CUARENTENA', tipo='cuarentena', activo=True)
        db.session.add(viejo)
        db.session.flush()
        assert es_ubicacion_vendible(viejo) is True, 'el escenario no reproduce el defecto'

        ub = _resolver(alm, prod)

        assert ub.id == viejo.id, 'creó uno nuevo en vez de reparar el que había'
        assert es_ubicacion_vendible(ub) is False, (
            'el bin mal marcado siguió siendo vendible — la mercancía averiada '
            'sigue saliendo al FEFO')

    def test_el_segundo_llamado_reusa_el_mismo_bin(self, app, escenario):
        """Find-or-create: no debe multiplicar bins por cada devolución."""
        alm, prod = escenario
        primero = _resolver(alm, prod)
        segundo = _resolver(alm, prod)

        assert primero.id == segundo.id
        assert Ubicacion.query.filter_by(
            codigo=_UBICACION_AVERIADOS, almacen_id=alm.id).count() == 1


class TestNoDisparaSobreLoQueEstaSano:
    """Detector en las dos direcciones. Marcar de más saca del FEFO stock
    vendible — ése es el error caro en la dirección contraria, y no lo cubre
    ningún assert de arriba."""

    def test_una_devolucion_no_averiada_no_va_al_bin_de_averias(self, app, escenario):
        alm, prod = escenario
        db.session.add(Ubicacion(codigo='PIK-02-A', almacen_id=alm.id,
                                 zona='PICKING', tipo='estanteria', activo=True))
        db.session.flush()

        ub = _resolver(alm, prod, averiado=False)

        assert ub.codigo != _UBICACION_AVERIADOS
        assert es_ubicacion_vendible(ub) is True, (
            'una devolución en buen estado terminó en zona no vendible')

    def test_no_toca_una_ubicacion_vendible_de_otro_codigo(self, app, escenario):
        """La reparación es estrecha a propósito: solo el bin de averías de este
        servicio. Una estantería normal no se marca aunque esté en el almacén."""
        alm, prod = escenario
        estante = Ubicacion(codigo='PIK-01-A', almacen_id=alm.id,
                            zona='PICKING', tipo='estanteria', activo=True)
        db.session.add(estante)
        db.session.flush()

        _resolver(alm, prod)
        db.session.refresh(estante)

        assert es_ubicacion_vendible(estante) is True, (
            'la reparación marcó una ubicación vendible que no era el bin de averías')

    def test_un_bin_de_averias_ya_bien_marcado_no_se_reescribe(self, app, escenario):
        alm, prod = escenario
        bueno = _resolver(alm, prod)
        marcado_antes = (bueno.tipo_zona, bueno.zona, bueno.tipo)

        de_nuevo = _resolver(alm, prod)

        assert (de_nuevo.tipo_zona, de_nuevo.zona, de_nuevo.tipo) == marcado_antes


class TestElEscritorVivoEsEsteYNoElOtro:
    """El trinquete contra la reincidencia. No mide el marcado —eso es lo de
    arriba— sino **a quién apuntan los tests**: si mañana alguien vuelve a
    arreglar solo el gemelo muerto, este archivo lo sigue viendo porque llama a
    la función que producción llama."""

    def test_el_gemelo_sigue_sin_callers_de_produccion(self, app):
        """Si `devolucion_service` recupera un caller vivo, esta afirmación deja
        de valer y hay que revisar cuál de los dos es el escritor real."""
        import ast
        import pathlib

        raiz = pathlib.Path(__file__).resolve().parents[1] / 'app'
        callers = []
        for ruta in raiz.rglob('*.py'):
            if ruta.name == 'devolucion_service.py':
                continue
            try:
                arbol = ast.parse(ruta.read_text())
            except SyntaxError:                     # pragma: no cover
                continue
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Call):
                    fuente = ast.unparse(nodo.func)
                    if 'confirmar_ubicacion' in fuente:
                        callers.append(f'{ruta.name}:{nodo.lineno}')

        assert not callers, (
            f'`devolucion_service.confirmar_ubicacion` recuperó callers: {callers}. '
            'Dejó de ser el gemelo muerto — revisar cuál es el escritor vivo antes '
            'de confiar en este archivo.')
