"""
El conductor como geocodificador — y la ausencia que NO es 0,0.

El sistema no sabe dónde queda ningún cliente. No es que las direcciones estén
sucias: **no hay direcciones** (`pedidos_sync_service.py:124` lee códigos de
departamento y ciudad). La salida es que el conductor ponga el punto al
confirmar la parada, y este archivo prueba las dos mitades de eso.

## Por qué cada test tiene su gemelo

La lección más cara de la auditoría del 2026-08-15 son **seis guards en verde
sobre propiedades que la vía sana satisfacía por construcción**: todos tenían
el test de «no dispara cuando está sano» y **ninguno** el de «dispara cuando
está roto». Acá los dos lados están escritos a propósito y en pares:

    una coordenada ausente NO se guarda como 0,0   ←→   una legítima SÍ se guarda
    el CHECK rechaza 0,0                           ←→   el CHECK acepta Neiva
    la mediana descarta al outlier                 ←→   la mediana no descarta un cluster sano
    el maestro no se pisa al re-confirmar          ←→   el maestro SÍ se completa si faltaba
    la cobertura cuenta a los que tienen           ←→   y también a los que no

`TestElCheckDeLaBaseMuerde` es el más importante de todos y es el único que no
pasa por el código: escribe 0,0 **directo con SQL** y exige que la base lo
rechace. Sin eso, «no se guarda 0,0» solo afirma que la ruta feliz de hoy no lo
hace — y la ruta feliz de hoy es exactamente la que no falla nunca.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.services import geo_cliente as geo

# Un punto real de Neiva (centro), y otro a ~120 m. Se usan en todo el archivo
# para que una coordenada legítima sea siempre reconocible al leer.
NEIVA = (2.927300, -75.281900)
NEIVA_CERCA = (2.928300, -75.281500)
#: A ~7 km del anterior — otra tienda, no otro GPS.
NEIVA_LEJOS = (2.990000, -75.281900)


class _CapturaFalsa:
    """Lo mínimo que `elegir_coordenada_del_maestro` necesita.

    Se construye a mano y no con `EntregaGeo` a propósito: la política tiene
    que poder probarse sin base de datos, que es lo que la hace legible y lo
    que impide que termine adentro de una consulta.
    """

    def __init__(self, lat, lon, precision_m=20.0, fuente=geo.GPS_CONDUCTOR,
                 capturado_en=None):
        self.lat, self.lon = lat, lon
        self.precision_m = precision_m
        self.fuente = fuente
        self.capturado_en = capturado_en


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Lo que llega del teléfono
# ═══════════════════════════════════════════════════════════════════════════

class TestLaAusenciaSeModelaConPalabras:
    """Regla 4 del módulo de flota. `0,0` es el Golfo de Guinea, no «no sé»."""

    @pytest.mark.parametrize('motivo', [
        'permiso_denegado', 'sin_senal', 'no_soportado', 'timeout', 'no_se_pidio'])
    def test_el_motivo_declarado_por_el_navegador_se_conserva(self, motivo):
        c = geo.leer_captura_del_conductor({'fuente': 'sin_dato', 'motivo': motivo})
        assert c.lat is None and c.lon is None
        assert c.fuente == geo.SIN_DATO
        assert c.motivo_sin_dato == motivo

    def test_cero_cero_no_es_una_coordenada(self):
        """**El caso.** 0,0 pasa cualquier validación de rango global y cae en
        el Golfo de Guinea, a 5.000 km de Neiva."""
        c = geo.leer_captura_del_conductor(
            {'lat': 0, 'lon': 0, 'precision_m': 10, 'fuente': 'gps_conductor'})
        assert c.lat is None and c.lon is None
        assert c.fuente == geo.SIN_DATO
        assert c.motivo_sin_dato == geo.FUERA_DE_RANGO

    def test_ejes_cambiados_tampoco(self):
        """`lat=-75.28, lon=2.93` también pasa las validaciones globales, y
        pone la tienda en el Pacífico Sur."""
        c = geo.leer_captura_del_conductor(
            {'lat': -75.2819, 'lon': 2.9273, 'precision_m': 10})
        assert c.fuente == geo.SIN_DATO
        assert c.motivo_sin_dato == geo.FUERA_DE_RANGO

    def test_un_motivo_desconocido_no_queda_en_null(self):
        c = geo.leer_captura_del_conductor({'fuente': 'sin_dato', 'motivo': 'ni_idea'})
        assert c.motivo_sin_dato == geo.NO_DECLARADO

    def test_no_venir_nada_no_es_lo_mismo_que_no_saber(self):
        """`None` = cliente viejo o vía que no captura. Guardar `sin_dato`
        para eso metería en el denominador de la adopción a paradas a las que
        nunca se les preguntó."""
        assert geo.leer_captura_del_conductor(None) is None
        assert geo.leer_captura_del_conductor('') is None
        assert geo.leer_captura_del_conductor(42) is None

    def test_un_booleano_no_es_una_latitud(self):
        """`isinstance(True, int)` es True en Python: sin el guard, `lat: true`
        entra como latitud **1.0**.

        La longitud va real a propósito. Con `lon: true` también, el par
        quedaría en (1.0, 1.0) — fuera de la caja de Colombia— y el rechazo
        vendría del rango, no del guard de tipo: el test pasaría con el guard
        borrado y no significaría nada. Verificado con el arnés de mutación:
        así escrito, sobrevivía.
        """
        c = geo.leer_captura_del_conductor(
            {'lat': True, 'lon': -75.2819, 'precision_m': 5})
        assert c.fuente == geo.SIN_DATO
        assert c.lat is None

    @pytest.mark.parametrize('valor', ['nan', 'inf', '-inf'])
    def test_nan_e_infinito_no_son_coordenadas(self, valor):
        c = geo.leer_captura_del_conductor(
            {'lat': float(valor), 'lon': -75.28, 'precision_m': 5})
        assert c.fuente == geo.SIN_DATO

    @pytest.mark.parametrize('valor', ['nan', 'inf'])
    def test_una_precision_no_numerica_se_lee_como_desconocida(self, valor):
        """Donde el guard de `NaN`/infinito hace falta de verdad.

        En `lat`/`lon` la caja de Colombia lo tapa (toda comparación con `NaN`
        da False), así que ese test solo mide el rango. En `precision_m` no hay
        red: un `NaN` pasaría el `<= 0`, llegaría a la columna y el CHECK
        `precision_m > 0` tumbaría la fila entera — la captura se perdería en
        el `except` sin que nadie se entere.
        """
        c = geo.leer_captura_del_conductor(
            {'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': float(valor)})
        assert c.lat == NEIVA[0], 'la coordenada es buena, no se descarta'
        assert c.precision_m is None


class TestUnaCoordenadaLegitimaSiPasa:
    """El gemelo del bloque de arriba. Sin esto, «nada se guarda» sería un
    detector perfecto y un producto inútil."""

    def test_neiva_entra_con_su_precision_y_su_procedencia(self):
        c = geo.leer_captura_del_conductor(
            {'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': 18.5})
        assert (c.lat, c.lon) == NEIVA
        assert c.precision_m == 18.5
        assert c.fuente == geo.GPS_CONDUCTOR
        assert c.motivo_sin_dato is None

    def test_sin_precision_la_coordenada_se_guarda_igual(self):
        """Una captura sin precisión declarada es información —el camión estuvo
        cerca— y se guarda. Lo que pierde es el voto en el maestro."""
        c = geo.leer_captura_del_conductor({'lat': NEIVA[0], 'lon': NEIVA[1]})
        assert (c.lat, c.lon) == NEIVA
        assert c.precision_m is None
        assert c.fuente == geo.GPS_CONDUCTOR

    def test_precision_cero_se_lee_como_desconocida(self):
        """Cero metros de incertidumbre no existe: es un campo mal llenado."""
        c = geo.leer_captura_del_conductor(
            {'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': 0})
        assert c.lat == NEIVA[0]
        assert c.precision_m is None

    def test_la_procedencia_no_la_elige_el_cliente_libremente(self):
        """Un `fuente` inventado cae a `gps_conductor`, no se propaga. La
        columna sirve para decidir precedencia en el maestro: aceptar
        cualquier string dejaría al navegador declarar `corregida_a_mano` y
        ganarle a toda la serie."""
        c = geo.leer_captura_del_conductor(
            {'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': 9, 'fuente': 'inventada'})
        assert c.fuente == geo.GPS_CONDUCTOR


class TestLaClaveDelCliente:

    def test_normaliza_caja_tildes_y_espacios(self):
        a = geo.clave_de_cliente('  Papelería   Él  Éxito ', 'Neiva')
        b = geo.clave_de_cliente('PAPELERIA EL EXITO', 'NEIVA')
        assert a == b == 'PAPELERIA EL EXITO|NEIVA'

    def test_el_municipio_separa_homonimos(self):
        """La decisión que evita el peor fallo: dos tiendas homónimas en
        municipios distintos colapsadas dan una mediana en la carretera entre
        las dos — un maestro **activamente equivocado**."""
        assert (geo.clave_de_cliente('MISCELANEA LA 5', 'Neiva')
                != geo.clave_de_cliente('MISCELANEA LA 5', 'Pitalito'))

    def test_sin_razon_social_no_hay_clave(self):
        assert geo.clave_de_cliente('', 'Neiva') is None
        assert geo.clave_de_cliente(None, 'Neiva') is None


# ═══════════════════════════════════════════════════════════════════════════
# 2 · La política del maestro
# ═══════════════════════════════════════════════════════════════════════════

class TestElMaestroEligeConMediana:

    def test_una_sola_captura_es_el_maestro_y_lo_declara(self):
        e = geo.elegir_coordenada_del_maestro([_CapturaFalsa(*NEIVA, 15.0)])
        assert (round(e.lat, 6), round(e.lon, 6)) == NEIVA
        assert e.fuente == geo.GPS_CONDUCTOR
        assert e.consideradas == 1

    def test_la_mediana_ignora_un_outlier_entre_muchas_buenas(self):
        """Cuatro capturas juntas y una a 7 km: la mediana no se mueve. Es el
        motivo entero de haber elegido mediana sobre «la más reciente»."""
        capturas = [_CapturaFalsa(2.9273, -75.2819),
                    _CapturaFalsa(2.9274, -75.2818),
                    _CapturaFalsa(2.9272, -75.2820),
                    _CapturaFalsa(2.9273, -75.2819),
                    _CapturaFalsa(*NEIVA_LEJOS)]
        e = geo.elegir_coordenada_del_maestro(capturas)
        assert geo.distancia_m(e.lat, e.lon, *NEIVA) < 50
        assert e.descartadas == 1, 'la lejana tiene que contarse, no desaparecer'

    def test_un_cluster_sano_no_descarta_nada(self):
        """El gemelo: el descarte tiene que ser 0 cuando no hay nada raro. Sin
        esto, un detector que descarta siempre pasaría el test de arriba."""
        e = geo.elegir_coordenada_del_maestro(
            [_CapturaFalsa(*NEIVA), _CapturaFalsa(*NEIVA_CERCA)])
        assert e.descartadas == 0
        assert e.consideradas == 2

    def test_precision_mala_no_vota_pero_se_cuenta(self):
        e = geo.elegir_coordenada_del_maestro([
            _CapturaFalsa(*NEIVA, 20.0),
            _CapturaFalsa(*NEIVA_LEJOS, 3000.0),
        ])
        assert geo.distancia_m(e.lat, e.lon, *NEIVA) < 5
        assert e.consideradas == 1
        assert e.descartadas == 1

    def test_precision_desconocida_no_vota(self):
        """«No sé qué tan bueno es este punto» no autoriza a usarlo para mandar
        un camión. Regla 0."""
        e = geo.elegir_coordenada_del_maestro([_CapturaFalsa(*NEIVA, None)])
        assert e.lat is None
        assert e.motivo_sin_maestro == geo.PRECISION_INSUFICIENTE

    def test_sin_capturas_y_sin_precision_son_motivos_distintos(self):
        """Se arreglan distinto: uno pidiéndole al conductor que toque el
        botón, el otro mirando el dispositivo. Colapsarlos deja al que mira el
        tablero sin saber a quién llamar."""
        assert geo.elegir_coordenada_del_maestro([]).motivo_sin_maestro == geo.SIN_CAPTURAS
        solo_sin_dato = [_CapturaFalsa(None, None, None, geo.SIN_DATO)]
        assert (geo.elegir_coordenada_del_maestro(solo_sin_dato).motivo_sin_maestro
                == geo.SIN_CAPTURAS)

    def test_mitad_y_mitad_no_elige_el_punto_del_medio(self):
        """Dos clusters iguales bajo una misma clave: el punto medio mandaría
        al conductor a **ninguna** de las dos tiendas. Peor que no tener
        maestro. Regla 0."""
        e = geo.elegir_coordenada_del_maestro([
            _CapturaFalsa(2.9273, -75.2819), _CapturaFalsa(2.9274, -75.2818),
            _CapturaFalsa(2.9900, -75.2819), _CapturaFalsa(2.9901, -75.2818),
        ])
        assert e.lat is None
        assert e.motivo_sin_maestro == geo.CAPTURAS_DISPERSAS

    def test_una_correccion_a_mano_le_gana_a_todo_el_gps(self):
        from datetime import datetime
        e = geo.elegir_coordenada_del_maestro([
            _CapturaFalsa(2.9273, -75.2819, 5.0),
            _CapturaFalsa(2.9273, -75.2819, 5.0),
            _CapturaFalsa(*NEIVA_LEJOS, 8.0, geo.CORREGIDA_A_MANO,
                          datetime(2026, 9, 1)),
        ])
        assert e.fuente == geo.CORREGIDA_A_MANO
        assert (round(e.lat, 6), round(e.lon, 6)) == NEIVA_LEJOS

    def test_entre_dos_correcciones_gana_la_ultima(self):
        from datetime import datetime
        e = geo.elegir_coordenada_del_maestro([
            _CapturaFalsa(*NEIVA, 5.0, geo.CORREGIDA_A_MANO, datetime(2026, 1, 1)),
            _CapturaFalsa(*NEIVA_LEJOS, 5.0, geo.CORREGIDA_A_MANO, datetime(2026, 9, 1)),
        ])
        assert (round(e.lat, 6), round(e.lon, 6)) == NEIVA_LEJOS


class TestLaDistanciaMideMetros:
    """Un haversine equivocado por un factor deja los tres umbrales sin
    sentido y ningún otro test lo notaría: la mediana seguiría dando el mismo
    punto."""

    def test_un_grado_de_latitud_son_unos_111_km(self):
        d = geo.distancia_m(0.0, -75.0, 1.0, -75.0)
        assert 110_000 < d < 112_000

    def test_el_mismo_punto_da_cero(self):
        assert geo.distancia_m(*NEIVA, *NEIVA) == pytest.approx(0.0, abs=1e-6)

    def test_los_120_metros_de_la_fixture_son_120_metros(self):
        assert 100 < geo.distancia_m(*NEIVA, *NEIVA_CERCA) < 200


# ═══════════════════════════════════════════════════════════════════════════
# 3 · La base
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def parada_lista(db, almacen):
    """Ruta EN_TRANSITO + tarea de un cliente con nombre + bulto."""
    from app.models.bulto import Bulto
    from app.models.conductor import Conductor
    from app.models.packing import TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    from app.models.usuario import Usuario

    user = Usuario(email='geo@test.com', nombre='Conductor Geo',
                   rol='conductor', activo=True)
    user.set_password('test123')
    db.session.add(user)
    db.session.flush()
    conductor = Conductor(usuario_id=user.id, nombre='Conductor Geo',
                          cedula='7778889', activo=True)
    db.session.add(conductor)
    db.session.flush()

    ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana',
                        estado='EN_TRANSITO')
    db.session.add(ruta)
    db.session.flush()

    tarea = TareaPacking(
        codigo=f'PK-GEO-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
        almacen_id=almacen.id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=2500,
        numero_pedido_siesa='PED-GEO',
        cliente='PAPELERIA LA 5', municipio='Neiva',
    )
    db.session.add(tarea)
    db.session.flush()

    db.session.add(Bulto(
        tarea_id=tarea.id, codigo_barras=f'GEO-{uuid.uuid4().hex[:6]}',
        tipo='Caja', numero=1, total=1, estado='CARGADO',
        ruta_despacho_id=ruta.id))
    db.session.commit()
    return ruta, tarea, conductor.usuario_id


def _entrega_base(**extra):
    d = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
         'monto_cobrado': 100000}
    d.update(extra)
    return d


class TestElCheckDeLaBaseMuerde:
    """**El detector ciego del invariante.** No pasa por el servicio.

    «El código no guarda 0,0» solo afirma que la vía de hoy no lo hace. Un
    `INSERT` directo prueba que la base **tampoco lo dejaría** por ninguna otra
    vía — la de mañana incluida. Es la lección de los seis guards en verde:
    ninguno tenía este lado.
    """

    def test_cero_cero_no_entra_ni_con_sql_directo(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista
        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
                                                     _entrega_base())
        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO entregas_geo "
                "(recaudo_id, cliente_clave, lat, lon, fuente, capturado_en) "
                "VALUES (:r, 'X|Y', 0.0, 0.0, 'gps_conductor', '2026-09-02')"),
                {'r': recaudo_id})
            db.session.flush()
        db.session.rollback()

    def test_una_coordenada_de_neiva_si_entra_con_sql_directo(self, app, db,
                                                              parada_lista):
        """El gemelo. Sin él, un CHECK que rechace TODO pasaría el test de
        arriba y nadie lo notaría hasta que el maestro quedara vacío."""
        from app.models.geo_entrega import EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista
        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
                                                     _entrega_base())
        db.session.query(EntregaGeo).filter_by(recaudo_id=recaudo_id).delete()
        db.session.commit()
        db.session.execute(text(
            "INSERT INTO entregas_geo "
            "(recaudo_id, cliente_clave, lat, lon, precision_m, fuente, capturado_en) "
            "VALUES (:r, 'X|Y', 2.9273, -75.2819, 20.0, 'gps_conductor', '2026-09-02')"),
            {'r': recaudo_id})
        db.session.commit()
        assert EntregaGeo.query.filter_by(recaudo_id=recaudo_id).one().lat is not None

    def test_una_coordenada_sin_procedencia_no_entra(self, app, db, parada_lista):
        """Regla 13: un dato con autoridad y sin procedencia es tradición oral
        con formato de columna."""
        from app.models.geo_entrega import EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista
        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
                                                     _entrega_base())
        db.session.query(EntregaGeo).filter_by(recaudo_id=recaudo_id).delete()
        db.session.commit()
        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO entregas_geo "
                "(recaudo_id, cliente_clave, lat, lon, fuente, capturado_en) "
                "VALUES (:r, 'X|Y', 2.9273, -75.2819, 'sin_dato', '2026-09-02')"),
                {'r': recaudo_id})
            db.session.flush()
        db.session.rollback()

    def test_el_maestro_tampoco_admite_un_punto_sin_procedencia(self, app, db):
        """El mismo invariante, en la otra tabla. **Estaba sin probar**: el
        arnés de mutación borró este CHECK y las 52 pruebas siguieron verdes.
        Un invariante que solo vive en una de las dos tablas es la mitad de un
        invariante."""
        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO clientes_geo "
                "(cliente_clave, lat, lon, fuente, capturas_consideradas, "
                " capturas_descartadas) "
                "VALUES ('X|Y', 2.9273, -75.2819, 'sin_dato', 1, 0)"))
            db.session.flush()
        db.session.rollback()

    def test_el_maestro_no_admite_ausencia_sin_motivo(self, app, db):
        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO clientes_geo "
                "(cliente_clave, fuente, capturas_consideradas, capturas_descartadas) "
                "VALUES ('X|Y', 'sin_dato', 0, 0)"))
            db.session.flush()
        db.session.rollback()

    def test_un_maestro_bien_formado_si_entra(self, app, db):
        """El gemelo de los dos de arriba."""
        from app.models.geo_entrega import ClienteGeo
        db.session.execute(text(
            "INSERT INTO clientes_geo "
            "(cliente_clave, lat, lon, precision_m, fuente, "
            " capturas_consideradas, capturas_descartadas) "
            "VALUES ('X|Y', 2.9273, -75.2819, 20.0, 'gps_conductor', 2, 0)"))
        db.session.commit()
        assert ClienteGeo.query.get('X|Y').tiene_coordenada()

    def test_un_sin_dato_sin_motivo_tampoco(self, app, db, parada_lista):
        from app.models.geo_entrega import EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista
        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
                                                     _entrega_base())
        db.session.query(EntregaGeo).filter_by(recaudo_id=recaudo_id).delete()
        db.session.commit()
        with pytest.raises(IntegrityError):
            db.session.execute(text(
                "INSERT INTO entregas_geo "
                "(recaudo_id, cliente_clave, fuente, capturado_en) "
                "VALUES (:r, 'X|Y', 'sin_dato', '2026-09-02')"), {'r': recaudo_id})
            db.session.flush()
        db.session.rollback()


class TestConfirmarParadaCapturaLaCoordenada:

    def test_una_coordenada_legitima_se_guarda_y_arma_el_maestro(self, app, db,
                                                                 parada_lista):
        from app.models.geo_entrega import ClienteGeo, EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'lat': NEIVA[0], 'lon': NEIVA[1],
                               'precision_m': 15, 'fuente': 'gps_conductor'}))

        fila = EntregaGeo.query.filter_by(recaudo_id=recaudo_id).one()
        assert float(fila.lat) == pytest.approx(NEIVA[0])
        assert fila.fuente == geo.GPS_CONDUCTOR
        assert fila.motivo_sin_dato is None

        maestro = ClienteGeo.query.get(geo.clave_de_cliente('PAPELERIA LA 5', 'Neiva'))
        assert maestro is not None
        assert float(maestro.lat) == pytest.approx(NEIVA[0], abs=1e-5)
        assert maestro.capturas_consideradas == 1

    def test_un_gps_negado_no_se_guarda_como_cero_cero(self, app, db, parada_lista):
        """**El caso del pedido.** El estado del maestro tiene que ser
        distinguible de «acá queda la tienda»."""
        from app.models.geo_entrega import ClienteGeo, EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'fuente': 'sin_dato', 'motivo': 'permiso_denegado'}))

        fila = EntregaGeo.query.filter_by(recaudo_id=recaudo_id).one()
        assert fila.lat is None and fila.lon is None
        assert fila.motivo_sin_dato == 'permiso_denegado'

        maestro = ClienteGeo.query.get(geo.clave_de_cliente('PAPELERIA LA 5', 'Neiva'))
        assert maestro.lat is None
        assert maestro.motivo_sin_maestro == geo.SIN_CAPTURAS

    def test_el_gps_apagado_no_traba_la_entrega(self, app, db, parada_lista):
        """Una entrega trabada en la calle no la desbloquea nadie. La parada se
        confirma, cobra y liquida igual."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'fuente': 'sin_dato', 'motivo': 'sin_senal'}))
        r = RecaudoEntrega.query.get(recaudo_id)
        assert r.estado_entrega == 'ENTREGADO'
        assert float(r.monto_cobrado) == 100000

    def test_una_coordenada_imposible_tampoco_traba_la_entrega(self, app, db,
                                                               parada_lista):
        """El caso que justifica el orden: la captura va **después** del commit
        de la entrega. Si fuera en la misma transacción, el CHECK que rechaza
        0,0 tumbaría la confirmación entera."""
        from app.models.geo_entrega import EntregaGeo
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'lat': 0, 'lon': 0, 'precision_m': 5}))
        assert RecaudoEntrega.query.get(recaudo_id).estado_entrega == 'ENTREGADO'
        fila = EntregaGeo.query.filter_by(recaudo_id=recaudo_id).one()
        assert fila.lat is None
        assert fila.motivo_sin_dato == geo.FUERA_DE_RANGO

    def test_sin_bloque_geo_no_se_escribe_fila(self, app, db, parada_lista):
        """Cliente viejo con el service worker en caché. «No vino nada» y «se
        preguntó y no se pudo» son cosas distintas, y el denominador de la
        adopción depende de la diferencia."""
        from app.models.geo_entrega import EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
                                                     _entrega_base())
        assert EntregaGeo.query.filter_by(recaudo_id=recaudo_id).first() is None


