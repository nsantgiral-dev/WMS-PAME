"""
Lleva UN conductor por su día completo de flota, **por los endpoints reales**.

## Por qué no inserta filas

Un arnés que escribe `Custodia(...)` directo prueba que la base acepta esas
filas — nada más. Es lo que ya hacen las 21 tandas de `tests/flota/`: cada
archivo arma su propio mundo coherente y verifica una etapa contra datos que él
mismo fabricó. La coherencia *entre* etapas no se ejercita nunca. No es un fallo
de esos tests: es su forma, la misma que `CLAUDE.md` documenta en «Auditoría de
invariantes de frontera».

Acá cada etapa se avanza **llamando al endpoint que el conductor llama**:

```
POST /flota/custodia/traspaso        recibir turno (con fotos + tablero)
GET  /flota/inspeccion/items/<placa> qué se pregunta hoy, en qué orden
POST /flota/inspeccion               la inspección respondida
POST /flota/hallazgos                el daño de las 9
POST /flota/tanqueos                 el tanqueo de las 11
POST /flota/custodia/traspaso        entregar turno a la sede
GET  /flota/health                   qué quedó escrito de todo eso
```

Ni un `client.post` menos, y **ningún adaptador**: `traspaso.traspasar` no se
llama desde acá. Si una etapa deja de producir lo que la siguiente espera, se
rompe acá igual que se rompería en el patio a las 5 a.m.

Lo único que se siembra directo es el **maestro**: vehículo, conductor, usuarios
y el catálogo de inspección. Es la etapa anterior al día —alguien dio de alta el
camión— y lo que este arnés verifica es de ahí en adelante. Mismo criterio que
`sembrar_catalogo` en `conductor_de_flujo.py`.

## El reloj

El módulo guarda `datetime.utcnow()` y el día operativo es Bogotá (UTC−5). Un
test que use las 08:00 UTC vive en la única franja donde las dos fechas
coinciden: **ahí no se puede ver un defecto de frontera de día**. `Reloj` fija
el instante que el módulo escribe, y el día se recorre en horas reales de
operación — incluido el cierre de las 20:00 de Bogotá, que **en UTC ya es
mañana**.

`Reloj` parchea dos cosas, porque hay dos formas de leer la hora:

  · el nombre `datetime` de cada módulo que lo enlaza **a nivel de módulo** —
    lista declarada en `BAJO_EL_RELOJ`, cruzada por AST contra `flota/` en
    `Reloj.verificar_cobertura()`. Un módulo nuevo que guarde la hora y no esté
    declarado deja de estar bajo el reloj **en silencio**, y el día volvería a
    correr con la hora de la máquina sin que nada falle;
  · la clase de la stdlib, por los `from datetime import datetime` **dentro de
    una función** (`medicion.hallazgos_vencidos`,
    `api/custodia.registrar_odometro`), que resuelven el nombre en cada llamada
    y que ningún parche de módulo alcanza. Sin esto, esas dos rutas corrían con
    otro reloj que el resto del día: el endpoint declaraba un hallazgo vencido y
    el health lo contaba como al día.

Ese segundo parche tiene un filo y está documentado en `_MetaFecha`: el dialecto
SQLite de SQLAlchemy discrimina `date` de `datetime` con un `isinstance` que
resuelve la clase en el momento de la llamada, y sin la metaclase el arnés
guardaba **todas las horas en 00:00:00**. Un arnés escrito para cazar defectos de
hora que borra la hora es el peor verde posible.
"""
import ast
import base64
import io
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

#: El día base del recorrido. **Fijo y no `date.today()`**: el barajado de
#: `ordenar_para_el_dia` se siembra con la fecha y los ítems semanales solo
#: entran los lunes, así que un día base móvil haría que el mismo test comparara
#: listas distintas según el día en que corra. Martes y miércoles: dos días
#: seguidos, ninguno lunes, mismo conjunto de ítems y distinto barajado.
DIA_1 = date(2026, 9, 15)          # martes
DIA_2 = DIA_1 + timedelta(days=1)  # miércoles

#: Horas del día operativo, en hora de **Bogotá**. La entrega de turno a las
#: 20:00 cae en el 15 de Bogotá y en el **16** de UTC — es la franja donde un
#: `utcnow().date()` mal puesto parte el día en dos.
RECIBO_TURNO = (5, 0)
INSPECCION = (5, 10)
DANO_REPORTADO = (9, 0)
TANQUEO = (11, 0)
ENTREGA_TURNO = (20, 0)

