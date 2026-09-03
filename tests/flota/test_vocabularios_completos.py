"""
Todos los valores de todos los vocabularios, por las tres vías que tienen que
decir lo mismo: el dominio, el CHECK de la base y la pantalla.

Cada agente que tocó este módulo eligió **sus** dos valores. Nadie recorrió un
vocabulario entero, y menos los tres lados a la vez. Los dos modos de falla son
distintos y los dos son caros:

  · un valor que el dominio acepta y la base rechaza es un **500 esperando** —
    el usuario ve la pantalla fea justo con el dato nuevo;
  · un valor que la base acepta y la pantalla no ofrece es **superficie muerta**:
    columna, CHECK y código para algo que nadie puede producir.

Nada se escribe a mano. Los ejes salen de `db.metadata`, del AST de
`flota/adaptadores/modelos.py`, de los `Enum` de `flota/dominio/` y de los
`<option>` de `flota.js`. El día que alguien agregue un valor a un solo lado, la
celda nueva aparece sola — que es la forma de `_BODEGA_CO_MAP` en el CLAUDE.md:
mientras el detector lleve escrita a mano la lista de lo que revisa, lo nuevo no
entra.
"""
import ast
import re
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

_RAIZ = Path(__file__).resolve().parents[2]
_MODELOS = _RAIZ / 'flota' / 'adaptadores' / 'modelos.py'
_JS = _RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'
_H = 'Authorization'
_HASH = generate_password_hash('x')


# ── Los ejes, por introspección ─────────────────────────────────────────────

def _checks_de_la_base():
    """`{(tabla, columna): valores}` leído de los CHECK `IN (...)` de metadata.

    De `db.metadata` y no del archivo: lo que importa es el CHECK que se va a
    crear, no el literal que alguien escribió. Un `_en(...)` que no llegue a
    `__table_args__` no está en ninguna base.
    """
    from sqlalchemy import CheckConstraint

    from app.extensions import db
    salida = {}
    for tabla in db.metadata.sorted_tables:
        if not tabla.name.startswith('flota'):
            continue
        for c in tabla.constraints:
            if not isinstance(c, CheckConstraint):
                continue
            m = re.match(r"^\s*(\w+)\s+IN\s+\((.*)\)\s*$", str(c.sqltext),
                         re.I | re.S)
            if not m:
                continue
            valores = tuple(v.strip().strip("'") for v in m.group(2).split(','))
            # Un CHECK compuesto («estado IN (...) OR otra_cosa») no es un
            # vocabulario cerrado: se descarta en vez de leerlo a medias.
            if any(' ' in v or "'" in v for v in valores):
                continue
            salida[(tabla.name, m.group(1))] = valores
    return salida


def _en_declarados():
    """`{(tabla, columna): nombre_de_la_tupla}` por cada `_en('col', VOCAB)`.

    Por AST sobre el archivo: es la única forma de saber **con qué nombre** se
    armó cada CHECK, y ese nombre es el que hay que cruzar contra el dominio.

    La clave lleva la tabla porque `tipo`, `origen`, `estado` y `criticidad` se
    repiten en varios modelos con vocabularios distintos. Indexar solo por
    columna hacía que el plan preventivo pisara a la orden de trabajo y el
    cruce comparara `TIPO_TAREA` contra el CHECK de `flota_orden_trabajo` — un
    detector que se contradice solo y se lee como un defecto del código.
    """
    arbol = ast.parse(_MODELOS.read_text(encoding='utf-8'))
    salida = {}
    for clase in ast.walk(arbol):
        if not isinstance(clase, ast.ClassDef):
            continue
        tabla = next(
            (n.value.value for n in clase.body
             if isinstance(n, ast.Assign)
             and any(getattr(t, 'id', '') == '__tablename__' for t in n.targets)
             and isinstance(n.value, ast.Constant)),
            None)
        if tabla is None:
            continue
        for nodo in ast.walk(clase):
            if (isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Name) and nodo.func.id == '_en'
                    and len(nodo.args) == 2
                    and isinstance(nodo.args[0], ast.Constant)
                    and isinstance(nodo.args[1], ast.Name)):
                salida[(tabla, nodo.args[0].value)] = nodo.args[1].id
    return salida


def _enums_del_dominio():
    """`{NOMBRE_EN_MAYUSCULAS: valores}` por cada `Enum` de `flota/dominio/`."""
    import importlib
    import pkgutil
    from enum import Enum

    import flota.dominio as dominio

    salida = {}
    for _, nombre, _ in pkgutil.iter_modules(dominio.__path__):
        modulo = importlib.import_module(f'flota.dominio.{nombre}')
        for n, obj in vars(modulo).items():
            if (isinstance(obj, type) and issubclass(obj, Enum)
                    and obj.__module__ == modulo.__name__):
                mayus = re.sub(r'(?<!^)(?=[A-Z])', '_', n).upper()
                salida[mayus] = tuple(x.value for x in obj)
    return salida


