"""
Quién puede escribir en flota. Hasta el 2026-08-03: cualquiera con sesión.

Todos los endpoints del módulo llevaban `@jwt_required()` y **nada más**. Un
usuario de tienda o un empacador podía sobrescribir el `km_inicial` de un
vehículo —el ancla contra la que se valida todo el histórico de odómetro—,
declarar un SOAT vigente con la fecha que quisiera, o tomar la custodia de
cualquier camión.

Lo encontró el review automático. **Ni los tests ni una tarde de uso real lo
habrían encontrado:** nadie intenta sobrescribir una ficha desde una cuenta de
tienda, y ningún test afirmaba que no se pudiera. Es la contracara exacta de la
lección del mismo día — la persona encuentra lo que nadie pensó en afirmar; el
review encuentra lo que nadie iba a intentar.

La ironía que lo vuelve evidente: `/flota/health`, que es **solo lectura**, sí
tenía control de rol desde el primer día.
"""
import ast
from pathlib import Path

import pytest

from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA

_API = Path(__file__).resolve().parents[2] / 'flota' / 'api'
_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


def _token(app, db, rol, email=None):
    from flask_jwt_extended import create_access_token

    from app.models.usuario import Usuario

    u = Usuario(email=email or f'{rol}-perm@x.com', nombre=rol, rol=rol, activo=True)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    with app.app_context():
        return create_access_token(identity=str(u.id))


@pytest.fixture
def vehiculo(db):
    from app.models.vehiculo import Vehiculo

    v = Vehiculo(placa='PRM100', tipo='Turbo', activo=True)
    db.session.add(v)
    db.session.commit()
    return v.placa


# ── TRINQUETE: ninguna ruta puede quedar solo con @jwt_required ─────────────

class TestTodaRutaDeFlotaTieneControlDeRol:
    """El guard general, no los tres casos que encontró el review.

    Un endpoint nuevo que se agregue mañana con solo `@jwt_required()` falla
    acá. Sin esto, el arreglo cubre lo que ya existe y el próximo repite el
    agujero — que es como aparecieron estos.
    """

    @staticmethod
    def _rutas_sin_exige():
        malas = []
        for archivo in sorted(_API.glob('*.py')):
            arbol = ast.parse(archivo.read_text(encoding='utf-8'))
            for nodo in ast.walk(arbol):
                if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                nombres = []
                for d in nodo.decorator_list:
                    f = d.func if isinstance(d, ast.Call) else d
                    nombres.append(
                        f.attr if isinstance(f, ast.Attribute) else
                        getattr(f, 'id', ''))
                es_ruta = 'route' in nombres
                # `exige_secreto` es para lo que llama una MÁQUINA: un webhook
                # de proveedor no puede llevar JWT porque quien lo invoca no es
                # un usuario. No afloja el trinquete — una ruta sin ninguno de
                # los dos sigue fallando, y el guard de secreto nace cerrado
                # (503 sin la variable) en vez de abierto.
                if es_ruta and not {'exige', 'exige_secreto'} & set(nombres):
                    # `health` usa su propio guard, anterior a este módulo.
                    if '_es_control_flota' in ast.dump(nodo):
                        continue
                    malas.append(f'{archivo.name}:{nodo.name}')
        return malas

    def test_ninguna_ruta_queda_solo_con_sesion(self):
        malas = self._rutas_sin_exige()
        assert not malas, (
            '\nRutas de flota sin control de rol — cualquier usuario con sesión '
            'las puede llamar:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nAgregar @exige(...) debajo de @jwt_required().')

    def test_el_extractor_encuentra_rutas(self):
        """Si el AST deja de encontrarlas, el test de arriba pasa vacío."""
        rutas = sum(
            1
            for archivo in _API.glob('*.py')
            for nodo in ast.walk(ast.parse(archivo.read_text(encoding='utf-8')))
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any('route' in ast.dump(d) for d in nodo.decorator_list)
        )
        assert rutas >= 10, f'solo se detectaron {rutas} rutas'


# ── Los tres agujeros concretos que encontró el review ─────────────────────