class TestReconfirmarNoMueveElMaestro:
    """Re-confirmar una parada está permitido a propósito (un dedazo se
    corrige). Lo que se corrige son montos, fotos y motivos — no geografía."""

    def test_la_segunda_confirmacion_no_pisa_la_coordenada(self, app, db,
                                                           parada_lista):
        """Sin este guard, una corrección hecha en la oficina al día siguiente
        escribiría la coordenada del CD sobre la de la tienda, y el maestro se
        movería solo hacia la bodega sin que nada falle."""
        from app.models.geo_entrega import EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': 12}))
        RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(monto_cobrado=90000,
                          geo={'lat': NEIVA_LEJOS[0], 'lon': NEIVA_LEJOS[1],
                               'precision_m': 8}))

        fila = EntregaGeo.query.filter_by(recaudo_id=recaudo_id).one()
        assert float(fila.lat) == pytest.approx(NEIVA[0])

    def test_pero_una_fila_sin_dato_si_se_completa(self, app, db, parada_lista):
        """El gemelo. Ahí no hay nada que perder: el conductor que llegó sin
        señal y consiguió señal media cuadra después tiene que poder aportar."""
        from app.models.geo_entrega import EntregaGeo
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'fuente': 'sin_dato', 'motivo': 'sin_senal'}))
        RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': 12}))

        fila = EntregaGeo.query.filter_by(recaudo_id=recaudo_id).one()
        assert float(fila.lat) == pytest.approx(NEIVA[0])
        assert fila.motivo_sin_dato is None