def _grupos_de_opciones_del_js():
    """Cada bloque de `<option value="…">` contiguo en `flota.js`, con su línea.

    Solo los **literales**. Las listas que el servidor manda (`d.sistemas`,
    `FLOTA_TALLER.categorias`) no pueden divergir por construcción y no hacen
    falta acá; las que están escritas en el JS sí, y son las que este cruce
    persigue.
    """
    grupos, actual, inicio = [], [], None
    for n, linea in enumerate(_JS.read_text(encoding='utf-8').split('\n'), 1):
        encontrados = re.findall(r'<option value="([a-z_]+)"', linea)
        if encontrados:
            if not actual:
                inicio = n
            actual += encontrados
        else:
            if len(actual) >= 2:
                grupos.append((inicio, tuple(actual)))
            actual = []
    if len(actual) >= 2:
        grupos.append((inicio, tuple(actual)))
    return grupos


def _opciones_de_ficha_del_js():
    """`FLOTA_OPCIONES`: los desplegables de la ficha técnica, campo por campo."""
    fuente = _JS.read_text(encoding='utf-8')
    bloque = re.search(r'const FLOTA_OPCIONES = \{(.*?)\n\};', fuente, re.S)
    assert bloque, 'FLOTA_OPCIONES desapareció de flota.js'
    return {
        campo: tuple(v.strip().strip("'")
                     for v in valores.split(',') if v.strip())
        for campo, valores in re.findall(r"(\w+):\s*\[([^\]]*)\]",
                                         bloque.group(1))
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Los tres lados dicen lo mismo
# ═══════════════════════════════════════════════════════════════════════════

class TestElTamanoDelEspacio:
    """El denominador tiene que ser visible: un cruce que encogió no avisa."""

    def test_hay_vocabularios_cerrados_de_sobra(self, app):
        with app.app_context():
            checks = _checks_de_la_base()
        assert len(checks) >= 35, f'solo {len(checks)} CHECK de pertenencia'
        assert sum(len(v) for v in checks.values()) >= 130, (
            'el total de valores del módulo encogió')

    def test_el_extractor_del_modelo_encuentra_los_en(self):
        assert len(_en_declarados()) >= 35

    def test_el_extractor_de_la_pantalla_encuentra_opciones(self):
        assert len(_grupos_de_opciones_del_js()) >= 6
        assert len(_opciones_de_ficha_del_js()) >= 7


class TestElDominioYLaBaseDicenLoMismo:
    """Un enum de Python que la base no conoce es una convención, no una
    garantía — y al revés es un 500 esperando."""

    @pytest.mark.parametrize('nombre', sorted(_enums_del_dominio()))
    def test_todo_enum_del_dominio_que_se_persiste_tiene_el_mismo_CHECK(
            self, app, nombre):
        """Se cruza por NOMBRE (`CustodioTipo` → `CUSTODIO_TIPO`), no por
        solape de valores: dos vocabularios distintos pueden compartir la
        palabra `sede` sin ser el mismo, y un cruce por solape los confundiría.

        Un enum sin columna —`QuienPide`, que no se guarda— no falla: se salta,
        porque no tener CHECK es correcto para lo que no se persiste.
        """
        from flota.adaptadores import modelos

        del_dominio = _enums_del_dominio()[nombre]
        del_modelo = getattr(modelos, nombre, None)
        if del_modelo is None:
            pytest.skip(f'{nombre} no se persiste: no hay columna que restringir')
        assert tuple(del_modelo) == del_dominio, (
            f'`flota.dominio` dice {del_dominio} y `modelos.{nombre}` dice '
            f'{tuple(del_modelo)}. La copia que gana es la del CHECK.')

    def test_todo_en_del_modelo_llega_a_metadata(self, app):
        """Un `_en(...)` escrito y no colgado de `__table_args__` no existe."""
        from flota.adaptadores import modelos

        with app.app_context():
            checks = _checks_de_la_base()
        faltan = []
        for clave, tupla in _en_declarados().items():
            esperado = tuple(getattr(modelos, tupla))
            if clave not in checks:
                faltan.append(f'{clave[0]}.{clave[1]} (declarado con {tupla})')
            elif tuple(checks[clave]) != esperado:
                faltan.append(
                    f'{clave[0]}.{clave[1]}: la base dice {checks[clave]} y '
                    f'{tupla} dice {esperado}')
        assert not faltan, (
            '\nCHECK declarados que no llegan a la base o llegan distintos:\n'
            + '\n'.join(f'  · {f}' for f in faltan))


class TestLaPantallaNoOfreceLoQueLaBaseRechaza:
    """La dirección que produce el 500: una opción que el CHECK no conoce.

    Se compara cada grupo literal de `<option>` contra el CHECK que lo contiene.
    El emparejamiento es por contenido y no por una tabla escrita a mano: se
    busca el vocabulario del que el grupo es subconjunto.
    """

    @staticmethod
    def _vocabulario_de(valores, checks):
        candidatos = [(k, v) for k, v in checks.items()
                      if set(valores) <= set(v)]
        # Con varios candidatos gana el más ajustado: `('si','no','sin_dato')`
        # es subconjunto de dos columnas distintas y la más chica es la suya.
        return min(candidatos, key=lambda kv: len(kv[1]), default=None)

    @pytest.mark.parametrize('grupo', _grupos_de_opciones_del_js(),
                             ids=lambda g: f'flota.js:{g[0]}')
    def test_todo_valor_de_la_pantalla_lo_acepta_la_base(self, app, grupo):
        linea, valores = grupo
        with app.app_context():
            checks = _checks_de_la_base()
        conocidos = set()
        for v in checks.values():
            conocidos |= set(v)
        # También cuentan los vocabularios que no son columna (los ángulos de
        # foto viven en su propio CHECK; los orígenes sueltos son un subconjunto).
        huerfanos = [v for v in valores if v not in conocidos]
        assert not huerfanos, (
            f'flota.js:{linea} ofrece {huerfanos}, que ningún CHECK de flota '
            f'acepta: el POST devuelve 500 o 409 en cuanto alguien lo elija')

    @pytest.mark.parametrize('campo', sorted(_opciones_de_ficha_del_js()))
    def test_el_desplegable_de_la_ficha_ofrece_el_vocabulario_entero(
            self, app, campo):
        """Y en este la otra dirección también importa.

        Un valor que la base acepta y el desplegable no ofrece es superficie
        muerta: nadie lo puede escribir desde la pantalla, y la columna guarda
        un vocabulario más ancho que la operación real.
        """
        with app.app_context():
            checks = _checks_de_la_base()
        del_check = next((v for (t, c), v in checks.items()
                          if c == campo and t == 'flota_ficha_tecnica'), None)
        assert del_check is not None, f'{campo} no tiene CHECK en la ficha'
        assert set(_opciones_de_ficha_del_js()[campo]) == set(del_check), (
            f'{campo}: la pantalla ofrece '
            f'{sorted(_opciones_de_ficha_del_js()[campo])} y la base acepta '
            f'{sorted(del_check)}')

    def test_el_primer_valor_de_cada_desplegable_es_sin_dato(self):
        """Regla 1: ningún ítem tiene valor por defecto.

        El primero de un `<select>` es el que queda marcado si nadie toca nada.
        Si fuera `gasolina`, una ficha sin levantar quedaría afirmando un
        combustible que nadie miró.
        """
        malos = [c for c, v in _opciones_de_ficha_del_js().items()
                 if v[0] != 'sin_dato']
        assert not malos, f'desplegables cuyo default no es «sin_dato»: {malos}'


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Todos los valores, por la vía real (HTTP)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def admin(app, db):
    from app.models.usuario import Usuario

    u = Usuario(email='vocab-admin@x.com', nombre='a', rol='admin',
                activo=True, password_hash=_HASH)
    db.session.add(u)
    db.session.commit()
    return {_H: f'Bearer {create_access_token(identity=str(u.id))}'}


@pytest.fixture
def conductor(app, db):
    from app.models.usuario import Usuario

    u = Usuario(email='vocab-cond@x.com', nombre='c', rol='conductor',
                activo=True, password_hash=_HASH)
    db.session.add(u)
    db.session.commit()
    return {_H: f'Bearer {create_access_token(identity=str(u.id))}'}


@pytest.fixture
def placa(db):
    from app.models.vehiculo import Vehiculo

    db.session.add(Vehiculo(placa='VOC100', tipo='Turbo', activo=True))
    db.session.commit()
    return 'VOC100'


def _vocabulario(nombre):
    from flota.adaptadores import modelos

    return tuple(getattr(modelos, nombre))


class TestLaFichaAceptaSuVocabularioEntero:
    """Los siete campos de vocabulario cerrado de la ficha, valor por valor.

    Son 29 valores. Cada agente probó `sin_dato` y uno más.
    """

    #: Campos cuyo CHECK está **acoplado** a su procedencia: declarar el dato
    #: sin decir de dónde salió lo rechaza la base
    #: (`ck_flota_frenos_con_procedencia`, `ck_flota_distribucion_con_procedencia`).
    #: No es un obstáculo del test: es la regla, y el barrido tiene que
    #: respetarla para poder ejercer el vocabulario en vez de chocar contra ella.
    _PAREJA = {'sistema_frenos': 'frenos_fuente',
               'distribucion': 'distribucion_fuente'}

    #: El caso INVERSO, y la asimetría que lo hace necesario.
    #:
    #: Los CHECK de frenos y distribución son de **una sola dirección**: prohíben
    #: «dato sin procedencia» y dejan pasar una procedencia colgada, porque
    #: `distribucion = 'sin_dato' OR distribucion_fuente <> 'sin_dato'` ya es
    #: verdadero con el dato en `sin_dato`.
    #:
    #: El de la capacidad del tanque (m018) es **bidireccional**: exige el par
    #: completo o los dos vacíos. Es más estricto —y mejor: una procedencia sin
    #: número no dice nada de nada— pero significa que mandar solo la fuente lo
    #: rechaza la base, y el barrido tiene que acompañarla con su número.
    #:
    #: Lo destapó agregar la capacidad al PUT el 2026-09-03: el test empezó a
    #: fallar con «lo ofrece la pantalla y la API lo rechaza», que es
    #: exactamente lo que este test existe para decir.
    _PAREJA_INVERSA = {'capacidad_tanque_fuente': ('capacidad_tanque_galones', 15)}

    @pytest.mark.parametrize('campo,valor', [
        (c, v) for c, vs in _opciones_de_ficha_del_js().items() for v in vs])
    def test_cada_valor_del_desplegable_entra_de_verdad(
            self, client, db, admin, placa, campo, valor):
        from flota.adaptadores.modelos import FichaTecnica

        cuerpo = {'posiciones_llanta': 6, 'km_inicial': 1000, campo: valor}
        pareja = self._PAREJA.get(campo)
        if pareja and valor != 'sin_dato':
            cuerpo[pareja] = 'manual_fabricante'
        inversa = self._PAREJA_INVERSA.get(campo)
        if inversa and valor != 'sin_dato':
            cuerpo[inversa[0]] = inversa[1]
        r = client.put(f'/flota/vehiculo/{placa}/ficha', json=cuerpo,
                       headers=admin)
        assert r.status_code in (200, 201), (
            f'{campo}={valor} lo ofrece la pantalla y la API lo rechaza: '
            f'{r.status_code} {r.get_json()}')
        assert getattr(FichaTecnica.query.one(), campo) == valor

    @pytest.mark.parametrize('campo,pareja', sorted(_PAREJA.items()))
    def test_el_dato_sin_su_procedencia_no_entra(
            self, client, db, admin, placa, campo, pareja):
        """La otra dirección del CHECK acoplado, ejercida.

        Un sistema de frenos declarado sin decir de dónde salió es tradición
        oral con formato de columna — y de ahí sale una tarea de seguridad.
        """
        from flota.adaptadores.modelos import FichaTecnica

        valor = next(v for v in _opciones_de_ficha_del_js()[campo]
                     if v != 'sin_dato')
        r = client.put(f'/flota/vehiculo/{placa}/ficha',
                       json={'posiciones_llanta': 6, 'km_inicial': 1000,
                             campo: valor, pareja: 'sin_dato'},
                       headers=admin)
        assert r.status_code == 409, (
            f'{campo}={valor} sin procedencia entró con {r.status_code}')
        assert FichaTecnica.query.first() is None

    @pytest.mark.parametrize('campo', sorted(_opciones_de_ficha_del_js()))
    def test_un_valor_inventado_falla_ruidosamente(
            self, client, db, admin, placa, campo):
        """Regla 5: o funciona, o falla ruidosamente. **Nunca degrada.**

        Lo que no puede pasar es que devuelva 2xx y guarde otra cosa —o nada—,
        que es como una ficha queda afirmando un dato que nadie escribió.
        """
        from flota.adaptadores.modelos import FichaTecnica

        r = client.put(f'/flota/vehiculo/{placa}/ficha',
                       json={'posiciones_llanta': 6, 'km_inicial': 1000,
                             campo: 'valor_que_no_existe'},
                       headers=admin)
        assert r.status_code >= 400, (
            f'{campo}=«valor_que_no_existe» devolvió {r.status_code}')
        assert FichaTecnica.query.first() is None or getattr(
            FichaTecnica.query.first(), campo) != 'valor_que_no_existe'


class TestLosDocumentosAceptanSuVocabularioEntero:

    @pytest.mark.parametrize('estado', _vocabulario('ESTADO_DOCUMENTO'))
    @pytest.mark.parametrize('tipo', _vocabulario('TIPO_DOCUMENTO'))
    def test_cada_tipo_en_cada_estado(self, client, db, admin, placa,
                                      tipo, estado):
        """4 tipos × 2 estados = 8 celdas. `TIPOS_DOCUMENTO` ya se cerró una vez
        en cuatro y los cuatro que el dueño pidió después dieron 400 — el
        vocabulario entero se ejerce para que el próximo se note al agregarlo.
        """
        r = client.post(f'/flota/vehiculo/{placa}/documentos',
                        json={'tipo': tipo, 'estado': estado,
                              'numero': 'X', 'entidad': 'Y',
                              'fecha_expedicion': '2026-01-01',
                              'fecha_vencimiento': '2027-01-01'},
                        headers=admin)
        assert r.status_code in (200, 201), (
            f'{tipo}/{estado}: {r.status_code} {r.get_json()}')

    @pytest.mark.parametrize('tipo', _vocabulario('TIPO_DOCUMENTO'))
    def test_el_vencimiento_lo_decide_el_tipo_y_no_el_cliente(
            self, client, db, admin, placa, tipo):
        """Un tipo sin vencimiento no puede guardar uno ni mandándolo."""
        from flota.dominio.valores import exige_vencimiento

        client.post(f'/flota/vehiculo/{placa}/documentos',
                    json={'tipo': tipo, 'estado': 'vigente',
                          'numero': 'X', 'entidad': 'Y',
                          'fecha_expedicion': '2026-01-01',
                          'fecha_vencimiento': '2027-01-01'},
                    headers=admin)
        cuerpo = client.get(f'/flota/vehiculo/{placa}/documentos',
                            headers=admin).get_json()
        doc = next(d for d in cuerpo['documentos'] if d['tipo'] == tipo)
        assert (doc['fecha_vencimiento'] is not None) == exige_vencimiento(tipo)

    def test_un_tipo_inventado_falla_ruidosamente(self, client, db, admin, placa):
        r = client.post(f'/flota/vehiculo/{placa}/documentos',
                        json={'tipo': 'permiso_lunar', 'estado': 'vigente'},
                        headers=admin)
        assert 400 <= r.status_code < 500, r.status_code


class TestElDineroAceptaSuVocabularioEntero:
    """13 categorías × 4 orígenes de costo, más los 3 estados de tanque.

    El vocabulario de gastos es el más ancho del módulo y el que más
    consecuencias tiene: de esta tabla sale el CPK.
    """

    @pytest.fixture
    def con_lectura(self, db, placa):
        """Un gasto de escritorio exige que el vehículo tenga odómetro."""
        from datetime import datetime

        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import LecturaOdometro

        v = Vehiculo.query.filter_by(placa=placa).one()
        db.session.add(LecturaOdometro(
            vehiculo_id=v.id, valor_km=1000,
            ts=datetime(2026, 8, 15, 13, 0), origen='cierre_dia',
            autor_usuario_id=1, confianza='declarada'))
        db.session.commit()
        return placa

    @pytest.mark.parametrize('origen', _vocabulario('ORIGEN_COSTO'))
    @pytest.mark.parametrize(
        'categoria', [c for c in _vocabulario('CATEGORIA_GASTO')
                      if c != 'combustible'])
    def test_cada_categoria_con_cada_origen_de_costo(
            self, client, db, admin, con_lectura, categoria, origen):
        from flota.adaptadores import gastos as adaptador
        from flota.dominio import costos

        cuerpo = {'placa': con_lectura, 'categoria': categoria,
                  'fecha': '2026-08-15', 'valor': 100000,
                  'proveedor': 'P', 'origen_costo': origen,
                  'descripcion': 'lo que fue'}
        if costos.exige_periodo(categoria):
            cuerpo['periodo_desde'] = '2026-01-01'
            cuerpo['periodo_hasta'] = '2026-12-31'
        # `es_de_campo` y no `CATEGORIAS_DE_CAMPO`: son dos listas distintas y
        # miden dos cosas distintas. La primera dice «ocurre con el vehículo
        # delante, así que lleva kilometraje» (regla 3); la segunda, «la puede
        # registrar el conductor» (autoridad). `mantenimiento` es de campo y NO
        # es del conductor — confundirlas manda el barrido contra un 409.
        if adaptador.es_de_campo(categoria):
            cuerpo['km'] = 1200
        r = client.post('/flota/gastos', json=cuerpo, headers=admin)
        assert r.status_code == 201, (
            f'{categoria}/{origen}: {r.status_code} {r.get_json()}')

    @pytest.mark.parametrize('origen', _vocabulario('ORIGEN_COSTO'))
    def test_el_combustible_no_entra_por_la_puerta_del_gasto_generico(
            self, client, db, admin, con_lectura, origen):
        """La categoría número catorce del barrido, y su celda es un rechazo.

        `combustible` está en `CATEGORIAS_GASTO` y **no** se registra por
        `/flota/gastos`: sin galones, estación y estado del tanque el gasto
        entraría al CPK sin aportar un solo galón al rendimiento. Se ejerce el
        rechazo en vez de sacar la categoría del barrido — una celda excluida
        sin afirmar nada es una celda que nadie volvió a mirar.
        """
        from flota.adaptadores.modelos import Gasto

        r = client.post('/flota/gastos',
                        json={'placa': con_lectura, 'categoria': 'combustible',
                              'fecha': '2026-08-15', 'valor': 100000,
                              'proveedor': 'P', 'origen_costo': origen,
                              'km': 1200},
                        headers=admin)
        assert r.status_code == 400, f'entró con {r.status_code}'
        assert Gasto.query.count() == 0

    @pytest.mark.parametrize('origen', _vocabulario('ORIGEN_COSTO'))
    @pytest.mark.parametrize('tanque', _vocabulario('ESTADO_TANQUE'))
    def test_cada_estado_de_tanque_con_cada_origen(
            self, client, db, conductor, con_lectura, tanque, origen):
        """Y por la puerta del **conductor**, que es quien tanquea."""
        r = client.post('/flota/tanqueos',
                        json={'placa': con_lectura, 'fecha': '2026-08-15',
                              'valor': 200000, 'galones': 10, 'tanque': tanque,
                              'estacion': 'E', 'km': 1300, 'proveedor': 'P',
                              'origen_costo': origen},
                        headers=conductor)
        assert r.status_code == 201, (
            f'tanque={tanque}/origen={origen}: {r.status_code} {r.get_json()}')

    @pytest.mark.parametrize('campo,malo', [
        ('categoria', 'sobornos'),
        ('origen_costo', 'trueque'),
    ])
    def test_un_valor_inventado_falla_ruidosamente(
            self, client, db, admin, con_lectura, campo, malo):
        from flota.adaptadores.modelos import Gasto

        cuerpo = {'placa': con_lectura, 'categoria': 'lavado',
                  'fecha': '2026-08-15', 'valor': 100000, 'proveedor': 'P',
                  'origen_costo': 'tarjeta_convenio'}
        cuerpo[campo] = malo
        r = client.post('/flota/gastos', json=cuerpo, headers=admin)
        assert 400 <= r.status_code < 500, (
            f'{campo}={malo} devolvió {r.status_code} — un 500 le muestra la '
            f'traza al usuario y un 2xx guarda basura')
        assert Gasto.query.count() == 0, 'quedó escrito pese al rechazo'

    def test_un_tanque_inventado_no_produce_una_ventana_de_rendimiento(
            self, client, db, conductor, con_lectura):
        """`tanque` decide si el rendimiento existe. Un valor raro degradado a
        `lleno` inventaría una ventana lleno-a-lleno."""
        from flota.adaptadores.modelos import Tanqueo

        r = client.post('/flota/tanqueos',
                        json={'placa': con_lectura, 'fecha': '2026-08-15',
                              'valor': 200000, 'galones': 10,
                              'tanque': 'casi_lleno', 'estacion': 'E',
                              'km': 1300, 'proveedor': 'P',
                              'origen_costo': 'sin_dato'},
                        headers=conductor)
        assert 400 <= r.status_code < 500, r.status_code
        assert Tanqueo.query.count() == 0


class TestElHallazgoYLaCustodiaAceptanSuVocabularioEntero:

    @pytest.mark.parametrize('criticidad', _vocabulario('CRITICIDAD'))
    def test_cada_criticidad_nace_con_su_plazo(
            self, client, db, conductor, placa, criticidad):
        from flota.adaptadores.modelos import Hallazgo, DIAS_DE_PLAZO

        r = client.post('/flota/hallazgos',
                        json={'placa': placa, 'criticidad': criticidad,
                              'descripcion': 'algo suena', 'km': 1000},
                        headers=conductor)
        assert r.status_code == 201, f'{criticidad}: {r.get_json()}'
        assert Hallazgo.query.one().fecha_limite is not None
        assert criticidad in DIAS_DE_PLAZO

    def test_una_criticidad_inventada_no_se_degrada_a_menor(
            self, client, db, conductor, placa):
        """Un `.get(criticidad, 'menor')` convertiría un bloqueante mal escrito
        en treinta días de plazo. Regla 5 y regla 6 a la vez."""
        from flota.adaptadores.modelos import Hallazgo

        r = client.post('/flota/hallazgos',
                        json={'placa': placa, 'criticidad': 'gravisimo',
                              'descripcion': 'x', 'km': 1000},
                        headers=conductor)
        assert 400 <= r.status_code < 500, r.status_code
        assert Hallazgo.query.count() == 0

    @pytest.mark.parametrize('ubicacion', _vocabulario('UBICACION'))
    def test_cada_ubicacion_exige_su_custodio(
            self, client, db, conductor, placa, ubicacion):
        """El vocabulario entero de `Ubicacion` contra la función que traduce
        ubicación → custodio. Las tres, no las dos de siempre."""
        from flota.dominio.valores import Ubicacion, custodio_de_ubicacion

        exigido = custodio_de_ubicacion(Ubicacion(ubicacion)).value
        # Una custodia lleva **exactamente un** custodio; el campo que se llena
        # depende del tipo, y eso también es parte del vocabulario que se ejerce.
        cuerpo = {'placa': placa, 'km': 1000, 'custodio_tipo': exigido,
                  'ubicacion': ubicacion, 'ubicacion_motivo': 'porque sí'}
        cuerpo['custodio_conductor_id' if exigido == 'conductor'
               else 'custodio_sede_id'] = 1
        r = client.post('/flota/custodia/traspaso', json=cuerpo,
                        headers=conductor)
        assert r.status_code == 201, f'{ubicacion}: {r.get_json()}'

    @pytest.mark.parametrize('ubicacion', _vocabulario('UBICACION'))
    def test_la_combinacion_imposible_se_rechaza_en_las_tres(
            self, client, db, conductor, placa, ubicacion):
        """La otra dirección: un custodio que no le corresponde a esa ubicación
        descargaría de responsabilidad a quien sí tiene el vehículo."""
        from flota.dominio.valores import Ubicacion, custodio_de_ubicacion

        exigido = custodio_de_ubicacion(Ubicacion(ubicacion)).value
        otro = next(t for t in _vocabulario('CUSTODIO_TIPO') if t != exigido)
        r = client.post('/flota/custodia/traspaso',
                        json={'placa': placa, 'km': 1000,
                              'custodio_tipo': otro, 'ubicacion': ubicacion},
                        headers=conductor)
        assert r.status_code == 400, (
            f'{ubicacion} + custodio «{otro}» entró con {r.status_code}')

    @pytest.mark.parametrize('campo,malo', [
        ('custodio_tipo', 'nadie'),
        ('ubicacion', 'la_luna'),
        ('custodio_estado', 'quizas'),
    ])
    def test_un_valor_inventado_del_traspaso_falla_ruidosamente(
            self, client, db, conductor, placa, campo, malo):
        from flota.adaptadores.modelos import Custodia

        cuerpo = {'placa': placa, 'km': 1000, 'custodio_tipo': 'conductor',
                  'custodio_conductor_id': 1}
        cuerpo[campo] = malo
        r = client.post('/flota/custodia/traspaso', json=cuerpo,
                        headers=conductor)
        assert 400 <= r.status_code < 500, f'{campo}={malo} → {r.status_code}'
        assert Custodia.query.count() == 0


class TestElOdometroAceptaSuVocabularioEntero:

    @pytest.mark.parametrize('origen', [o.value for o in __import__(
        'flota.dominio.valores', fromlist=['x']).OrigenLectura])
    def test_todo_origen_del_enum_entra_o_explica_por_que_no(
            self, client, db, conductor, placa, origen):
        """Los **siete**, no los tres habilitados.

        Un origen excluido tiene que dar 400 **con su motivo escrito**: el mapa
        `MOTIVO_ORIGEN_NO_SUELTO` es total sobre el complemento y la frontera lo
        indexa directo. Si alguien agrega un valor al enum sin su motivo, esto
        cae en `KeyError` acá y no en producción.
        """
        from flota.dominio.valores import ORIGENES_LECTURA_SUELTA

        suelto = origen in [o.value for o in ORIGENES_LECTURA_SUELTA]
        cuerpo = {'placa': placa, 'valor_km': 1000, 'origen': origen}
        if origen == 'correccion':
            cuerpo['motivo_correccion'] = 'se digitó mal'
        r = client.post('/flota/odometro', json=cuerpo, headers=conductor)
        if suelto:
            assert r.status_code == 201, f'{origen}: {r.get_json()}'
        else:
            assert r.status_code == 400, f'{origen} entró con {r.status_code}'
            assert r.get_json()['motivo'], (
                f'{origen} se rechaza sin decir por qué')

    @pytest.mark.parametrize('confianza', _vocabulario('CONFIANZA'))
    def test_ninguna_lectura_nace_verificada_venga_como_venga(
            self, client, db, conductor, placa, confianza):
        """`verificada` no la escribe ningún automatismo, ni el cliente.

        Se manda el vocabulario entero en el cuerpo: si la frontera copiara el
        campo, la marca que afirma que **una persona miró** se podría poner
        sola. Es la regla 11 — la forma de maximizarla sin hacer el trabajo.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        r = client.post('/flota/odometro',
                        json={'placa': placa, 'valor_km': 1000 + 10,
                              'origen': 'cierre_dia', 'confianza': confianza},
                        headers=conductor)
        assert r.status_code == 201
        assert LecturaOdometro.query.one().confianza != 'verificada'


class TestElTallerAceptaSuVocabularioEntero:

    @pytest.fixture
    def orden(self, client, db, admin, placa):
        r = client.post('/flota/ordenes',
                        json={'placa': placa, 'tipo': 'correctiva',
                              'km': 1000, 'taller': 'T',
                              'descripcion': 'entra'},
                        headers=admin)
        assert r.status_code == 201, r.get_json()
        return r.get_json()['id']

    @pytest.mark.parametrize('tipo', _vocabulario('TIPO_OT'))
    def test_cada_tipo_de_orden(self, client, db, admin, placa, tipo):
        r = client.post('/flota/ordenes',
                        json={'placa': placa, 'tipo': tipo, 'km': 1000,
                              'taller': 'T', 'descripcion': 'entra'},
                        headers=admin)
        assert r.status_code == 201, f'{tipo}: {r.get_json()}'

    @pytest.mark.parametrize('garantia', _vocabulario('GARANTIA_DECL'))
    @pytest.mark.parametrize('sistema', _vocabulario('SISTEMA'))
    def test_cada_sistema_con_cada_declaracion_de_garantia(
            self, client, db, admin, orden, sistema, garantia):
        """13 sistemas × 3 respuestas de garantía = 39 celdas."""
        cuerpo = {'sistema': sistema, 'descripcion': 'se hizo',
                  'garantia_declarada': garantia}
        if garantia == 'si':
            cuerpo['garantia_meses'] = 6
        r = client.post(f'/flota/ordenes/{orden}/intervenciones',
                        json=cuerpo, headers=admin)
        assert r.status_code == 201, (
            f'{sistema}/{garantia}: {r.status_code} {r.get_json()}')

    @pytest.mark.parametrize('campo,malo', [
        ('sistema', 'karma'), ('garantia_declarada', 'puede_ser')])
    def test_un_valor_inventado_falla_ruidosamente(
            self, client, db, admin, orden, campo, malo):
        from flota.adaptadores.modelos import Intervencion

        cuerpo = {'sistema': 'motor', 'descripcion': 'x',
                  'garantia_declarada': 'no'}
        cuerpo[campo] = malo
        r = client.post(f'/flota/ordenes/{orden}/intervenciones',
                        json=cuerpo, headers=admin)
        assert 400 <= r.status_code < 500, f'{campo}={malo} → {r.status_code}'
        assert Intervencion.query.count() == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3 · El vocabulario que la base acepta y ninguna puerta escribe
# ═══════════════════════════════════════════════════════════════════════════

class TestNingunaColumnaDeLaFichaSeQuedaSinPuerta:
    """Superficie muerta: columna, CHECK y detector para un dato que **no se
    puede escribir por ninguna vía viva**.

    Es la otra mitad del cruce de tres lados, y la que no produce ningún error:
    la base acepta el valor, el dominio sabe qué hacer con él, y no hay gesto
    que lo produzca. La regla 12 del módulo y `funcion-sin-caller` a la vez.
    """

    def test_la_lista_de_editables_no_encogio(self):
        from flota.api.ficha import _EDITABLES

        assert len(_EDITABLES) >= 21

    # xfail retirado el 2026-09-03: el PUT ya acepta la capacidad
    # y su procedencia (`_EDITABLES` en flota/api/ficha.py). Lo avisó
    # el propio `strict=True`, que falla cuando un xfail empieza a
    # pasar — un marcador que sobrevive a su defecto es deuda falsa.
    def test_toda_columna_de_la_ficha_tiene_forma_de_escribirse(self, app):
        from flota.adaptadores.modelos import FichaTecnica
        from flota.api.ficha import _EDITABLES

        columnas = {c.name for c in FichaTecnica.__table__.columns}
        sin_puerta = sorted(columnas - set(_EDITABLES) - {'vehiculo_id'})
        assert not sin_puerta, (
            'columnas de la ficha que ninguna petición puede escribir: '
            + ', '.join(sin_puerta))

    # xfail retirado el 2026-09-03: el PUT ya acepta la capacidad
    # y su procedencia (`_EDITABLES` en flota/api/ficha.py). Lo avisó
    # el propio `strict=True`, que falla cuando un xfail empieza a
    # pasar — un marcador que sobrevive a su defecto es deuda falsa.
    def test_el_put_no_puede_contestar_201_y_no_guardar_la_capacidad(
            self, client, db, admin, placa):
        from flota.adaptadores.modelos import FichaTecnica

        r = client.put(f'/flota/vehiculo/{placa}/ficha',
                       json={'posiciones_llanta': 6, 'km_inicial': 1000,
                             'capacidad_tanque_galones': 40,
                             'capacidad_tanque_fuente': 'manual_fabricante'},
                       headers=admin)
        assert r.status_code in (200, 201)
        ficha = FichaTecnica.query.one()
        assert ficha.capacidad_tanque_galones is not None, (
            'el PUT dijo que sí y la base quedó en NULL: el detector de '
            'capacidad no se puede armar desde ninguna pantalla')

    def test_sin_capacidad_el_detector_dice_que_no_sabe(self):
        """Sin capacidad en la ficha, `excede_capacidad` devuelve `SIN_DATO`.

        Es lo correcto —«no hay contra qué revisar»— y NO es lo mismo que «no
        se excedió». Regla 4: el «no sé» se modela con palabras.

        Hasta el 2026-09-03 esto además significaba que el detector **no podía
        disparar nunca**, porque el PUT de la ficha no aceptaba la capacidad y
        la guardaba en NULL. Eso ya no es cierto: el par número↔procedencia se
        puede cargar, y el test de abajo ejerce el disparo.
        """
        from flota.dominio.costos import excede_capacidad
        from flota.dominio.valores import SIN_DATO

        assert excede_capacidad(galones=99, capacidad_galones=None) is SIN_DATO

    def test_CON_capacidad_el_detector_dispara(self):
        """La otra dirección, y la que faltaba.

        Un tanque de 15 galones que recibe 22 **no es error de medición**. Es el
        único detector de sobre-tanqueo que no necesita umbral, canon ni un mes
        de mediciones — y estuvo construido, probado y ciego mientras nadie
        pudo cargarle una capacidad.

        Y no acusa a nadie (regla 2): afirma que la capacidad de la ficha y los
        galones registrados no pueden ser los dos ciertos. Puede ser el tanque,
        puede ser la factura, puede ser un sifón — las tres se investigan igual
        de rápido.
        """
        from decimal import Decimal

        from flota.dominio.costos import excede_capacidad

        assert excede_capacidad(galones=Decimal('22'),
                                capacidad_galones=Decimal('15')) is True
        assert excede_capacidad(galones=Decimal('14'),
                                capacidad_galones=Decimal('15')) is False
