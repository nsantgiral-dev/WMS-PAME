"""
Ningún endpoint escribe sin saber quién es el que escribe.

Auditoría del 2026-08-04, disparada por tres hallazgos del review automático.
El review nombró `compras`, `armador` y `bloqueo_recompra`; la auditoría del
repo entero encontró 28 rutas sin control de rol, seis de ellas de escritura.

La peor no estaba en la lista del review: **`vigia.cerrar_alarma`**. Cerrar una
alarma del CUSUM es silenciar el detector — y el endpoint tenía guard
anti-silencio (causa de 20 caracteres mínimo) y ninguno de QUIÉN. El autor pensó
en que no se cerrara sin explicación, no en quién podía cerrarla.

──────────────────────────────────────────────────────────────────────────────
NOTA SOBRE CÓMO SE MIDE, porque la primera versión de esta auditoría dio 134
rutas y era basura:

Buscaba el texto `'rol not in'` dentro de `ast.dump()`. Ahí `usuario.rol not in
Roles.ALMACEN` se representa como `Compare(..., ops=[NotIn()], ...)` — el texto
nunca aparece, así que endpoints correctamente protegidos salían como agujeros.
La segunda versión detectó el acceso a `.rol` pero no la delegación en helpers
locales (`_es_recepcion_autorizado`, `_verificar_rol_para_tipo`) y dio 45.

Esta versión busca la PROPIEDAD —¿esta función, o algo que llama, consulta un
rol?— con punto fijo sobre los helpers del módulo. 28. Cada iteración bajó el
número porque cada una medía mejor lo mismo.
──────────────────────────────────────────────────────────────────────────────
"""
import ast
from pathlib import Path

import pytest

_RUTAS = Path(__file__).resolve().parents[1] / 'app' / 'routes'

#: Helpers de `_auth_helpers` que consultan el rol. La semilla del punto fijo.
_BASE = {
    '_solo_admin', '_es_admin_o_jefe', '_es_gestion', '_es_personal_almacen',
    '_es_compras', '_es_control_flota', '_lee_flota', '_puede_empacar', 'exige',
}

# ══════════════════════════════════════════════════════════════════════════
# BASELINE — rutas SIN control de rol que hoy están así por diseño.
#
# Solo puede ENCOGER; hay un test que lo obliga. Cada línea lleva por qué.
# ══════════════════════════════════════════════════════════════════════════
_ABIERTAS_A_PROPOSITO = {
    # Público por diseño: el balanceador y el service worker lo consultan sin
    # sesión. Documentado en CLAUDE.md § Health Check.
    ('health.py', 'health_ping'),
    # Devuelve TU propio usuario. Pedir rol para saber tu rol es circular.
    ('auth.py', 'me'),
    # Catálogo de motivos de rechazo: lo necesita el CONDUCTOR, que es el rol
    # más bajo del sistema, y no expone nada del negocio — son siete etiquetas
    # fijas. Pedir rol acá dejaría al conductor sin poder registrar un rechazo,
    # que es justo lo que este catálogo vino a hacer más difícil de falsear.
    ('rutas.py', 'motivos_rechazo'),
    # Catálogo DANE de municipios: dato público, sin nada del negocio.
    ('rutas.py', 'listar_municipios'),
    # El stub 503 de flota cuando el módulo no importa. No toca datos.
    ('__init__.py', '_flota_no_disponible'),
    # Alcance propio: filtran por el usuario del token, así que el rol no
    # agrega nada — lo que devuelven ya es "lo tuyo".
    ('conteo.py', 'mis_tareas'),
    ('conteo.py', 'obtener_tarea'),
    ('picking.py', 'mis_tareas_activas'),
    ('traslados.py', 'mis_traslados'),
    # Consulta de catálogo para escanear: cualquier operario con sesión
    # necesita resolver un código de barras a un producto.
    ('siesa.py', 'buscar_producto'),
    ('kardex.py', 'estado_descarga'),
}


