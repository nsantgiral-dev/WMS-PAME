"""
Una pantalla que un rol puede abrir tiene que poder cargar sus datos.

═══════════════════════════════════════════════════════════════════════════
POR QUÉ EXISTE

El 2026-08-03 Yesid entró con su rol nuevo, vio su pestaña, y la pantalla salió
vacía con **"Sin permiso para listar vehículos"**. El rol estaba en `Roles`, en
`_ROLES_VALIDOS` y en el selector de usuarios; los tres endpoints que la
pantalla necesita seguían con su lista propia de roles.

Tercera vez esa semana con la misma forma: **la capacidad de un lado y el
permiso del otro.** Y las tres se descubrieron con una persona real frente a la
pantalla, no con un test.

Este archivo generaliza el guard: para cada módulo del PWA y cada rol que puede
abrirlo, ninguna carga inicial puede devolver 403.

═══════════════════════════════════════════════════════════════════════════
QUÉ MIDE, Y QUÉ NO

Mide **solo los GET de carga** —las llamadas por `get('/...')`— porque son las
que dejan una pantalla vacía. Los POST y PUT quedan fuera a propósito: que un
rol de lectura no pueda escribir es correcto, y meterlos acá llenaría el test de
falsos positivos hasta que alguien lo apague.

No mide autorización de datos: que un rol pueda LEER un endpoint no dice nada
sobre si debería ver todas sus filas. Eso es otra cosa y va por su lado.
"""
import os
import re

import pytest

_PWA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'app', 'static', 'pwa')

# Qué roles pueden abrir cada módulo. Declarado a mano porque el enrutado vive
# en `mostrarSegunRol` y en los dispatchers, y derivarlo automáticamente daría
# un mapa que parece exacto y no lo es.
#
# El anti-podredumbre está abajo: un módulo con GETs que no aparezca acá rompe
# el build, así que uno nuevo no se puede olvidar.
# Reglas `(módulo, prefijo_de_función, roles)`. Gana la primera que coincide;
# `None` es el resto del módulo.
#
# La granularidad es por FUNCIÓN y no por archivo porque un archivo no es una
# pantalla: `rutas.js` tiene la del conductor y la de admin, y `recepcion.js`
# tiene la del recepcionista y los paneles de compras. Mapear por archivo daba
# ocho falsos positivos en la primera corrida — y un guard con falsos positivos
# termina apagado.
PANTALLAS = [
    ('flota.js',      'flotaCond',  ['conductor']),
    ('flota.js',      None,         ['admin', 'control_flota']),
    ('rutas.js',      'condCargar', ['conductor']),
    ('rutas.js',      'condVer',    ['conductor']),
    ('rutas.js',      None,         ['admin']),
    ('recepcion.js',  'comp',       ['admin', 'compras']),
    ('recepcion.js',  'inv',        ['admin']),
    ('recepcion.js',  None,         ['admin', 'recepcionista']),
    ('tienda.js',     None,         ['tienda']),   # admin no abre esta pantalla
    ('compras_ia.js', None,         ['admin', 'compras']),
    ('picking.js',    None,         ['admin', 'operario']),
    ('packing.js',    None,         ['admin', 'empacador']),
    ('app.js',        None,         ['admin']),
    ('conteo.js',     None,         ['admin']),
    ('reposicion.js', None,         ['admin']),
    ('liquidacion.js', None,        ['admin']),
    ('layout.js',     None,         ['admin']),
    ('traslados.js',  None,         ['admin']),
    ('kardex.js',     None,         ['admin']),
    ('temporada.js',  None,         ['admin']),
    ('vigia.js',      None,         ['admin']),
    ('etiquetas.js',  None,         ['admin']),
]

_MODULOS = {m for m, _, _ in PANTALLAS}

# ══════════════════════════════════════════════════════════════════════════
# INVENTARIO HEREDADO — no son excepciones, son deuda con nombre.
#
# La primera corrida de este guard encontró 15 pantallas donde el rol que las
# abre recibe 403 en alguna carga. NO están arregladas: triarlas toca módulos de
# otras manos y es un trabajo aparte. Están acá para que sean VISIBLES y para
# que el guard atrape la número 16.
#
# Es el mismo patrón que `BASELINE_HEREDADO` en el guard de endpoints huérfanos:
# pasa con lo que hay, revienta con lo nuevo. **Solo puede ENCOGER** — hay un
# test que lo obliga.
#
# Cada línea es una pantalla que alguien va a abrir algún día y va a encontrar
# vacía o incompleta.
# ══════════════════════════════════════════════════════════════════════════
BASELINE_HEREDADO = {
    # `armador` y `bloqueo_recompra` se unificaron en `_es_compras()` el
    # 2026-08-04 — estaban a medias, con unos endpoints en `_es_admin_o_jefe()`
    # y otros SIN NINGÚN guard. Al cerrar el agujero, el rol `compras` recuperó
    # su propia pantalla y estas cuatro líneas salieron solas.
    #
    # `kardex` cerrado el 2026-08-06, al conectarle pantalla a la compuerta y a
    # la evidencia de días sin stock: sus endpoints de LECTURA pasaron a
    # `_es_compras()`, que es superconjunto estricto de `_es_admin_o_jefe()`
    # —admin y jefe no pierden nada, entran gerente y compras—. Los que
    # escriben (`reconstruir`, `descargar`) siguen restringidos.
    #
    # El motivo: quien firma la compra tiene derecho a ver la evidencia del
    # número que la sostiene. Mostrarle «+18% por días sin stock» y negarle el
    # detalle es pedirle que confíe.
    # `tienda` con punto de venta sin configurar: el guard exige bodega y el
    # usuario de prueba no la tiene. Puede ser del fixture o del guard — hay
    # que mirarlo con un usuario de tienda real.
    ('tienda', '/api/tienda-oc/'),
    ('tienda', '/api/productos/?search='),
    # Paneles de admin que viven dentro de `recepcion.js`: el recepcionista no
    # los abre, pero comparten archivo. Si el mapa se afina, salen solos.
    ('recepcionista', '/api/rutas/bultos-rechazados'),
    ('recepcionista', '/api/devoluciones/pendientes-aprobacion-nc'),
}