class TestLosMaestrosNoLosToquiaCualquiera:

    def test_tienda_no_puede_sobrescribir_la_ficha(self, app, db, client, vehiculo):
        """`km_inicial` es el ancla de todo el módulo.

        Sobrescribirlo corrompe la validación de cada lectura posterior, y no
        hay forma de detectarlo después: el histórico queda coherente con el
        ancla equivocada.
        """
        t = _token(app, db, 'tienda')
        r = client.put(f'/flota/vehiculo/{vehiculo}/ficha',
                       json={'km_inicial': 1, 'posiciones_llanta': 6},
                       headers=_auth(t))
        assert r.status_code == 403

    def test_un_empacador_no_puede_declarar_un_soat_vigente(
            self, app, db, client, vehiculo):
        """Un documento de compliance declarado por quien no lo verificó es
        evidencia falsa frente a una autoridad de tránsito."""
        t = _token(app, db, 'empacador')
        r = client.post(f'/flota/vehiculo/{vehiculo}/documentos',
                        json={'tipo': 'soat', 'estado': 'vigente',
                              'numero': 'X', 'entidad': 'Y',
                              'fecha_expedicion': '2026-01-01',
                              'fecha_vencimiento': '2027-01-01'},
                        headers=_auth(t))
        assert r.status_code == 403

    def test_el_conductor_tampoco_toca_los_maestros(self, app, db, client, vehiculo):
        """Y este es el que no es obvio.

        El conductor opera el turno, pero la ficha es levantamiento de campo
        (FLO-PR-01, trabajo de control de flota). Dejarle escribir `km_inicial`
        sería darle la llave del dato que después lo respalda a él mismo.
        """
        t = _token(app, db, 'conductor')
        r = client.put(f'/flota/vehiculo/{vehiculo}/ficha',
                       json={'km_inicial': 1, 'posiciones_llanta': 6},
                       headers=_auth(t))
        assert r.status_code == 403

    def test_control_flota_si_puede_cargar_la_ficha(self, app, db, client, vehiculo):
        """Es su trabajo: si no puede, el procedimiento no se puede ejecutar."""
        t = _token(app, db, 'control_flota')
        r = client.get(f'/flota/vehiculo/{vehiculo}/ficha', headers=_auth(t))
        assert r.status_code != 403

    def test_control_flota_no_esta_en_gestion_pero_si_en_maestros(self):
        assert Roles.CONTROL_FLOTA not in Roles.GESTION
        assert Roles.CONTROL_FLOTA in MAESTROS_FLOTA
        assert Roles.CONDUCTOR not in MAESTROS_FLOTA


class TestLaOperacionDelTurnoSigueAbiertaAlConductor:
    """El arreglo no puede haber cerrado la puerta principal.

    Si el conductor no puede recibir su turno, el módulo entero deja de
    funcionar y nadie lo nota hasta las 5 a.m.
    """

    @pytest.mark.parametrize('ruta', ['/flota/custodia/activa/PRM100'])
    def test_el_conductor_lee_lo_de_su_turno(self, app, db, client, vehiculo, ruta):
        """**Lo de SU turno**, y la lista se acortó el 2026-09-03.

        Hasta ese día este test parametrizaba además `/flota/custodia/fuera-de-sede`
        y `/flota/custodia/cierres-forzados`, que son de la FLOTA ENTERA. El
        nombre del test decía «lo de su turno» y dos de sus tres rutas no lo
        eran: el test afirmaba con su nombre una cosa y medía otra.

        Que un conductor pudiera leerlas no lo vio ningún trinquete, porque
        todos miden **presencia** de `exige(...)`, o que el rol declarado entre
        y el no declarado no — ninguno pregunta si el rol declarado DEBERÍA
        estar en la tupla. Lo encontró un detector nuevo que cruza los roles que
        el endpoint autoriza contra los roles que pueden llegar al botón.
        """
        t = _token(app, db, 'conductor')
        assert client.get(ruta, headers=_auth(t)).status_code != 403

    @pytest.mark.parametrize('ruta', [
        '/flota/custodia/fuera-de-sede',
        '/flota/custodia/cierres-forzados',
        '/flota/avisos',
    ])
    def test_el_conductor_NO_lee_la_flota_entera(self, app, db, client,
                                                 vehiculo, ruta):
        """La otra mitad, que faltaba: el permiso no puede ser más ancho que el
        gesto que la pantalla ofrece.

        `cierres-forzados` es el caso que lo vuelve concreto — el propio módulo
        lo describe como *«mide conducta, no fallas»*. Un conductor no tiene por
        qué leer el registro de conducta de sus compañeros, y ninguna pantalla
        se lo ofrecía: la API contestaba igual.
        """
        t = _token(app, db, 'conductor')
        r = client.get(ruta, headers=_auth(t))
        assert r.status_code == 403, (
            f'{ruta} le contestó {r.status_code} a un conductor')
        assert r.get_json()['tu_rol'] == 'conductor'

    def test_pero_control_de_flota_SI_la_lee(self, app, db, client, vehiculo):
        """Y la dirección contraria: estrechar no puede haber cerrado la puerta
        de quien sí tiene que ver la flota entera."""
        t = _token(app, db, 'control_flota')
        for ruta in ('/flota/custodia/fuera-de-sede',
                     '/flota/custodia/cierres-forzados', '/flota/avisos'):
            assert client.get(ruta, headers=_auth(t)).status_code != 403, ruta

    def test_el_conductor_registra_odometro(self, app, db, client, vehiculo):
        t = _token(app, db, 'conductor')
        r = client.post('/flota/odometro',
                        json={'placa': vehiculo, 'valor_km': 100,
                              'origen': 'tanqueo'},
                        headers=_auth(t))
        assert r.status_code != 403

    def test_un_operario_ajeno_a_flota_no_entra(self, app, db, client, vehiculo):
        t = _token(app, db, 'operario')
        r = client.get(f'/flota/custodia/activa/{vehiculo}', headers=_auth(t))
        assert r.status_code == 403

    def test_el_403_dice_que_hace_falta(self, app, db, client, vehiculo):
        """Un "sin permiso" pelado termina en un WhatsApp al desarrollador."""
        t = _token(app, db, 'operario')
        cuerpo = client.get(f'/flota/custodia/activa/{vehiculo}',
                            headers=_auth(t)).get_json()
        assert cuerpo['tu_rol'] == 'operario'
        assert cuerpo['roles_permitidos']


