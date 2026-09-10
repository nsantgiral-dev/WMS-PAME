"""
El módulo de flota recorrido **con los datos que HAY**, no con los que debería
haber.

Todos los tests de flota existentes se escribieron con mundos completos:
vehículo con ficha, conductor con cuenta, lectura con foto, tanque con
capacidad. La producción no se parece a eso, y está medido
(`docs/flota/ESTADO.md`, regla 13 — cada número con su fecha y su base):

| Hecho medido en producción | Fecha |
|---|---|
| 26 de 26 lecturas **sin foto** del tablero | 2026-09-01 |
| **4 de 6 fichas** contradicen su serie (THP696: ancla 433.434, primera 55.349) | 2026-09-01 |
| Un salto de **16.697.948 km** en una lectura | 2026-08-18 |
| ~10-20 lecturas con el **mismo segundo**, de un reintento | 2026-08-03 |
| Conductores activos **sin cuenta de usuario** | — |
| Una RTM **vencida desde 2025-11-11** | — |

Los hechos se reproducen **como fixture**. No se consulta producción.

## Qué busca este archivo

Un solo defecto, en todas sus formas: **un «no sé» convertido en un número.**
Un CPK de cero kilómetros que sale 0 en vez de `sin_dato`; un rendimiento
imposible; un `0,0` de coordenada; un `activo=True` por defecto. Todos se ven
bien en la pantalla, y ésa es exactamente la propiedad que los hace caros.

Es la regla 4 del módulo (*«un estado que puede ser "no sé" se modela con
palabras, nunca con booleano ni con `None`»*) y la regla 0 del WMS (*ante dato
ausente, fallar hacia el lado conservador y declararlo*).

## Cómo leer los `xfail`

Cada `xfail(strict=True)` es un defecto **medido y registrado, no escondido**.
El día que alguien lo arregle, el test se pone verde y `strict` lo convierte en
fallo — que es la única forma de que un arreglo avise que ocurrió. Ninguno se
afloja ni se borra.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.models.conductor import Conductor
from app.models.usuario import Usuario
from app.models.vehiculo import Vehiculo
from app.utils.fecha import dia_operativo
from flota.adaptadores.medicion import MedidorSQL
from flota.adaptadores.modelos import FichaTecnica, Foto, LecturaOdometro

_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


def _usuario(db, almacen, email, rol='admin', nombre='X'):
    u = Usuario(nombre=nombre, email=email,
                password_hash=generate_password_hash('x'),
                rol=rol, almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u, create_access_token(identity=str(u.id))


def _vehiculo(db, placa, tipo='camion'):
    v = Vehiculo(placa=placa, tipo=tipo, activo=True)
    db.session.add(v)
    db.session.commit()
    return v


def _ventana_del_mes():
    """La ventana que el endpoint usa por defecto, calculada como él la calcula.

    `dia_operativo()` y no `date.today()`: en Railway, a partir de las 7 p.m. de
    Colombia `today()` ya es mañana, y un test que arma fechas con el reloj
    equivocado pasa local y rompe el deploy (regla 5 del WMS).
    """
    hoy = dia_operativo()
    return hoy.replace(day=1), hoy


def _cpk(client, token, placa):
    desde, hasta = _ventana_del_mes()
    return client.get(f'/flota/gastos/{placa}?desde={desde}&hasta={hasta}',
                      headers=_auth(token)).get_json()


# ══════════════════════════════════════════════════════════════════════════
# Mundo 1 — Vehículo sin ficha técnica
#
# Sin `posiciones_llanta`, sin `capacidad_tanque_galones`, sin
# `distribucion_km_cambio`. La pregunta no es si el sistema aguanta: es si
# **dice que no sabe** o muestra un número.
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sin_ficha(db, almacen):
    veh = _vehiculo(db, 'SNFICH')
    u, t = _usuario(db, almacen, 'mi_sinficha@test.com')
    return {'veh': veh, 'placa': veh.placa, 'u': u, 't': t, 'db': db}


class TestVehiculoSinFichaTecnica:
    """El detector que no tiene contra qué comparar tiene que decirlo."""

    def test_el_detector_de_sobre_tanqueo_dice_sin_dato_y_no_false(
            self, client, sin_ficha):
        """`False` significaría «se revisó y está bien».

        Lo que pasa es que no hay contra qué revisar. Un vehículo sin capacidad
        declarada saldría limpio para siempre — que es exactamente cómo un
        detector se apaga sin que nadie lo note.
        """
        r = client.post('/flota/tanqueos', headers=_auth(sin_ficha['t']), json={
            'placa': sin_ficha['placa'], 'fecha': str(dia_operativo()),
            'valor': '300000', 'galones': '999', 'tanque': 'lleno',
            'estacion': 'T', 'km': 1000, 'proveedor': 'T',
            'origen_costo': 'tarjeta_convenio'})
        assert r.status_code == 201, r.get_json()
        assert r.get_json()['tanqueo']['excede_capacidad'] == 'sin_dato'

    def test_el_health_cuenta_aparte_los_que_no_se_pudieron_mirar(
            self, client, sin_ficha):
        """Sumarlos a `tanqueos_sobre_capacidad` daría un número sin significado.

        Y sin el segundo contador, un parque entero sin capacidad levantada se
        ve idéntico a un parque limpio.
        """
        client.post('/flota/tanqueos', headers=_auth(sin_ficha['t']), json={
            'placa': sin_ficha['placa'], 'fecha': str(dia_operativo()),
            'valor': '300000', 'galones': '999', 'tanque': 'lleno',
            'estacion': 'T', 'km': 1000, 'proveedor': 'T',
            'origen_costo': 'tarjeta_convenio'})
        m = MedidorSQL()
        assert m.tanqueos_sin_capacidad_declarada() == 1
        assert m.tanqueos_sobre_capacidad() == 0

    def test_el_preventivo_no_siembra_un_plan_inventado(self, client, sin_ficha):
        """Falla RUIDOSAMENTE (409) y explica de dónde sale el plan.

        Sembrar un plan por defecto pondría fechas de cambio de correa que
        nadie levantó, sobre un motor que nadie miró.
        """
        r = client.post(f'/flota/preventivo/{sin_ficha["placa"]}/sembrar',
                        headers=_auth(sin_ficha['t']))
        assert r.status_code == 409
        assert 'no tiene ficha técnica' in r.get_json()['error']

    def test_las_posiciones_de_llanta_declaran_que_no_se_saben(
            self, client, sin_ficha):
        """`posiciones_declaradas: null` + `posiciones_libres: 'sin_dato'`.

        Contar «0 posiciones sin llanta» haría que un parque sin ficha se viera
        idéntico a uno con las 24 llantas registradas.
        """
        d = client.get(f'/flota/llantas/{sin_ficha["placa"]}',
                       headers=_auth(sin_ficha['t'])).get_json()
        assert d['posiciones_declaradas'] is None
        assert d['posiciones_libres'] == 'sin_dato'

    def test_la_ficha_ausente_no_se_disfraza_de_ficha_vacia(
            self, client, sin_ficha):
        """`existe: false` y `completa: null` — no `completa: false`.

        «La miramos y está incompleta» y «no hay ficha» se corrigen distinto.
        """
        d = client.get(f'/flota/vehiculo/{sin_ficha["placa"]}/ficha',
                       headers=_auth(sin_ficha['t'])).get_json()
        assert d['existe'] is False
        assert d['completa'] is None and d['atributos_sin_dato'] is None

    def test_la_custodia_declara_de_donde_salieron_las_posiciones(
            self, client, sin_ficha):
        """El formulario se arma igual —no se deja al conductor sin pantalla a
        las 5 a.m.— pero **dice que el número es un supuesto**."""
        d = client.get(f'/flota/custodia/activa/{sin_ficha["placa"]}',
                       headers=_auth(sin_ficha['t'])).get_json()
        assert d['posiciones_llanta_fuente'] == 'tipo'
        assert d['odometro_actual'] == 'sin_dato'


# ══════════════════════════════════════════════════════════════════════════
# Mundo 2 — Vehículo sin NINGUNA lectura de odómetro
#
# Regla 3: sin odómetro no se persiste ningún evento de flota. Las seis vías
# —hallazgo, inspección, gasto, tanqueo, OT, llanta— o piden el kilometraje o
# se lo inventan.
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sin_lectura(db, almacen):
    veh = _vehiculo(db, 'SNLECT')
    u, t = _usuario(db, almacen, 'mi_sinlect@test.com')
    return {'veh': veh, 'placa': veh.placa, 'u': u, 't': t, 'db': db}


class TestVehiculoSinNingunaLectura:

    _VIAS = (
        ('hallazgo',   'POST', '/flota/hallazgos',
         {'criticidad': 'menor', 'descripcion': 'raya en el capó'}),
        ('inspeccion', 'POST', '/flota/inspeccion',
         {'respuestas': [], 'segundos_llenado': 90}),
        ('tanqueo',    'POST', '/flota/tanqueos',
         {'fecha': '2026-09-01', 'valor': '1', 'galones': '1',
          'tanque': 'lleno', 'estacion': 'T', 'proveedor': 'T',
          'origen_costo': 'tarjeta_convenio'}),
        ('orden',      'POST', '/flota/ordenes',
         {'tipo': 'correctiva', 'taller': 'T', 'descripcion': 'ruido'}),
    )

    @pytest.mark.parametrize('nombre,_m,url,cuerpo', _VIAS,
                             ids=[v[0] for v in _VIAS])
    def test_ninguna_via_persiste_sin_kilometraje(
            self, client, sin_lectura, nombre, _m, url, cuerpo):
        """Regla 3, ejercida por HTTP en cada vía.

        El kilometraje es la única clave que une los tres formatos que hoy no se
        hablan entre sí. Sin él no hay CPK, no hay preventivo por km y no se
        puede auditar si un mantenimiento era necesario.

        Falla **ruidosamente**: 400 con el nombre del campo que falta.
        """
        r = client.post(url, headers=_auth(sin_lectura['t']),
                        json={'placa': sin_lectura['placa'], **cuerpo})
        assert r.status_code == 400, (
            f'la vía «{nombre}» aceptó un evento sin kilometraje: '
            f'{r.status_code} {r.get_json()}')
        assert 'km' in r.get_json()['error']
        assert LecturaOdometro.query.count() == 0, (
            f'la vía «{nombre}» inventó una lectura de odómetro')

    def test_el_montaje_de_llanta_tampoco(self, client, sin_lectura):
        """La sexta vía. Se prueba aparte porque necesita una llanta primero."""
        r = client.post('/flota/llantas', headers=_auth(sin_lectura['t']),
                        json={'codigo': 'LL-1', 'medida': '295/80R22.5'})
        assert r.status_code == 201, r.get_json()
        r = client.post('/flota/montajes', headers=_auth(sin_lectura['t']),
                        json={'placa': sin_lectura['placa'],
                              'llanta_id': r.get_json()['id'], 'posicion': 1})
        assert r.status_code == 400 and 'km' in r.get_json()['error']
        assert LecturaOdometro.query.count() == 0

    def test_el_odometro_actual_es_sin_dato_y_nunca_cero(
            self, client, sin_lectura):
        """0 km es «no ha rodado». `sin_dato` es «no sabemos»."""
        d = client.get(f'/flota/custodia/activa/{sin_lectura["placa"]}',
                       headers=_auth(sin_lectura['t'])).get_json()
        assert d['odometro_actual'] == 'sin_dato'

    def test_el_ritmo_de_uso_dice_por_que_no_se_pudo_medir(
            self, client, sin_lectura):
        """Un `sin_dato` sin motivo obliga a adivinar si faltan lecturas, si el
        vehículo está quieto o si el tramo es dudoso — y se corrigen distinto."""
        d = client.get(f'/flota/preventivo/{sin_lectura["placa"]}',
                       headers=_auth(sin_lectura['t'])).get_json()
        assert d['ritmo']['km_dia'] == 'sin_dato'
        assert d['ritmo']['n'] == 0
        assert 'menos de dos lecturas' in d['ritmo']['motivo']

    def test_el_cpk_no_publica_numero(self, client, sin_lectura):
        d = _cpk(client, sin_lectura['t'], sin_lectura['placa'])
        assert d['cpk'] == 'sin_dato' and d['cpk_marca'] == 'sin_dato'

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO — el CPK sale `sin_dato` y su propio insumo publica `0`. '
        'Un vehículo SIN NINGUNA LECTURA y un vehículo QUIETO (dos lecturas '
        'con el mismo kilometraje) publican los dos `km_recorridos: 0`, y son '
        'afirmaciones distintas: «no se puede medir el tramo» contra «se midió '
        'y no se movió». `_tramo_de` devuelve `(0, SIN_DATO)` y el `0` viaja '
        'entero hasta el JSON y hasta `cpk_mes` del health. Es el mismo criterio '
        'de los dos ceros que el canon del CPK resolvió para el numerador y '
        'dejó abierto para el denominador. '
        'FALLA EN SILENCIO: la pantalla muestra «0 km recorridos». '
        'Está fijado por `tests/flota/test_gastos.py:837`, que afirma '
        '`d["km_recorridos"] == 0` — un test que verifica la implementación, no '
        'la propiedad. El arreglo toca `_tramo_de`, `cpk_de`, el JSON de '
        '`/flota/gastos/<placa>` y `medicion.cpk_mes`: no es pequeño ni seguro.'))
    def test_km_recorridos_deberia_decir_sin_dato_cuando_no_hay_tramo(
            self, client, sin_lectura):
        d = _cpk(client, sin_lectura['t'], sin_lectura['placa'])
        assert d['km_recorridos'] == 'sin_dato', (
            f'un vehículo sin ninguna lectura publica km_recorridos='
            f'{d["km_recorridos"]!r}, indistinguible de un camión quieto')


# ══════════════════════════════════════════════════════════════════════════
# Mundo 3 — Todas las lecturas sin foto  →  todas `dudosa`
#
# Es el estado REAL de producción: 26 de 26, medido el 2026-09-01. El CPK se
# niega a publicar. La pregunta es qué hace el resto del tablero con los
# mismos kilómetros.
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def todo_dudoso(db, almacen):
    """Dos tanqueos sin foto: las dos lecturas nacen `dudosa`."""
    veh = _vehiculo(db, 'DUDOSA')
    u, t = _usuario(db, almacen, 'mi_dudosa@test.com')
    return {'veh': veh, 'placa': veh.placa, 'u': u, 't': t, 'db': db}


def _dos_tanqueos(client, token, placa, km_a=1000, km_b=1400):
    hoy = dia_operativo()
    ayer = hoy - timedelta(days=1)
    for km, f in ((km_a, ayer), (km_b, hoy)):
        r = client.post('/flota/tanqueos', headers=_auth(token), json={
            'placa': placa, 'fecha': str(f), 'valor': '300000',
            'galones': '20', 'tanque': 'lleno', 'estacion': 'T', 'km': km,
            'proveedor': 'T', 'origen_costo': 'tarjeta_convenio'})
        assert r.status_code == 201, r.get_json()


class TestTodasLasLecturasSinFoto:

    def test_las_lecturas_nacen_dudosas_con_su_motivo_escrito(
            self, client, todo_dudoso):
        """Una `dudosa` sin motivo escrito no la puede revisar nadie."""
        _dos_tanqueos(client, todo_dudoso['t'], todo_dudoso['placa'])
        filas = LecturaOdometro.query.all()
        assert len(filas) == 2
        for l in filas:
            assert l.confianza == 'dudosa'
            assert 'sin foto del tablero' in l.motivo_dudosa

    def test_el_cpk_se_niega_a_publicar(self, client, todo_dudoso):
        """Los dos extremos sin respaldo no son un número malo: no son un número."""
        _dos_tanqueos(client, todo_dudoso['t'], todo_dudoso['placa'])
        d = _cpk(client, todo_dudoso['t'], todo_dudoso['placa'])
        assert d['cpk'] == 'sin_dato'
        assert d['cpk_marca'] == 'sin_dato'

    def test_la_cola_de_verificacion_dice_que_no_hay_foto_que_mirar(
            self, client, todo_dudoso):
        """La cola tiene sentido **porque lo declara**: `tiene_foto: false`
        significa «confirmarla es tu palabra», no un recuadro vacío que se lee
        como un error de carga."""
        _dos_tanqueos(client, todo_dudoso['t'], todo_dudoso['placa'])
        d = client.get('/flota/odometro/dudosas',
                       headers=_auth(todo_dudoso['t'])).get_json()
        assert d['total'] == 2
        assert all(p['tiene_foto'] is False for p in d['pendientes'])

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO — el rendimiento km/galón NO TIENE CANAL DE CONFIANZA. '
        'Sobre las MISMAS dos lecturas, el CPK devuelve `sin_dato` (los dos '
        'extremos son `dudosa`, la resta no tiene denominador confiable) y '
        '`rendimiento_km_galon` publica un número. `costos.rendimiento_km_galon` '
        'y `gastos.tanqueos_de` no consultan `confianza` en ninguna línea, y el '
        'JSON de `/flota/gastos/<placa>` no tiene un campo `rendimiento_marca` '
        'con el que la pantalla pudiera marcarlo. '
        'En producción esto no es un caso de borde: son 26 de 26 lecturas sin '
        'foto (2026-09-01), o sea que HOY todo rendimiento publicado está '
        'construido sobre kilómetros que el propio módulo declara inservibles '
        'para dividir. FALLA EN SILENCIO: el número se ve normal. '
        'El arreglo exige decidir el contrato del segundo canal de marca — no '
        'es pequeño ni seguro.'))
    def test_el_rendimiento_no_deberia_publicar_sobre_km_dudosos(
            self, client, todo_dudoso):
        _dos_tanqueos(client, todo_dudoso['t'], todo_dudoso['placa'])
        d = _cpk(client, todo_dudoso['t'], todo_dudoso['placa'])
        assert d['cpk'] == 'sin_dato', 'precondición: el CPK sí se niega'
        assert d['rendimiento_km_galon'] == 'sin_dato', (
            f'el CPK dice `sin_dato` sobre estas lecturas y el rendimiento '
            f'publica {d["rendimiento_km_galon"]!r} sobre las mismas')


# ══════════════════════════════════════════════════════════════════════════
# Mundo 4 — La corrección no corrige la plata
#
# El caso del THP696 entero: entran 16.697.948 km, alguien los corrige, y
# después alguien verifica la serie contra las fotos.
#
# `vigentes_tras_la_ultima_correccion` tiene TRES consumidores declarados
# (`km_por_dia`, `verificacion.pendientes`, `validar_lectura`) y **los dos que
# deciden la plata no la llaman**. Regla 0, corolario: una política, una
# función — la copia que diverge es la que decide si un número se publica.
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def serie_envenenada(client, db, almacen):
    """55.349 → 16.697.948 → corrección a 55.600, y un seguro de $1.200.000.

    Los kilómetros REALES del mes son 251 (55.600 − 55.349). Con $1.200.000
    imputados, el CPK honesto es ~$4.780/km.
    """
    veh = _vehiculo(db, 'THP696')
    u, t = _usuario(db, almacen, 'mi_envenenada@test.com')
    for km in (55349, 16697948):
        r = client.post('/flota/odometro', headers=_auth(t), json={
            'placa': 'THP696', 'valor_km': km, 'origen': 'cierre_dia'})
        assert r.status_code == 201, r.get_json()
    r = client.post('/flota/odometro', headers=_auth(t), json={
        'placa': 'THP696', 'valor_km': 55600, 'origen': 'correccion',
        'motivo_correccion': 'los 16.697.948 fueron un dedo en el teclado'})
    assert r.status_code == 201, r.get_json()

    desde, hasta = _ventana_del_mes()
    r = client.post('/flota/gastos', headers=_auth(t), json={
        'placa': 'THP696', 'categoria': 'seguro', 'fecha': str(hasta),
        'valor': '1200000', 'proveedor': 'Aseguradora',
        'origen_costo': 'tarjeta_convenio',
        'periodo_desde': str(desde), 'periodo_hasta': str(hasta)})
    assert r.status_code == 201, r.get_json()
    return {'veh': veh, 'placa': 'THP696', 'u': u, 't': t, 'db': db}


def _verificar_todo(client, token):
    """Alguien atiende la cola y confirma cada lectura contra su foto."""
    for l in LecturaOdometro.query.order_by(LecturaOdometro.id).all():
        client.post(f'/flota/odometro/{l.id}/verificar', headers=_auth(token),
                    json={})


class TestLaCorreccionNoCorrigeLaPlata:

    def test_el_salto_entra_marcado_dudoso_y_con_su_motivo(
            self, client, serie_envenenada):
        """La mitad que SÍ funciona: `validar_lectura` deja pasar (crecer es lo
        único que la monotonía exige) y `confianza_al_nacer` lo marca."""
        l = LecturaOdometro.query.filter_by(valor_km=16697948).one()
        assert l.confianza == 'dudosa'
        assert 'salto de ×301' in l.motivo_dudosa

    def test_la_cola_de_verificacion_si_respeta_la_correccion(
            self, client, serie_envenenada):
        """El contraste que hace el defecto visible.

        La cola aplica `vigentes_tras_la_ultima_correccion` y ofrece **una sola**
        fila: no le pide a nadie que confirme un número que la corrección ya
        reemplazó. La política existe, está escrita una vez y funciona.
        """
        d = client.get('/flota/odometro/dudosas',
                       headers=_auth(serie_envenenada['t'])).get_json()
        assert d['total'] == 1
        assert d['pendientes'][0]['valor_km'] == 55600

    def test_el_ritmo_de_uso_si_respeta_la_correccion(
            self, client, serie_envenenada):
        """`km_por_dia` llama a la función y por eso no hereda los 16 millones."""
        r = MedidorSQL().km_dia_por_vehiculo()
        fila = [x for x in r if x['placa'] == 'THP696'][0]
        assert fila['n'] == 1
        assert fila['km_dia'] == 'sin_dato'

    # xfail retirado el 2026-09-03: el defecto se arregló (`cpk_de` ya aplica
    # `vigentes_tras_la_ultima_correccion`). Lo avisó el propio `strict=True`,
    # que falla cuando un xfail empieza a pasar — un marcador que sobrevive
    # a su defecto es una deuda falsa, y de esas ya hubo tres.
    def test_el_cpk_deberia_medir_sobre_las_lecturas_vigentes(
            self, client, serie_envenenada):
        d = _cpk(client, serie_envenenada['t'], 'THP696')
        assert d['km_recorridos'] != 16642599, (
            'el CPK sigue midiendo sobre la lectura que la corrección reemplazó')

    # xfail retirado el 2026-09-03: el defecto se arregló (`cpk_de` ya aplica
    # `vigentes_tras_la_ultima_correccion`). Lo avisó el propio `strict=True`,
    # que falla cuando un xfail empieza a pasar — un marcador que sobrevive
    # a su defecto es una deuda falsa, y de esas ya hubo tres.
    def test_el_cpk_verificado_no_deberia_salir_68000_veces_mas_barato(
            self, client, serie_envenenada):
        _verificar_todo(client, serie_envenenada['t'])
        d = _cpk(client, serie_envenenada['t'], 'THP696')
        assert d['cpk'] == 'sin_dato' or Decimal(d['cpk']) > Decimal('1000'), (
            f"CPK publicado {d['cpk']!r} con marca {d['cpk_marca']!r} y "
            f"km_recorridos {d['km_recorridos']!r}; el real del mes es "
            f"~$4.780/km sobre 251 km vigentes")

    # xfail retirado el 2026-09-03: el defecto se arregló (`cpk_de` ya aplica
    # `vigentes_tras_la_ultima_correccion`). Lo avisó el propio `strict=True`,
    # que falla cuando un xfail empieza a pasar — un marcador que sobrevive
    # a su defecto es una deuda falsa, y de esas ya hubo tres.
    def test_el_health_no_deberia_publicar_cpk_cero_verificado(
            self, client, serie_envenenada):
        _verificar_todo(client, serie_envenenada['t'])
        fila = [x for x in MedidorSQL().cpk_mes() if x['placa'] == 'THP696'][0]
        assert (fila['cpk'] == 'sin_dato'
                or fila['marca'] != 'verificada'
                or Decimal(fila['cpk']) > Decimal('1000')), (
            f'el health publica {fila!r}; el CPK real del mes es ~$4.780/km '
            f'sobre 251 km vigentes')

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO — MISMA CAUSA en el rendimiento. Un tanqueo cargado con el '
        'kilometraje envenenado produce 834.847,40 km/galón, y la corrección '
        'posterior no lo mueve: `tanqueos_de` lee la lectura de cada tanqueo '
        'sin pasar por `vigentes_tras_la_ultima_correccion`. '
        'FALLA EN SILENCIO — y sin marca de confianza, la pantalla no tiene con '
        'qué señalarlo (ver el `xfail` del mundo 3).'))
    def test_el_rendimiento_deberia_respetar_la_correccion(
            self, client, db, almacen):
        veh = _vehiculo(db, 'RENVEN')
        u, t = _usuario(db, almacen, 'mi_renven@test.com')
        hoy = dia_operativo()
        for km, f in ((1000, hoy - timedelta(days=1)), (16697948, hoy)):
            client.post('/flota/tanqueos', headers=_auth(t), json={
                'placa': 'RENVEN', 'fecha': str(f), 'valor': '300000',
                'galones': '20', 'tanque': 'lleno', 'estacion': 'T', 'km': km,
                'proveedor': 'T', 'origen_costo': 'tarjeta_convenio'})
        client.post('/flota/odometro', headers=_auth(t), json={
            'placa': 'RENVEN', 'valor_km': 1400, 'origen': 'correccion',
            'motivo_correccion': 'el tanqueo se cargó con dígitos de más'})
        d = _cpk(client, t, 'RENVEN')
        r = d['rendimiento_km_galon']
        assert r == 'sin_dato' or Decimal(r) < Decimal('100'), (
            f'rendimiento publicado: {r} km/galón tras corregir la serie')


# ══════════════════════════════════════════════════════════════════════════
# Mundo 5 — Ficha con el ancla incoherente (el caso THP696 real)
#
# Ancla 433.434, primera lectura 55.349. 4 de 6 fichas de producción están así.
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ancla_incoherente(client, db, almacen):
    veh = _vehiculo(db, 'ANCLAI')
    u, t = _usuario(db, almacen, 'mi_ancla@test.com')
    db.session.add(FichaTecnica(
        vehiculo_id=veh.id, posiciones_llanta=6, km_inicial=433434,
        km_inicial_ts=datetime(2026, 1, 1), distribucion='correa',
        distribucion_km_cambio=60000, distribucion_fuente='manual_fabricante'))
    db.session.commit()
    for km in (55349, 55600):
        r = client.post('/flota/odometro', headers=_auth(t), json={
            'placa': 'ANCLAI', 'valor_km': km, 'origen': 'cierre_dia'})
        assert r.status_code == 201, r.get_json()
    return {'veh': veh, 'placa': 'ANCLAI', 'u': u, 't': t, 'db': db}


class TestLecturasConElMismoSegundo:
    """El reintento del 2026-08-03 dejó ~10-20 lecturas compartiendo `ts`.

    Importa porque el orden de la serie —y por lo tanto qué lectura supersede a
    cuál tras una corrección— se resuelve por `ts`. Un empate no rompe nada hoy,
    pero **una consulta que eligiera una fila distinta cada vez publicaría dos
    números distintos para el mismo mes**, y eso es indistinguible de un dato
    que cambió.
    """

    @pytest.fixture
    def empatadas(self, db, almacen):
        veh = _vehiculo(db, 'MISMOS')
        u, t = _usuario(db, almacen, 'mi_empate@test.com')
        # El hecho que se reproduce es **el segundo compartido**, no la fecha:
        # en producción fue el 2026-08-03 20:04:50, y acá se ancla a un instante
        # reciente para caer dentro de la ventana de 30 días que mide el health.
        # Fijar `2026-08-03` haría que este test dejara de ejercer el campo un
        # mes después de escribirse, en silencio.
        mismo = datetime.utcnow().replace(microsecond=0) - timedelta(days=1)
        for km in (1000, 1000, 1400, 1400):
            db.session.add(LecturaOdometro(
                vehiculo_id=veh.id, valor_km=km, ts=mismo,
                origen='cierre_dia', autor_usuario_id=u.id))
        db.session.commit()
        return {'veh': veh, 'placa': 'MISMOS', 'u': u, 't': t}

    def test_el_health_los_cuenta_en_vez_de_callarlos(self, client, empatadas):
        assert MedidorSQL().lecturas_ts_duplicado() == 4

    def test_el_ritmo_no_divide_por_cero_dias(self, client, empatadas):
        """Cero días entre la primera y la última no es un ritmo infinito ni
        cero: es que no hay días entre los que dividir, y se dice."""
        fila = [x for x in MedidorSQL().km_dia_por_vehiculo()
                if x['placa'] == 'MISMOS'][0]
        assert fila['km_dia'] == 'sin_dato'
        assert 'misma marca de tiempo' in fila['motivo']

    def test_el_salto_informa_el_par_y_no_una_velocidad_infinita(
            self, client, empatadas):
        """`horas = 0` no se convierte en km/h infinitos: se informa el par y
        quien lea decide."""
        d = MedidorSQL().salto_km_maximo_30d()
        assert d['delta_km'] == 400
        assert d['horas'] == 0.0

    def test_el_tramo_del_cpk_es_estable_entre_consultas(
            self, client, empatadas):
        """El desempate por `(valor_km, ts, id)` es lo que impide que la misma
        ventana publique dos marcas distintas en dos consultas seguidas."""
        from flota.adaptadores.gastos import cpk_de

        desde, hasta = dia_operativo() - timedelta(days=30), dia_operativo()
        r = [cpk_de(empatadas['veh'].id, desde, hasta) for _ in range(5)]
        assert len({(x['km'], str(x['marca'])) for x in r}) == 1


class TestFichaConAnclaIncoherente:
    """El día completo del vehículo cuya ficha contradice su propia serie."""

    def test_la_serie_entra_pese_a_contradecir_el_ancla(
            self, client, ancla_incoherente):
        """Decisión deliberada, no descuido: **no se sabe cuál de los dos miente**.

        Imponer el ancla como piso trabaría el vehículo por una segunda vía.
        Se cuenta, se mira, y recién después se decide (regla 0: ante dato en
        conflicto, declarar antes que elegir en silencio).
        """
        assert LecturaOdometro.query.filter_by(
            vehiculo_id=ancla_incoherente['veh'].id).count() == 2

    def test_el_health_lo_cuenta_en_vez_de_callarlo(self, client, ancla_incoherente):
        assert MedidorSQL().fichas_con_ancla_incoherente() == 1

    def test_el_preventivo_no_proyecta_desde_el_ancla(
            self, client, ancla_incoherente):
        """La tarea nace `sin_linea_base` con todo en `sin_dato`.

        Si proyectara desde `km_inicial = 433.434` contra un odómetro de 55.600,
        la correa saldría vencida por 373.834 km el primer día — y cuarenta
        avisos el día uno es cómo un canal se vuelve ilegible.
        """
        client.post(f'/flota/preventivo/{ancla_incoherente["placa"]}/sembrar',
                    headers=_auth(ancla_incoherente['t']))
        d = client.get(f'/flota/preventivo/{ancla_incoherente["placa"]}',
                       headers=_auth(ancla_incoherente['t'])).get_json()
        tarea = d['tareas'][0]
        assert tarea['estado'] == 'sin_linea_base'
        assert tarea['km_restante'] == 'sin_dato'
        assert tarea['proximo_km'] == 'sin_dato'
        assert tarea['ultima_ejecucion_km'] == 'sin_dato'

    def test_el_ritmo_se_mide_sobre_la_serie_no_sobre_el_ancla(
            self, client, ancla_incoherente):
        """Las dos lecturas cuentan (`n == 2`) y el ancla no entra en la resta.

        Y el ritmo sale `sin_dato`, no un número: las dos son `dudosa` —sin foto
        del tablero, que es el estado de las 26 de producción— y un cociente con
        los dos extremos en duda no tiene numerador ni denominador confiables.
        **Éste es el comportamiento correcto**, y es el que hace visible por
        contraste el defecto del rendimiento km/galón (mundo 3): dos métricas
        sobre los mismos kilómetros, una se calla y la otra publica.
        """
        fila = [x for x in MedidorSQL().km_dia_por_vehiculo()
                if x['placa'] == 'ANCLAI'][0]
        assert fila['n'] == 2
        assert fila['km_dia'] == 'sin_dato'
        assert fila['marca'] == 'sin_dato'
        assert 'los dos extremos del tramo son lecturas dudosas' in fila['motivo']


# ══════════════════════════════════════════════════════════════════════════
# Mundo 6 — Conductor activo sin cuenta de usuario
# ══════════════════════════════════════════════════════════════════════════

class TestConductorSinCuenta:

    def test_el_health_lo_cuenta(self, client, db, almacen):
        """No es trivia: `custodia.registrado_por_usuario_id` es NOT NULL.

        Cada conductor sin cuenta es alguien cuya entrega de turno la va a tener
        que registrar otro.
        """
        db.session.add(Conductor(nombre='Sin Cuenta', cedula='999', activo=True))
        db.session.commit()
        assert MedidorSQL().conductores_activos_sin_cuenta() == 1

    def test_un_conductor_inactivo_no_infla_el_contador(self, client, db, almacen):
        """Detector en las dos direcciones: que cuente lo que hay **y que no
        cuente lo que no**."""
        db.session.add(Conductor(nombre='Baja', cedula='998', activo=False))
        db.session.commit()
        assert MedidorSQL().conductores_activos_sin_cuenta() == 0

    def test_sin_cuenta_no_hay_turno_y_se_dice_por_que(self, client, db, almacen):
        """El mensaje nombra el arreglo. Un 404 pelado deja al conductor
        parado al lado del camión sin saber a quién llamar."""
        u, t = _usuario(db, almacen, 'mi_sincuenta@test.com', rol='conductor')
        r = client.get('/flota/conductor/mi-turno', headers=_auth(t))
        assert r.status_code == 404
        assert 'no está vinculado a un conductor' in r.get_json()['error']
        assert 'vincule la ficha' in r.get_json()['detalle']

    def test_puede_recibir_el_vehiculo_aunque_no_tenga_cuenta(
            self, client, db, almacen):
        """Y **debe poder**: quien registra es el del token, quien responde es
        el conductor. Son dos campos distintos a propósito — si hicieran falta
        los dos, el camión no sale a las 5 a.m.
        """
        veh = _vehiculo(db, 'SNCTA1')
        cond = Conductor(nombre='Sin Cuenta', cedula='997', activo=True)
        db.session.add(cond)
        db.session.commit()
        u, t = _usuario(db, almacen, 'mi_traspaso@test.com')
        r = client.post('/flota/custodia/traspaso', headers=_auth(t), json={
            'placa': 'SNCTA1', 'km': 1000, 'custodio_tipo': 'conductor',
            'custodio_conductor_id': cond.id, 'ubicacion': 'fuera_de_sede',
            'ubicacion_motivo': 'el camión duerme en la casa del conductor'})
        assert r.status_code == 201, r.get_json()
        from flota.adaptadores.modelos import Custodia

        fila = Custodia.query.filter_by(vehiculo_id=veh.id,
                                        fin_ts=None).one()
        # Los dos campos, y son distintos: uno responde por el vehículo, el otro
        # tecleó. Un conductor sin cuenta puede ser lo primero sin ser lo segundo.
        assert fila.custodio_conductor_id == cond.id
        assert fila.registrado_por_usuario_id == u.id


# ══════════════════════════════════════════════════════════════════════════
# Mundo 7 — Documento vencido y documento «no encontrado»
#
# No son lo mismo: una es buscar el papel, la otra es sacar el camión de ruta.
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def papeles(client, db, almacen):
    """La RTM vencida desde 2025-11-11 (medida en producción) y un SOAT que
    nadie pudo mostrar."""
    veh = _vehiculo(db, 'PAPELE')
    u, t = _usuario(db, almacen, 'mi_papeles@test.com')
    r = client.post('/flota/vehiculo/PAPELE/documentos', headers=_auth(t), json={
        'tipo': 'rtm', 'estado': 'vigente', 'numero': 'R-1', 'entidad': 'CDA',
        'fecha_expedicion': '2024-11-11', 'fecha_vencimiento': '2025-11-11'})
    assert r.status_code == 201, r.get_json()
    r = client.post('/flota/vehiculo/PAPELE/documentos', headers=_auth(t), json={
        'tipo': 'soat', 'estado': 'no_encontrado'})
    assert r.status_code == 201, r.get_json()
    return {'veh': veh, 'placa': 'PAPELE', 'u': u, 't': t, 'db': db}


class TestDocumentoVencidoYNoEncontrado:

    def test_el_health_los_cuenta_por_separado(self, client, papeles):
        m = MedidorSQL()
        assert m.documentos_vencidos() == 1
        assert m.documentos_no_encontrados() == 1

    def test_el_no_encontrado_no_inventa_fechas(self, client, papeles):
        """Si el papel no apareció, no hay de dónde salieron esas fechas.

        Aceptarlas sería inventar procedencia — que es cómo la tarjeta de
        propiedad terminó con «vence en 6955 días».
        """
        d = client.get('/flota/vehiculo/PAPELE/documentos',
                       headers=_auth(papeles['t'])).get_json()
        soat = [x for x in d['documentos'] if x['tipo'] == 'soat'][0]
        assert soat['fecha_vencimiento'] is None
        assert soat['dias_para_vencer'] is None

    def test_lo_no_mirado_se_distingue_de_lo_mirado_y_no_encontrado(
            self, client, papeles):
        """`sin_verificar` es la tercera categoría, y es la que faltaría."""
        d = client.get('/flota/vehiculo/PAPELE/documentos',
                       headers=_auth(papeles['t'])).get_json()
        assert set(d['sin_verificar']) == {'poliza_rc', 'tarjeta_propiedad'}

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO — `vencido` es un BOOLEANO sobre una pregunta que el sistema '
        'no puede contestar. Para un documento `no_encontrado`, '
        '`_serializar` calcula `vencido = fecha_vencimiento is not None and '
        '... < hoy`, y como las fechas se borran a propósito, sale '
        '`vencido: false` junto con `vence: true`: el payload afirma «es un '
        'documento que vence, y no está vencido» sobre un papel que nadie pudo '
        'mostrar. Es la regla 4 del módulo al pie de la letra — el tercer '
        'estado necesita una palabra, no un booleano. '
        'FALLA EN SILENCIO. Hoy no se ve porque `flota.js:1235` ramifica sobre '
        '`estado` ANTES de mirar `vencido`; el defecto está en el contrato, y '
        'lo paga el segundo consumidor. Arreglarlo cambia el TIPO de un campo '
        'ya publicado: no es pequeño ni seguro.'))
    def test_vencido_no_deberia_afirmar_false_sobre_un_papel_que_nadie_vio(
            self, client, papeles):
        d = client.get('/flota/vehiculo/PAPELE/documentos',
                       headers=_auth(papeles['t'])).get_json()
        soat = [x for x in d['documentos'] if x['tipo'] == 'soat'][0]
        assert soat['vencido'] == 'sin_dato', (
            f"un SOAT `no_encontrado` publica vencido={soat['vencido']!r} "
            f"con vence={soat['vence']!r}")


# ══════════════════════════════════════════════════════════════════════════
# Mundo 8 — GPS negado / sin señal / timeout
#
# Regla 4: `0,0` es un punto real en el Golfo de Guinea y el valor al que cae
# todo `float` sin inicializar.
# ══════════════════════════════════════════════════════════════════════════

class TestGpsNegadoEnLaEvidenciaDeFlota:

    def test_la_politica_existe_escrita_en_el_otro_modulo(self):
        """`app/models/geo_entrega.py` la tiene entera: `fuente`,
        `motivo_sin_dato`, y CHECK que prohíben 0,0 y el rango de Colombia.

        Se afirma acá para que el `xfail` de abajo no se lea como una carencia
        general del repo: la política está decidida y escrita **una vez**, y lo
        que falta es que el segundo consumidor la use.
        """
        from app.models.geo_entrega import EntregaGeo

        cols = {c.name for c in EntregaGeo.__table__.columns}
        assert {'fuente', 'motivo_sin_dato'} <= cols
        checks = ' '.join(str(c.sqltext) for c in EntregaGeo.__table__.constraints
                          if hasattr(c, 'sqltext'))
        assert 'motivo_sin_dato' in checks

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO — `flota_foto` guarda `gps_lat`/`gps_lon` como dos `Float` '
        'nullable pelados, sin `fuente` ni `motivo_sin_dato` y sin un solo '
        'CHECK. Un GPS negado, apagado o con timeout que llegue como 0,0 se '
        'persiste como una coordenada válida en el Golfo de Guinea, sobre la '
        'evidencia fotográfica de una custodia o un daño — que es justamente la '
        'que se mira cuando hay que atribuir algo. '
        'La misma política ya está escrita, con cuatro CHECK y hasta el rango '
        'de Colombia, en `app/models/geo_entrega.py`: es el corolario de la '
        'regla 0 del WMS cruzando el borde de dos módulos, y flota se quedó con '
        'la copia débil. '
        'FALLA EN SILENCIO. Hoy está LATENTE —`flota.js` no captura GPS, así '
        'que las columnas viven en NULL— pero la puerta está abierta para '
        'cualquier cliente. Arreglarlo es DDL: ver '
        '`scratchpad/ddl_e2e_degradacion.md`.'))
    def test_la_foto_de_flota_no_deberia_aceptar_la_coordenada_cero_cero(
            self, client, db, almacen):
        u, t = _usuario(db, almacen, 'mi_gps@test.com')
        f = Foto(clase='evidencia_estado', entidad_tipo='custodia_inicio',
                 entidad_id=1, storage_ref='2026/09/x.jpg', hash_sha256='a' * 64,
                 bytes=1000, ancho=1600, alto=1200, mime='image/jpeg',
                 angulo='frontal', ts_captura=datetime(2026, 9, 2),
                 gps_lat=0.0, gps_lon=0.0, autor_usuario_id=u.id)
        db.session.add(f)
        with pytest.raises(Exception):
            db.session.commit()


# ══════════════════════════════════════════════════════════════════════════
# Mundo 9 — Cero de todo
#
# `null` es una afirmación sobre el SISTEMA («esto no se puede medir»); `0` es
# una afirmación sobre la FLOTA («medí y no hay»). Confundirlas convierte el
# tablero en decoración con autoridad.
# ══════════════════════════════════════════════════════════════════════════

class TestCeroDeTodo:

    def test_flota_vacia_devuelve_ceros_medidos_no_nulls(
            self, client, db, almacen):
        """Con las tablas creadas y cero filas, la respuesta honesta es un
        número: **se midió, y no hay**."""
        u, t = _usuario(db, almacen, 'mi_cero@test.com')
        d = client.get('/flota/health', headers=_auth(t)).get_json()
        assert d['vehiculos_activos'] == 0
        assert d['documentos_vencidos'] == 0
        assert d['hallazgos_abiertos'] == 0

    def test_los_campos_compuestos_dicen_por_que_estan_vacios(
            self, client, db, almacen):
        """Un dict vacío y `None` significarían lo mismo, y no lo son.

        `salto_km_maximo_30d` devuelve `{'delta_km': None, 'nota': ...}` en vez
        de `None`, porque `None` ya está reservado para «no hay tabla».
        """
        u, t = _usuario(db, almacen, 'mi_cero2@test.com')
        d = client.get('/flota/health', headers=_auth(t)).get_json()
        assert d['salto_km_maximo_30d']['delta_km'] is None
        assert 'nota' in d['salto_km_maximo_30d']
        assert d['segundos_llenado_30d']['n'] == 0
        assert 'nota' in d['segundos_llenado_30d']

    def test_sin_la_tabla_el_campo_vale_null_y_jamas_cero(self, app, monkeypatch):
        """El otro lado de la distinción, ejercido quitándole la fuente al medidor."""
        import flota.adaptadores.medicion as med
        from flota.api.health import _CAMPOS, _CAMPOS_SIN_TABLA

        # La lista de exentos se IMPORTA. Estaba escrita a mano acá —
        # `('ambiente', 'datos_reales')`— y también en `test_health_flota.py`:
        # dos copias de la misma política, y agregar un campo exento exigía
        # acordarse de las dos. El 2026-09-04 se agregó uno y este test fue el
        # que lo dijo, con el otro ya en verde.
        monkeypatch.setattr(med, '_tabla_existe', lambda nombre: False)
        medidor = med.MedidorSQL()
        with app.app_context():
            for campo in _CAMPOS:
                if campo in _CAMPOS_SIN_TABLA:
                    continue
                assert getattr(medidor, campo)() is None, campo

    def test_un_vehiculo_sin_nada_SI_entra_al_cpk_y_dice_por_que(
            self, client, db, almacen):
        """Invertido el 2026-09-04, y el motivo viejo era el que estaba mal.

        Decía: *«meterlo con `sin_dato` llenaría el tablero de renglones vacíos
        el primer mes — que es cómo un tablero se deja de mirar»*. Con cero
        filas en `flota_gasto`, que es el estado real, el filtro no escondía
        renglones vacíos: devolvía `[]` y **escondía el tablero entero**. Y un
        `[]` la pantalla lo lee igual que «no hay nada que reportar».

        El vehículo del que nadie registró nada es exactamente el caso a
        atender. Lo que evita el renglón vacío no es esconderlo: es que traiga
        el motivo y el gesto que lo enciende.
        """
        _vehiculo(db, 'VACIO1')
        filas = MedidorSQL().cpk_mes()
        assert [f['placa'] for f in filas] == ['VACIO1']
        assert filas[0]['cpk'] == 'sin_dato'
        assert filas[0]['hubo_gastos'] is False
        assert filas[0]['motivo'], 'un sin_dato sin motivo no manda a nadie a hacer nada'


# ══════════════════════════════════════════════════════════════════════════
# Mundo 10 — Degradación de servicios
#
# Regla 5: ningún adaptador degrada hacia algo que se parezca al éxito.
# ══════════════════════════════════════════════════════════════════════════

class TestDegradacionDeServicios:

    def test_gupshup_caido_deja_la_fila_en_fallido_con_su_detalle(self, db):
        """No cuenta como avisado y se puede saber por qué."""
        from flota.adaptadores import avisos
        from flota.adaptadores.gupshup import AvisoNoEnviado
        from flota.adaptadores.modelos import Aviso

        class Caido:
            simulado = False

            def enviar(self, *a, **k):
                raise AvisoNoEnviado('no se pudo hablar con Gupshup: timeout')

        fila = avisos._registrar('k-1', 'flota_documento_vence', '573001112233',
                                 ['THP696', 'SOAT', '11 de septiembre'], Caido())
        assert fila.estado == 'fallido'
        assert 'timeout' in fila.detalle
        assert fila.proveedor_msg_id is None

    def test_el_canal_simulado_deja_rastro_distinguible_del_real(self):
        """Regla 8. Un id que se parece a uno real es cómo un tablero de pruebas
        se lee como producción."""
        from flota.adaptadores.gupshup import CanalSimulado

        canal = CanalSimulado()
        assert canal.simulado is True
        assert canal.enviar('573001112233', 'flota_documento_vence',
                            ['THP696', 'SOAT', '11 de septiembre']).startswith('SIMULADO-')

    def test_gupshup_que_acepta_sin_id_se_trata_como_fallo(self, monkeypatch):
        """El modo de fallo que un contador de «enviados» no puede ver: sin id
        no hay forma de cruzar el evento de entrega."""
        import flota.adaptadores.gupshup as g

        class Resp:
            status_code = 200
            text = '{}'

            def json(self):
                return {'status': 'submitted'}

        monkeypatch.setenv('GUPSHUP_API_KEY', 'k')
        monkeypatch.setenv('GUPSHUP_SOURCE', '573001112233')
        monkeypatch.setenv('GUPSHUP_APP_NAME', 'app')
        monkeypatch.setenv('GUPSHUP_TEMPLATE_IDS',
                           '{"flota_documento_vence": "0e1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b"}')
        monkeypatch.setattr(g.requests, 'post', lambda *a, **k: Resp())
        with pytest.raises(g.AvisoNoEnviado, match='messageId'):
            g.CanalGupshup().enviar('573001112233', 'flota_documento_vence',
                                    ['THP696', 'SOAT', '11 de septiembre'])

    def test_el_almacen_que_no_escribe_no_devuelve_un_hash_inventado(
            self, monkeypatch, tmp_path):
        """La foto SÍ existe y se conoce su tamaño; lo que falta es dónde quedó.

        Eso es `pendiente_evidencia`, y el health lo cuenta. Un hash de ceros
        sería una firma que dice que la foto es íntegra sin haberla mirado.
        """
        import base64
        from io import BytesIO

        from PIL import Image

        from flota.adaptadores import almacen_fotos

        buf = BytesIO()
        Image.new('RGB', (1600, 1200), 'white').save(buf, 'JPEG')
        url = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()

        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        monkeypatch.setattr(
            almacen_fotos.AlmacenLocal, 'guardar',
            lambda self, c, m: (_ for _ in ()).throw(
                almacen_fotos.ErrorAlmacen('disco lleno')))

        campos = almacen_fotos.guardar_foto(
            {'clase': 'foto_dato', 'data_url': url, 'angulo': 'tablero'})
        assert campos['estado'] == 'pendiente_evidencia'
        assert campos['hash_sha256'] == ''
        assert campos['storage_ref'].startswith('sin-guardar:')
        assert 'disco lleno' in campos['storage_ref']

    def test_sin_FLOTA_FOTOS_DIR_no_hay_default_silencioso(self, monkeypatch):
        """Un default a `/tmp` haría que las fotos se guarden y desaparezcan en
        el próximo deploy: peor que no guardarlas."""
        from flota.adaptadores import almacen_fotos

        monkeypatch.delenv('FLOTA_FOTOS_DIR', raising=False)
        with pytest.raises(almacen_fotos.ErrorAlmacen, match='FLOTA_FOTOS_DIR'):
            almacen_fotos.AlmacenLocal().guardar(b'x', 'image/jpeg')

    def test_pil_ausente_degrada_a_no_medi_y_no_a_cero(self, monkeypatch):
        """`None` es un tercer estado: «esto no es una imagen que yo sepa
        abrir», no «mide cero». Unas dimensiones de 0x0 pasarían el CHECK de
        resolución por la puerta de atrás."""
        import builtins

        from flota.adaptadores import almacen_fotos

        real = builtins.__import__

        def sin_pil(nombre, *a, **k):
            if nombre.startswith('PIL'):
                raise ImportError('sin Pillow en este entorno')
            return real(nombre, *a, **k)

        monkeypatch.setattr(builtins, '__import__', sin_pil)
        assert almacen_fotos._medir_dimensiones(b'\xff\xd8\xff\xe0 loquesea') is None

    def test_lo_declarado_por_el_cliente_no_reemplaza_a_lo_medido(
            self, monkeypatch, tmp_path):
        """Detector en las dos direcciones: cuando PIL SÍ puede medir, **gana lo
        medido**.

        El CHECK que exige 1600 px de lado largo para una `foto_dato` evaluaba
        el número autorreportado: cualquiera que arme el POST a mano declara
        `{ancho: 4000}` sobre una imagen de 100 px y pasa. Acá se declara
        4000×3000 sobre un JPEG de 100×80 y lo que queda en la fila es 100×80 —
        que es lo que después el CHECK rechaza.
        """
        import base64
        from io import BytesIO

        from PIL import Image

        from flota.adaptadores import almacen_fotos

        buf = BytesIO()
        Image.new('RGB', (100, 80), 'white').save(buf, 'JPEG')
        url = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()

        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        campos = almacen_fotos.guardar_foto({
            'clase': 'foto_dato', 'data_url': url, 'angulo': 'tablero',
            'ancho': 4000, 'alto': 3000})
        assert (campos['ancho'], campos['alto']) == (100, 80)