_UTC_MENOS_5 = timedelta(hours=5)


def instante_utc(dia: date, hora_bogota) -> datetime:
    """El instante UTC-naive que el módulo va a escribir, dado un reloj de pared
    de Bogotá. `(20, 0)` del 15 devuelve el **16 a las 01:00**."""
    h, m = hora_bogota
    return datetime.combine(dia, datetime.min.time()) + timedelta(hours=h, minutes=m) + _UTC_MENOS_5


class _MetaFecha(type):
    """`isinstance(cualquier datetime, _DatetimeFalso)` tiene que ser verdadero.

    **Sin esto, el arnés corrompe las filas que escribe.** El dialecto SQLite de
    SQLAlchemy discrimina `date` de `datetime` con un `isinstance` que resuelve
    `datetime.datetime` en el momento de la llamada; con la clase de la stdlib
    parcheada, un `datetime` común dejaba de ser instancia de «datetime» y caía
    por la rama de `date`, que **pone la hora en 00:00:00**. El síntoma medido:
    todas las lecturas del día guardadas a medianoche, y un arnés escrito para
    cazar defectos de hora que borraba la hora.
    """

    def __instancecheck__(cls, obj):
        return isinstance(obj, datetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, datetime)


class _DatetimeFalso(datetime, metaclass=_MetaFecha):
    """`utcnow()` y `now(tz)` contestan lo que diga el reloj del arnés.

    Subclase de `datetime` y no un mock: `isinstance(x, datetime)` sigue siendo
    verdadero, y SQLAlchemy escribe la fila sin enterarse.
    """

    _ahora = None

    @classmethod
    def utcnow(cls):
        return cls._ahora

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._ahora
        return cls._ahora.replace(tzinfo=timezone.utc).astimezone(tz)

    @classmethod
    def today(cls):
        return cls._ahora


#: Módulos cuyo nombre local `datetime` decide qué hora se escribe.
#:
#: Declarada, no derivada — por el mismo motivo que `_CAMPOS` en
#: `flota/api/health.py`: un módulo que desaparece de la lista sin que nadie lo
#: note deja de estar bajo el reloj, y el test sigue verde midiendo la hora de
#: la máquina.
BAJO_EL_RELOJ = (
    ('app.utils.fecha', 'datetime'),
    ('flota.adaptadores.traspaso', 'datetime'),
    ('flota.adaptadores.hallazgos', 'datetime'),
    ('flota.adaptadores.inspecciones', 'datetime'),
    ('flota.adaptadores.gastos', 'datetime'),
    ('flota.adaptadores.almacen_fotos', 'datetime'),
    ('flota.adaptadores.verificacion', 'datetime'),
    ('flota.adaptadores.modelos', 'datetime'),
    ('flota.adaptadores.medicion', '_datetime'),
    ('flota.api.hallazgos', 'datetime'),
)

#: Módulos de `flota/` que enlazan `datetime` y **no** están bajo el reloj, con
#: la razón. Sin esta lista, `verificar_cobertura` sería un guard que se apaga
#: solo el día que alguien agregue un módulo.
FUERA_DEL_RELOJ = {
    'flota.adaptadores.avisos': 'cron de vencimientos; no participa del día del conductor',
    'flota.adaptadores.preventivo': 'siembra del plan preventivo; cron aparte',
    'flota.adaptadores.llantas': 'montaje de llantas; no participa del día del conductor',
    'flota.adaptadores.taller': 'órdenes de trabajo; no participa del día del conductor',
    'flota.dominio.hallazgo': 'solo tipos y funciones puras — recibe `ahora`, no lo lee',
    'flota.dominio.custodia': 'solo tipos y funciones puras — recibe `ahora`, no lo lee',
    'flota.dominio.odometro': 'solo tipos y funciones puras — recibe `ahora`, no lo lee',
    'flota.dominio.valores': 'solo tipos y enums',
    'flota.api._tiempo': 'formatea un instante recibido; no lo produce',
}