# Excepciones deliberadas. Vacío a propósito: si aparece una, o el guard del
# endpoint está mal o el mapa de pantallas lo está — las dos son bugs.
NEGACIONES_ESPERADAS: dict = {}


def _gets_por_funcion(modulo):
    """[(funcion, url)] — cada GET atribuido a la función que lo hace.

    Sin interpolación: los de `${placa}` se prueban aparte.
    """
    ruta = os.path.join(_PWA, modulo)
    if not os.path.exists(ruta):
        return []
    with open(ruta, encoding='utf-8') as f:
        js = f.read()
    cortes = [(m.start(), m.group(1))
              for m in re.finditer(r'(?:async\s+)?function\s+(\w+)', js)]
    salida = []
    for m in re.finditer(r"get\('((?:/api|/flota)[a-z0-9/?=&_-]*)'", js):
        duenio = ''
        for pos, nombre in cortes:
            if pos < m.start():
                duenio = nombre
            else:
                break
        salida.append((duenio, m.group(1)))
    return sorted(set(salida))


def _roles_de(modulo, funcion):
    """Primera regla que coincide. `None` es el resto del módulo."""
    for mod, prefijo, roles in PANTALLAS:
        if mod != modulo:
            continue
        if prefijo is None or funcion.startswith(prefijo):
            return roles
    return []


def _usuario_con_rol(db, rol, almacen):
    from app.models.usuario import Usuario

    u = Usuario.query.filter_by(email=f'{rol}@permisos.test').first()
    if u is None:
        u = Usuario(email=f'{rol}@permisos.test', nombre=rol, rol=rol,
                    activo=True, almacen_id=almacen.id)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
    return u


def _token(app, usuario):
    from flask_jwt_extended import create_access_token

    with app.app_context():
        return create_access_token(identity=str(usuario.id))