class TestLaPantallaDelConductorRecibeElPunto:

    def test_listar_paradas_trae_geo_null_cuando_nadie_capturo(self, app, db,
                                                               parada_lista):
        from app.services.ruta_service import RutaService
        ruta, _tarea, _uid = parada_lista
        p = RutaService.listar_paradas(ruta.id)['paradas'][0]
        assert 'geo' in p and p['geo'] is None

    def test_listar_paradas_trae_el_punto_una_vez_capturado(self, app, db,
                                                            parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista
        RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': 20}))
        p = RutaService.listar_paradas(ruta.id)['paradas'][0]
        assert p['geo']['lat'] == pytest.approx(NEIVA[0], abs=1e-5)
        assert p['geo']['fuente'] == geo.GPS_CONDUCTOR

    def test_el_geo_sin_punto_llega_con_su_motivo_y_no_como_null(self, app, db,
                                                                 parada_lista):
        """«Nadie capturó nada» y «se capturó y no se pudo elegir» se pintan
        distinto en la pantalla. Si el segundo llegara como `null`, el conductor
        vería el mismo mensaje para dos problemas que se arreglan distinto."""
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista
        RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'lat': NEIVA[0], 'lon': NEIVA[1]}))  # sin precisión
        p = RutaService.listar_paradas(ruta.id)['paradas'][0]
        assert p['geo'] is not None
        assert p['geo']['lat'] is None
        assert p['geo']['motivo_sin_maestro'] == geo.PRECISION_INSUFICIENTE


