"""Los seis campos de odómetro del health miden, y miden hechos.

## Por qué existen

Hasta el 2026-09-01 el health de flota tenía **quince campos y ninguno del
odómetro**: `flota_lectura_odometro` aparecía en `medicion.py` solo dentro de
`_TABLAS_TANDA_1`, para declarar que la tabla existe.

Por eso nada avisó de lo que había adentro. Medido contra producción ese día:

    26 lecturas · 0 con foto · 20 duplicados exactos
    +16.354.514 km en 312 horas (THP696) → el camión dejó de poder medirse
    4 de 6 fichas con `km_inicial` que contradice su primera lectura

Un tablero que no mira una tabla no puede decir que está bien: no dice nada, y
el silencio se lee como conformidad.

## Publican HECHOS, no umbrales — y eso es deliberado

`salto_km_maximo_30d` no dice si un salto está mal. Dice **cuánto fue, de qué
vehículo y en cuántas horas**. Con un mes de esto se puede fijar un
`km_dia_plausible_max` por vehículo con procedencia —igual que
`distribucion_fuente` en la ficha— en vez de escribir hoy un techo a ojo.

Es la regla 13 aplicada antes de que el número exista: *ningún número entra sin
decir contra qué base y en qué fecha*. Un umbral inventado produce alertas
inventadas, y un canal que grita se apaga solo.

Y por eso tampoco se impone `km_inicial` como piso del odómetro: **4 de 6
fichas lo contradicen**, la del THP696 dice 433.434 contra una primera lectura
de 55.349, y no se sabe cuál miente. Ponerlo como techo habría trabado ese
camión por una segunda vía. Se cuenta, se mira, y después se decide.

## La disciplina de este archivo

`test_health_flota.py` la deja escrita: *«cada uno de estos campos tiene su test
que MUEVE UN DATO REAL y verifica que el número se mueve. Un campo que devuelve
una constante plausible es indistinguible de uno medido hasta el día en que
importa.»* Acá se cumple para los seis.
"""
from datetime import datetime, timedelta

import pytest

_T0 = datetime(2026, 9, 1, 5, 0, 0)


@pytest.fixture
def medidor():
    from flota.adaptadores.medicion import MedidorSQL
    return MedidorSQL()


@pytest.fixture
def base(db):
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    v1 = Vehiculo(placa='ODO-001', tipo='NHR', activo=True)
    v2 = Vehiculo(placa='ODO-002', tipo='NHR', activo=True)
    u = Usuario(email='odo_h@test.com', nombre='CT', rol='admin', activo=True)
    u.set_password('x')
    db.session.add_all([v1, v2, u])
    db.session.commit()
    return {'v1': v1.id, 'v2': v2.id, 'u': u.id}


def _lectura(db, base, vehiculo, km, minutos=0, origen='entrega',
             motivo=None, foto_id=None):
    from flota.adaptadores.modelos import LecturaOdometro
    l = LecturaOdometro(vehiculo_id=vehiculo, valor_km=km,
                        ts=_T0 + timedelta(minutes=minutos), origen=origen,
                        autor_usuario_id=base['u'], motivo_correccion=motivo,
                        foto_id=foto_id)
    db.session.add(l)
    db.session.commit()
    return l


class TestVehiculosSinLectura:
    def test_se_mueve_al_registrar_la_primera_lectura(self, app, db, base, medidor):
        assert medidor.vehiculos_sin_lectura() == 2
        _lectura(db, base, base['v1'], 1000)
        assert medidor.vehiculos_sin_lectura() == 1, (
            'el contador no bajó: no está midiendo, está devolviendo una '
            'constante')

    def test_no_confunde_sin_lectura_con_cero_kilometros(self, app, db, base,
                                                         medidor):
        """Regla 4: un vehículo sin lecturas no es un vehículo con 0 km."""
        _lectura(db, base, base['v1'], 0)
        assert medidor.vehiculos_sin_lectura() == 1, (
            'una lectura de 0 km cuenta como lectura: el vehículo ya no es '
            '«sin dato»')