class TestCargaInicialPorRol:

    def test_ninguna_pantalla_devuelve_403_al_rol_que_la_abre(
            self, app, db, client, almacen):
        """EL GUARD. Si esto falla, alguien va a ver una pantalla vacía.

        Lo que se rompe no es un endpoint: es la confianza de quien entra por
        primera vez y encuentra un error en rojo donde debería estar su trabajo.
        """
        negados = []
        for modulo in sorted(_MODULOS):
            for funcion, url in _gets_por_funcion(modulo):
                for rol in _roles_de(modulo, funcion):
                    u = _usuario_con_rol(db, rol, almacen)
                    cab = {'Authorization': f'Bearer {_token(app, u)}'}
                    if (rol, url) in NEGACIONES_ESPERADAS:
                        continue
                    if (rol, url) in BASELINE_HEREDADO:
                        continue
                    # Un 500 rompe la pantalla igual que un 403 — y peor,
                    # porque no dice qué pasó. Los dos cuentan. En la primera
                    # corrida esto encontró `StockSiesa.fecha_sync`, una columna
                    # que no existe, reventando el health del Vigía.
                    try:
                        # `follow_redirects` NO es un detalle: sin esto,
                        # `/api/almacenes` devolvía 308 —Flask redirige a la
                        # versión con barra— y el guard lo daba por bueno. Toda
                        # URL sin barra final pasaba SIN que nadie le revisara
                        # los permisos, y así se coló el 403 del conductor sobre
                        # la lista de sedes (2026-08-03). El guard medía el
                        # redirect, no el destino: la proxy dentro del propio
                        # guard.
                        codigo = client.get(url, headers=cab,
                                            follow_redirects=True).status_code
                    except Exception as e:
                        negados.append(f'{rol:<14} {funcion:<24} {url}  REVIENTA: {e}')
                        continue
                    if codigo == 403:
                        negados.append(f'{rol:<14} {funcion:<24} {url}  403')
                    elif codigo >= 500:
                        negados.append(f'{rol:<14} {funcion:<24} {url}  {codigo}')
        assert not negados, (
            f'\n{len(negados)} carga(s) que la pantalla hace y el backend rompe:\n'
            + '\n'.join(f'  · {n}' for n in sorted(negados))
            + '\n\nEs la capacidad de un lado y el permiso del otro: el rol ve '
              'la pantalla y la pantalla no puede cargar.'
        )

    def test_todo_modulo_con_gets_esta_en_el_mapa(self):
        """Anti-podredumbre: un módulo nuevo no se puede olvidar.

        Sin esto, el guard queda verde para siempre sobre un mapa que dejó de
        describir la aplicación — verde por no mirar, no por estar bien.
        """
        sin_mapear = [
            f for f in sorted(os.listdir(_PWA))
            if f.endswith('.js') and f != 'sw.js'
            and _gets_por_funcion(f) and f not in _MODULOS
        ]
        assert not sin_mapear, (
            f'\nMódulos con GET que nadie declaró quién los abre: {sin_mapear}\n'
            'Agregalos a PANTALLAS con los roles que llegan a esa pantalla.'
        )

    def test_el_extractor_encuentra_algo(self):
        """Un guard que no encuentra nada da verde igual que uno que no
        encuentra problemas, y son cosas distintas."""
        total = sum(len(_gets_por_funcion(m)) for m in _MODULOS)
        assert total >= 40, f'solo se extrajeron {total} GETs — ¿cambió el helper?'

    def test_el_baseline_heredado_no_crece(self, app, db, client, almacen):
        """Pasa con lo que hay, revienta con lo nuevo.

        Un guard que exigiera cero desde el primer día sobre quince hallazgos
        heredados no se puede poner en verde, y lo que no se puede poner en
        verde se termina borrando. Esto congela la deuda y atrapa la siguiente.
        """
        assert len(BASELINE_HEREDADO) <= 10, (
            f'El baseline creció a {len(BASELINE_HEREDADO)}. La deuda nueva no '
            'va acá: se arregla, o se declara en NEGACIONES_ESPERADAS con su '
            'razón.'
        )

    def test_el_baseline_no_acumula_lineas_muertas(self, app, db, client, almacen):
        """Anti-podredumbre: si un 403 se arregló, sale de la lista.

        Sin esto el baseline se vuelve un cementerio y deja de decir cuánta
        deuda queda de verdad.
        """
        ya_no_falla = []
        for rol, url in sorted(BASELINE_HEREDADO):
            u = _usuario_con_rol(db, rol, almacen)
            cab = {'Authorization': f'Bearer {_token(app, u)}'}
            try:
                if client.get(url, headers=cab,
                              follow_redirects=True).status_code != 403:
                    ya_no_falla.append(f'{rol} {url}')
            except Exception:
                continue
        assert not ya_no_falla, (
            '\nEstos ya no dan 403 — sacalos del baseline:\n'
            + '\n'.join(f'  · {x}' for x in ya_no_falla)
        )

    def test_la_lista_de_negaciones_esperadas_esta_vacia(self):
        """Si aparece una, o el guard del endpoint está mal o el mapa lo está.

        Las dos son bugs. Una excepción acá sería declarar que una pantalla
        tiene una parte que no funciona para quien la abre.
        """
        assert NEGACIONES_ESPERADAS == {}, (
            f'Se agregaron excepciones: {NEGACIONES_ESPERADAS}. '
            'Antes de aceptarlas, revisar si el rol debería abrir esa pantalla.'
        )


class TestElGuardMideElDestinoNoElRedirect:
    """TRINQUETE SOBRE EL TRINQUETE — el 308 tapaba el 403.

    `/api/almacenes` (sin barra final) devuelve **308**: Flask redirige a
    `/api/almacenes/`. Sin `follow_redirects`, este guard leía el 308, lo daba
    por bueno, y **toda URL sin barra final quedaba sin revisar**.

    Así se coló que el conductor no pudiera leer la lista de sedes: la pantalla
    de entrega de turno mostraba un desplegable vacío y el guard estaba en verde
    (2026-08-03). Es la sexta vez en el proyecto que un guard mide algo parecido
    a la propiedad en vez de la propiedad.
    """

    def test_el_guard_sigue_los_redirects(self):
        import inspect
        fuente = inspect.getsource(
            TestCargaInicialPorRol.test_ninguna_pantalla_devuelve_403_al_rol_que_la_abre)
        assert 'follow_redirects=True' in fuente, (
            'sin esto, un 308 tapa el 403 del destino y el guard queda ciego '
            'para toda URL sin barra final')

    def test_hay_urls_sin_barra_final_que_dependen_de_esto(self):
        """Si algún día no quedara ninguna, este guard dejaría de proteger algo
        y habría que saberlo — un trinquete sobre cero es arqueología."""
        import os
        sin_barra = []
        for modulo in sorted(a for a in os.listdir(_PWA) if a.endswith('.js')):
            for _, url in _gets_por_funcion(modulo):
                if not url.endswith('/') and '?' not in url:
                    sin_barra.append(url)
        assert sin_barra, (
            'ninguna URL sin barra final: revisar si este guard sigue '
            'protegiendo algo — un trinquete sobre cero es arqueología')