class _BuscaRol(ast.NodeVisitor):
    """¿Esta función **decide** con el rol? No: ¿lo menciona?

    ## La diferencia costó una auditoría entera

    La primera versión marcaba `tiene = True` con **cualquier** llamada a un
    guard, sin comprobar nunca que el resultado se usara. Con eso, degradar

        if not _es_compras():
            return jsonify(...), 403

    a una llamada suelta —`_es_compras()`, resultado descartado— dejaba el
    endpoint abierto a cualquier usuario autenticado **y el trinquete en
    verde**. Demostrado por mutación el 2026-08-19 sobre
    `routes/compras.py:576`: la suite completa pasó con el endpoint abierto.

    Es la misma forma que ya costó en el trinquete de rutas huérfanas —*medía
    presencia, no adyacencia*— y en el mapa bodega→CO. Un detector que busca
    que el nombre esté escrito no mide autorización: mide vocabulario.

    ## Qué se exige ahora

    Que el resultado del guard **participe en una decisión**: estar en el test
    de un `if`, en un `assert`, o pasar por `abort()`. La llamada suelta no
    cuenta, y `usuario.rol` tampoco si nadie lo compara.
    """

    def __init__(self, guards):
        self.tiene = False
        self.guards = guards
        #: Variables ligadas al resultado de un guard. `u = _es_compras()`
        #: seguido de `if not u:` es asignar-y-ramificar, que es tan válido
        #: como llamar dentro del `if` — y la primera versión de este detector
        #: estricto lo marcaba como endpoint sin rol. Un trinquete que manda a
        #: arreglar lo que no está roto se apaga, y apagado cuesta lo mismo
        #: que no existir.
        self.ligadas = set()

    def _es_llamada_a_guard(self, nodo) -> bool:
        for n in ast.walk(nodo):
            if isinstance(n, ast.Call):
                f = n.func
                nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', '')
                if nombre in self.guards:
                    return True
            if isinstance(n, ast.Attribute):
                if n.attr == 'rol':
                    return True
                if isinstance(n.value, ast.Name) and n.value.id == 'Roles':
                    return True
        return False

    def visit(self, nodo):
        """Antes de recorrer, se anotan las variables que llevan el resultado
        de un guard. Se hace en una pasada previa para no depender del orden
        del recorrido."""
        if not self.ligadas:
            for n in ast.walk(nodo):
                destinos = []
                if isinstance(n, ast.Assign):
                    destinos, valor = n.targets, n.value
                elif isinstance(n, ast.AnnAssign) and n.value is not None:
                    destinos, valor = [n.target], n.value
                elif isinstance(n, ast.Tuple):
                    continue
                else:
                    continue
                if not self._es_llamada_a_guard(valor):
                    continue
                for t in destinos:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            self.ligadas.add(sub.id)
        return super().visit(nodo)

    # ── lo que sí cuenta ────────────────────────────────────────────────
    def _menciona_rol(self, nodo) -> bool:
        """¿En este subárbol se consulta un rol, directo o por una variable
        que ya lo lleva?"""
        if self._es_llamada_a_guard(nodo):
            return True
        for n in ast.walk(nodo):
            if isinstance(n, ast.Name) and n.id in self.ligadas:
                return True
        return False

    def visit_If(self, n):
        # El caso normal: `if not _solo_admin(): return 403`
        if self._menciona_rol(n.test):
            self.tiene = True
        self.generic_visit(n)

    def visit_IfExp(self, n):
        if self._menciona_rol(n.test):
            self.tiene = True
        self.generic_visit(n)

    def visit_Assert(self, n):
        if self._menciona_rol(n.test):
            self.tiene = True
        self.generic_visit(n)

    def visit_Return(self, n):
        # `return jsonify(...), 403 if not _solo_admin() else 200` y la forma
        # `return _solo_admin() or abort(403)`.
        if n.value is not None and self._menciona_rol(n.value):
            self.tiene = True
        self.generic_visit(n)

    def visit_Call(self, n):
        f = n.func
        nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', '')
        if nombre in self.guards and nombre in _ABORTAN_SOLOS:
            self.tiene = True
        self.generic_visit(n)


#: Guards que no devuelven un booleano: cortan la petición ellos mismos. Para
#: éstos la llamada suelta SÍ es una decisión. La lista es explícita a
#: propósito — si se dedujera, volveríamos a contar vocabulario.
_ABORTAN_SOLOS = frozenset()


def _arboles():
    return {f.name: ast.parse(f.read_text(encoding='utf-8'))
            for f in sorted(_RUTAS.glob('*.py'))}