class TestLecturasSinFoto:
    def test_cuenta_las_que_no_tienen_evidencia(self, app, db, base, medidor):
        _lectura(db, base, base['v1'], 1000)
        _lectura(db, base, base['v1'], 1100, minutos=60)
        assert medidor.lecturas_sin_foto() == 2

    def test_baja_cuando_la_lectura_trae_su_foto(self, app, db, base, medidor,
                                                 tmp_path, monkeypatch):
        """La lectura nace con `foto_id` — no se puede actualizar después,
        la tabla es append-only."""
        import base64
        import io

        from PIL import Image
        from flota.adaptadores.almacen_fotos import guardar_foto
        from flota.adaptadores.modelos import Foto

        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
        buf = io.BytesIO()
        Image.new('RGB', (1600, 1200)).save(buf, 'JPEG')
        url = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()
        campos = guardar_foto({'clase': 'foto_dato', 'data_url': url,
                               'ancho': 1600, 'alto': 1200})
        f = Foto(entidad_tipo='custodia_inicio', entidad_id=1,
                 ts_captura=_T0, autor_usuario_id=base['u'], **campos)
        db.session.add(f)
        db.session.commit()

        _lectura(db, base, base['v1'], 1000)
        assert medidor.lecturas_sin_foto() == 1
        _lectura(db, base, base['v1'], 1100, minutos=60, foto_id=f.id)
        assert medidor.lecturas_sin_foto() == 1, (
            'la lectura con foto siguió contando como sin evidencia')


class TestLecturasCorreccion:
    def test_solo_cuenta_correcciones(self, app, db, base, medidor):
        _lectura(db, base, base['v1'], 1000)
        assert medidor.lecturas_correccion_30d() == 0
        _lectura(db, base, base['v1'], 500, minutos=60, origen='correccion',
                 motivo='dedazo')
        assert medidor.lecturas_correccion_30d() == 1

    def test_no_cuenta_las_viejas(self, app, db, base, medidor):
        """La ventana es de 30 días: una corrección de hace un año no dice
        nada sobre cómo se está operando hoy."""
        _lectura(db, base, base['v1'], 1000, minutos=-60 * 24 * 400,
                 origen='correccion', motivo='vieja')
        assert medidor.lecturas_correccion_30d() == 0


class TestSaltoMaximo:
    def test_informa_el_salto_con_su_vehiculo_y_sus_horas(self, app, db, base,
                                                          medidor):
        """**El caso THP696.** No juzga: informa."""
        _lectura(db, base, base['v1'], 55_349)
        _lectura(db, base, base['v1'], 16_697_948, minutos=312 * 60)
        r = medidor.salto_km_maximo_30d()
        assert r['delta_km'] == 16_642_599
        assert r['vehiculo_id'] == base['v1']
        assert r['horas'] == 312.0

    def test_toma_el_mayor_entre_vehiculos(self, app, db, base, medidor):
        _lectura(db, base, base['v1'], 100)
        _lectura(db, base, base['v1'], 300, minutos=60)      # +200
        _lectura(db, base, base['v2'], 100)
        _lectura(db, base, base['v2'], 5_100, minutos=60)    # +5.000
        r = medidor.salto_km_maximo_30d()
        assert r['delta_km'] == 5_000
        assert r['vehiculo_id'] == base['v2']

    def test_no_mezcla_vehiculos_al_calcular_el_delta(self, app, db, base,
                                                      medidor):
        """Sin agrupar por vehículo, la lectura de un camión menos la del otro
        daría un salto que nunca ocurrió."""
        _lectura(db, base, base['v1'], 900_000)
        _lectura(db, base, base['v2'], 100, minutos=1)
        _lectura(db, base, base['v2'], 200, minutos=60)
        r = medidor.salto_km_maximo_30d()
        assert r['delta_km'] == 100, (
            f'calculó {r["delta_km"]}: está restando lecturas de vehículos '
            f'distintos')

    def test_sin_saltos_devuelve_un_dict_que_lo_dice_no_None(self, app, db, base,
                                                             medidor):
        """`None` ya significa «no hay tabla». Un mismo valor con dos
        significados es el defecto que este módulo persigue."""
        _lectura(db, base, base['v1'], 1000)
        r = medidor.salto_km_maximo_30d()
        assert r is not None
        assert r['delta_km'] is None
        assert 'nota' in r

    def test_las_horas_cero_no_se_vuelven_velocidad_infinita(self, app, db, base,
                                                             medidor):
        """Dos lecturas en el mismo instante son un hecho común acá — hay diez
        así en producción. Se informa el par, no se divide."""
        _lectura(db, base, base['v1'], 1000, minutos=0)
        _lectura(db, base, base['v1'], 1500, minutos=0)
        r = medidor.salto_km_maximo_30d()
        assert r['delta_km'] == 500
        assert r['horas'] == 0.0