class Reloj:
    """Fija la hora que el módulo escribe. Se mueve con `en(dia, hora)`."""

    def __init__(self, monkeypatch):
        self._mp = monkeypatch
        self.ahora = None

    def en(self, dia: date, hora_bogota) -> datetime:
        """Pone el reloj en esa hora de pared de Bogotá y devuelve el UTC."""
        return self.en_utc(instante_utc(dia, hora_bogota))

    def en_utc(self, cuando: datetime) -> datetime:
        import datetime as _stdlib

        _DatetimeFalso._ahora = cuando
        self.ahora = cuando
        for modulo, nombre in BAJO_EL_RELOJ:
            self._mp.setattr(f'{modulo}.{nombre}', _DatetimeFalso, raising=True)
        # Y la clase de la stdlib, **por los imports dentro de una función**.
        #
        # `medicion.hallazgos_vencidos` y `api/custodia.registrar_odometro`
        # hacen `from datetime import datetime` en el cuerpo, así que resuelven
        # el nombre EN CADA LLAMADA y ningún parche de módulo los alcanza. Sin
        # esto, esas dos rutas corrían con la hora de la máquina mientras el
        # resto del día corría con la del arnés — y el síntoma era un hallazgo
        # que el endpoint declaraba vencido y el health contaba como al día.
        #
        # Es seguro porque `_DatetimeFalso` **es** un `datetime`: lo que ya
        # enlazó el nombre (SQLAlchemy, Flask) no se entera, y `isinstance`
        # sigue siendo verdadero. Y lo deshace `monkeypatch` al terminar.
        self._mp.setattr(_stdlib, 'datetime', _DatetimeFalso)
        return cuando

    def mas(self, **delta) -> datetime:
        return self.en_utc(self.ahora + timedelta(**delta))

    @staticmethod
    def verificar_cobertura():
        """Los módulos de `flota/` que enlazan `datetime` **a nivel de módulo**.

        Devuelve la lista de los que no están ni bajo el reloj ni declarados
        fuera. Se lee por AST y no con un `grep`: los detectores de texto de este
        repo se atraparon en sus propios docstrings siete veces en una semana.

        Solo mira los de `col_offset == 0` **a propósito**: un `from datetime
        import datetime` dentro de una función resuelve el nombre en cada
        llamada, así que lo cubre el parche de la clase de la stdlib y no hace
        falta declararlo. Exigirlos acá haría una lista que crece sola y que
        alguien acabaría vaciando.
        """
        bajo = {m for m, _ in BAJO_EL_RELOJ}
        sueltos = []
        for py in sorted((RAIZ / 'flota').rglob('*.py')):
            modulo = '.'.join(py.relative_to(RAIZ).with_suffix('').parts)
            arbol = ast.parse(py.read_text(encoding='utf-8'))
            enlaza = any(
                isinstance(n, ast.ImportFrom) and n.module == 'datetime'
                and n.col_offset == 0
                and any(a.name == 'datetime' for a in n.names)
                for n in ast.walk(arbol)
            )
            if enlaza and modulo not in bajo and modulo not in FUERA_DEL_RELOJ:
                sueltos.append(modulo)
        return sueltos


# ── Fotos ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=8)
def _jpeg(ancho, alto):
    """Un JPEG real de esas dimensiones, como data URL.

    Cacheado: `guardar_foto` **mide** el archivo con Pillow (el ancho declarado
    no se cree desde el 2026-09-01), así que hace falta una imagen de verdad, y
    un día completo pide 13+13+5 de ellas. El almacén está direccionado por
    contenido, así que repetir la misma foto es lo que ya hace la operación.
    """
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (ancho, alto), (ancho % 255, alto % 255, 33)).save(
        buf, 'JPEG', quality=85)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


def fotos_de_recibo(angulos):
    """Las 13 del recibo: evidencia de estado + el tablero como `foto_dato`.

    El tablero va a 1600 px porque la regla 7 lo exige y el CHECK de la base lo
    respalda. Los ángulos NO se escriben a mano: salen de
    `GET /flota/custodia/activa/<placa>`, que es de donde los saca la pantalla —
    escribirlos acá haría que el arnés verificara una lista que el servidor ya
    no publica.
    """
    chico, tablero = _jpeg(800, 600), _jpeg(1600, 1200)
    return [
        {'clase': 'foto_dato' if a == 'tablero' else 'evidencia_estado',
         'angulo': a,
         'data_url': tablero if a == 'tablero' else chico,
         'ancho': 1600 if a == 'tablero' else 800,
         'alto': 1200 if a == 'tablero' else 600}
        for a in angulos
    ]