def _guards(arboles):
    """Punto fijo: un helper que consulta rol convierte en guard a quien lo llame.

    Sin esta transitividad la auditoría acusa a `mobile.confirmar_tarea`, que
    delega en `_verificar_rol_para_tipo()` — un helper local que sí verifica.
    """
    guards = set(_BASE)
    for _ in range(5):
        nuevos = set(guards)
        for arbol in arboles.values():
            for n in ast.walk(arbol):
                if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any('route' in ast.dump(d) for d in n.decorator_list):
                    continue    # una ruta no es un helper
                v = _BuscaRol(guards)
                v.visit(n)
                if v.tiene:
                    nuevos.add(n.name)
        if nuevos == guards:
            break
        guards = nuevos
    return guards


def _sin_rol():
    arboles = _arboles()
    guards = _guards(arboles)
    fuera = []
    for archivo, arbol in arboles.items():
        for n in ast.walk(arbol):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decs = n.decorator_list
            if not any('route' in ast.dump(d) for d in decs):
                continue
            v = _BuscaRol(guards)
            for d in decs:
                v.visit(d)
            v.visit(n)
            if v.tiene:
                continue
            metodos = [c.value for d in decs if isinstance(d, ast.Call)
                       for k in d.keywords if k.arg == 'methods'
                       for c in getattr(k.value, 'elts', [])]
            escribe = any(m in ('POST', 'PUT', 'PATCH', 'DELETE') for m in metodos)
            fuera.append((archivo, n.name, escribe))
    return fuera


class TestNingunEndpointSinRol:

    def test_ninguno_fuera_del_baseline(self):
        nuevos = [(a, f, e) for a, f, e in _sin_rol()
                  if (a, f) not in _ABIERTAS_A_PROPOSITO]
        assert not nuevos, (
            '\nEndpoints sin control de rol — cualquier usuario con sesión los '
            'puede llamar:\n'
            + '\n'.join(f'  · {"ESCRIBE " if e else "lee     "} {a}:{f}'
                        for a, f, e in nuevos)
            + '\n\nAgregar el guard del archivo, o justificar en '
              '_ABIERTAS_A_PROPOSITO con el motivo.')

    def test_ninguno_del_baseline_escribe(self):
        """La línea que no se cruza: **nada abierto puede escribir.**

        Leer un catálogo público es una decisión discutible. Escribir sin saber
        quién es, no.
        """
        escriben = [(a, f) for a, f, e in _sin_rol()
                    if e and (a, f) in _ABIERTAS_A_PROPOSITO
                    and (a, f) != ('__init__.py', '_flota_no_disponible')]
        assert not escriben, (
            f'\nRutas de ESCRITURA en el baseline de abiertas: {escriben}\n'
            'Eso no se justifica: se guarda o se quita.')

    def test_el_baseline_no_acumula_lineas_muertas(self):
        """Solo puede encoger. Una excepción que ya no aplica es arqueología."""
        reales = {(a, f) for a, f, _ in _sin_rol()}
        muertas = sorted(_ABIERTAS_A_PROPOSITO - reales)
        assert not muertas, (
            f'\nYa tienen guard — sacalas del baseline: {muertas}')

    def test_la_auditoria_no_esta_ciega(self):
        """Si el detector deja de encontrar rutas, todo lo de arriba pasa vacío.

        La primera versión de esta auditoría midió una proxy —texto dentro de
        `ast.dump`— y dio 134 falsos positivos. La segunda, 45. Un detector que
        deja de detectar da verde igual que uno que no encuentra problemas.
        """
        arboles = _arboles()
        rutas = sum(
            1 for arbol in arboles.values() for n in ast.walk(arbol)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any('route' in ast.dump(d) for d in n.decorator_list))
        assert rutas > 250, f'solo se detectaron {rutas} rutas de ~312'
        assert len(_guards(arboles)) > len(_BASE), (
            'el punto fijo no encontró ningún helper local: sin transitividad '
            'la auditoría acusa endpoints que sí verifican')


class TestLosQueSeCerraronHoy:
    """Los seis de escritura, por nombre. Si alguien los abre, se entera."""

    @pytest.mark.parametrize('archivo,funcion', [
        ('vigia.py', 'cerrar_alarma'),
        ('compras.py', 'crear_acuerdo'),
        ('compras.py', 'registrar_precio'),
        ('bloqueo_recompra.py', 'verificar_oc'),
        ('bloqueo_recompra.py', 'desbloquear'),
        ('armador.py', 'listar_contenedores'),
    ])
    def test_sigue_protegido(self, archivo, funcion):
        assert (archivo, funcion) not in {(a, f) for a, f, _ in _sin_rol()}, (
            f'{archivo}:{funcion} volvió a quedar sin control de rol')