class TestLaMedidaCuentaLosDosLados:

    def test_cuenta_al_que_tiene_y_al_que_no(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = parada_lista

        antes = geo.cobertura()
        assert antes['clientes_visitados'] == 0

        RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'fuente': 'sin_dato', 'motivo': 'permiso_denegado'}))
        medio = geo.cobertura()
        assert medio['clientes_visitados'] == 1
        assert medio['con_coordenada'] == 0
        assert medio['sin_coordenada'] == 1
        assert medio['motivos_sin_captura']['permiso_denegado'] == 1

        RutaService.confirmar_parada(ruta.id, tarea.id, uid,
            _entrega_base(geo={'lat': NEIVA[0], 'lon': NEIVA[1], 'precision_m': 20}))
        despues = geo.cobertura()
        assert despues['con_coordenada'] == 1
        assert despues['sin_coordenada'] == 0
        assert despues['con_una_sola_captura'] == 1
        assert despues['ultima_captura'] is not None

    def test_el_umbral_de_ruteo_viaja_con_el_numero(self, app, db):
        """La pregunta «¿ya alcanza para rutear?» se contesta en la misma
        pantalla y no de memoria."""
        assert geo.cobertura()['umbral_para_rutear'] == geo.UMBRAL_PARA_RUTEAR


