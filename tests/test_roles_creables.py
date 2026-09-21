"""Todo rol que el backend acepta se tiene que poder crear desde la pantalla.

El 2026-09-21, de los 13 roles de `_ROLES_VALIDOS` el desplegable de Usuarios
ofrecía 11. Los dos ausentes no eran teóricos:

  · `gerente` — con autoridad en CINCO tuplas (`GESTION`, `DESPACHO`,
    `COMPRAS_ROLES`, `LECTURA_FLOTA`, `VISTA_FLOTA`). Es quien DECIDE en flota:
    cerrar un hallazgo, abrir y anular una orden de trabajo. No se podía crear
    desde ninguna pantalla.
  · `empacador` — en `PACKING_ROLES`.

La divergencia no daba ningún error. El backend los aceptaba por API, así que
la única forma de tener un gerente era un INSERT a mano — y un rol escrito a
mano con un typo crea un usuario que no puede nada y tampoco falla al crearse
(`Usuario.rol` es texto libre, sin CHECK en la base).

Se cruza en las dos direcciones: un rol que el backend no acepta tampoco puede
estar en la pantalla, o el formulario ofrece algo que el POST rechaza.
"""
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _ofrecidos():
    """Los `value` del `<select id="u-rol">`, leídos del JS que lo pinta."""
    js = (RAIZ / 'app' / 'static' / 'pwa' / 'app.js').read_text(encoding='utf-8')
    i = js.index('id="u-rol"')
    j = js.index('</select>', i)
    return set(re.findall(r'<option value="([a-z_]+)"', js[i:j]))


def _aceptados():
    from app.routes.auth import _ROLES_VALIDOS
    return set(_ROLES_VALIDOS)


def test_todo_rol_aceptado_se_puede_crear_desde_la_pantalla():
    faltan = sorted(_aceptados() - _ofrecidos())
    assert not faltan, (
        f'{faltan} los acepta el backend y NO están en el desplegable de '
        f'Usuarios. La única vía para crearlos es un INSERT a mano, y un rol '
        f'escrito a mano con un typo crea un usuario que no puede nada sin '
        f'fallar al crearse.')


def test_la_pantalla_no_ofrece_roles_que_el_backend_rechaza():
    """La otra dirección. Un rol en el desplegable que el POST rechaza es un
    formulario que falla después de que la persona llenó todo."""
    sobran = sorted(_ofrecidos() - _aceptados())
    assert not sobran, (
        f'{sobran} está en el desplegable y `_ROLES_VALIDOS` no lo acepta: '
        f'el registro va a fallar con 400 después de llenar el formulario.')


def test_el_detector_no_esta_ciego():
    """Si el `<select>` cambia de id o de forma, los dos tests de arriba
    comparan contra un conjunto vacío y pasan diciendo que no hay nada malo."""
    ofrecidos = _ofrecidos()
    assert len(ofrecidos) >= 12, (
        f'solo se detectaron {len(ofrecidos)} opciones de rol en el '
        f'desplegable. El lector se desincronizó del HTML y estos tests '
        f'dejaron de medir.')
    assert 'admin' in ofrecidos, 'el lector no encuentra ni el rol admin'


def test_los_roles_con_autoridad_en_flota_son_creables():
    """El caso concreto que lo destapó: sin `gerente` en la pantalla, el rol
    que el módulo de flota nombra como su autoridad máxima (`DECIDE_FLOTA`)
    no existía como opción."""
    from flota.api._permisos import DECIDE_FLOTA
    faltan = sorted(set(DECIDE_FLOTA) - _ofrecidos())
    assert not faltan, (
        f'{faltan} decide en flota —cerrar hallazgos, abrir y anular órdenes '
        f'de trabajo— y no se puede crear desde la pantalla de Usuarios.')