def fotos_de_entrega():
    """Las de la entrega, **exactamente las que manda el PWA**.

    `ANGULOS_ENTREGA` (cuatro, asimetría deliberada: recibir es exhaustivo
    porque protege a quien asume; entregar cierra el reloj) **más el tablero
    como `foto_dato`**, que `flota.js:2161` concatena aparte y no está en la
    tupla. Un arnés que mandara solo las cuatro dejaría la lectura de cierre sin
    foto y estaría midiendo su propio recorte, no la operación.
    """
    from flota.dominio.valores import ANGULOS_ENTREGA
    chico = _jpeg(800, 600)
    return [{'clase': 'evidencia_estado', 'angulo': a, 'data_url': chico,
             'ancho': 800, 'alto': 600} for a in ANGULOS_ENTREGA] + [
        {'clase': 'foto_dato', 'angulo': 'tablero', 'data_url': _jpeg(1600, 1200),
         'ancho': 1600, 'alto': 1200}]


# ── El mundo: solo maestros ──────────────────────────────────────────────

def sembrar_flota(db, placa='DIA100', tipo='camion'):
    """Un camión, dos conductores con cuenta, control de flota y el catálogo.

    Es lo único que se escribe directo. Todo lo demás del día entra por HTTP.
    """
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import catalogo

    sede = Almacen(codigo=f'{placa}-SEDE', nombre='Patio de flota', activo=True)
    veh = Vehiculo(placa=placa, tipo=tipo, activo=True)
    db.session.add_all([sede, veh])
    db.session.flush()

    def _usuario(nombre, rol, correo):
        u = Usuario(nombre=nombre, email=correo, rol=rol,
                    password_hash=generate_password_hash('x'),
                    almacen_id=sede.id, activo=True)
        db.session.add(u)
        return u

    u_a = _usuario('Conductor A', 'conductor', f'{placa}_a@test.com')
    u_b = _usuario('Conductor B', 'conductor', f'{placa}_b@test.com')
    u_flota = _usuario('Control de flota', 'control_flota', f'{placa}_cf@test.com')
    # Gestión. Hace falta aparte de `control_flota` porque el traspaso resuelve
    # `quien_pide` con `_es_gestion()`, que **no** incluye a `control_flota`
    # aunque `MAESTROS_FLOTA` sí lo incluya: son dos nociones de autoridad
    # distintas en el mismo módulo, y solo la primera puede forzar un cierre.
    u_admin = _usuario('Admin de zona', 'admin', f'{placa}_ad@test.com')
    db.session.flush()

    cond_a = Conductor(nombre='Conductor A', cedula=f'{placa}-A', activo=True,
                       usuario_id=u_a.id)
    cond_b = Conductor(nombre='Conductor B', cedula=f'{placa}-B', activo=True,
                       usuario_id=u_b.id)
    db.session.add_all([cond_a, cond_b])
    db.session.commit()

    catalogo.sembrar(db)

    return {
        'placa': placa,
        'vehiculo_id': veh.id,
        'sede_id': sede.id,
        'conductor_a': cond_a.id, 'conductor_b': cond_b.id,
        'usuario_a': u_a.id, 'usuario_b': u_b.id, 'usuario_flota': u_flota.id,
        'usuario_admin': u_admin.id,
        't_a': create_access_token(identity=str(u_a.id)),
        't_b': create_access_token(identity=str(u_b.id)),
        't_flota': create_access_token(identity=str(u_flota.id)),
        't_admin': create_access_token(identity=str(u_admin.id)),
    }


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


# ── Las etapas, cada una por su endpoint ─────────────────────────────────

def angulos_esperados(client, mundo, token=None):
    """Lo primero que carga la pantalla de recibo: quién lo tiene y qué fotos
    pedir. Devuelve el JSON entero — el día lo lee para varias cosas."""
    r = client.get(f"/flota/custodia/activa/{mundo['placa']}",
                   headers=_auth(token or mundo['t_a']))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def dejar_en_sede(client, mundo, km, token=None, con_fotos=True):
    """El estado del que arranca el día: el camión durmió en el patio.

    Se hace con el mismo endpoint y no con un `Custodia(...)`: es la custodia
    `linea_base` del vehículo, y que sea la primera es lo que hace que los daños
    del día 1 NO nazcan preexistentes.

    Va con las trece fotos **a propósito**: es el arranque en frío que control de
    flota levanta con el camión delante. Sin ellas la primera lectura de la serie
    nacería `dudosa` y el arnés estaría midiendo su propio atajo en vez de la
    operación.
    """
    cuerpo = {
        'placa': mundo['placa'], 'km': km,
        'custodio_tipo': 'sede', 'custodio_sede_id': mundo['sede_id'],
        'ubicacion': 'sede',
    }
    if con_fotos:
        cuerpo['fotos_inicio'] = fotos_de_recibo(
            angulos_esperados(client, mundo, token or mundo['t_flota'])['angulos'])
    return client.post('/flota/custodia/traspaso', json=cuerpo,
                       headers=_auth(token or mundo['t_flota']))