class TestTimestampsDuplicados:
    def test_cuenta_las_que_comparten_instante(self, app, db, base, medidor):
        assert medidor.lecturas_ts_duplicado() == 0
        _lectura(db, base, base['v1'], 1000, minutos=0)
        _lectura(db, base, base['v1'], 1000, minutos=0)
        assert medidor.lecturas_ts_duplicado() == 2

    def test_no_cuenta_el_mismo_instante_de_vehiculos_distintos(self, app, db,
                                                                base, medidor):
        """Dos camiones entregados a la misma hora es lo normal, no un
        duplicado."""
        _lectura(db, base, base['v1'], 1000, minutos=0)
        _lectura(db, base, base['v2'], 2000, minutos=0)
        assert medidor.lecturas_ts_duplicado() == 0


class TestAnclaIncoherente:
    @staticmethod
    def _ficha(db, vehiculo, km_inicial):
        from flota.adaptadores.modelos import FichaTecnica
        # `posiciones_llanta` es NOT NULL: cuántas ruedas tiene el vehículo no
        # admite «no sé», y el número decide cuántas fotos se exigen.
        # `km_inicial_ts` va junto al ancla: el modelo no admite un
        # kilometraje de referencia sin decir cuándo se levantó. Es el mismo
        # patrón de procedencia que `distribucion_fuente`.
        db.session.add(FichaTecnica(
            vehiculo_id=vehiculo, km_inicial=km_inicial, posiciones_llanta=6,
            km_inicial_ts=_T0 if km_inicial is not None else None))
        db.session.commit()

    def test_detecta_el_ancla_por_encima_de_la_primera_lectura(self, app, db,
                                                               base, medidor):
        """**El caso THP696**: ficha 433.434, primera lectura 55.349."""
        self._ficha(db, base['v1'], 433_434)
        _lectura(db, base, base['v1'], 55_349)
        assert medidor.fichas_con_ancla_incoherente() == 1

    def test_un_ancla_coherente_no_cuenta(self, app, db, base, medidor):
        self._ficha(db, base['v1'], 50_000)
        _lectura(db, base, base['v1'], 55_349)
        assert medidor.fichas_con_ancla_incoherente() == 0

    # No hay test de «ficha sin ancla»: `km_inicial` es `nullable=False` en el
    # modelo y en la migración, así que ese estado no existe. Un test que
    # construye un estado imposible pasa siempre y no mide nada — y obliga a
    # mantener en el código la rama muerta que lo atiende.

    def test_una_ficha_sin_lecturas_no_cuenta(self, app, db, base, medidor):
        """No hay contra qué compararla todavía."""
        self._ficha(db, base['v1'], 433_434)
        assert medidor.fichas_con_ancla_incoherente() == 0


class TestSinTablaEsNoneYNoCero:
    """La regla del archivo de medición: `None` por una sola causa declarada
    —la tabla no existe— y nunca 0 por defecto. Un 0 es una afirmación sobre la
    flota; `null` es una afirmación sobre el sistema."""

    @pytest.mark.parametrize('campo', [
        'vehiculos_sin_lectura', 'lecturas_sin_foto', 'lecturas_correccion_30d',
        'salto_km_maximo_30d', 'lecturas_ts_duplicado',
        'fichas_con_ancla_incoherente',
    ])
    def test_sin_tabla_devuelve_None(self, app, monkeypatch, campo):
        import flota.adaptadores.medicion as med
        monkeypatch.setattr(med, '_tabla_existe', lambda nombre: False)
        assert getattr(med.MedidorSQL(), campo)() is None