class TestElGuardDeSecretoNoEsUnaPuertaAbierta:
    """`exige_secreto` existe para webhooks, no para saltarse el trinquete.

    Si fuera un decorador que solo marca la ruta como "exenta", habría cambiado
    un endpoint sin control por uno con la apariencia de control — que es peor,
    porque el trinquete queda en verde.
    """

    def test_sin_la_variable_responde_503_y_no_ejecuta(self, client, monkeypatch):
        monkeypatch.delenv('FLOTA_AVISO_WEBHOOK_TOKEN', raising=False)
        r = client.post('/flota/avisos/entrega',
                        json={'payload': {'gsId': 'x', 'type': 'delivered'}})
        assert r.status_code == 503, (
            'nace cerrado: un webhook que se abre porque falta configuración '
            'invierte la regla 10 — lo peligroso pasaría cuando alguien NO hizo algo')

    def test_sin_token_en_la_url_rechaza(self, client, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        assert client.post('/flota/avisos/entrega', json={}).status_code == 403

    def test_con_token_equivocado_rechaza(self, client, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        assert client.post('/flota/avisos/entrega?token=x',
                           json={}).status_code == 403

    def test_con_el_token_correcto_entra(self, app, db, client, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        r = client.post('/flota/avisos/entrega?token=secreto',
                        json={'payload': {'gsId': 'x', 'type': 'delivered'}})
        assert r.status_code == 200

    def test_el_trinquete_sigue_atrapando_una_ruta_sin_ningun_guard(self):
        """Se ejerce contra código sintético: si el reconocimiento del guard
        nuevo hubiera vuelto permisivo al extractor, esto lo delata."""
        import ast as _ast

        fuente = (
            "@bp.route('/x', methods=['POST'])\n"
            "@jwt_required()\n"
            "def sin_guard():\n"
            "    return {}\n"
        )
        nodo = _ast.parse(fuente).body[0]
        nombres = []
        for d in nodo.decorator_list:
            f = d.func if isinstance(d, _ast.Call) else d
            nombres.append(f.attr if isinstance(f, _ast.Attribute)
                           else getattr(f, 'id', ''))
        assert 'route' in nombres
        assert not {'exige', 'exige_secreto'} & set(nombres)


def _roles_de(vf):
    """La tupla que `@exige(...)` recibió, del closure del decorador.

    Del objeto vivo y no del AST: importa con qué tupla quedó montada la ruta,
    no qué nombre se escribió en el archivo.
    """
    fn = vf
    for _ in range(12):
        celdas = getattr(fn, '__closure__', None) or ()
        nombres = getattr(getattr(fn, '__code__', None), 'co_freevars', ())
        d = {}
        for n, c in zip(nombres, celdas):
            try:
                d[n] = c.cell_contents
            except ValueError:      # celda vacía — recursión aún sin cerrar
                pass
        if 'roles' in d:
            return tuple(d['roles'])
        fn = getattr(fn, '__wrapped__', None)
        if fn is None:
            return None
    return None


class TestElPermisoNoPuedeSerMasAnchoQueElGesto:
    """TRINQUETE — la tercera forma, y ningún guard anterior la veía.

    Los que ya existen miden:

      · `test_permisos_flota` — **presencia** de `exige(...)`. Proxy: afirma que
        hay decorador, no que la tupla sea la correcta.
      · `test_matriz_roles_endpoints` — que el rol declarado entre y el no
        declarado reciba 403. Verifica que el decorador FUNCIONE, no que su
        tupla esté BIEN.

    Ninguno pregunta **si el rol declarado debería estar ahí**. Por eso, hasta
    el 2026-09-03, cualquier conductor podía consultar los avisos de toda la
    flota, dónde duerme cada camión, y `cierres-forzados` — que el propio módulo
    describe como *«mide conducta, no fallas»*. Ninguna pantalla se lo ofrecía;
    la API contestaba igual.

    La regla que se impone acá: **si un rol puede llamar a un endpoint, alguna
    pantalla suya tiene que ofrecerle ese gesto.** Un permiso que ninguna vista
    ejerce es superficie que nadie usa y alguien puede.
    """

    #: Endpoints que un rol autoriza sin que su pantalla los ofrezca, con el
    #: motivo. **Vacía a propósito**: cada entrada que se agregue es una
    #: decisión que alguien tiene que poder defender por escrito.
    _ANCHOS_ACEPTADOS: dict = {}

    def _funciones_del_pwa(self):
        import re

        import pathlib as _pl
        js = (_pl.Path(__file__).resolve().parents[2]
              / 'app' / 'static' / 'pwa' / 'flota.js').read_text(encoding='utf-8')
        funcs = {}
        for m in re.finditer(r'^(?:async )?function (\w+)', js, re.M):
            prof, i = 0, js.find('{', m.end())
            while i < len(js):
                if js[i] == '{':
                    prof += 1
                elif js[i] == '}':
                    prof -= 1
                    if prof == 0:
                        break
                i += 1
            funcs[m.group(1)] = js[m.start():i + 1]
        return js, funcs

    def _alcanzable_por_el_conductor(self, fn, funcs, visto=None):
        """Colgada del bloque `flotaCond*`, directa o transitivamente."""
        import re

        visto = visto or set()
        if fn in visto:
            return False
        visto.add(fn)
        if fn.startswith('flotaCond'):
            return True
        for otra, cuerpo in funcs.items():
            if otra != fn and re.search(rf'\b{re.escape(fn)}\s*\(', cuerpo):
                if self._alcanzable_por_el_conductor(otra, funcs, visto):
                    return True
        return False

    def test_ningun_endpoint_autoriza_al_conductor_sin_ofrecerselo(self, app):
        import re

        from app.routes._auth_helpers import Roles

        js, funcs = self._funciones_del_pwa()
        anchos = []
        for r in app.url_map.iter_rules():
            ruta = str(r.rule)
            if not ruta.startswith('/flota') or ruta in self._ANCHOS_ACEPTADOS:
                continue
            roles = _roles_de(app.view_functions[r.endpoint])
            if not roles or Roles.CONDUCTOR not in roles:
                continue

            trozo = [s for s in re.split(r'<[^>]+>', ruta)
                     if s.strip('/')][-1].rstrip('/')
            # Las URL izadas a constantes de módulo no viven dentro de ninguna
            # función: buscarlas solo en los cuerpos declara huérfano lo que
            # está bien cableado. Es el punto ciego que ya costó un falso
            # positivo — se resuelve la constante a su nombre.
            nombres = [m.group(1) for m in re.finditer(
                rf"const (\w+)\s*=\s*'{re.escape(trozo)}'", js)]
            llamadoras = [f for f, c in funcs.items()
                          if trozo in c or any(n in c for n in nombres)]
            if not any(self._alcanzable_por_el_conductor(f, funcs)
                       for f in llamadoras):
                anchos.append(ruta)

        assert not anchos, (
            '\n'.join(f'  · {a}' for a in sorted(anchos))
            + '\n\nEstos endpoints autorizan al conductor y ninguna pantalla '
              'suya se los ofrece. Dos salidas, y hay que elegir una: dale el '
              'botón, o sacale el rol de la tupla. Si hay una razón para dejarlo '
              'así, va a `_ANCHOS_ACEPTADOS` CON SU MOTIVO — la lista nace vacía '
              'porque cada entrada es una decisión que alguien tiene que poder '
              'defender.')

    def test_el_detector_ve_un_permiso_ancho_de_verdad(self, app):
        """La otra dirección. Un detector que no sabe reconocer la forma
        devuelve lista vacía sobre un sistema abierto, y eso se lee como «está
        todo bien» — que es exactamente cómo los otros dos guards no lo vieron.
        """
        js, funcs = self._funciones_del_pwa()
        assert self._alcanzable_por_el_conductor('flotaCondInspeccion', funcs)
        assert not self._alcanzable_por_el_conductor('flotaAbrirFicha', funcs), (
            'la ficha se abre desde el panel del encargado; si el detector la '
            'da por alcanzable desde el conductor, no distingue nada')


class TestLaUIYElBackendDicenLoMismo:
    """La lista de roles que decide está escrita **dos veces**: en Python y en
    `flota.js`. No hay un `/me` que devuelva permisos, así que la pantalla no
    tiene de dónde derivarla — y una segunda fuente sería igual de inventada.

    La duplicación se acepta y se vigila. Es el corolario de la regla 0 del WMS
    («una política, una función») aplicado al caso en que la función no se puede
    unificar: si no se puede tener una sola copia, hay que tener un test que
    falle cuando las dos dejen de decir lo mismo.

    Lo que este trinquete NO es: control de acceso. Ese vive en `@exige` y lo
    ejerce por HTTP la matriz rol × endpoint. Acá solo se compara texto.
    """

    #: Dónde vive la copia del cliente.
    _JS = 'app/static/pwa/flota.js'

    def _lista_del_js(self):
        import pathlib as _p
        import re as _re

        fuente = _p.Path(self._JS).read_text(encoding='utf-8')
        m = _re.search(r'const FLOTA_ROLES_DECIDEN = \[([^\]]*)\]', fuente)
        assert m, ('no existe `FLOTA_ROLES_DECIDEN` en flota.js. Si se renombró, '
                   'este trinquete dejó de comparar nada — arreglar el nombre '
                   'acá, no borrar el test.')
        return tuple(sorted(_re.findall(r"'([a-z_]+)'", m.group(1))))

    def test_la_lista_del_js_es_exactamente_DECIDE_FLOTA(self):
        from flota.api._permisos import DECIDE_FLOTA

        assert self._lista_del_js() == tuple(sorted(DECIDE_FLOTA)), (
            'la pantalla y el backend no dicen lo mismo sobre quién decide. '
            'La que manda es la del backend; la que la gente ve es la otra.')

    def test_control_de_flota_NO_esta_en_ninguna_de_las_dos(self):
        """La afirmación concreta, escrita aparte del `==`.

        Un `DECIDE_FLOTA` que alguien ensanchara «para destrabar» seguiría
        pasando el test de arriba mientras las dos copias crecieran juntas.
        """
        from app.routes._auth_helpers import Roles
        from flota.api._permisos import DECIDE_FLOTA

        assert Roles.CONTROL_FLOTA not in DECIDE_FLOTA
        assert Roles.CONTROL_FLOTA not in self._lista_del_js()
        assert Roles.CONDUCTOR not in DECIDE_FLOTA

    def test_pero_SIGUE_en_MAESTROS_porque_el_registro_es_suyo(self):
        """El otro borde. Un recorte que se pasara de largo dejaría al rol sin
        poder hacer su trabajo, y eso no se nota hasta que alguien lo intenta —
        el mismo motivo por el que existen las dos direcciones en la matriz."""
        from app.routes._auth_helpers import Roles
        from flota.api._permisos import MAESTROS_FLOTA

        assert Roles.CONTROL_FLOTA in MAESTROS_FLOTA