def recibir_turno(client, mundo, km, conductor=None, token=None, con_fotos=True,
                  **extra):
    """05:00 — el conductor recibe el vehículo, con las 13 fotos y el tablero."""
    cuerpo = {
        'placa': mundo['placa'], 'km': km,
        'custodio_tipo': 'conductor',
        'custodio_conductor_id': conductor or mundo['conductor_a'],
    }
    if con_fotos:
        cuerpo['fotos_inicio'] = fotos_de_recibo(
            angulos_esperados(client, mundo, token)['angulos'])
    cuerpo.update(extra)
    return client.post('/flota/custodia/traspaso', json=cuerpo,
                       headers=_auth(token or mundo['t_a']))


def items_del_dia(client, mundo, token=None):
    """05:10 — qué se le pregunta hoy a este camión, en el orden de pantalla."""
    return client.get(f"/flota/inspeccion/items/{mundo['placa']}",
                      headers=_auth(token or mundo['t_a']))


def responder_inspeccion(client, mundo, items, km, no_aptos=(), omitir=(),
                         segundos=95, token=None, **extra):
    """05:20 — la inspección contestada. `no_aptos` son los que hacen nacer daño.

    `omitir` deja ítems sin responder: entran como `sin_dato` y arrastran el
    veredicto a `incompleta` (regla 1). Es una respuesta legítima del formulario,
    no un error del arnés.
    """
    cuerpo = {
        'placa': mundo['placa'], 'km': km, 'segundos_llenado': segundos,
        'respuestas': [
            {'item_id': i['item_id'],
             'respuesta': 'no_apto' if i['item_id'] in no_aptos else 'optimo'}
            for i in items if i['item_id'] not in omitir
        ],
    }
    cuerpo.update(extra)
    return client.post('/flota/inspeccion', json=cuerpo,
                       headers=_auth(token or mundo['t_a']))


def reportar_dano(client, mundo, km, criticidad='mayor',
                  descripcion='Golpe en el guardabarros trasero derecho',
                  token=None, **extra):
    """09:00 — el conductor ve un golpe en la calle y lo reporta."""
    cuerpo = {'placa': mundo['placa'], 'km': km, 'criticidad': criticidad,
              'descripcion': descripcion}
    cuerpo.update(extra)
    return client.post('/flota/hallazgos', json=cuerpo,
                       headers=_auth(token or mundo['t_a']))


def hallazgos_de(client, mundo, token=None, todos=False):
    url = f"/flota/hallazgos/{mundo['placa']}" + ('?todos=1' if todos else '')
    return client.get(url, headers=_auth(token or mundo['t_a']))


def tanquear(client, mundo, km, dia=DIA_1, galones='18.5', valor='250000',
             tanque='lleno', token=None, **extra):
    """11:00 — el tanqueo, con su kilometraje y su estado de tanque."""
    cuerpo = {
        'placa': mundo['placa'], 'fecha': dia.isoformat(), 'valor': valor,
        'galones': galones, 'tanque': tanque, 'estacion': 'Terpel La Plata',
        'km': km, 'proveedor': 'TERPEL', 'origen_costo': 'tarjeta_convenio',
    }
    cuerpo.update(extra)
    return client.post('/flota/tanqueos', json=cuerpo,
                       headers=_auth(token or mundo['t_a']))


def entregar_turno(client, mundo, km, token=None, con_fotos=True, **extra):
    """18:00–20:00 — el conductor entrega a la sede y cierra su reloj."""
    cuerpo = {
        'placa': mundo['placa'], 'km': km,
        'custodio_tipo': 'sede', 'custodio_sede_id': mundo['sede_id'],
        'ubicacion': 'sede',
    }
    if con_fotos:
        cuerpo['fotos_fin'] = fotos_de_entrega()
    cuerpo.update(extra)
    return client.post('/flota/custodia/traspaso', json=cuerpo,
                       headers=_auth(token or mundo['t_a']))