class TestElEndpointDeCobertura:

    def test_admin_lo_lee(self, client, jwt_token_admin):
        r = client.get('/api/rutas/geo/cobertura',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        assert 'con_coordenada' in r.get_json()

    def test_un_operario_no(self, client, jwt_token):
        r = client.get('/api/rutas/geo/cobertura',
                       headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403


class TestLoQueNoSeConstruyoEstaDeclarado:
    """Regla 12 y deuda declarada. Un módulo que promete ruteo y no lo tiene es
    peor que uno que dice que no lo tiene."""

    def test_no_hay_ninguna_llamada_a_nominatim_ni_a_osrm(self):
        """Verificado: Nominatim **prohíbe explícitamente** este caso de uso
        (*«package/vehicle tracking applications must run their own service»*)
        y el demo de OSRM es solo no comercial. El día que alguien lo agregue
        «para probar», esto se pone rojo."""
        import pathlib
        raiz = pathlib.Path(__file__).resolve().parents[1]
        objetivos = [raiz / 'app' / 'services' / 'geo_cliente.py',
                     raiz / 'app' / 'models' / 'geo_entrega.py',
                     raiz / 'app' / 'static' / 'pwa' / 'rutas.js']
        for p in objetivos:
            txt = p.read_text().lower()
            # Se busca el host, no la palabra: los dos nombres aparecen en la
            # prosa de los docstrings explicando por qué NO se usan.
            for host in ('nominatim.openstreetmap.org', 'router.project-osrm.org'):
                assert host not in txt, f'{p.name} llama a {host}'

    def test_el_umbral_de_disparo_esta_escrito_en_estado_md(self):
        """La deuda sin condición de disparo es una intención. `ESTADO.md`
        tiene que decir cuántos clientes con coordenada hacen falta antes de
        que «ruteo» signifique algo."""
        import pathlib
        estado = (pathlib.Path(__file__).resolve().parents[1]
                  / 'docs' / 'flota' / 'ESTADO.md').read_text()
        assert str(geo.UMBRAL_PARA_RUTEAR) in estado
        assert 'clientes_geo' in estado
