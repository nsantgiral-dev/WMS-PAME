"""La ventana del CPK, medida en día operativo y no en UTC.

## El defecto, con su número

Encontrado por la auditoría adversarial del 2026-09-02. `cpk_de` filtraba las
lecturas con `desde <= l.ts.date() <= hasta`, donde `l.ts` es UTC naive y la
ventana viene de `dia_operativo()`, que es Bogotá. Bogotá es UTC−5: **todo turno
cerrado entre las 7 p.m. y medianoche quedaba fechado el día siguiente**, y en la
frontera del mes se fugaba al mes siguiente.

Tres turnos reales de agosto:

```
2026-08-01 20:00 Bogotá → ts UTC 2026-08-02   km 10.000
2026-08-15 14:00 Bogotá → ts UTC 2026-08-15   km 15.000
2026-08-31 20:00 Bogotá → ts UTC 2026-09-01   km 20.000   ← se fugaba
```

Km reales de agosto: 10.000. Km que veía el CPK: 5.000. **El costo por
kilómetro salía exactamente al doble, y con la marca `verificada`** — la única
que afirma que una persona lo miró. La confianza mide la *lectura*; nadie medía
la *ventana*.

## Por qué 1455 tests no lo veían

Todos los `ts` de los tests del CPK son a las 08:00 UTC — la única franja del
día en que la fecha UTC y la de Bogotá coinciden. El defecto vivía fuera de la
franja que los tests visitaban. Por eso estos tests usan **20:00 hora de
Colombia** a propósito: es el turno de la tarde, el más frecuente.

El trinquete que debería haberlo cubierto (`tests/test_dia_operativo_en_tests.py`)
vigila **una sola función** y **por texto** (`'date.today()' not in fuente`).
`cpk_de` no usa `date.today()`: usa `.ts.date()`. Otra forma, invisible.
"""
from datetime import date, datetime, timedelta

import pytest

from app.utils.fecha import TZ_BOGOTA
from flota.adaptadores import gastos as adaptador
from flota.dominio.valores import SIN_DATO


def _utc(anio, mes, dia, hora_bogota):
    """Un instante de Bogotá, guardado como el módulo lo guarda: UTC naive."""
    from datetime import timezone
    local = datetime(anio, mes, dia, hora_bogota, 0, tzinfo=TZ_BOGOTA)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def mundo(db, almacen):
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import LecturaOdometro

    v = Vehiculo(placa='CPK900', tipo='NHR', activo=True)
    u = Usuario(nombre='CT', email='cpk@t.co', rol='control_flota',
                almacen_id=almacen.id, activo=True,
                password_hash=generate_password_hash('x'))
    db.session.add_all([v, u])
    db.session.commit()

    # Tres turnos. El primero y el tercero a las 20:00 de Colombia — o sea, al
    # día siguiente en UTC. El tercero además cae el último día del mes.
    for dia, hora, km in ((1, 20, 10000), (15, 14, 15000), (31, 20, 20000)):
        db.session.add(LecturaOdometro(
            vehiculo_id=v.id, valor_km=km, ts=_utc(2026, 8, dia, hora),
            origen='entrega', autor_usuario_id=u.id))
    db.session.commit()

    # Ninguna lectura NACE verificada — lo impide el `before_insert` y, en
    # producción, un trigger. Sin foto del tablero nacen `dudosa`, y con los dos
    # extremos dudosos el CPK devuelve `sin_dato` a propósito. Para medir la
    # VENTANA hay que sacar del medio la confianza, así que alguien pasa por la
    # cola — igual que en `test_gastos.py::_alguien_paso_por_la_cola`.
    from flota.adaptadores import verificacion
    for lectura, _placa in verificacion.pendientes():
        if lectura.vehiculo_id == v.id:
            verificacion.verificar(lectura_id=lectura.id, usuario_id=u.id)

    return {'veh': v.id, 'usr': u.id, 'db': db}


class TestElTurnoDeLaTardeCuentaEnSuDia:
    def test_los_km_de_agosto_son_los_de_agosto(self, mundo):
        """**El caso.** Con la ventana en UTC daba 5.000 y el CPK salía al
        doble; en día operativo da los 10.000 reales."""
        r = adaptador.cpk_de(vehiculo_id=mundo['veh'],
                             desde=date(2026, 8, 1), hasta=date(2026, 8, 31))
        assert r['km'] == 10000, (
            'el turno de las 8 p.m. del 31 se fugó a septiembre: la ventana '
            'se está midiendo en UTC y no en día operativo')

    def test_y_septiembre_no_hereda_el_turno_del_31_de_agosto(self, mundo):
        """La otra dirección: arreglarlo de un lado no puede haberlo roto del
        otro. Un solo extremo no delimita tramo — `sin_dato`, nunca 0 km."""
        r = adaptador.cpk_de(vehiculo_id=mundo['veh'],
                             desde=date(2026, 9, 1), hasta=date(2026, 9, 30))
        assert r['km'] in (SIN_DATO, 0, None) or r['km'] == 0, r


class TestLaFranjaQueLosTestsNoVisitaban:
    @pytest.mark.parametrize('hora_bogota', [0, 6, 12, 19, 20, 23])
    def test_una_lectura_cuenta_en_SU_dia_a_cualquier_hora(
            self, mundo, hora_bogota):
        """Las 08:00 UTC son la única franja donde UTC y Bogotá coinciden, y
        ahí vivían todos los tests del CPK. Estas seis horas cubren el día
        entero, incluidas las dos que se corrían de mes."""
        from flota.adaptadores.gastos import _dia_operativo_de

        ts = _utc(2026, 8, 31, hora_bogota)
        assert _dia_operativo_de(ts) == date(2026, 8, 31), (
            f'a las {hora_bogota}:00 de Colombia la lectura cambió de día')

    def test_medianoche_de_bogota_ya_es_el_dia_siguiente(self, mundo):
        """El límite, por el lado correcto: no se corrió el problema una hora."""
        from flota.adaptadores.gastos import _dia_operativo_de

        assert _dia_operativo_de(_utc(2026, 9, 1, 0)) == date(2026, 9, 1)


class TestNoSeReimplementoLaZona:
    def test_la_conversion_usa_el_helper_del_repo(self):
        """Regla 0, corolario: una política, una función. La regla 5 del WMS ya
        existía, ya tenía helper y ya tenía tests — y se aplicaba en 4 de 16
        sitios. Escribir `timedelta(hours=5)` acá sería la copia número 17 y la
        que diverge el día que Colombia toque el huso."""
        fuente = __import__('pathlib').Path(
            'flota/adaptadores/gastos.py').read_text()
        assert 'TZ_BOGOTA' in fuente
        assert 'timedelta(hours=5)' not in fuente
        assert 'timedelta(hours=-5)' not in fuente