def health(client, mundo, token=None):
    r = client.get('/flota/health', headers=_auth(token or mundo['t_flota']))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def lecturas_de(vehiculo_id):
    """La serie del vehículo, ordenada. Para preguntarle a la base qué quedó —
    **leer** no es avanzar una etapa, y esto no escribe nada."""
    from flota.adaptadores.modelos import LecturaOdometro
    return (LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id)
            .order_by(LecturaOdometro.ts, LecturaOdometro.id).all())


def custodias_de(vehiculo_id):
    from flota.adaptadores.modelos import Custodia
    return (Custodia.query.filter_by(vehiculo_id=vehiculo_id)
            .order_by(Custodia.inicio_ts, Custodia.id).all())


# ── El día completo, de una ──────────────────────────────────────────────

def dia_completo(client, mundo, dia=DIA_1, reloj=None, km_apertura=100_000,
                 km_dia=180, no_aptos=None, conductor=None, token=None):
    """Recibe, inspecciona, reporta, tanquea y entrega. Devuelve lo que dejó.

    Los kilómetros suben a lo largo del día porque el camión rueda: el que
    entrega no puede declarar el mismo número con el que recibió, y esa
    coherencia es la que ninguna tanda por separado puede comprobar.
    """
    assert reloj is not None, 'el día se recorre con reloj: sin él vive en UTC'
    km = {
        'recibo': km_apertura,
        'inspeccion': km_apertura,
        'dano': km_apertura + int(km_dia * 0.4),
        'tanqueo': km_apertura + int(km_dia * 0.6),
        'entrega': km_apertura + km_dia,
    }
    salida = {'km': km}

    reloj.en(dia, RECIBO_TURNO)
    r = recibir_turno(client, mundo, km['recibo'], conductor=conductor, token=token)
    assert r.status_code == 201, r.get_json()
    salida['custodia'] = r.get_json()

    reloj.en(dia, INSPECCION)
    r = items_del_dia(client, mundo, token=token)
    assert r.status_code == 200, r.get_json()
    salida['items'] = r.get_json()

    elegidos = no_aptos if no_aptos is not None else [
        # El primer ítem NO bloqueante: un `no_apto` bloqueante deja la
        # inspección en `no_apto` y el día siguiente arranca distinto. El día
        # normal tiene un daño menor, que es lo que pasa casi siempre.
        next(i['item_id'] for i in salida['items']['items'] if not i['bloqueante'])
    ]
    r = responder_inspeccion(client, mundo, salida['items']['items'],
                             km['inspeccion'], no_aptos=elegidos, token=token)
    assert r.status_code == 201, r.get_json()
    salida['inspeccion'] = r.get_json()

    reloj.en(dia, DANO_REPORTADO)
    r = reportar_dano(client, mundo, km['dano'], token=token)
    assert r.status_code == 201, r.get_json()
    salida['hallazgo'] = r.get_json()

    reloj.en(dia, TANQUEO)
    r = tanquear(client, mundo, km['tanqueo'], dia=dia, token=token)
    assert r.status_code == 201, r.get_json()
    salida['tanqueo'] = r.get_json()

    reloj.en(dia, ENTREGA_TURNO)
    r = entregar_turno(client, mundo, km['entrega'], token=token)
    assert r.status_code == 201, r.get_json()
    salida['entrega'] = r.get_json()
    return salida


__all__ = [
    'DIA_1', 'DIA_2', 'RECIBO_TURNO', 'INSPECCION', 'DANO_REPORTADO',
    'TANQUEO', 'ENTREGA_TURNO', 'BAJO_EL_RELOJ', 'FUERA_DEL_RELOJ',
    'Reloj', 'instante_utc', 'sembrar_flota', 'angulos_esperados',
    'dejar_en_sede', 'recibir_turno', 'items_del_dia', 'responder_inspeccion',
    'reportar_dano', 'hallazgos_de', 'tanquear', 'entregar_turno', 'health',
    'lecturas_de', 'custodias_de', 'dia_completo', 'fotos_de_recibo',
    'fotos_de_entrega',
]
